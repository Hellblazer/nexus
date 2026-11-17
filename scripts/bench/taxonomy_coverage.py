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
    and ``gate_uncertain`` (the gate says no but the upper bound says yes).

The collection set is rebuilt as the spike built it: the non-empty prose collections of
``nx collection list`` that have topics (``taxonomy_ab2.py``, ``prose_collections`` and
``get_distinct_collections``). The saved spike output records the size of that set; a
rebuild that does not match it, or names a collection the spike never saw, is recorded as
drift in the output and never adjusted.

    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_coverage.py
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Protocol

import click
import httpx

GATE_THRESHOLD = 0.9
PAGE_SIZE = 1000  # the engine's cap per page (TaxonomyRepository.MAX_UNASSIGNED_CHASHES)
_MAX_PAGES = 100_000  # a bound on a runaway cursor, far above any real collection
SAVED_SPIKE = Path("bench/results/rdr134-taxonomy-ab-prod-2026-11-16.json")


class EngineTooOld(RuntimeError):
    """The engine has no ``/v1/taxonomy/assignments/unassigned`` route (below v0.1.132)."""


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
            if status == 422:
                return UnassignedCount(0, False, False, pages + 1)
            raise
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


def non_live_chunks(stats_rows: list[dict[str, Any]], collection: str) -> int:
    """Stored chunks with no live owner: ``stored_count`` less ``count``. An engine that
    predates ``stored_count`` has none to report, so its ``count`` stands for both."""
    rows = [r for r in stats_rows if r.get("name") == collection]
    return sum(
        max(0, int(r.get("stored_count", r.get("count")) or 0) - int(r.get("count") or 0)) for r in rows
    )


def measure_collection(
    collection: str, *, chunk_rows: list[dict[str, Any]], taxonomy: _TaxonomyReader,
) -> dict[str, Any]:
    chunks, dims = live_chunks(chunk_rows, collection)
    row: dict[str, Any] = {
        "collection": collection, "live_chunks": chunks, "unassigned": None, "has_taxonomy": None,
        "coverage": None, "above_0_9": False, "status": "ok", "dims": dims,
        "non_live_chunks": non_live_chunks(chunk_rows, collection),
        "coverage_upper": None, "gate_uncertain": False,
    }
    if chunks == 0:
        row["status"] = "no_chunks"
        return row
    if len(dims) > 1:
        row["status"] = "multi_dim"
        return row
    result = count_unassigned(taxonomy, collection)
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
) -> list[dict[str, Any]]:
    """Measure each collection against the engine behind the injected clients: one
    ``vector_client.collection_stats()`` read, then the unassigned pages per collection.
    Nothing here reads the environment, so a caller decides which engine it is."""
    stats = vector_client.collection_stats()
    rows: list[dict[str, Any]] = []
    for c in collections:
        rows.append(measure_collection(c, chunk_rows=stats, taxonomy=taxonomy))
        if progress is not None:
            progress(rows[-1])
    return rows


def routed_by_gate(rows: list[dict[str, Any]]) -> list[str]:
    return [r["collection"] for r in rows if r["above_0_9"]]


def build_report(rows: list[dict[str, Any]], *, engine: str, date: str, rebuild: dict[str, Any]) -> dict[str, Any]:
    kept = set(routed_by_gate(rows))
    not_routed = [r["collection"] for r in rows if r["collection"] not in kept]
    return {
        "bead": "nexus-7fku2.1.7",
        "date": date,
        "engine": engine,
        "threshold": GATE_THRESHOLD,
        "denominator": "collection_stats count of the collection's (name, dim) row: live owner and embedding",
        "rebuild": rebuild,
        "measured": len(rows),
        "above_0_9": len(kept),
        "not_routed_by_gate": len(not_routed),
        "not_routed_by_gate_names": not_routed,
        "gate_uncertain_names": [r["collection"] for r in rows if r["gate_uncertain"]],
        "coverage_below_0_9": sum(1 for r in rows if r["coverage"] is not None and not r["above_0_9"]),
        "collections": rows,
        "table_markdown": render_table(rows),
    }


def render_table(rows: list[dict[str, Any]]) -> str:
    def cell(v: Any) -> str:
        return "-" if v is None else str(v)

    lines = ["| collection | live chunks | unassigned | coverage | >= 0.9 |", "|---|---:|---:|---:|---|"]
    for r in rows:
        cov = "-" if r["coverage"] is None else f"{r['coverage']:.3f}"
        lines.append(
            f"| {r['collection']} | {r['live_chunks']} | {cell(r['unassigned'])} | {cov} | "
            f"{'yes' if r['above_0_9'] else 'no'} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    ap.add_argument("--saved", type=Path, default=SAVED_SPIKE, help="the saved spike output the set is checked against")
    ap.add_argument("--expected-set-size", type=int, default=None,
                    help="override the saved summary's routed_collections")
    ap.add_argument("--date", default=None, help="date stamp (default: today)")
    ap.add_argument("--out", type=Path, default=None,
                    help="default bench/results/rdr134-taxonomy-coverage-<date>.json")
    ap.add_argument("--strict", action="store_true", help="exit 2 when the rebuilt set has drifted")
    a = ap.parse_args(argv)
    date = a.date or datetime.date.today().isoformat()
    out = a.out or Path(f"bench/results/rdr134-taxonomy-coverage-{date}.json")

    from bench.manifest import engine_build  # noqa: PLC0415
    from bench.taxonomy_ab2 import prose_collections  # noqa: PLC0415
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.http_vector_client import get_http_vector_client  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    saved = json.loads(a.saved.read_text())
    vector_client = get_http_vector_client()
    prod = prose_collections()
    with T2Database(default_db_path()) as t2:
        tax = t2.taxonomy
        rebuilt = rebuild_set(prod, tax)
        check = rebuild_check(rebuilt, saved, expected_size=a.expected_set_size)
        if check["drift"]:
            click.echo(f"DRIFT: {check}", err=True)
        rows = measure_collections(
            rebuilt, vector_client=vector_client, taxonomy=tax,
            progress=lambda r: click.echo(
                f"{r['collection']} {r['live_chunks']} {r['unassigned']} {r['coverage']}", err=True),
        )

    report = build_report(rows, engine=engine_build(), date=date, rebuild=check)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    click.echo(report["table_markdown"])
    click.echo(f"\nmeasured {report['measured']}; at or above 0.9: {report['above_0_9']}; "
        f"not routed by the gate: {report['not_routed_by_gate']} "
        f"(coverage below 0.9: {report['coverage_below_0_9']}); wrote {out}")
    return 2 if a.strict and check["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
