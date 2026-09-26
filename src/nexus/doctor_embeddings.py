# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-embeddings``: does each stored vector still embed its
own text? (nexus-f9duo, indexing-brittleness proposal P0.4.)

Nothing binds a stored vector to its text or model. nexus-tysei found
stale vectors at cosine 0.25 to 0.87 against their own text across 11
voyage-context-3 collections, invisible to every audit for months. This
probe samples chunks per collection, embeds their stored text again with
the collection's registered model (``/v1/vectors/embed``, which stores
nothing), and compares that with the stored vector.

A healthy engine-written chunk matches to within the model's call-to-call
noise: the nexus-tysei repair measured every re-embedded row at 0.998 or
above. :data:`COSINE_FLOOR` sits below that noise and above every stale
row seen.

Sampling draws :data:`_WINDOWS` windows at random offsets, so one run
covers several parts of a collection, not one run of adjacent rows. The
seed defaults to today's date: the same day reproduces the same sample,
and successive days move it. Every probe costs one embedding call per
sampled chunk, which is why this is opt-in.

Exit 0 when every sampled chunk clears the floor, 1 when any does not or
when any collection could not be probed. An unprobed collection is never
counted as clean.
"""
from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)

#: Minimum cosine between a stored vector and a fresh embed of its text.
COSINE_FLOOR = 0.99
#: Chunks sampled per collection when the caller names no size.
DEFAULT_SAMPLE = 20
#: Random windows the sample is split across.
_WINDOWS = 4
#: Collections probed at once. Each probe is embedding-bound, so this stays
#: below the engine's CCE concurrency rather than the search fan-out.
_WORKERS = 4
#: Worst rows named per collection.
_MAX_NAMED = 5
#: Attempts per engine call, through ``nexus.retry._vector_with_retry``.
#: Fewer than its default 5: a doctor probe should report a sick engine,
#: not wait out a minute of backoff on each of several collections.
_ATTEMPTS = 3


@dataclass
class CollectionDrift:
    """One collection's probe result. ``error`` set means not probed."""

    collection: str
    size: int
    cosines: dict[str, float] = field(default_factory=dict)
    skipped_empty_text: int = 0
    #: Sampled ids with text but no stored vector at the collection's dim:
    #: a re-embed in progress, or a row deleted between the two reads.
    no_vector: int = 0
    error: str | None = None

    @property
    def below_floor(self) -> list[tuple[str, float]]:
        # NaN compares False with everything, so it is tested explicitly:
        # a non-finite cosine is a failed comparison, never a pass.
        return sorted(
            ((cid, c) for cid, c in self.cosines.items()
             if not math.isfinite(c) or c < COSINE_FLOOR),
            key=lambda kv: (math.isfinite(kv[1]), kv[1]),
        )


def default_seed(today: datetime | None = None) -> int:
    """Seed for a run: the UTC date as ``YYYYMMDD``."""
    day = (today or datetime.now(UTC)).date()
    return day.year * 10000 + day.month * 100 + day.day


def window_offsets(size: int, sample: int, rng: random.Random) -> list[tuple[int, int]]:
    """``(offset, limit)`` windows covering exactly ``min(sample, size)`` rows.

    The whole collection when it is no bigger than the sample. Below twice
    the sample, one window at a random offset. Otherwise the collection is
    cut into :data:`_WINDOWS` equal strata and each gets one window at a
    random offset inside it, so windows never overlap and the sample is
    never short. At twice the sample every stratum is at least as long as
    its window (``floor(2s/w) >= ceil(s/w)`` for ``w <= s``).

    Rows are ordered by chash on the engine (``getWhere``), and a chash is
    the hash of the text, so a window is not a run of one document's
    chunks or one indexing era.
    """
    if size <= 0 or sample <= 0:
        return []
    if size <= sample:
        return [(0, size)]
    if size < 2 * sample:
        return [(rng.randrange(size - sample + 1), sample)]
    windows = min(_WINDOWS, sample)
    parts = [sample // windows + (1 if i < sample % windows else 0) for i in range(windows)]
    out: list[tuple[int, int]] = []
    for i, part in enumerate(parts):
        lo, hi = i * size // windows, (i + 1) * size // windows
        out.append((lo + rng.randrange(hi - lo - part + 1), part))
    return out


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine in plain Python. numpy's ``matmul`` on the macOS Accelerate
    BLAS raised spurious overflow and invalid-value warnings on these
    unit-norm vectors (measured 2026-09-25, values correct), and a probe
    whose output a reader has to second-guess is worse than a slower one.
    A zero-length vector gives NaN, which :attr:`CollectionDrift.below_floor`
    flags.
    """
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b))
    return dot / norm if norm else math.nan


def probe_collection(t3: Any, name: str, size: int, sample: int, rng: random.Random) -> CollectionDrift:
    """Sample *name*, re-embed the sampled texts, compare with stored vectors.

    Any failure is recorded in ``error``; the collection then counts as
    not probed.
    """
    from nexus.retry import _vector_with_retry  # noqa: PLC0415 — deferred, only the probe needs it

    def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return _vector_with_retry(fn, *args, max_attempts=_ATTEMPTS, **kwargs)

    result = CollectionDrift(collection=name, size=size)
    try:
        # Not get_collection(): it re-lists the whole tenant to prove the
        # collection exists, and the caller has just listed it.
        col = t3.get_or_create_collection(name)
        texts: dict[str, str] = {}
        for offset, limit in window_offsets(size, sample, rng):
            page = _call(col.get, include=["documents"], limit=limit, offset=offset)
            for cid, text in zip(page.get("ids") or [], page.get("documents") or []):
                if text:
                    texts.setdefault(cid, text)
                else:
                    result.skipped_empty_text += 1
        if not texts:
            return result
        stored = _call(t3.get_embeddings_by_id, name, list(texts))
        ids = [i for i in texts if i in stored]
        result.no_vector = len(texts) - len(ids)
        if not ids:
            return result
        fresh = _call(t3.embed_for_collection, name, [texts[i] for i in ids])
        if len(fresh) != len(ids):
            raise RuntimeError(f"embed returned {len(fresh)} vectors for {len(ids)} texts")
        result.cosines = {i: _cosine(stored[i], f) for i, f in zip(ids, fresh)}
    except Exception as exc:  # noqa: BLE001 — one collection's failure is reported, never hides the rest
        _log.debug("doctor_embeddings_probe_failed", collection=name, error=str(exc))
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def probe_collections(
    t3: Any,
    sizes: dict[str, int],
    *,
    sample: int,
    seed: int,
) -> list[CollectionDrift]:
    """Probe every collection in *sizes*, in name order.

    Each collection gets its own RNG derived from the seed and its name,
    so its sample does not depend on which other collections were probed.
    """
    names = sorted(sizes)

    def _one(name: str) -> CollectionDrift:
        return probe_collection(t3, name, sizes[name], sample, random.Random(f"{seed}:{name}"))

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return list(pool.map(_one, names))


def format_report(results: list[CollectionDrift], *, sample: int, seed: int) -> tuple[list[str], bool]:
    """Human lines and whether the run is clean."""
    lines: list[str] = []
    drifted = [r for r in results if r.error is None and r.below_floor]
    failed = [r for r in results if r.error is not None]
    probed = [r for r in results if r.error is None and r.cosines]
    empty = [r for r in results if r.error is None and not r.cosines]
    total = sum(len(r.cosines) for r in probed)
    # A run that compared nothing has not shown anything is healthy.
    ok = not drifted and not failed and total > 0

    mark = "✓" if ok else "✗"
    lines.append(
        f"[{mark}] Embedding drift: {total} chunk(s) sampled in {len(probed)} "
        f"collection(s), floor cos {COSINE_FLOOR}, sample {sample}, seed {seed}; "
        f"{len(drifted)} collection(s) below floor, {len(failed)} not probed"
    )
    for r in drifted:
        cos = sorted(r.cosines.values())
        worst = ", ".join(f"{cid[:12]} {c:.3f}" for cid, c in r.below_floor[:_MAX_NAMED])
        lines.append(
            f"      ✗ {r.collection}: {len(r.below_floor)}/{len(cos)} below floor, "
            f"median {statistics.median(cos):.4f}, min {cos[0]:.4f} ({r.size} chunks); worst: {worst}"
        )
    for r in failed:
        lines.append(f"      ✗ {r.collection}: NOT PROBED ({r.error})")
    if empty:
        lines.append(
            f"      NOT CHECKED: {len(empty)} collection(s) gave no comparable chunk "
            "(no stored text, as with reference-only rows, or no stored vector): "
            + ", ".join(r.collection for r in empty[:5])
            + (f" (+{len(empty) - 5} more)" if len(empty) > 5 else "")
        )
    no_vector = [r for r in results if r.error is None and r.no_vector]
    if no_vector:
        lines.append(
            f"      {sum(r.no_vector for r in no_vector)} sampled chunk(s) have text but "
            "no stored vector at the collection's dim (a re-embed in progress, or "
            "deleted mid-probe), not compared: "
            + ", ".join(f"{r.collection} ({r.no_vector})" for r in no_vector[:5])
        )
    if total == 0 and not failed:
        lines.append("      nothing was compared, so this is not a clean result")
    if drifted:
        lines.append(
            "      remedy: nx collection re-embed <collection>  (a production write; "
            "confirm the population with a larger --embeddings-sample first)"
        )
    return lines, ok


def run_check_embeddings(*, sample: int, collections: tuple[str, ...], seed: int | None) -> None:
    """CLI entry for ``nx doctor --check-embeddings``."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import

    run_seed = default_seed() if seed is None else seed
    try:
        t3 = make_t3()
        listed = {str(c.get("name", "")): int(c.get("count", 0) or 0) for c in t3.list_collections()}
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable tenant is a hard failure here
        click.echo(f"[✗] Embedding drift: T3 UNREADABLE ({type(exc).__name__}: {exc})")
        raise SystemExit(1) from exc

    if collections:
        unknown = [c for c in collections if c not in listed]
        if unknown:
            click.echo(f"[✗] Embedding drift: no such collection(s): {', '.join(unknown)}")
            raise SystemExit(1)
        sizes = {c: listed[c] for c in collections}
    else:
        sizes = {n: s for n, s in listed.items() if n and s > 0}
    if not sizes:
        click.echo("[✓] Embedding drift: not applicable (no collection holds chunks)")
        return

    results = probe_collections(t3, sizes, sample=sample, seed=run_seed)
    lines, ok = format_report(results, sample=sample, seed=run_seed)
    for line in lines:
        click.echo(line)
    if not ok:
        raise SystemExit(1)
