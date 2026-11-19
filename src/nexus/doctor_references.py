# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-references``: are reference-only catalog documents
still fresh? (nexus-tb2yj, RDR-169 Gap 6 leg 3.)

``stat_source``/``staleness_signal`` (``nexus.aspect_readers``) had no
production caller: nexus-oqenh leg 1 shipped the real ``https://`` HEAD
stat, and nexus-0ne1m taught it to compare a recorded ``ETag``, but nothing
called either over the catalog. This module is that caller.

A "reference-only" catalog document is one whose ``source_uri`` names an
EXTERNAL resource — any scheme other than ``file://`` (a repo-local path,
whose freshness a filesystem sync/reindex sweep already covers, not this
check): ``https://``, ``obsidian://``, ``x-devonthink-item://``,
``nx-scratch://``, ``nx-note://`` (content-addressed — always reads
'fresh', read for completeness but never contributes a stale/dangling
row). Candidates come from one engine by-scheme read per scheme the stat
registry covers (nexus-uwggq.1.5, RDR-241 Phase 2), so a row no handler
claims, such as ``chroma://`` residue (RDR-231), is not read at all. Sampling draws up to ``sample`` such documents (seeded like
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
``aspect_readers.HTTPS_STAT_RETRY_DELAYS_S`` backoff between attempts, plus
one confirming HEAD after an ETag mismatch (nexus-ltrjf) — about 81.5s worst
case per document. Sampled documents are stat'd with up
to :data:`MAX_CONCURRENCY` (nexus-0ne1m critique, significant #1) concurrent
threads sharing ONE ``httpx.Client`` — the shared sync ``Client`` is used
from worker threads here; httpx's own docs make an explicit sharing claim
only for ``AsyncClient``, but the underlying connection pool is built for
concurrent use and sharing one sync ``Client`` across threads is a common,
well-tested pattern in the httpx ecosystem (round-2 critique, nexus-0ne1m/
nexus-tb2yj: softened from "documented thread-safe", which overclaimed an
httpx doc citation that doesn't exist for the sync client) — so worst case
for the whole run, if every sampled document were ``https://`` and every
one exhausted its retries, is
``ceil(sample / min(MAX_CONCURRENCY, sample)) * that per-call bound`` — see
:func:`estimated_worst_case_s`. ``nx doctor --check-references --help``
states the number at the default sample size, and a run prints its own
estimate (from the REQUESTED ``--references-sample``, before sampling
narrows it to however many candidates actually exist) as its first line.

Exit 3 ("could not check") when the engine does not answer the by-scheme
route; nothing walks the catalog in its place (RDR-241).

Exit 0 (informational) when the sample found zero reference-only documents
at all — "not applicable", never a red line on a box with none (the
nexus-7zhag doctrine). Otherwise exit 1 when any sampled document reads
'stale' or 'dangling'; 'unknown' NEVER fails this check on its own — an
indeterminate check (a timeout, a permissions error, a deferred scheme) is
not evidence of a real problem, only of an inconclusive one.

**A host with no stable validator** (round-2 critique, nexus-0ne1m/
nexus-tb2yj, significant #S2; fixed by nexus-ltrjf): a server that mints a
FRESH ``ETag`` on every response, with no ``Last-Modified``, used to read
'stale' on EVERY run. ``nx catalog backfill-etags`` cannot fix that. The
``https://`` stat now confirms an ETag mismatch with one more HEAD: an ETag
that changes again while ``Content-Length`` holds (or is absent from both)
is an unstable validator and reads 'unknown', which never fails this check.
A fresh-ETag host whose length also varies still reads 'stale'.
"""
from __future__ import annotations

import math
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import click
import structlog

from nexus.aspect_readers import (
    HTTPS_ETAG_META_KEY,
    HTTPS_STAT_MAX_ATTEMPTS,
    HTTPS_STAT_RETRY_DELAYS_S,
    HTTPS_STAT_TIMEOUT_S,
    StalenessSignal,
    registered_stat_schemes,
    staleness_signal,
    stat_source,
)
from nexus.doctor_embeddings import default_seed

_log = structlog.get_logger(__name__)

#: Reference-only documents sampled when the caller names no size (nexus-0ne1m
#: critique, significant #1: 50 made this a check nobody would run — sized
#: so the default estimate stays in the tens-of-seconds range, not tens of
#: minutes, even before --references-sample is narrowed for a specific need).
DEFAULT_SAMPLE = 10
#: Concurrent https:// stats in flight at once, sharing ONE httpx.Client
#: (nexus-0ne1m critique, significant #1). The shared client is a SYNC
#: httpx.Client used from worker threads: httpx's docs make an explicit
#: sharing claim only for AsyncClient, but the underlying connection pool
#: is built for concurrent use and sharing one sync Client across threads
#: is a common, well-tested pattern in the httpx ecosystem — N workers
#: sharing one client is the intended usage here, not a foot-gun.
MAX_CONCURRENCY = 8
#: Stale/dangling rows named per run.
_MAX_NAMED = 10


def _https_worst_case_per_call_s() -> float:
    """Worst-case wall time for ONE https:// stat call — derived from
    ``aspect_readers``' own bounded-retry constants so this cannot drift
    from them (mirrors ``commands/doctor.py``'s own computation)."""
    # + one confirming HEAD after an ETag mismatch (nexus-ltrjf), no retry.
    return (HTTPS_STAT_MAX_ATTEMPTS + 1) * 2 * HTTPS_STAT_TIMEOUT_S + sum(HTTPS_STAT_RETRY_DELAYS_S)


def estimated_worst_case_s(sample: int, concurrency: int = MAX_CONCURRENCY) -> float:
    """Worst-case wall time for a run sampling *sample* documents with up to
    *concurrency* concurrent stats, if every one were ``https://`` and every
    one exhausted its retries: ``ceil(sample / workers) * per_call_bound``,
    where ``workers = max(1, min(concurrency, sample))``.
    """
    if sample <= 0:
        return 0.0
    workers = max(1, min(concurrency, sample))
    return math.ceil(sample / workers) * _https_worst_case_per_call_s()


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
    """Does *source_uri* name an external, non-``file://`` resource (any parsed scheme but ``file``)?

    The scheme predicate the check was built on; candidates now come from the engine's by-scheme read, which
    applies the same scheme rule, so production no longer calls this (it is kept as the predicate's statement and
    is tested on its own).
    """
    if not source_uri:
        return False
    scheme = _scheme_of(source_uri)
    return bool(scheme) and scheme != "file"


#: The scheme's lexical form, as the engine's by-scheme route matches it (``CatalogDocReads.SCHEME_GRAMMAR``).
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
#: What ``urlsplit`` (and the route) strips before reading a scheme: leading U+0001..U+0020, then tab, CR and LF anywhere.
_LEADING_STRIP = "".join(map(chr, range(0x01, 0x21)))
_UNSAFE_BYTES = {ord(c): None for c in "\t\r\n"}


def _scheme_of(source_uri: str) -> str:
    """``urlparse(source_uri).scheme``. Where ``urlparse`` raises ``ValueError`` (an invalid bracketed host, an
    NFKC-invalid netloc) the engine's by-scheme route still answers the row, so the scheme is read lexically
    instead, as the route does (leading controls and spaces dropped, lowercased), and ``stat_one`` reports the
    row as not stattable rather than aborting the check (nexus-uwggq.1.5)."""
    try:
        return urlparse(source_uri).scheme
    except ValueError:
        m = _SCHEME_RE.match(source_uri.lstrip(_LEADING_STRIP).translate(_UNSAFE_BYTES))
        return m.group(1).lower() if m else ""


def reference_only_candidates(cat: Any) -> list[Any] | None:
    """Every live (non-alias) ``CatalogEntry`` in *cat* whose scheme a reader can stat, ``file`` excepted.

    One engine read per registered non-``file`` scheme (``cat.documents_by_scheme``, ``GET
    /v1/catalog/docs/by-scheme``, RDR-241 Phase 2), never a walk of the catalog. The route keeps a row iff
    ``urlparse(source_uri).scheme`` equals the scheme lowercased, so ``HTTPS://...`` is read as ``https``, as the
    walk did (Sam 2026-11-16, ask uwggq15-scheme-case option C); a row whose scheme no reader can stat
    (``chroma://``, a Windows drive path) is not read, since it could only ever report "unknown". Rows come back
    sorted by tumbler text in code-point order. The walk listed them in the database's ``ORDER BY tumbler``, which
    is the same on a C-collated database and can swap mixed-depth tumblers on a locale-collated one; order matters
    only to which rows the seeded sample picks when candidates outnumber it.

    ``None`` when the engine lacks the route: the caller reports the check not checked (exit 3, "could not
    check"), and nothing walks in its place (RDR-241).
    """
    out: list[Any] = []
    for scheme in registered_stat_schemes():
        if scheme == "file":
            continue
        rows = cat.documents_by_scheme(scheme)
        if rows is None:
            return None
        # The client's own scheme test runs last, so an engine bug cannot widen the answer silently (RDR-241).
        out.extend(e for e in rows if not e.alias_of and _scheme_of(e.source_uri) == scheme)
    return sorted(out, key=lambda e: str(e.tumbler))


def sample_candidates(candidates: list[Any], sample: int, seed: int) -> list[Any]:
    """Up to *sample* entries from *candidates*, seeded — every entry when
    there are fewer than *sample* candidates (never pads or repeats)."""
    if len(candidates) <= sample:
        return list(candidates)
    return random.Random(seed).sample(candidates, sample)


def stat_one(entry: Any, *, http_client: Any) -> ReferenceCheckResult:
    """Stat one catalog entry's ``source_uri`` and classify its staleness."""
    scheme = _scheme_of(entry.source_uri)
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
    ONE ``http_client`` across up to :data:`MAX_CONCURRENCY` concurrent
    threads (nexus-0ne1m critique, significant #1: a strictly serial walk
    made the wall-time bound ``sample * per_call``, with no mitigation).
    ``ThreadPoolExecutor.map`` preserves input order in its results, so the
    report lists documents in the same order regardless of which thread's
    stat finished first."""
    if not candidates:
        return []
    workers = max(1, min(MAX_CONCURRENCY, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda entry: stat_one(entry, http_client=http_client), candidates))


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

    if candidates is None:
        click.echo(
            "[ ] Reference staleness: not checked (the engine does not answer GET /v1/catalog/docs/by-scheme; "
            "an engine that advertises catalog_docs_by_scheme_supported does)"
        )
        # Exit 3, "could not check", as nx catalog doctor --sensitive-with-aspects does (RDR-241, 2026-11-17): a
        # dedicated check that examined nothing is not a pass (the nexus-moht0 vacuous-gate rule).
        raise SystemExit(3)
    if not candidates:
        click.echo("[✓] Reference staleness: not applicable (no reference-only catalog documents)")
        return

    # nexus-0ne1m critique, significant #1: print the worst-case estimate
    # (computed from the REQUESTED sample and MAX_CONCURRENCY, before
    # sampling narrows it to however many candidates actually exist) as the
    # run's first line, not only in --help.
    workers = max(1, min(MAX_CONCURRENCY, sample))
    click.echo(
        f"[i] Reference staleness: worst case for this run (sample={sample}, "
        f"concurrency={workers}) is ~{estimated_worst_case_s(sample):.0f}s if "
        "every sampled document is https:// and every one exhausts its retries"
    )

    chosen = sample_candidates(candidates, sample, run_seed)

    import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

    with httpx.Client(timeout=HTTPS_STAT_TIMEOUT_S, follow_redirects=True) as client:
        results = check_references(chosen, http_client=client)

    lines, ok = format_report(results, total_candidates=len(candidates), sample=sample, seed=run_seed)
    for line in lines:
        click.echo(line)
    if not ok:
        raise SystemExit(1)
