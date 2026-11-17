# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 coverage baseline for the collections the taxonomy spike routed (nexus-7fku2.1.7).

Gate round 2, Significant 2: the spike (``taxonomy_ab2.py``) routed collections on their
topics, and no coverage value existed for them, so nobody knew how many the 0.9 gate of
Step 5a-ii would have left unrouted. This reads that baseline from the engine: for each
collection, the live chunk count and the number of chunks with no assignment to the
collection's own topics, and coverage = 1 - unassigned / chunks (RDR-134 "Coverage").
A collection with no chunks has no coverage and is never routed.

Reads only. No language model is called and nothing is written to the engine.

  * Numerator: ``HttpTaxonomyStore.unassigned_chashes`` (POST
    ``/v1/taxonomy/assignments/unassigned``), paged 1000 at a time along ``next_after``.
    The engine function lists chunks with an embedding at the collection's dimension, a
    catalog manifest reference and no own-collection ``topic_assignments`` row.
  * Denominator: ``HttpVectorClient.collection_stats`` (GET ``/v1/vectors/stats``), the
    ``count`` of the collection's ``(name, dim)`` row: chunks with an embedding at that
    dimension and a live owner (a manifest row whose document is not tombstoned,
    RDR-192 live(c)). That is the denominator ``taxonomy_coverage_counts_<dim>`` uses. It
    differs from the numerator's predicate only by chunks whose every owner is a
    tombstoned document (the real engine lists those as unassigned). A row where that makes
    unassigned exceed live is flagged, not clamped, and every row carries ``coverage_upper``
    (the bound if all ``stored_count - count`` non-live chunks were among the unassigned)
    and ``gate_uncertain`` (the gate says no but the upper bound says yes). The bound can be
    loose: ``stored_count - count`` also counts manifest-less ghost chunks that the
    unassigned read never lists. A ``gate_uncertain`` row stays unresolved until Step 5a's
    engine arm reports coverage.

The run is pinned to the engine it reads. Before any read it refuses a local-mode process
or an unresolvable service URL (``--allow-local`` is for a rehearsal against the local
substrate), reads the engine build first, and records the build and the scrubbed host in the
report and on stderr. A collection whose read fails (a 5xx, a statement timeout, a 422 after
the first page) is recorded as status ``error`` and the run continues; the report is written
whatever happens, and the exit is non-zero when any row errored, the rebuilt set is empty or
nothing was measured.

The collection set is rebuilt as the spike built it: the non-empty prose collections of
``nx collection list`` that have topics (``taxonomy_ab2.py``, ``prose_collections`` and
``get_distinct_collections``). The saved spike output records the size of that set; a
rebuild that does not match it, or names a collection the spike never saw, is recorded as
drift in the output and never adjusted.

    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_coverage.py
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import click
import httpx

from bench.manifest import engine_build
from nexus._log_safe_url import scrub_transport_error
from nexus.db.http_vector_client import VectorServiceError

GATE_THRESHOLD = 0.9
PAGE_SIZE = 1000  # the engine's cap per page (TaxonomyRepository.MAX_UNASSIGNED_CHASHES)
_MAX_PAGES = 100_000  # a bound on a runaway cursor, far above any real collection
MIN_ENGINE = (0, 1, 132)  # the release that added /v1/taxonomy/assignments/unassigned
GATE_UNRESOLVED = "unresolved until Step 5a's engine arm reports coverage"
SAVED_SPIKE = Path("bench/results/rdr134-taxonomy-ab-prod-2026-11-16.json")


class EngineTooOld(RuntimeError):
    """The engine has no ``/v1/taxonomy/assignments/unassigned`` route (below v0.1.132)."""


class EngineRefused(RuntimeError):
    """The process is not pointed at an engine this baseline may read."""


class _TaxonomyReader(Protocol):
    def unassigned_chashes(self, collection: str, *, limit: int = ..., after: str | None = ...) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UnassignedCount:
    unassigned: int
    has_taxonomy: bool
    registered: bool
    pages: int


def count_unassigned(taxonomy: _TaxonomyReader, collection: str) -> UnassignedCount:
    """Count *collection*'s unassigned chunks by paging ``next_after`` at 1000 a page.

    A 404 is an engine below v0.1.132 (:class:`EngineTooOld`); a 422 is a collection the
    engine does not know for this tenant, which holds no chunks (``drain_unassigned``
    reads it the same way). Any other status propagates.
    """
    after: str | None = None
    total = pages = 0
    has_taxonomy = False
    while pages < _MAX_PAGES:
        try:
            page = taxonomy.unassigned_chashes(collection, limit=PAGE_SIZE, after=after)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                raise EngineTooOld(
                    "engine has no /v1/taxonomy/assignments/unassigned (below engine-service-v0.1.132)"
                ) from exc
            if status == 422 and pages == 0:
                return UnassignedCount(0, False, False, 1)
            raise  # a 422 after the first page is the collection disappearing mid-read: an error
        pages += 1
        has_taxonomy = bool(page.get("has_taxonomy"))
        chashes = list(page.get("chashes") or [])
        if not has_taxonomy or not chashes:
            break
        total += len(chashes)
        cursor = page.get("next_after") or None
        if cursor is None:
            break
        if cursor == after:
            raise RuntimeError(f"{collection}: the unassigned cursor did not advance past {after!r}")
        after = cursor
    else:
        raise RuntimeError(f"{collection}: more than {_MAX_PAGES} unassigned pages")
    return UnassignedCount(total, has_taxonomy, True, pages)


def coverage_of(chunks: int, unassigned: int) -> float | None:
    """1 - unassigned / chunks; ``None`` for a collection with no chunks."""
    if chunks <= 0:
        return None
    return 1 - unassigned / chunks


def _gate_passes(chunks: int, unassigned: int, threshold: float = GATE_THRESHOLD) -> bool:
    """Route only if chunks > 0 and coverage >= threshold, in exact arithmetic so that a
    collection at exactly 0.9 is not lost to float rounding."""
    if chunks <= 0:
        return False
    return Fraction(chunks - unassigned, chunks) >= Fraction(str(threshold))


def live_chunks(stats_rows: list[dict[str, Any]], collection: str) -> tuple[int, list[int]]:
    """The collection's live chunk count and the dimensions its stats rows carry.

    ``collection_stats`` has one row per ``(name, dim)``; ``count`` is the live count
    (a chunk counts iff it has a live owner in its own collection). A collection absent
    from the stats has no chunks.
    """
    rows = [r for r in stats_rows if r.get("name") == collection]
    return sum(int(r.get("count") or 0) for r in rows), sorted(int(r["dim"]) for r in rows if r.get("dim"))


def _describe(exc: BaseException, base_url: str = "") -> str:
    """The error for a row: type, HTTP status when there is one, and the message with the
    transport's own redaction applied (a bearer token httpx quotes in an "Illegal header
    value" is replaced; text that quotes the URL's credentials is withheld) and every URL
    replaced, so a committed report carries neither a token nor the engine's address."""
    status = f" (HTTP {exc.response.status_code})" if isinstance(exc, httpx.HTTPStatusError) else ""
    text = scrub_transport_error(exc, base_url or "http://engine.invalid/")
    return re.sub(r"https?://[^\s'\"]+", "<url>", f"{type(exc).__name__}{status}: {text}")[:300]


def non_live_chunks(stats_rows: list[dict[str, Any]], collection: str) -> int:
    """Stored chunks with no live owner: ``stored_count`` less ``count``. An engine that
    predates ``stored_count`` has none to report, so its ``count`` stands for both."""
    rows = [r for r in stats_rows if r.get("name") == collection]
    return sum(
        max(0, int(r.get("stored_count", r.get("count")) or 0) - int(r.get("count") or 0)) for r in rows
    )


def measure_collection(
    collection: str, *, chunk_rows: list[dict[str, Any]], taxonomy: _TaxonomyReader, base_url: str = "",
) -> dict[str, Any]:
    """One collection's row. A failed read (``httpx.HTTPError``, ``VectorServiceError``) is
    status ``error`` with the message, so one 5xx does not lose the rest of the run;
    :class:`EngineTooOld` is not caught."""
    chunks, dims = live_chunks(chunk_rows, collection)
    row: dict[str, Any] = {
        "collection": collection, "live_chunks": chunks, "unassigned": None, "has_taxonomy": None,
        "coverage": None, "above_0_9": False, "status": "ok", "dims": dims,
        "non_live_chunks": non_live_chunks(chunk_rows, collection),
        "coverage_upper": None, "gate_uncertain": False, "note": None,
    }
    if chunks == 0:
        row["status"] = "no_chunks"
        return row
    if len(dims) > 1:
        row["status"] = "multi_dim"
        return row
    try:
        result = count_unassigned(taxonomy, collection)
    except (httpx.HTTPError, VectorServiceError) as exc:
        row["status"] = "error"
        row["error"] = _describe(exc, base_url)
        return row
    if not result.registered:
        row["status"] = "unregistered"
        return row
    row["has_taxonomy"] = result.has_taxonomy
    if not result.has_taxonomy:
        row["status"] = "no_taxonomy"
        return row
    row["unassigned"] = result.unassigned
    row["coverage"] = coverage_of(chunks, result.unassigned)
    row["above_0_9"] = _gate_passes(chunks, result.unassigned)
    # The unassigned read lists a chunk whose every owner is a tombstoned document; the live
    # count leaves it out. At most `non_live_chunks` of the listed chunks are such, so the true
    # coverage is no higher than this, and a collection the gate leaves out but this would let
    # in is marked uncertain rather than read as a plain "no".
    upper_unassigned = max(0, result.unassigned - row["non_live_chunks"])
    row["coverage_upper"] = coverage_of(chunks, upper_unassigned)
    row["gate_uncertain"] = not row["above_0_9"] and _gate_passes(chunks, upper_unassigned)
    if row["gate_uncertain"]:
        row["note"] = GATE_UNRESOLVED
    if result.unassigned > chunks:
        row["anomaly"] = "unassigned_exceeds_live"
    return row


def rebuild_check(
    rebuilt: list[str], saved: dict[str, Any], *, expected_size: int | None = None,
) -> dict[str, Any]:
    """Compare the rebuilt collection set with the saved spike output. Drift is recorded
    here and acted on by nobody: the caller measures the rebuilt set as it is."""
    summary = saved.get("summary", {})
    expected = expected_size if expected_size is not None else int(summary.get("routed_collections", 0))
    prod = set(saved.get("prod", []))
    outside = sorted(set(rebuilt) - prod)
    return {
        "expected_set_size": expected,
        "rebuilt_set_size": len(rebuilt),
        # the spike's per-question routed count (a collection whose centroid ranking came back
        # empty was searched unscoped); not recoverable per collection from the saved output
        "spike_per_question_routed": (summary.get("fallback") or {}).get("routed"),
        "not_in_saved_prod": outside,
        "drift": len(rebuilt) != expected or bool(outside),
    }


def rebuild_set(prose: list[str], taxonomy: Any) -> list[str]:
    """The spike's candidate set: the non-empty prose collections that have topics
    (``taxonomy_ab2.py``: ``prod`` filtered by ``get_distinct_collections``)."""
    with_topics = set(taxonomy.get_distinct_collections())
    return [c for c in prose if c in with_topics]


def measure_collections(
    collections: list[str], *, vector_client: Any, taxonomy: _TaxonomyReader,
    progress: Callable[[dict[str, Any]], None] | None = None,
    rows_out: list[dict[str, Any]] | None = None, base_url: str = "",
) -> list[dict[str, Any]]:
    """Measure each collection against the engine behind the injected clients: one
    ``vector_client.collection_stats()`` read, then the unassigned pages per collection.

    This function reads no environment; which engine it reaches is decided by the clients it
    is handed, which resolve their endpoint and token when they send. *rows_out*, when given,
    is the list the rows are appended to as they finish, so a caller keeps them if a later
    collection raises a fatal error."""
    stats = vector_client.collection_stats()
    rows: list[dict[str, Any]] = rows_out if rows_out is not None else []
    for c in collections:
        rows.append(measure_collection(c, chunk_rows=stats, taxonomy=taxonomy, base_url=base_url))
        if progress is not None:
            progress(rows[-1])
    return rows


def routed_by_gate(rows: list[dict[str, Any]]) -> list[str]:
    return [r["collection"] for r in rows if r["above_0_9"]]


def partition(rows: list[dict[str, Any]], *, spike_routed: int | None = None) -> dict[str, Any]:
    """Split the candidates into: A routed by the gate; B definite coverage misses; C uncertain
    (the gate says no, the upper bound says yes); D not measurable or never routable. The four
    add up to the candidates."""
    a = [r for r in rows if r["above_0_9"]]
    c = [r for r in rows if r["gate_uncertain"]]
    d = [r for r in rows if r["status"] != "ok"]
    b = [r for r in rows if r["status"] == "ok" and not r["above_0_9"] and not r["gate_uncertain"]]
    by_status: dict[str, int] = {}
    for r in d:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    detail = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "none"
    n = len(rows)
    lo, hi = len(b), len(b) + len(c)
    spike = "?" if spike_routed is None else str(spike_routed)
    unread_statuses = ("no_chunks", "multi_dim", "error", "unregistered")
    unread = {k: by_status.get(k, 0) for k in unread_statuses}
    unread_text = ", ".join(f"{k} {v}" for k, v in unread.items())
    no_tax = by_status.get("no_taxonomy", 0)
    with_tax = len(a) + len(b) + len(c)
    # The candidates the spike could not route are the ones with no taxonomy at their dimension
    # (read: no_taxonomy) or that the engine does not know (unregistered), so the two sides agree.
    gap = None if spike_routed is None else n - spike_routed
    not_spike_routable = no_tax + by_status.get("unregistered", 0)
    reconciles = None if gap is None else gap == not_spike_routable
    if gap is None:
        check = "The spike-routed count is unknown, so candidates minus spike-routed was not checked."
    elif reconciles:
        check = (f"Candidates minus spike-routed ({gap}) equals no_taxonomy plus unregistered "
                 f"({not_spike_routable}), as expected.")
    else:
        check = (f"RECONCILIATION MISMATCH: candidates minus spike-routed is {gap}, but no_taxonomy plus "
                 f"unregistered is {not_spike_routable}; the set or the spike count has drifted, so read "
                 "the range with care.")
    return {
        "candidates": n,
        "routed_by_gate": len(a),
        "definite_miss": len(b),
        "uncertain": len(c),
        "not_measurable": len(d),
        "not_measurable_by_status": by_status,
        "with_taxonomy": with_tax,
        "no_taxonomy": no_tax,
        "taxonomy_not_read": unread,
        "reconciles": reconciles,
        "unrouted_low": lo,
        "unrouted_high": hi,
        "line": (f"{n} candidates: {len(a)} routed by the gate; {len(b)} definite coverage misses; "
                 f"{len(c)} uncertain; {len(d)} not measurable or never routable ({detail})"),
        "statement": (
            f"Of {n} candidates ({spike} routed by the spike), the 0.9 gate would leave {lo} to {hi} "
            f"of the spike-routed collections unrouted: {lo} definite, {len(c)} {GATE_UNRESOLVED}. "
            f"With a measured taxonomy: {with_tax}; spike-routed: {spike}. "
            f"Candidates with no taxonomy at their dimension: {no_tax} (read, so not spike-routable). "
            f"has_taxonomy was not read for {unread_text}, so any of those the spike routed adds to the range. "
            f"{check}"
        ),
    }


def build_report(
    rows: list[dict[str, Any]], *, engine: str, date: str, rebuild: dict[str, Any],
    host: str | None = None, complete: bool = True,
) -> dict[str, Any]:
    kept = set(routed_by_gate(rows))
    not_routed = [r["collection"] for r in rows if r["collection"] not in kept]
    part = partition(rows, spike_routed=rebuild.get("spike_per_question_routed"))
    statement = part["statement"]
    if not complete:
        total = rebuild.get("rebuilt_set_size")
        statement = f"PARTIAL RUN, {len(rows)} of {'?' if total is None else total} measured: {statement}"
    return {
        "bead": "nexus-7fku2.1.7",
        "date": date,
        "engine": engine,
        "base_url_host": host,
        "complete": complete,
        "threshold": GATE_THRESHOLD,
        "denominator": "collection_stats count of the collection's (name, dim) row: live owner and embedding",
        "bound_note": ("coverage_upper assumes every stored chunk without a live owner is among the listed "
                       "unassigned; stored_count - count also counts manifest-less ghost chunks the "
                       "unassigned read never lists, so the bound can be loose"),
        "rebuild": rebuild,
        "partition": part,
        "partition_line": part["line"],
        "spike_statement": statement,
        "measured": len(rows),
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "above_0_9": len(kept),
        "not_routed_by_gate": len(not_routed),
        "not_routed_by_gate_names": not_routed,
        "gate_uncertain_names": [r["collection"] for r in rows if r["gate_uncertain"]],
        "coverage_below_0_9": sum(
            1 for r in rows if r["coverage"] is not None and not r["above_0_9"] and not r["gate_uncertain"]),
        "collections": rows,
        "table_markdown": render_table(rows),
    }


def render_table(rows: list[dict[str, Any]]) -> str:
    def cell(v: Any) -> str:
        return "-" if v is None else str(v)

    lines = ["| collection | status | live chunks | unassigned | coverage | >= 0.9 |",
             "|---|---|---:|---:|---:|---|"]
    for r in rows:
        cov = "-" if r["coverage"] is None else f"{r['coverage']:.4f}"
        if r["above_0_9"]:
            gate = "yes"
        elif r["gate_uncertain"]:
            gate = f"uncertain (upper {r['coverage_upper']:.4f})"
        else:
            gate = "no"
        lines.append(
            f"| {r['collection']} | {r['status']} | {r['live_chunks']} | {cell(r['unassigned'])} | {cov} | {gate} |"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class Env:
    """What the run reads from the process: injected, so the guard and the error path are
    testable without an engine."""

    is_local_mode: Callable[[], bool]
    service_base_url: Callable[[], str]
    engine_version: Callable[[], tuple[int, int, int] | None]
    vector_client: Callable[[], Any]
    open_taxonomy: Callable[[], AbstractContextManager[Any]]
    prose_collections: Callable[[], list[str]]


def default_env() -> Env:
    from bench.taxonomy_ab2 import prose_collections  # noqa: PLC0415
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.config import is_local_mode  # noqa: PLC0415
    from nexus.db.http_vector_client import get_http_vector_client  # noqa: PLC0415
    from nexus.db.serving_engine_version import probe_serving_engine_version  # noqa: PLC0415
    from nexus.db.service_endpoint import resolve_service_endpoint_with_evidence_gate  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    @contextlib.contextmanager
    def open_taxonomy() -> Iterator[Any]:
        with T2Database(default_db_path()) as t2:
            yield t2.taxonomy

    return Env(
        is_local_mode=is_local_mode,
        service_base_url=lambda: resolve_service_endpoint_with_evidence_gate()[0],
        engine_version=probe_serving_engine_version,
        vector_client=get_http_vector_client,
        open_taxonomy=open_taxonomy,
        prose_collections=prose_collections,
    )


@dataclass(frozen=True)
class EnginePin:
    base_url: str
    host: str
    build: str


def pin_engine(env: Env, *, allow_local: bool) -> EnginePin:
    """Refuse before any read unless the process is pointed at a service engine this baseline
    may read, and read the engine build first. Raises :class:`EngineRefused`."""
    if env.is_local_mode() and not allow_local:
        raise EngineRefused("this process is in local mode; the baseline reads the cloud engine "
                            "(pass --allow-local for a rehearsal against the local substrate)")
    try:
        base_url = env.service_base_url()
    except Exception as exc:  # noqa: BLE001 -- any resolver failure means no service URL resolved
        raise EngineRefused(f"no service URL resolves: {type(exc).__name__}") from exc
    host = urlparse(base_url).hostname or ""
    if not base_url or not host:
        raise EngineRefused("no service URL resolves")
    version = env.engine_version()
    build = engine_build(lambda: version)
    if version is not None and version < MIN_ENGINE:
        raise EngineRefused(f"engine {build} is below release {'.'.join(map(str, MIN_ENGINE))}, "
                            "which has no unassigned route")
    return EnginePin(base_url, host, build)


def main(argv: list[str] | None = None, *, env: Env | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    ap.add_argument("--saved", type=Path, default=SAVED_SPIKE, help="the saved spike output the set is checked against")
    ap.add_argument("--expected-set-size", type=int, default=None,
                    help="override the saved summary's routed_collections")
    ap.add_argument("--date", default=None, help="date stamp (default: today)")
    ap.add_argument("--out", type=Path, default=None,
                    help="default bench/results/rdr134-taxonomy-coverage-<date>.json")
    ap.add_argument("--strict", action="store_true", help="exit 2 when the rebuilt set has drifted")
    ap.add_argument("--allow-local", action="store_true",
                    help="permit a local-mode process (a rehearsal; the baseline itself reads the cloud engine)")
    a = ap.parse_args(argv)
    date = a.date or datetime.date.today().isoformat()
    out = a.out or Path(f"bench/results/rdr134-taxonomy-coverage-{date}.json")
    env = env or default_env()

    try:
        pin = pin_engine(env, allow_local=a.allow_local)
    except EngineRefused as exc:
        click.echo(f"refused: {exc}", err=True)
        return 3
    click.echo(f"engine: {pin.build} at {pin.host}", err=True)

    saved = json.loads(a.saved.read_text())
    rows: list[dict[str, Any]] = []
    check: dict[str, Any] = {}
    complete = False
    try:
        vector_client = env.vector_client()
        prod = env.prose_collections()
        with env.open_taxonomy() as tax:
            rebuilt = rebuild_set(prod, tax)
            check = rebuild_check(rebuilt, saved, expected_size=a.expected_set_size)
            if check["drift"]:
                click.echo(f"DRIFT: {check}", err=True)
            measure_collections(
                rebuilt, vector_client=vector_client, taxonomy=tax, rows_out=rows, base_url=pin.base_url,
                progress=lambda r: click.echo(
                    f"{r['collection']} {r['status']} {r['live_chunks']} {r['unassigned']} {r['coverage']}",
                    err=True),
            )
        complete = True
    finally:
        report = build_report(rows, engine=pin.build, date=date, rebuild=check, host=pin.host, complete=complete)
        if not complete:
            out = out.with_name(out.name + ".partial")  # never over a complete report at the dated path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1) + "\n")
    click.echo(report["table_markdown"])
    click.echo(f"\n{report['partition_line']}\n{report['spike_statement']}\nwrote {out}")
    errors = report["errors"]
    if errors:
        click.echo(f"{errors} collection(s) errored; the exit is non-zero", err=True)
    if not rows:
        click.echo("the rebuilt set is empty: nothing was measured", err=True)
    if errors or not rows:
        return 1
    return 2 if a.strict and check["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
