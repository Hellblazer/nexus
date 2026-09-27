# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-references``: are reference-only catalog documents
still fresh? (nexus-tb2yj, RDR-169 Gap 6 leg 3.)

``stat_source``/``staleness_signal`` (``nexus.aspect_readers``) had no
production caller: nexus-oqenh leg 1 shipped the real ``https://`` HEAD
stat, and nexus-0ne1m taught it to compare a recorded ``ETag``, but nothing
walked the catalog and called either. This module is that caller.

A "reference-only" catalog document is one whose ``source_uri`` names an
EXTERNAL resource — any scheme other than ``file://`` (a repo-local path,
whose freshness a filesystem sync/reindex sweep already covers, not this
check): ``https://``, ``obsidian://``, ``x-devonthink-item://``,
``nx-scratch://``, ``chroma://`` (content-addressed — always reads
'fresh', walked for completeness but never contributes a stale/dangling
row). Sampling draws up to ``sample`` such documents (seeded like
``--check-embeddings``: today's UTC date by default, so the same day
reproduces the same sample) and stats each through ``stat_source`` with ONE
shared ``httpx.Client``, reusing the recorded ``ETag``
(``aspect_readers.HTTPS_ETAG_META_KEY``) when the entry carries one.

Bounded wall time: every scheme but ``https://`` is a cheap local check
(``os.stat``, an ``osascript`` round-trip, a scratch lookup). The
``https://`` HEAD is the only cost that can genuinely run long — bounded per
call to ``aspect_readers.HTTPS_STAT_MAX_ATTEMPTS`` attempts, each up to
twice ``aspect_readers.HTTPS_STAT_TIMEOUT_S`` (httpx applies that timeout
to connect/read/write/pool separately), plus
``aspect_readers.HTTPS_STAT_RETRY_DELAYS_S`` backoff between attempts —
about 61.5s worst case per document. Worst case for the whole run, if every
sampled document were ``https://`` and every one exhausted its retries, is
``sample * that per-call bound``; ``nx doctor --check-references --help``
computes and states the number at the default sample size
(``commands/doctor.py``'s ``_REFERENCES_HTTPS_WORST_CASE_S``, derived from
the same three constants so it cannot silently drift from them).

Exit 0 (informational) when the sample found zero reference-only documents
at all — "not applicable", never a red line on a box with none (the
nexus-7zhag doctrine). Otherwise exit 1 when any sampled document reads
'stale' or 'dangling'; 'unknown' NEVER fails this check on its own — an
indeterminate check (a timeout, a permissions error, a deferred scheme) is
not evidence of a real problem, only of an inconclusive one.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import click
import structlog

from nexus.aspect_readers import (
    HTTPS_ETAG_META_KEY,
    HTTPS_STAT_TIMEOUT_S,
    StalenessSignal,
    staleness_signal,
    stat_source,
)
from nexus.doctor_embeddings import default_seed

_log = structlog.get_logger(__name__)

#: Reference-only documents sampled when the caller names no size.
DEFAULT_SAMPLE = 50
#: Stale/dangling rows named per run.
_MAX_NAMED = 10


@dataclass
class ReferenceCheckResult:
    """One sampled document's stat outcome. ``error`` set means the stat
    itself raised (defensive — ``stat_source``/``staleness_signal`` with
    ``allow_dangling=True`` are not documented to raise, but a row's
    failure here is reported, never allowed to abort the whole sweep)."""

    scheme: str
    tumbler: str
    title: str
    source_uri: str
    signal: StalenessSignal | None = None
    error: str | None = None


def is_reference_only(source_uri: str) -> bool:
    """A catalog document is "reference-only" for this check when its
    ``source_uri`` names an external, non-``file://`` resource — every
    scheme ``stat_source``'s registry covers except ``file`` (see the
    module docstring for why ``file://`` is out of scope here).
    """
    if not source_uri:
        return False
    scheme = urlparse(source_uri).scheme
    return bool(scheme) and scheme != "file"


def reference_only_candidates(cat: Any) -> list[Any]:
    """Every live (non-alias) reference-only ``CatalogEntry`` in *cat*.

    Walks the full catalog (``all_documents(limit=0)``, the same unbounded-
    but-paged pattern ``nx catalog reconcile-stale`` uses) — a metadata-only
    read, not the expensive part of this check.
    """
    return [
        e for e in cat.all_documents(limit=0)
        if not e.alias_of and is_reference_only(e.source_uri)
    ]


def sample_candidates(candidates: list[Any], sample: int, seed: int) -> list[Any]:
    """Up to *sample* entries from *candidates*, seeded — every entry when
    there are fewer than *sample* candidates (never pads or repeats)."""
    if len(candidates) <= sample:
        return list(candidates)
    return random.Random(seed).sample(candidates, sample)


def stat_one(entry: Any, *, http_client: Any) -> ReferenceCheckResult:
    """Stat one catalog entry's ``source_uri`` and classify its staleness."""
    scheme = urlparse(entry.source_uri).scheme
    meta = entry.meta or {}
    recorded_etag = meta.get(HTTPS_ETAG_META_KEY, "") if scheme == "https" else None
    try:
        result = stat_source(
            entry.source_uri, http_client=http_client, recorded_etag=recorded_etag,
        )
        signal = staleness_signal(entry.source_mtime or 0.0, result, allow_dangling=True)
        return ReferenceCheckResult(
            scheme=scheme, tumbler=str(entry.tumbler), title=entry.title,
            source_uri=entry.source_uri, signal=signal,
        )
    except Exception as exc:  # noqa: BLE001 — one row's failure is reported, never aborts the sweep
        _log.debug(
            "doctor_references_stat_failed", tumbler=str(entry.tumbler),
            source_uri=entry.source_uri, error=str(exc),
        )
        return ReferenceCheckResult(
            scheme=scheme, tumbler=str(entry.tumbler), title=entry.title,
            source_uri=entry.source_uri, error=f"{type(exc).__name__}: {exc}",
        )


def check_references(candidates: list[Any], *, http_client: Any) -> list[ReferenceCheckResult]:
    """Stat every entry in *candidates* through :func:`stat_one`, sharing
    ONE ``http_client`` across every ``https://`` call."""
    return [stat_one(entry, http_client=http_client) for entry in candidates]


def _bucket(r: ReferenceCheckResult) -> str:
    """The counting bucket for one result: the signal when the stat
    succeeded, ``"error"`` when it didn't. ``r.signal`` is only ``None``
    when ``r.error`` is set (``ReferenceCheckResult`` always sets exactly
    one of the two — see :func:`stat_one`), so this never actually returns
    the ``"error"`` fallback for a ``None`` signal at runtime; it exists to
    give the type checker a plain ``str`` instead of ``StalenessSignal |
    None``.
    """
    if r.error is not None:
        return "error"
    return r.signal or "error"


def format_report(
    results: list[ReferenceCheckResult], *, total_candidates: int, sample: int, seed: int,
) -> tuple[list[str], bool]:
    """Human lines and whether the run is clean (never gated on 'unknown')."""
    lines: list[str] = []
    by_signal: Counter[str] = Counter(_bucket(r) for r in results)
    stale = [r for r in results if r.signal == "stale"]
    dangling = [r for r in results if r.signal == "dangling"]
    errored = [r for r in results if r.error is not None]
    ok = not stale and not dangling and not errored

    mark = "✓" if ok else "✗"
    lines.append(
        f"[{mark}] Reference staleness: {len(results)} reference-only document(s) "
        f"sampled (of {total_candidates} candidate(s)), sample {sample}, seed {seed}; "
        f"{by_signal.get('fresh', 0)} fresh, {len(stale)} stale, {len(dangling)} dangling, "
        f"{by_signal.get('unknown', 0)} unknown, {len(errored)} not stattable"
    )

    by_scheme: dict[str, Counter[str]] = {}
    for r in results:
        by_scheme.setdefault(r.scheme, Counter())[_bucket(r)] += 1
    for scheme in sorted(by_scheme):
        counts = by_scheme[scheme]
        lines.append(
            f"      {scheme}: fresh={counts.get('fresh', 0)} stale={counts.get('stale', 0)} "
            f"dangling={counts.get('dangling', 0)} unknown={counts.get('unknown', 0)} "
            f"error={counts.get('error', 0)}"
        )

    if stale:
        lines.append(f"      Stale ({len(stale)}):")
        for r in stale[:_MAX_NAMED]:
            lines.append(f"        {r.tumbler:<14} [{r.scheme}] {r.title}  {r.source_uri}")
        if len(stale) > _MAX_NAMED:
            lines.append(f"        ... and {len(stale) - _MAX_NAMED} more")
    if dangling:
        lines.append(f"      Dangling ({len(dangling)}):")
        for r in dangling[:_MAX_NAMED]:
            lines.append(f"        {r.tumbler:<14} [{r.scheme}] {r.title}  {r.source_uri}")
        if len(dangling) > _MAX_NAMED:
            lines.append(f"        ... and {len(dangling) - _MAX_NAMED} more")
    if errored:
        lines.append(f"      Not stattable ({len(errored)}):")
        for r in errored[:_MAX_NAMED]:
            lines.append(f"        {r.tumbler:<14} [{r.scheme}] {r.title}  ({r.error})")

    return lines, ok


def run_check_references(*, sample: int, seed: int | None) -> None:
    """CLI entry for ``nx doctor --check-references``."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    run_seed = default_seed() if seed is None else seed
    try:
        cat = make_catalog_reader()
        if cat is None:
            raise RuntimeError("catalog is not available")
        candidates = reference_only_candidates(cat)
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable catalog is a hard failure here
        click.echo(f"[✗] Reference staleness: catalog UNREADABLE ({type(exc).__name__}: {exc})")
        raise SystemExit(1) from exc

    if not candidates:
        click.echo("[✓] Reference staleness: not applicable (no reference-only catalog documents)")
        return

    chosen = sample_candidates(candidates, sample, run_seed)

    import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

    with httpx.Client(timeout=HTTPS_STAT_TIMEOUT_S, follow_redirects=True) as client:
        results = check_references(chosen, http_client=client)

    lines, ok = format_report(results, total_candidates=len(candidates), sample=sample, seed=run_seed)
    for line in lines:
        click.echo(line)
    if not ok:
        raise SystemExit(1)
