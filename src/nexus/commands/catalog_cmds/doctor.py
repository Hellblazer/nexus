# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog diagnostics for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: ``doctor``, the multi-check catalog/T3
health report, together with the ``_run_*`` / ``_print_*`` check pairs and the
doctor threshold constants. ``register`` attaches it to the shared ``catalog``
group.

The LOCAL-EVENT-LOG half of this module is gone (nexus-i711w Stage 2 sub-stage
C-store): the ``synthesize-log`` command, ``_check_bootstrap_status``,
``_run_replay_equality`` / ``_snapshot_table`` / its printer, and
``_run_t3_doc_id_coverage`` / its printer. All four of this module's
local-presence gates lived in them, which is why its kmo9h census entry drops
to zero rather than shrinking. (Spelling the gate's dotted name here would
re-trip that census: it skips ``#`` comments, not docstrings.) They were local-only BY DESIGN and
said so — the event log, the JSONL, and the SQLite projection are local
artifacts, and in service mode the live catalog is owned by the Java service —
so each already refused there via a local-artifacts error (that helper, and
this module's ``nexus.catalog.catalog`` import, went with them). Replay
equality in particular has no service meaning at all: it diffs a projection
rebuilt from events.jsonl against .catalog.db, and service mode has neither.

The only catalog-side helper still referenced here is ``_get_catalog``,
reached through the ``nexus.commands.catalog`` module object inside the
``_run_*`` helpers that need it — keeping imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click



@click.command("doctor")
@click.option(
    "--collections-drift",
    "collections_drift",
    is_flag=True,
    help=(
        "Phase 6 check: every T3 collection and every distinct "
        "documents.physical_collection has a row in the collections "
        "projection. Drift is a release blocker; remediate with "
        "'nx catalog backfill-collections'."
    ),
)
@click.option(
    "--chunk-size-distribution",
    "chunk_size_distribution",
    is_flag=True,
    help=(
        "nexus-6dan: per-collection chunk size stats (p50/p95/p99/max). "
        "FAIL on any chunk > MAX_DOCUMENT_BYTES (Voyage will reject); "
        "WARN when >5% of chunks are < 100 bytes (micro-chunks)."
    ),
)
@click.option(
    "--chunk-text-dedup",
    "chunk_text_dedup",
    is_flag=True,
    help=(
        "nexus-6dan: collect chunk_text_hash across all collections. "
        "Within-collection dupe ratio > 5% signals a chunker bug; "
        "cross-collection dupe count > 100 chunks signals a cross-"
        "ingest investigation lead."
    ),
)
@click.option(
    "--t3-vs-catalog",
    "t3_vs_catalog",
    is_flag=True,
    help=(
        "nexus-6dan: bridge the projection-vs-T3 gap. Reports T3 "
        "collections with no catalog documents (orphan), T3 collections "
        "in catalog projection but with 0 chunks (zombie), and catalog "
        "documents whose physical_collection is gone from T3."
    ),
)
@click.option(
    "--name-vs-embed-dim",
    "name_vs_embed_dim",
    is_flag=True,
    help=(
        "nexus-j9ey: detect pre-4.32 mislabeled collections. Samples "
        "one chunk per conformant T3 collection and compares the "
        "actual embedding dim to the dim implied by the collection's "
        "__<model>__ segment. FAIL on mismatch; suggests `nx collection "
        "rename` to relabel the collection cosmetically (no re-embed)."
    ),
)
@click.option(
    "--store-put-integrity",
    "store_put_integrity",
    is_flag=True,
    help=(
        "nexus-b6enc (GH #1419 Issue 8): store_put-origin integrity. "
        "For content_type='knowledge' docs with no file_path: FAIL on "
        "chunk_count != manifest-row count (drift) and on ghosts (row "
        "with zero manifest AND zero T3 chunks — reported by title + "
        "tumbler so the content can be re-created while still "
        "remembered)."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def doctor_cmd(
    collections_drift: bool,
    chunk_size_distribution: bool,
    chunk_text_dedup: bool,
    t3_vs_catalog: bool,
    name_vs_embed_dim: bool,
    store_put_integrity: bool,
    as_json: bool,
) -> None:
    """RDR-101 catalog doctor surface.

    Every check here works against the SERVICE-owned catalog and T3. The two
    that could not — ``--replay-equality`` and ``--t3-doc-id-coverage``, both
    of which read expectations out of the local ``events.jsonl`` — were removed
    with the local catalog in nexus-i711w Stage 2 sub-stage C-store.

      - ``--collections-drift`` (Phase 6, nexus-o6aa.14): every T3
        collection and every documents.physical_collection has a row
        in the collections projection.
      - ``--chunk-size-distribution`` / ``--chunk-text-dedup`` /
        ``--t3-vs-catalog`` / ``--name-vs-embed-dim`` (nexus-6dan) and
        ``--store-put-integrity``: read-only audits over T3 + catalog.
    """
    any_check = (
        collections_drift
        or chunk_size_distribution or chunk_text_dedup or t3_vs_catalog
        or name_vs_embed_dim or store_put_integrity
    )
    if not any_check:
        raise click.UsageError(
            "Pass a check flag: --collections-drift, "
            "--chunk-size-distribution, --chunk-text-dedup, "
            "--t3-vs-catalog, --name-vs-embed-dim, or "
            "--store-put-integrity."
        )

    overall_pass = True
    json_payload: dict = {}

    if collections_drift:
        report = _run_collections_drift()
        if as_json:
            json_payload["collections_drift"] = report
        else:
            _print_collections_drift_text(report)
        if not report["pass"]:
            overall_pass = False

    # nexus-6dan: 3 new checks. Each is read-only against T3 + catalog.
    _printed_anything = collections_drift
    if chunk_size_distribution:
        report = _run_chunk_size_distribution()
        if as_json:
            json_payload["chunk_size_distribution"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_chunk_size_distribution_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if chunk_text_dedup:
        report = _run_chunk_text_dedup()
        if as_json:
            json_payload["chunk_text_dedup"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_chunk_text_dedup_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if t3_vs_catalog:
        report = _run_t3_vs_catalog()
        if as_json:
            json_payload["t3_vs_catalog"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_t3_vs_catalog_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if name_vs_embed_dim:
        report = _run_name_vs_embed_dim()
        if as_json:
            json_payload["name_vs_embed_dim"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_name_vs_embed_dim_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if store_put_integrity:
        report = _run_store_put_integrity()
        if as_json:
            json_payload["store_put_integrity"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_store_put_integrity_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False

    if as_json:
        click.echo(json.dumps(json_payload, indent=2))

    if not overall_pass:
        raise click.exceptions.Exit(1)


def _run_collections_drift() -> dict:
    """Phase 6 check: collections projection vs T3 + documents.physical_collection.

    Returns ``{"pass": bool, "t3_not_in_projection": list,
    "doc_collections_not_in_projection": list, "projection_not_in_t3": list}``.

    A projection row whose ``superseded_by`` is set is allowed to be
    absent from T3 (post-rename state). Bypass-schema collections
    (``taxonomy__*``) are out of scope for this check.
    """
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    try:
        t3_db = make_t3()
        t3_names = {
            c["name"] for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "t3_not_in_projection": [],
            "doc_collections_not_in_projection": [],
            "projection_not_in_t3": [],
            "error": f"Failed to list T3 collections: {exc}",
        }

    projection = cat.list_collections()
    projection_names = {r["name"] for r in projection}
    superseded_names = {
        r["name"] for r in projection if r.get("superseded_by")
    }

    # nexus-xnz0o: use distinct_doc_collections() (uniform API).
    doc_collections = set(cat.distinct_doc_collections())

    t3_not_in_projection = sorted(t3_names - projection_names)
    doc_not_in_projection = sorted(doc_collections - projection_names)
    projection_not_in_t3 = sorted(
        projection_names - t3_names - superseded_names
    )

    passed = (
        not t3_not_in_projection
        and not doc_not_in_projection
        and not projection_not_in_t3
    )
    return {
        "pass": passed,
        "t3_not_in_projection": t3_not_in_projection,
        "doc_collections_not_in_projection": doc_not_in_projection,
        "projection_not_in_t3": projection_not_in_t3,
    }


def _print_collections_drift_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"collections-drift: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"collections-drift: {status}")
    if report["t3_not_in_projection"]:
        click.echo(
            f"  T3 collections without projection rows "
            f"({len(report['t3_not_in_projection'])}):"
        )
        for n in report["t3_not_in_projection"]:
            click.echo(f"    {n}")
        click.echo(
            "  Remediate: nx catalog backfill-collections"
        )
    if report["doc_collections_not_in_projection"]:
        click.echo(
            f"  documents.physical_collection without projection rows "
            f"({len(report['doc_collections_not_in_projection'])}):"
        )
        for n in report["doc_collections_not_in_projection"]:
            click.echo(f"    {n}")
        click.echo(
            "  Remediate: nx catalog backfill-collections"
        )
    if report["projection_not_in_t3"]:
        click.echo(
            f"  Projection rows whose T3 collection is gone and not "
            f"superseded ({len(report['projection_not_in_t3'])}):"
        )
        for n in report["projection_not_in_t3"]:
            click.echo(f"    {n}")
        # 'rename-collection' would refuse here (it requires the old
        # T3 collection to exist). Direct supersede is the correct
        # recovery; a future 'nx catalog supersede-collection' verb
        # would wrap this script.
        click.echo(
            "  Remediate: register a target collection and supersede manually:\n"
            "    python -c \"from nexus.catalog.catalog import Catalog; "
            "from nexus.config import catalog_path; "
            "p=catalog_path(); c=Catalog(p, p / '.catalog.db'); "
            "c.register_collection('<TARGET>'); "
            "c.supersede_collection('<OLD>', '<TARGET>')\""
        )


# nexus-6dan: tunable thresholds for the 3 new doctor checks. Module-
# level constants so tests can stub them without re-implementing.
_MICRO_CHUNK_BYTES = 100
_MICRO_CHUNK_WARN_RATIO = 0.05
_WITHIN_COLL_DUPE_WARN_RATIO = 0.05
_CROSS_COLL_DUPE_WARN_COUNT = 100


def _percentile(sorted_values: list[int], q: float) -> int:
    """Return the q-th percentile (q in [0,1]) of a sorted-ascending
    int list. Empty list returns 0; single value returns itself.
    Linear interpolation between adjacent values; matches numpy
    default semantics closely enough for ops display.
    """
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return int(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def _run_chunk_size_distribution() -> dict:
    """Per-collection chunk-size statistics (nexus-6dan).

    Walks every T3 collection (paginating <= 300 records per call),
    measures ``len(document_text)`` for each chunk, and reports
    p50/p95/p99/max + counts of micro-chunks (< 100 bytes) and
    over-quota chunks (> ``MAX_DOCUMENT_BYTES``). FAIL on any
    over-quota chunk (Voyage will reject these at embed time);
    WARN flagged at the per-collection level when > 5% of chunks
    are micro-chunks (likely a chunker bug).

    Returns ``{"pass": bool, "tables": {coll_name: {...stats...}}}``.
    Bypass-schema (``taxonomy__*``) collections are skipped: they
    carry centroid embeddings, not chunked text, so size stats
    aren't meaningful.
    """
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)
    from nexus.db.limits import QUOTAS  # noqa: PLC0415  — command-local import (nexus.db.limits)

    try:
        t3 = make_t3()
        collections = [
            c["name"] for c in t3.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "tables": {},
        }

    page = QUOTAS.MAX_QUERY_RESULTS  # 300
    max_doc_bytes = QUOTAS.MAX_DOCUMENT_BYTES
    overall_pass = True
    tables: dict[str, dict] = {}
    for name in collections:
        try:
            col = t3.get_collection(name=name)
        except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            tables[name] = {"error": f"open: {exc}"}
            overall_pass = False
            continue
        sizes: list[int] = []
        offset = 0
        while True:
            try:
                got = col.get(
                    limit=page, offset=offset, include=["documents"],
                )
            except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
                tables[name] = {"error": f"get: {exc}"}
                overall_pass = False
                break
            docs = got.get("documents") or []
            if not docs:
                break
            sizes.extend(len(d or "") for d in docs)
            if len(docs) < page:
                break
            offset += page
        else:
            continue
        sizes.sort()
        n = len(sizes)
        micros = sum(1 for s in sizes if s < _MICRO_CHUNK_BYTES)
        over_quota = sum(1 for s in sizes if s > max_doc_bytes)
        ratio = (micros / n) if n else 0.0
        coll_pass = over_quota == 0
        if not coll_pass:
            overall_pass = False
        tables[name] = {
            "total_chunks": n,
            "p50": _percentile(sizes, 0.5),
            "p95": _percentile(sizes, 0.95),
            "p99": _percentile(sizes, 0.99),
            "max": sizes[-1] if sizes else 0,
            "micro_count": micros,
            "micro_ratio": round(ratio, 4),
            "over_quota_count": over_quota,
            "warn": ratio > _MICRO_CHUNK_WARN_RATIO,
            "pass": coll_pass,
        }
    return {
        "pass": overall_pass,
        "max_document_bytes": max_doc_bytes,
        "micro_chunk_bytes": _MICRO_CHUNK_BYTES,
        "tables": tables,
    }


def _print_chunk_size_distribution_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"chunk-size-distribution: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"chunk-size-distribution: {status}")
    click.echo(
        f"  thresholds: micro < {report['micro_chunk_bytes']}B, "
        f"over-quota > {report['max_document_bytes']}B"
    )
    for name, t in report["tables"].items():
        if "error" in t:
            click.echo(f"  ERROR {name}: {t['error']}")
            continue
        marker = "FAIL" if not t["pass"] else ("WARN" if t["warn"] else "ok")
        click.echo(
            f"  {marker} {name}  total={t['total_chunks']}  "
            f"p50={t['p50']}  p95={t['p95']}  p99={t['p99']}  "
            f"max={t['max']}  micro={t['micro_count']} "
            f"({t['micro_ratio']:.2%})  over_quota={t['over_quota_count']}"
        )


def _run_chunk_text_dedup() -> dict:
    """Cross-collection chunk_text_hash dedup audit (nexus-6dan).

    Walks every non-bypass-schema T3 collection, collects each
    chunk's ``chunk_text_hash`` metadata, and reports:
      - within-collection dupe ratio (one chash mapping to >1 cid):
        WARN when > 5% (signals a chunker bug producing non-distinct
        chunk text from distinct source positions).
      - cross-collection dupes (one chash present in >= 2 collections):
        WARN when count > 100 chunks (signals a cross-ingest pattern
        worth investigating, e.g. fixture re-import or multi-corpus
        leakage).

    Returns
    ``{"pass": bool, "within": {coll: {...}}, "cross": [{...}]}``.
    """
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)
    from nexus.db.limits import QUOTAS  # noqa: PLC0415  — command-local import (nexus.db.limits)

    try:
        t3 = make_t3()
        collections = [
            c["name"] for c in t3.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "within": {},
            "cross": [],
        }

    page = QUOTAS.MAX_QUERY_RESULTS
    overall_pass = True
    within_summary: dict[str, dict] = {}
    chash_to_collections: dict[str, set[str]] = {}
    for name in collections:
        try:
            col = t3.get_collection(name=name)
        except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            within_summary[name] = {"error": f"open: {exc}"}
            overall_pass = False
            continue
        chash_count: dict[str, int] = {}
        offset = 0
        while True:
            try:
                got = col.get(
                    limit=page, offset=offset, include=["metadatas"],
                )
            except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
                within_summary[name] = {"error": f"get: {exc}"}
                overall_pass = False
                break
            metas = got.get("metadatas") or []
            ids = got.get("ids") or []
            if not metas:
                break
            for meta in metas:
                meta = meta or {}
                ch = meta.get("chunk_text_hash") or ""
                if not ch:
                    continue
                chash_count[ch] = chash_count.get(ch, 0) + 1
                chash_to_collections.setdefault(ch, set()).add(name)
            if len(ids) < page:
                break
            offset += page
        else:
            continue
        total = sum(chash_count.values())
        # within-coll dupes: chashes seen >= 2 times in the same collection.
        dupe_chunks = sum(c for c in chash_count.values() if c >= 2)
        ratio = (dupe_chunks / total) if total else 0.0
        warn = ratio > _WITHIN_COLL_DUPE_WARN_RATIO
        within_summary[name] = {
            "total_chunks_with_hash": total,
            "dupe_chunks": dupe_chunks,
            "dupe_ratio": round(ratio, 4),
            "warn": warn,
        }
        # within-coll dupes are surfaced as WARN, not FAIL; the only
        # FAIL surface here is the open/get exception path.

    # Cross-collection: chashes present in >= 2 collections.
    cross = [
        {"chash": ch[:32], "collections": sorted(colls)}
        for ch, colls in chash_to_collections.items()
        if len(colls) >= 2
    ]
    cross_warn = len(cross) > _CROSS_COLL_DUPE_WARN_COUNT
    return {
        "pass": overall_pass,
        "within": within_summary,
        "cross_dupe_chunk_count": len(cross),
        "cross_dupe_warn_threshold": _CROSS_COLL_DUPE_WARN_COUNT,
        "cross_dupe_warn": cross_warn,
        "cross_sample": cross[:20],
    }


def _print_chunk_text_dedup_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"chunk-text-dedup: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"chunk-text-dedup: {status}")
    for name, t in report["within"].items():
        if "error" in t:
            click.echo(f"  ERROR {name}: {t['error']}")
            continue
        marker = "WARN" if t["warn"] else "ok"
        click.echo(
            f"  {marker} {name}  total={t['total_chunks_with_hash']}  "
            f"dupes={t['dupe_chunks']} ({t['dupe_ratio']:.2%})"
        )
    cross_marker = "WARN" if report["cross_dupe_warn"] else "ok"
    click.echo(
        f"  {cross_marker} cross-collection dupes: "
        f"{report['cross_dupe_chunk_count']} "
        f"(threshold {report['cross_dupe_warn_threshold']})"
    )


def _run_t3_vs_catalog() -> dict:
    """Bridge T3 vs catalog: surface 3 drift classes (nexus-6dan).

    Reports:
      - ``t3_orphans``: T3 collections with chunks but no catalog
        documents at all (no row referencing the collection).
      - ``zombies``: collections in the catalog projection that have
        a T3 collection but with 0 chunks.
      - ``docs_pointing_at_missing_t3``: catalog documents whose
        ``physical_collection`` value is not in T3 (e.g. T3 collection
        was deleted out from under the catalog).

    All read-only. PASS when all three lists are empty. Bypass-schema
    collections (``taxonomy__*``) are skipped from all three.
    """
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    try:
        t3_db = make_t3()
        t3_listing = {
            c["name"]: c for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "t3_orphans": [], "zombies": [],
            "docs_pointing_at_missing_t3": [],
        }

    t3_names = set(t3_listing.keys())
    # nexus-xnz0o: use collection_doc_counts() (uniform API).
    docs_per_coll: dict[str, int] = cat.collection_doc_counts()

    # T3 collections with chunks but zero catalog docs:
    t3_orphans = []
    for name in sorted(t3_names):
        if docs_per_coll.get(name, 0) > 0:
            continue
        # Only flag if the T3 collection actually has chunks; an empty
        # T3 collection with no docs is the zombie class below.
        try:
            col = t3_db.get_collection(name=name)
            count = col.count()
        except Exception as exc:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            # nexus-pyv0e sibling: this used to reach into t3_db._client
            # (Chroma-only), which raised AttributeError in service mode
            # and got silently swallowed here into count=0 — a false PASS
            # with zero detection capability, not a loud error. Record the
            # failure instead of pretending the collection is empty.
            t3_orphans.append({"name": name, "error": str(exc)})
            continue
        if count > 0:
            t3_orphans.append({"name": name, "chunk_count": count})

    # Zombies: in catalog projection AND in T3 BUT 0 chunks in T3.
    projection = cat.list_collections()
    projection_names = {
        r["name"] for r in projection if not r.get("superseded_by")
    }
    zombies = []
    zombie_errors: list[dict] = []
    for name in sorted(projection_names & t3_names):
        try:
            col = t3_db.get_collection(name=name)
            count = col.count()
        except Exception as exc:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            # nexus-pyv0e sibling: `continue`-past-error here previously
            # meant a failed check for a candidate zombie silently dropped
            # it from consideration — a false PASS, not a loud error.
            zombie_errors.append({"name": name, "error": str(exc)})
            continue
        if count == 0:
            zombies.append(name)

    # Catalog docs whose physical_collection is missing from T3.
    docs_missing = [
        {"physical_collection": pc, "doc_count": cnt}
        for pc, cnt in sorted(docs_per_coll.items())
        if pc and pc not in t3_names
    ]

    overall_pass = (
        not t3_orphans and not zombies and not docs_missing
        and not zombie_errors
    )
    return {
        "pass": overall_pass,
        "t3_orphans": t3_orphans,
        "zombies": zombies,
        "zombie_errors": zombie_errors,
        "docs_pointing_at_missing_t3": docs_missing,
    }


def _print_t3_vs_catalog_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"t3-vs-catalog: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"t3-vs-catalog: {status}")
    if report["t3_orphans"]:
        click.echo(
            f"  T3 collections with chunks but no catalog docs "
            f"({len(report['t3_orphans'])}):"
        )
        for o in report["t3_orphans"][:20]:
            if "error" in o:
                click.echo(f"    {o['name']}  ERROR: {o['error']}")
            else:
                click.echo(f"    {o['name']}  chunks={o['chunk_count']}")
    if report["zombies"]:
        click.echo(
            f"  Zombie collections (registered, 0 chunks in T3) "
            f"({len(report['zombies'])}):"
        )
        for n in report["zombies"][:20]:
            click.echo(f"    {n}")
        click.echo(
            "  Remediate: nx catalog collection-gc --apply"
        )
    if report.get("zombie_errors"):
        click.echo(
            f"  Collections that could not be checked for zombie status "
            f"({len(report['zombie_errors'])}):"
        )
        for e in report["zombie_errors"][:20]:
            click.echo(f"    {e['name']}  ERROR: {e['error']}")
    if report["docs_pointing_at_missing_t3"]:
        click.echo(
            f"  Catalog documents whose physical_collection is gone "
            f"from T3 ({len(report['docs_pointing_at_missing_t3'])}):"
        )
        for d in report["docs_pointing_at_missing_t3"][:20]:
            click.echo(
                f"    {d['physical_collection']}  docs={d['doc_count']}"
            )


# ── nexus-j9ey: --name-vs-embed-dim ──────────────────────────────────────


_VOYAGE_DIM = 1024
"""All current voyage-3 family embedders produce 1024-dim vectors
(voyage-3, voyage-code-3, voyage-context-3). Hardcoded because the
token alone has no dim suffix. If Voyage adds a different-dim model
to the canonical set this needs to grow into a map."""


def _expected_dim_for_model_token(token: str) -> int | None:
    """Return the dim implied by a conformant ``__<model>__`` segment,
    or None if the token is unrecognized.

    Local-mode tokens encode the dim in the suffix
    (``minilm-l6-v2-384`` -> 384, ``bge-base-en-v15-768`` -> 768).
    Voyage tokens are hardcoded to 1024."""
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        CANONICAL_EMBEDDING_MODELS,
        LOCAL_EMBEDDING_MODELS,
    )
    if token in CANONICAL_EMBEDDING_MODELS:
        return _VOYAGE_DIM
    if token in LOCAL_EMBEDDING_MODELS:
        tail = token.rsplit("-", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return None
    return None


def _run_name_vs_embed_dim() -> dict:
    """Detect mislabeled conformant collections (4.28-era write-side bug).

    Iterates T3 collections, skips bypass-schema and non-conformant
    names, samples one chunk per remaining collection, and compares
    actual embedding dim to the dim implied by the name's
    ``__<model>__`` segment. Read-only against T3."""
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    mismatches: list[dict] = []
    empty: list[str] = []
    checked = 0
    skipped_non_conformant = 0
    unknown_token: list[dict] = []
    read_errors: list[dict] = []

    try:
        t3_db = make_t3()
        cols = [
            c["name"] for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "checked": 0,
            "mismatches": [],
            "empty": [],
            "skipped_non_conformant": 0,
            "unknown_token": [],
            "error": f"Failed to list T3 collections: {exc}",
        }

    for name in cols:
        if not is_conformant_collection_name(name):
            skipped_non_conformant += 1
            continue
        parsed = parse_conformant_collection_name(name)
        token = parsed["embedding_model"]
        expected = _expected_dim_for_model_token(token)
        if expected is None:
            unknown_token.append({"collection": name, "token": token})
            continue
        probe_err: str | None = None
        ids: list = []
        embs = None
        # ou4tb critique: ONE bounded retry so a single transient blip
        # during a multi-collection run doesn't flap the whole check to
        # FAIL; persistent unreadability still fails loud below.
        for _attempt in (1, 2):
            try:
                # nexus-pyv0e: sample via the dual-mode-safe public surface
                # (get_collection + get_embeddings), not client._client — the
                # service-mode HttpVectorClient has no ._client attribute, only
                # local T3Database's raw chromadb client does.
                coll = t3_db.get_collection(name)
                sample = coll.get(limit=1)
                ids = sample.get("ids") or []
                embs = t3_db.get_embeddings(name, ids[:1]) if ids else None
                probe_err = None
                break
            except Exception as exc:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
                probe_err = str(exc)
        if probe_err is not None:
            # nexus-ou4tb walk (MEDIUM): a read failure is NOT an
            # "unrecognized model token" — burying it there let a fully
            # unreadable/degraded store render PASS (checked=0), the
            # confident-but-blind false all-clear this check exists to
            # prevent. Read errors get their own bucket and FAIL the check.
            read_errors.append(
                {"collection": name, "token": token, "error": probe_err}
            )
            continue
        if not ids:
            empty.append(name)
            continue
        if embs is None or len(embs) == 0:
            empty.append(name)
            continue
        actual = len(embs[0])
        checked += 1
        if actual != expected:
            mismatches.append({
                "collection": name,
                "claimed_model": token,
                "expected_dim": expected,
                "actual_dim": actual,
            })

    return {
        "pass": not mismatches and not read_errors,
        "checked": checked,
        "mismatches": mismatches,
        "empty": empty,
        "skipped_non_conformant": skipped_non_conformant,
        "unknown_token": unknown_token,
        "read_errors": read_errors,
    }


def _print_name_vs_embed_dim_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"name-vs-embed-dim: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"name-vs-embed-dim: {status}")
    click.echo(
        f"  checked={report['checked']}  "
        f"mismatches={len(report['mismatches'])}  "
        f"empty={len(report['empty'])}  "
        f"skipped-non-conformant={report['skipped_non_conformant']}"
    )
    if report["mismatches"]:
        click.echo(
            f"\n  Mislabeled collections ({len(report['mismatches'])}):"
        )
        for m in report["mismatches"]:
            click.echo(
                f"    {m['collection']}\n"
                f"      claims {m['claimed_model']} "
                f"({m['expected_dim']}d) but holds {m['actual_dim']}d vectors"
            )
        click.echo(
            "\n  Remediate: relabel the collection to match its actual "
            "embeddings:\n"
            "    nx collection rename <old> <new>\n"
            "  Local-mode users: replace the voyage-* segment with the "
            "matching local token (e.g. minilm-l6-v2-384 for 384d, "
            "bge-base-en-v15-768 for 768d). No re-embed; cosmetic only."
        )
    if report["unknown_token"]:
        click.echo(
            f"\n  Collections with unrecognized model token "
            f"({len(report['unknown_token'])}):"
        )
        for u in report["unknown_token"][:20]:
            extra = f"  ({u['error']})" if u.get("error") else ""
            click.echo(f"    {u['collection']}  token={u['token']}{extra}")
    if report.get("read_errors"):
        click.echo(
            f"\n  UNREADABLE collections ({len(report['read_errors'])}) — "
            f"embedding probe failed; the store could not be verified "
            f"(degraded vector service?):"
        )
        for u in report["read_errors"][:20]:
            click.echo(f"    {u['collection']}  {u['error']}")


def _run_store_put_integrity() -> dict:
    """store_put-origin integrity check (nexus-b6enc / GH #1419 Issue 8).

    Scope: catalog documents with ``content_type='knowledge'``, no
    ``file_path`` and a ``meta.doc_id`` (the chunk natural id) — the
    store_put / memory-promote origin signature.

    Two failure classes, both FATAL (``pass=False``):
      - **drift**: ``documents.chunk_count`` != COUNT of manifest rows
        (the C3 manifest-swallow damage class).
      - **ghost**: row with zero manifest rows AND zero T3 chunks (the
        C2 ghost-register damage class). Reported with TITLE + TUMBLER
        so the content can be re-created while it is still remembered.

    A third, NON-fatal bucket (critic Sig 1 / CRE Imp 2):
      - **unverifiable**: a ghost CANDIDATE (zero manifest + zero
        chunk_count) whose T3 presence could not be confirmed — either
        ``make_t3()`` itself failed, or this doc's chunk lookup raised
        (``get_by_id`` already maps a missing collection to ``None``, so
        a raise here is a transient/environmental error, never the
        ghost signature). These are WARNED with named docs and never
        classified as ghosts: "content is GONE" is a claim this check
        must only make about a VERIFIED zero-chunk doc.

    ``checked`` counts scanned store_put-origin docs so a clean result
    is provably non-vacuous.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import (nexus.db)

    try:
        cat = _cat_cmd._get_catalog()
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "error": f"Failed to open catalog: {exc}",
            "checked": 0, "drift": [], "ghosts": [],
        }
    try:
        docs = [
            e for e in cat.all_documents(content_type="knowledge")
            if not e.file_path and (e.meta or {}).get("doc_id")
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "error": f"Failed to list knowledge documents: {exc}",
            "checked": 0, "drift": [], "ghosts": [],
        }

    t3_db = None
    t3_error = ""
    try:
        t3_db = make_t3()
    except Exception as exc:  # noqa: BLE001 — T3 unavailable: ghost detection degrades loudly, never a silent all-clean
        t3_error = str(exc)

    drift: list[dict] = []
    ghosts: list[dict] = []
    check_errors: list[dict] = []
    unverifiable: list[dict] = []
    for e in docs:
        tumbler = str(e.tumbler)
        try:
            manifest_count = len(cat.get_manifest(tumbler))
        except Exception as exc:  # noqa: BLE001 — a failed per-doc read must be reported, not silently skipped (false PASS)
            check_errors.append({"tumbler": tumbler, "error": str(exc)})
            continue
        if e.chunk_count != manifest_count:
            drift.append({
                "tumbler": tumbler,
                "title": e.title,
                "chunk_count": e.chunk_count,
                "manifest_count": manifest_count,
            })
        if e.chunk_count == 0 and manifest_count == 0:
            # Ghost candidate: confirm zero T3 chunks for the doc's
            # chunk natural id in its physical_collection. A ghost
            # verdict ("content is GONE") requires a VERIFIED lookup:
            # when T3 is unavailable, or this doc's lookup raises
            # (``get_by_id`` returns None for a missing collection, so
            # a raise is transient — timeout/auth/network, not the
            # ghost signature), the doc is UNVERIFIABLE, never a ghost
            # (critic Sig 1 / CRE Imp 2).
            chash = (e.meta or {}).get("doc_id", "")
            if t3_db is None:
                unverifiable.append({
                    "tumbler": tumbler, "title": e.title,
                    "reason": f"T3 unavailable: {t3_error}",
                })
                continue
            if not e.physical_collection or not chash:
                # No collection/chash to look up — nothing can be in T3.
                ghosts.append({"tumbler": tumbler, "title": e.title})
                continue
            try:
                chunk_present = (
                    t3_db.get_by_id(e.physical_collection, chash)
                    is not None
                )
            except Exception as exc:  # noqa: BLE001 — transient per-doc lookup failure: unverifiable, never a false "GONE" verdict
                unverifiable.append({
                    "tumbler": tumbler, "title": e.title,
                    "reason": f"chunk lookup failed: {exc}",
                })
                continue
            if not chunk_present:
                ghosts.append({"tumbler": tumbler, "title": e.title})

    report = {
        "pass": not drift and not ghosts and not check_errors,
        "checked": len(docs),
        "drift": drift,
        "ghosts": ghosts,
        "check_errors": check_errors,
        "unverifiable": unverifiable,
    }
    if t3_error:
        report["t3_unavailable"] = t3_error
    return report


def _print_store_put_integrity_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"store-put-integrity: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(
        f"store-put-integrity: {status} "
        f"(checked {report['checked']} store_put-origin docs)"
    )
    if report.get("t3_unavailable"):
        click.echo(
            f"  WARNING: T3 unavailable ({report['t3_unavailable']}) — "
            "ghost candidates could not be verified (see unverifiable)."
        )
    if report["drift"]:
        click.echo(
            f"  chunk_count/manifest drift ({len(report['drift'])}):"
        )
        for d in report["drift"][:20]:
            click.echo(
                f"    {d['tumbler']}  {d['title']!r}  "
                f"chunk_count={d['chunk_count']} "
                f"manifest={d['manifest_count']}"
            )
        click.echo("  Remediate: nx catalog reconcile")
    if report["ghosts"]:
        click.echo(
            f"  GHOSTS — row with zero manifest AND zero chunks "
            f"({len(report['ghosts'])}); content is GONE, re-create it "
            f"from the titles below:"
        )
        for g in report["ghosts"][:50]:
            click.echo(f"    {g['tumbler']}  {g['title']!r}")
    if report.get("unverifiable"):
        click.echo(
            f"  WARNING: unverifiable ghost candidates "
            f"({len(report['unverifiable'])}) — T3 presence could not "
            f"be confirmed; NOT claiming their content is gone. Re-run "
            f"when T3 is reachable:"
        )
        for u in report["unverifiable"][:50]:
            click.echo(
                f"    {u['tumbler']}  {u['title']!r}  ({u['reason']})"
            )
    if report.get("check_errors"):
        click.echo(
            f"  Docs that could not be checked "
            f"({len(report['check_errors'])}):"
        )
        for e in report["check_errors"][:20]:
            click.echo(f"    {e['tumbler']}  ERROR: {e['error']}")


def register(group: click.Group) -> None:
    """Attach the diagnostics commands to the shared ``catalog`` group."""
    group.add_command(doctor_cmd)
