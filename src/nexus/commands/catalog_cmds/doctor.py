# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog diagnostics for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: ``doctor`` (the multi-check catalog/T3
health report) and ``synthesize-log`` (RDR-101 Phase 2 in-place fallback
recovery), together with every diagnostic helper they use — the ``_run_*`` /
``_print_*`` check pairs, ``_check_bootstrap_status``, ``_run_replay_equality``
/ ``_snapshot_table`` (shared between doctor and synthesize-log, which is why
both commands are carved together), and the doctor threshold constants.
Behaviour-preserving; ``register`` attaches both commands to the shared
``catalog`` group.

The only catalog-side helper still referenced here is ``_get_catalog``,
reached through the ``nexus.commands.catalog`` module object inside the two
``_run_*`` helpers that need it — keeping imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from nexus.catalog.catalog import Catalog


@click.command("synthesize-log")
@click.option(
    "--check", is_flag=True,
    help=(
        "Detect bootstrap-fallback mode without writing. Exit 0 when not "
        "in fallback, exit 1 when fallback is active."
    ),
)
@click.option(
    "--dry-run", is_flag=True,
    help="Print event counts that would be synthesized; write nothing.",
)
@click.option(
    "--no-verify", is_flag=True,
    help=(
        "Skip the post-write replay-equality verification. Use only when "
        "you have already verified the catalog independently."
    ),
)
@click.option(
    "--force", is_flag=True,
    help=(
        "Synthesize even when the catalog is not in bootstrap-fallback. "
        "Existing event-log doc_ids are harvested and preserved so that "
        "T3 chunk metadata referencing them does not become stale."
    ),
)
def synthesize_log_cmd(
    check: bool, dry_run: bool, no_verify: bool, force: bool
) -> None:
    """Rebuild ``events.jsonl`` from the catalog's JSONL state in place.

    Companion to ``nx catalog doctor`` for catalogs in bootstrap-fallback
    mode. Calls ``nexus.catalog.synthesizer.synthesize_from_jsonl`` with
    ``mint_doc_id=True`` and writes the resulting envelope stream to
    ``events.jsonl`` atomically. Snapshots the entire catalog directory
    before touching it; on a verify FAIL, rolls the snapshot back into
    place and retains both copies for forensics.

    Lossless alternative to ``rm -rf catalog && nx catalog setup``, which
    discards user-authored typed links and owner registrations because
    those are not reconstructible from T3 alone.
    """
    import dataclasses  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    import shutil  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    import time  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from datetime import datetime, timezone  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog import events as ev  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.synthesizer import synthesize_from_jsonl  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        raise click.ClickException(
            f"Catalog at {cat_path} is not initialized. "
            "Run 'nx catalog setup' first."
        )

    bootstrap_status = _check_bootstrap_status()
    fallback_active = bool(bootstrap_status.get("fallback_active"))

    if check:
        if fallback_active:
            click.echo(
                "fallback-active: events.jsonl is sparse vs documents.jsonl. "
                "Run 'nx catalog synthesize-log' to repair in place.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        click.echo("not-in-fallback: events.jsonl matches documents.jsonl.")
        return

    if not fallback_active and not force:
        click.echo(
            "no-op: catalog is not in bootstrap-fallback mode. "
            "Pass --force to synthesize anyway."
        )
        return

    # --force on a healthy catalog: harvest existing tumbler->doc_id from
    # events.jsonl so re-synthesis preserves T3-side doc_id references.
    preserve_doc_ids: dict[str, str] = {}
    events_path = cat_path / "events.jsonl"
    if force and events_path.exists():
        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != ev.TYPE_DOCUMENT_REGISTERED:
                    continue
                payload = obj.get("payload") or {}
                tumbler = payload.get("tumbler")
                doc_id = payload.get("doc_id")
                if tumbler and doc_id:
                    preserve_doc_ids[tumbler] = doc_id

    # Synthesize the full event stream into memory and tally per-type.
    events_list = list(
        synthesize_from_jsonl(
            cat_path,
            mint_doc_id=True,
            preserve_doc_ids=preserve_doc_ids or None,
        )
    )
    counts: dict[str, int] = {}
    for e in events_list:
        counts[e.type] = counts.get(e.type, 0) + 1
    total = sum(counts.values())

    click.echo("== synthesizing events ==")
    for type_name in sorted(counts):
        click.echo(f"  {type_name:<28} {counts[type_name]:>6}")
    click.echo(f"  {'TOTAL':<28} {total:>6}")

    if dry_run:
        click.echo("(dry-run: no files written)")
        return

    # Snapshot the entire catalog directory to a sibling. Forensic
    # retention: this command never deletes the snapshot, even on PASS.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = cat_path.parent / f"{cat_path.name}.synth-snapshot-{ts}"
    if snapshot_dir.exists():
        # Disambiguate when a prior run within the same second exists.
        snapshot_dir = cat_path.parent / (
            f"{cat_path.name}.synth-snapshot-{ts}-{int(time.time() * 1000) % 1000}"
        )
    # Skip ``.db-shm`` and ``.db-wal`` (transient WAL artifacts). On
    # Linux either file may be listed by the directory scan but
    # disappear before the per-file copy, raising FileNotFoundError.
    # ``.db-shm`` is regenerated by SQLite on next open; ``.db-wal``
    # checkpoints fold back into the main db on connection close.
    # Nothing forensic survives a completed checkpoint, so omitting both
    # keeps the snapshot reproducible across runs without losing state.
    # nexus-fmhv: CI hit the race consistently in
    # test_force_synthesizes_when_not_in_fallback.
    shutil.copytree(
        cat_path,
        snapshot_dir,
        ignore=shutil.ignore_patterns("*.db-shm", "*.db-wal"),
    )
    click.echo(f"snapshot: {snapshot_dir}")

    # Atomic write: serialize to events.jsonl.tmp, fsync, rename.
    tmp_path = events_path.with_suffix(".jsonl.tmp")
    with tmp_path.open("w") as f:
        for e in events_list:
            line = json.dumps(
                {
                    "type": e.type,
                    "v": e.v,
                    "payload": dataclasses.asdict(e.payload),
                    "ts": e.ts,
                },
                separators=(",", ":"),
            )
            f.write(line)
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, events_path)
    click.echo(f"wrote: {events_path} ({total} events)")

    if no_verify:
        click.echo("PASS (verify skipped via --no-verify)")
        return

    # Verify by re-running the doctor's replay-equality check against
    # the freshly-written log. _run_replay_equality reads catalog_path()
    # so it picks up the new state automatically.
    report = _run_replay_equality()
    if report.get("pass"):
        click.echo("PASS: replay-equality verified against fresh log.")
        click.echo(f"snapshot retained for forensics: {snapshot_dir}")
        return

    # Verify FAIL: rotate the failed live state aside, restore from the
    # snapshot via copytree. Snapshot is left pristine so the operator
    # has three artifacts for forensics: the pristine pre-synthesis
    # snapshot, the failed live state, and the restored live catalog.
    failed_dir = cat_path.parent / f"{cat_path.name}.synth-failed-{ts}"
    if failed_dir.exists():
        failed_dir = cat_path.parent / (
            f"{cat_path.name}.synth-failed-{ts}-{int(time.time() * 1000) % 1000}"
        )
    os.rename(cat_path, failed_dir)
    shutil.copytree(snapshot_dir, cat_path)

    click.echo(
        f"FAIL: replay-equality verification did not pass: {report}",
        err=True,
    )
    click.echo(f"failed-state retained: {failed_dir}", err=True)
    click.echo(f"snapshot retained: {snapshot_dir}", err=True)
    click.echo(f"catalog restored from snapshot at: {cat_path}", err=True)
    raise click.exceptions.Exit(1)


# ── RDR-101 Phase 1: doctor --replay-equality ────────────────────────────


@click.command("doctor")
@click.option(
    "--replay-equality",
    "replay_equality",
    is_flag=True,
    help=(
        "Drive the synthesizer + projector against the live catalog and "
        "diff the projected SQLite against the live .catalog.db. "
        "Confirms that the event-sourced projection is deterministic for "
        "the current catalog state. Read-only against the live catalog."
    ),
)
@click.option(
    "--t3-doc-id-coverage",
    "t3_doc_id_coverage",
    is_flag=True,
    help=(
        "Walk every T3 collection and report doc_id coverage. PASS = "
        "every non-orphan chunk in every collection carries a doc_id "
        "matching what events.jsonl claims. Read-only against T3 and "
        "the catalog. The Phase 2 backfill verb that originally "
        "populated chunks with their doc_id was retired post Phase 5b "
        "(nexus-iftc); operators on conformant catalogs should see "
        "PASS without further action."
    ),
)
@click.option(
    "--strict-not-in-t3",
    "strict_not_in_t3",
    is_flag=True,
    help=(
        "With --t3-doc-id-coverage: treat 'event log claims a chunk T3 "
        "doesn't have' as a hard failure rather than a warning. Default "
        "is warning so legitimate operational deletions (re-ingestion, "
        "pruning) don't permanently red the doctor; pass --strict-not-"
        "in-t3 to enforce 'event log = authoritative ledger, T3 must "
        "match exactly'."
    ),
)
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
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def doctor_cmd(
    replay_equality: bool,
    t3_doc_id_coverage: bool,
    strict_not_in_t3: bool,
    collections_drift: bool,
    chunk_size_distribution: bool,
    chunk_text_dedup: bool,
    t3_vs_catalog: bool,
    name_vs_embed_dim: bool,
    as_json: bool,
) -> None:
    """RDR-101 catalog doctor surface.

    Supports three checks today:
      - ``--replay-equality`` (Phase 1, PR C): synthesizer + projector
        round-trip against the live SQLite.
      - ``--t3-doc-id-coverage`` (Phase 2, PR δ): T3 chunks carry the
        doc_id metadata that events.jsonl claims.
      - ``--collections-drift`` (Phase 6, nexus-o6aa.14): every T3
        collection and every documents.physical_collection has a row
        in the collections projection.

    Future flags land in later phases.
    """
    any_check = (
        replay_equality or t3_doc_id_coverage or collections_drift
        or chunk_size_distribution or chunk_text_dedup or t3_vs_catalog
        or name_vs_embed_dim
    )
    if not any_check:
        raise click.UsageError(
            "Pass a check flag: --replay-equality, "
            "--t3-doc-id-coverage, --collections-drift, "
            "--chunk-size-distribution, --chunk-text-dedup, "
            "--t3-vs-catalog, or --name-vs-embed-dim."
        )
    if strict_not_in_t3 and not t3_doc_id_coverage:
        raise click.UsageError(
            "--strict-not-in-t3 requires --t3-doc-id-coverage; the "
            "flag scopes the not-in-T3 fail behaviour of the coverage "
            "check and is meaningless without it."
        )

    overall_pass = True
    json_payload: dict = {}

    # RDR-101 Phase 3 follow-up B (nexus-o6aa.9.7): surface bootstrap
    # fallback state to the operator. When _ensure_consistent runtime-
    # decides to fall back to legacy reads (events.jsonl is non-empty
    # but sparse vs documents.jsonl), ES writes still land in the log
    # while reads come from legacy JSONL — a silent split state where
    # replay-equality is fundamentally not testing what it claims.
    # Construct a Catalog up front so _ensure_consistent runs and
    # bootstrap_fallback_active is current.
    bootstrap_status = _check_bootstrap_status()
    if bootstrap_status["fallback_active"]:
        if as_json:
            json_payload["bootstrap_fallback"] = bootstrap_status
        else:
            click.echo(
                "WARNING: catalog is operating in bootstrap-fallback mode.\n"
                "  events.jsonl is non-empty but sparse vs documents.jsonl;\n"
                "  ES writes are landing in the log but reads come from\n"
                "  legacy JSONL; replay equality is silently broken.\n"
                "\n"
                "  Restore in place with:\n"
                "    nx catalog synthesize-log\n"
                "\n"
                "  This rebuilds events.jsonl from the JSONL state with\n"
                "  zero data loss. 'nx catalog setup' from a clean state\n"
                "  is a lossy fallback - it cannot reconstruct user-\n"
                "  authored typed links or owner registrations from T3.\n",
                err=True,
            )
        overall_pass = False

    if replay_equality:
        report = _run_replay_equality()
        if as_json:
            json_payload["replay_equality"] = report
        else:
            _print_replay_equality_text(report)
        if not report["pass"]:
            overall_pass = False

    if t3_doc_id_coverage:
        report = _run_t3_doc_id_coverage(strict_not_in_t3=strict_not_in_t3)
        if as_json:
            json_payload["t3_doc_id_coverage"] = report
        else:
            if replay_equality:
                click.echo("")  # separator between checks
            _print_t3_doc_id_coverage_text(report)
        if not report["pass"]:
            overall_pass = False

    if collections_drift:
        report = _run_collections_drift()
        if as_json:
            json_payload["collections_drift"] = report
        else:
            if replay_equality or t3_doc_id_coverage:
                click.echo("")
            _print_collections_drift_text(report)
        if not report["pass"]:
            overall_pass = False

    # nexus-6dan: 3 new checks. Each is read-only against T3 + catalog.
    _printed_anything = (
        replay_equality or t3_doc_id_coverage or collections_drift
    )
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
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415  — command-local import (nexus.db.chroma_quotas)

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
            col = t3._client.get_collection(name=name)
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
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415  — command-local import (nexus.db.chroma_quotas)

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
            col = t3._client.get_collection(name=name)
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
            col = t3_db._client.get_collection(name=name)
            count = col.count()
        except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            count = 0
        if count > 0:
            t3_orphans.append({"name": name, "chunk_count": count})

    # Zombies: in catalog projection AND in T3 BUT 0 chunks in T3.
    projection = cat.list_collections()
    projection_names = {
        r["name"] for r in projection if not r.get("superseded_by")
    }
    zombies = []
    for name in sorted(projection_names & t3_names):
        try:
            col = t3_db._client.get_collection(name=name)
            count = col.count()
        except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
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
    )
    return {
        "pass": overall_pass,
        "t3_orphans": t3_orphans,
        "zombies": zombies,
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

    client = t3_db._client  # type: ignore[attr-defined]
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
        try:
            coll = client.get_collection(name)
            sample = coll.get(limit=1, include=["embeddings"])
        except Exception as exc:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            unknown_token.append(
                {"collection": name, "token": token, "error": str(exc)}
            )
            continue
        embs = sample.get("embeddings")
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
        "pass": not mismatches,
        "checked": checked,
        "mismatches": mismatches,
        "empty": empty,
        "skipped_non_conformant": skipped_non_conformant,
        "unknown_token": unknown_token,
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


def _check_bootstrap_status() -> dict:
    """Inspect the canonical-truth files at the configured catalog
    path and report whether the ES rebuild path would currently fall
    back to legacy (RDR-101 Phase 3 follow-up B, nexus-o6aa.9.7).

    Returns ``{"fallback_active": bool, "events_path": str,
    "documents_path": str}``. Used by the doctor verb to surface the
    silent split state where ``NEXUS_EVENT_SOURCED`` is on but reads
    come from legacy JSONL.

    Pure file inspection — does NOT construct a ``Catalog`` instance.
    Constructing one would trigger ``_ensure_consistent``, which
    re-projects events.jsonl into SQLite. That re-projection would
    silently overwrite any operator-injected drift the downstream
    doctor checks (e.g. ``--replay-equality``) are meant to detect.
    """
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.catalog import _read_event_sourced_gate  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog import events as _ev  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        return {"fallback_active": False, "reason": "catalog_not_initialized"}

    events_path = cat_path / "events.jsonl"
    documents_path = cat_path / "documents.jsonl"

    if not _read_event_sourced_gate():
        # Legacy mode: no ES rebuild path runs, no fallback state.
        return {
            "fallback_active": False,
            "events_path": str(events_path),
            "documents_path": str(documents_path),
        }
    if (
        not events_path.exists()
        or events_path.stat().st_size == 0
        or not documents_path.exists()
    ):
        # ``use_event_log`` is False at the size gate before the
        # guardrail check fires; not a fallback state.
        return {
            "fallback_active": False,
            "events_path": str(events_path),
            "documents_path": str(documents_path),
        }

    # Replicate the ``_event_log_covers_legacy`` math non-mutatively.
    try:
        registered: set[str] = set()
        tombstoned: set[str] = set()
        with documents_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tumbler = rec.get("tumbler")
                if not tumbler:
                    continue
                if rec.get("_deleted"):
                    tombstoned.add(tumbler)
                else:
                    registered.add(tumbler)
        legacy_doc_count = len(registered - tombstoned)
        if legacy_doc_count == 0:
            return {
                "fallback_active": False,
                "events_path": str(events_path),
                "documents_path": str(documents_path),
            }

        event_doc_count = 0
        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == _ev.TYPE_DOCUMENT_REGISTERED:
                    event_doc_count += 1
                elif t == _ev.TYPE_DOCUMENT_DELETED:
                    event_doc_count -= 1

        threshold = max(1, int(legacy_doc_count * 0.95))
        fallback_active = event_doc_count < threshold
    except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
        fallback_active = False

    return {
        "fallback_active": fallback_active,
        "events_path": str(events_path),
        "documents_path": str(documents_path),
    }


def _run_replay_equality() -> dict:
    """Drive the projector against the live catalog and diff.

    Source of truth depends on the catalog's write path:

    * **Event-sourced** (``events.jsonl`` exists and is non-empty): replay
      the native event log directly. This is the path that matters once
      ``NEXUS_EVENT_SOURCED=1`` is on by default, since the legacy JSONL
      becomes a back-compat shadow rather than canonical state and a
      synthesizer-driven check would silently miss any divergence in the
      native write path.
    * **Legacy** (no events.jsonl, or empty): synthesize v: 0 events
      from ``owners.jsonl``/``documents.jsonl``/``links.jsonl`` (the
      Phase 1 path).

    Steps in either mode:
      1. Resolve ``catalog_path()`` and require an initialized catalog.
      2. Open ``.catalog.db`` read-only (sqlite URI ``mode=ro``) for the
         live snapshot. Snapshot owners + documents + links rows.
      3. Build a fresh ``CatalogDB`` under a TemporaryDirectory; drive
         ``Projector.apply_all`` over the chosen event stream into it.
         Snapshot the same three tables.
      4. Diff the snapshots. Report counts and the first 5 mismatches per
         table. Pass = every table identical; fail = any difference.

    The live ``.catalog.db`` is opened read-only so an operator running
    this verb on a working host cannot accidentally corrupt the cached
    SQLite. JSONL / events.jsonl files are read but not written. The
    projected SQLite is ephemeral and discarded with the
    TemporaryDirectory.
    """
    import tempfile  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.catalog_db import CatalogDB  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.projector import Projector  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.synthesizer import synthesize_from_jsonl  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )

    live_db_path = cat_dir / ".catalog.db"
    if not live_db_path.exists():
        raise click.ClickException(
            f"Catalog SQLite missing at {live_db_path}; run 'nx catalog "
            "pull' to rebuild from JSONL."
        )

    # ── Pick the event source ──────────────────────────────────────────
    # Stat-only check; never construct ``EventLog`` here because its
    # constructor touch-creates ``events.jsonl`` if missing, which
    # would (a) shift the catalog dir's mtime mid-doctor-run and (b)
    # break the doctor's "read-only against live state" guarantee for a
    # legacy-mode catalog that has no events.jsonl yet.
    events_path = cat_dir / "events.jsonl"
    event_source: str
    if events_path.exists() and events_path.stat().st_size > 0:
        event_source = "events.jsonl"
    else:
        event_source = "synthesized"

    # Round-4 review (reviewer B EC-4): when ``NEXUS_EVENT_LOG_SHADOW=1``
    # but ``NEXUS_EVENT_SOURCED`` is unset, events.jsonl is being
    # shadow-emitted (subset of mutations, post-commit) — it is NOT
    # the canonical source of truth. A doctor replay against a
    # shadow-only log will report bogus divergence (legacy bootstrap
    # rows are absent from the log). Surface that explicitly so an
    # operator reading a FAIL report does not waste time hunting a
    # projector bug.
    shadow_only = (
        event_source == "events.jsonl"
        and os.environ.get("NEXUS_EVENT_LOG_SHADOW", "").strip().lower() in ("1", "true", "yes", "on")
        and os.environ.get("NEXUS_EVENT_SOURCED", "").strip().lower() not in ("1", "true", "yes", "on")
    )

    # ── Snapshot live ──────────────────────────────────────────────────
    # Links carry an autoincrement ``id`` PK that the projector restarts
    # at 1; the live db's ids depend on insertion history. RF-101-2 does
    # not claim the autoincrement is part of the projection contract, so
    # both snapshots exclude the ``id`` column by name (not by position
    # — a future schema migration that adds a column before ``id`` would
    # silently strip the wrong field under positional indexing).
    LINKS_EXCLUDE = ["id"]
    # nexus-vxz3: documents.chunk_count is a denormalised cache populated
    # by the post-store manifest-write batch hook (resync_chunk_count_cache),
    # not by event-log events. The in-memory replay catalog has no
    # document_chunks table to derive chunk_count from, so the projector's
    # post-replay re-derive sees zero manifest rows and keeps the
    # register-time chunk_count (typically 0). Live SQLite reflects the
    # hook-driven value (typically 1+ for docs with at least one chunk).
    # Exclude chunk_count from the comparison — it's intentionally
    # non-event-sourced and the boundary is documented at
    # mcp_infra.manifest_write_batch_hook.
    DOCUMENTS_EXCLUDE = ["chunk_count"]
    # RDR-120 P5.A.3 (nexus-nbsng): the live snapshot routes through the
    # T2 ``CatalogStore`` in read-only mode (``mode=ro`` URI) rather
    # than a direct ``sqlite3.connect`` so all catalog SQLite traffic
    # flows through the substrate-allowlisted path.
    from nexus.db.t2.catalog import CatalogStore as _CatalogStore  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    live_store = _CatalogStore(live_db_path, read_only=True)
    try:
        live_snap = {
            "owners": _snapshot_table(live_store, "owners"),
            "documents": _snapshot_table(
                live_store, "documents", exclude_cols=DOCUMENTS_EXCLUDE,
            ),
            "links": _snapshot_table(live_store, "links", exclude_cols=LINKS_EXCLUDE),
            # RDR-101 Phase 6 prophylactic-review fix: include the
            # collections projection in replay-equality. Pre-fix this
            # gate was blind to Phase 6's new projection state.
            "collections": _snapshot_table(live_store, "collections"),
        }
    finally:
        live_store.close()

    # ── Project + snapshot ────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        projected_path = Path(tmpdir) / "projected.db"
        proj_db = CatalogDB(projected_path)
        try:
            if event_source == "events.jsonl":
                # Local import: deferring to call-time avoids module-load
                # side effects in environments that never need this path.
                from nexus.catalog.event_log import EventLog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
                applied = Projector(proj_db).apply_all(
                    EventLog(cat_dir).replay()
                )
            else:
                applied = Projector(proj_db).apply_all(
                    synthesize_from_jsonl(cat_dir)
                )
            # Snapshot the projected DB through the same CatalogDB
            # instance — no second direct sqlite3 open required.
            projected_snap = {
                "owners": _snapshot_table(proj_db, "owners"),
                "documents": _snapshot_table(
                    proj_db, "documents", exclude_cols=DOCUMENTS_EXCLUDE,
                ),
                "links": _snapshot_table(proj_db, "links", exclude_cols=LINKS_EXCLUDE),
                "collections": _snapshot_table(proj_db, "collections"),
            }
        finally:
            proj_db.close()

    # ── Diff ──────────────────────────────────────────────────────────
    table_diffs: dict[str, dict] = {}
    overall_pass = True
    for table in ("owners", "documents", "links", "collections"):
        live_rows = live_snap[table]
        proj_rows = projected_snap[table]
        live_set = set(live_rows)
        proj_set = set(proj_rows)
        only_live = sorted(live_set - proj_set)
        only_proj = sorted(proj_set - live_set)
        equal = not only_live and not only_proj
        table_diffs[table] = {
            "live_count": len(live_rows),
            "projected_count": len(proj_rows),
            "only_in_live": [list(r) for r in only_live[:5]],
            "only_in_projected": [list(r) for r in only_proj[:5]],
            "equal": equal,
        }
        if not equal:
            overall_pass = False

    return {
        "pass": overall_pass,
        "events_applied": applied,
        "catalog_dir": str(cat_dir),
        "live_db": str(live_db_path),
        "event_source": event_source,
        "shadow_only": shadow_only,
        "tables": table_diffs,
    }


def _snapshot_table(
    conn, table: str, *, exclude_cols: list[str] | None = None,
) -> list[tuple]:
    """Snapshot one catalog table in deterministic row order.

    Sort by every column so the comparison is independent of insertion
    order. ``documents.metadata`` and ``links.metadata`` are JSON blobs
    that round-trip as strings, which sort byte-wise.

    ``exclude_cols`` removes named columns from both the SELECT and the
    ORDER BY. Used by the doctor's links snapshot to exclude the
    autoincrement ``id`` column without a fragile positional slice.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if not cols:
        return []
    if exclude_cols:
        exclude = set(exclude_cols)
        cols = [c for c in cols if c not in exclude]
        if not cols:
            return []
    sort_cols = ", ".join(cols)
    rows = conn.execute(
        f"SELECT {sort_cols} FROM {table} ORDER BY {sort_cols}"
    ).fetchall()
    return rows


def _print_replay_equality_text(report: dict) -> None:
    """Operator-friendly text rendering of the replay-equality report."""
    click.echo(f"Catalog: {report['catalog_dir']}")
    click.echo(f"Live db: {report['live_db']}")
    click.echo(f"Event source: {report.get('event_source', 'synthesized')}")
    if report.get("shadow_only"):
        click.echo(
            "WARNING: events.jsonl is shadow-emitted "
            "(NEXUS_EVENT_LOG_SHADOW=1, NEXUS_EVENT_SOURCED unset). "
            "Divergence below may reflect missing bootstrap events, "
            "not a projector bug. The synthesize-log remediation verb "
            "was retired post Phase 5b (nexus-iftc); run with "
            "NEXUS_EVENT_SOURCED=1 to populate the log naturally."
        )
    click.echo(f"Events applied: {report['events_applied']}")
    click.echo("")

    for table, diff in report["tables"].items():
        marker = "✓" if diff["equal"] else "✗"
        click.echo(
            f"  {marker} {table:<10}  live={diff['live_count']:>6}  "
            f"projected={diff['projected_count']:>6}"
        )
        if not diff["equal"]:
            if diff["only_in_live"]:
                click.echo(
                    f"    only in live ({len(diff['only_in_live'])} sample"
                    + ("s" if len(diff["only_in_live"]) != 1 else "")
                    + "):"
                )
                for row in diff["only_in_live"]:
                    click.echo(f"      {row!r}")
            if diff["only_in_projected"]:
                click.echo(
                    f"    only in projected "
                    f"({len(diff['only_in_projected'])} sample"
                    + ("s" if len(diff["only_in_projected"]) != 1 else "")
                    + "):"
                )
                for row in diff["only_in_projected"]:
                    click.echo(f"      {row!r}")

    click.echo("")
    if report["pass"]:
        click.echo("PASS — projector replay matches live SQLite for the current catalog state.")
    else:
        click.echo("FAIL — projector replay diverges from live SQLite. See diffs above.")


def _run_t3_doc_id_coverage(
    *, strict_not_in_t3: bool = False, progress: bool = False,
) -> dict:
    """Walk every T3 collection in events.jsonl and report doc_id coverage.

    Steps:
      1. Read events.jsonl. Build the expected doc_id per (coll_id, chunk_id)
         from ChunkIndexed events. Track orphans separately.
      2. For each collection, paginate col.get(limit=300, offset=...,
         include=["metadatas"]); compare each chunk's actual doc_id against
         the expected one.
      3. Report per-collection counts: total_chunks, with_doc_id,
         missing_doc_id, mismatched_doc_id, expected_orphans.
      4. PASS = every non-orphan event has a matching T3 chunk with the
         right doc_id, AND no T3 chunk lacks a doc_id outside the
         expected-orphan set.

    Read-only against T3 (col.get only, no col.update).
    """
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.event_log import EventLog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog import events as ev  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db import make_t3  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )
    log = EventLog(cat_dir)
    if not log.path.exists() or log.path.stat().st_size == 0:
        raise click.ClickException(
            f"events.jsonl is empty at {log.path}. The synthesize-log "
            "migration verb that historically populated it was retired "
            "post Phase 5b (nexus-iftc); restore by deleting the "
            "catalog directory and re-running 'nx catalog setup' to "
            "bootstrap from current T3 state."
        )

    # nexus-wszt: bypass-schema collections (taxonomy__*) carry their
    # own metadata vocabulary and intentionally have no doc_id (they
    # are BERTopic centroids / embedding anchors, not document chunks).
    # The doc_id-coverage audit must skip them or it reports 100%
    # orphan ratio on every centroid set (false positive class).
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    # Build expected (coll_id, chunk_id) → doc_id; track orphans.
    # RDR-102 D3: also track every coll_id that appears in events.jsonl
    # (whether non-orphan or orphan-only) so the orphan-ratio surface
    # can report on collections that don't appear in ``expected``.
    expected: dict[str, dict[str, str]] = {}
    expected_orphans: dict[str, set[str]] = {}
    all_event_collections: set[str] = set()
    for event in log.replay():
        if event.type != ev.TYPE_CHUNK_INDEXED:
            continue
        coll = event.payload.coll_id
        if coll.startswith(_BYPASS_SCHEMA_PREFIXES):
            continue
        cid = event.payload.chunk_id
        all_event_collections.add(coll)
        if event.payload.synthesized_orphan:
            expected_orphans.setdefault(coll, set()).add(cid)
            continue
        expected.setdefault(coll, {})[cid] = event.payload.doc_id

    try:
        t3 = make_t3()
    except Exception as exc:  # noqa: BLE001 — re-raises after cleanup/translation
        raise click.ClickException(
            f"Failed to open T3 client: {exc}. Check ChromaDB credentials."
        )

    # nexus-esrl (RDR-108 Phase 4 review D-M3): the audit reads
    # ``meta.get("doc_id", "")`` from chunk metadata to compare
    # against the event-log expected value. RDR-108 Phase 3
    # (nexus-bdag) removed doc_id from chunk metadata; the read
    # returns "" for every Phase-3 chunk. Without manifest
    # resolution the audit unconditionally reports near-100%
    # ``missing_doc_id``, masking real coverage problems.
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    cat = make_catalog_reader()

    # nexus-yrka: collections renamed via ``nx catalog rename-collection``
    # leave their old name in events.jsonl (events are append-only) but
    # the old T3 collection no longer exists. The catalog records the
    # rename via ``superseded_by``; skip those in T3 lookups instead of
    # reporting them as ``error: open: Collection X does not exist``
    # (which would flip overall_pass to false on every renamed coll).
    # nexus-xnz0o: use list_collections() (uniform API) — superseded_by is in every row.
    superseded_map: dict[str, str] = {}
    try:
        superseded_map = {
            r["name"]: r["superseded_by"]
            for r in cat.list_collections()
            if r.get("superseded_by")
        }
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        pass

    per_coll: dict[str, dict] = {}
    overall_pass = True
    skipped_superseded = 0
    coll_count = len(expected)
    import time as _time  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    for coll_idx, (coll_name, expected_chunks) in enumerate(
        expected.items(), start=1,
    ):
        if progress:
            click.echo(
                f"  [coverage {coll_idx}/{coll_count}] {coll_name}: "
                f"{len(expected_chunks)} expected chunks…",
                err=True,
            )
        if coll_name in superseded_map:
            per_coll[coll_name] = {
                "skipped": f"superseded_by={superseded_map[coll_name]}",
                "expected_chunks": len(expected_chunks),
            }
            skipped_superseded += 1
            continue
        _tc = _time.monotonic()
        try:
            col = t3._client.get_collection(name=coll_name)
        except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            per_coll[coll_name] = {
                "error": f"open: {exc}",
                "expected_chunks": len(expected_chunks),
            }
            overall_pass = False
            continue

        total = 0
        with_doc_id = 0
        mismatched: list[dict] = []
        missing: list[str] = []
        seen: set[str] = set()
        offset = 0
        while True:
            try:
                page = col.get(
                    limit=300, offset=offset, include=["metadatas"],
                )
            except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
                per_coll[coll_name] = {
                    "error": f"get: {exc}",
                    "expected_chunks": len(expected_chunks),
                }
                overall_pass = False
                break
            ids = page.get("ids") or []
            metas = page.get("metadatas") or []
            if not ids:
                break
            # nexus-esrl: resolve actual doc_id via the catalog
            # manifest for this page's chashes when chunk metadata
            # lacks doc_id (Phase-3 chunks). One batched lookup per
            # page; the per-chunk resolution below tries metadata
            # first, falls through to the manifest map.
            page_chashes = [
                (m or {}).get("chunk_text_hash", "") for m in metas
            ]
            page_chashes_nonempty = [c for c in page_chashes if c]
            chash_to_doc_for_page: dict[str, str] = {}
            if page_chashes_nonempty:
                try:
                    by_chash = cat.docs_for_chashes(page_chashes_nonempty)
                except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
                    by_chash = {}
                for c, doc_ids in by_chash.items():
                    if doc_ids:
                        chash_to_doc_for_page[c] = sorted(doc_ids)[0]
            for cid, meta in zip(ids, metas):
                meta = meta or {}
                total += 1
                seen.add(cid)
                actual = meta.get("doc_id", "") or ""
                # Manifest fallback when metadata lacks doc_id.
                if not actual:
                    chash = meta.get("chunk_text_hash", "")
                    if chash:
                        actual = chash_to_doc_for_page.get(chash, "")
                expected_doc_id = expected_chunks.get(cid, "")
                is_orphan = cid in expected_orphans.get(coll_name, set())
                if actual:
                    with_doc_id += 1
                    if expected_doc_id and actual != expected_doc_id:
                        mismatched.append({
                            "chunk_id": cid,
                            "actual": actual,
                            "expected": expected_doc_id,
                        })
                else:
                    if not is_orphan:
                        missing.append(cid)
            if len(ids) < 300:
                break
            offset += 300

        # Chunks the event log expected but T3 doesn't have. By default
        # this is a WARNING rather than a hard failure: the most common
        # cause is legitimate operational deletion of T3 chunks (re-
        # ingestion, pruning) without a corresponding event in the log,
        # which over time would make the doctor permanently red. Pass
        # ``--strict-not-in-t3`` to make it a hard failure (the contract
        # then becomes "the event log is the authoritative ledger and
        # T3 must match it exactly").
        not_in_t3 = sorted(set(expected_chunks) - seen)

        coverage = with_doc_id / total if total else 1.0
        # RDR-102 D3: per-collection orphan ratio = orphan_events /
        # (orphan_events + non_orphan_events). The denominator is the
        # event-log population for this collection, NOT total T3
        # chunks, because the surface is "what fraction of this
        # collection's catalog projection is orphan'd". A 0/0 case
        # (collection appears in no events at all) is unreachable
        # here — we're inside the `for coll_name in expected.items()`
        # loop so this branch always has at least one non-orphan event.
        n_orphans = len(expected_orphans.get(coll_name, set()))
        n_non_orphan = len(expected_chunks)
        orphan_ratio = (
            n_orphans / (n_orphans + n_non_orphan)
            if (n_orphans + n_non_orphan)
            else 0.0
        )
        pass_for_coll = (
            not mismatched
            and not missing
            and (not strict_not_in_t3 or not not_in_t3)
        )
        per_coll[coll_name] = {
            "total_chunks": total,
            "with_doc_id": with_doc_id,
            "expected_chunks": len(expected_chunks),
            "expected_orphans": n_orphans,
            "orphan_ratio": round(orphan_ratio, 4),
            "missing_doc_id_sample": missing[:5],
            "missing_doc_id_count": len(missing),
            "mismatched_doc_id_sample": mismatched[:5],
            "mismatched_doc_id_count": len(mismatched),
            "not_in_t3_sample": not_in_t3[:5],
            "not_in_t3_count": len(not_in_t3),
            "coverage": round(coverage, 4),
            "pass": pass_for_coll,
        }
        if not pass_for_coll:
            overall_pass = False
        if progress:
            elapsed = _time.monotonic() - _tc
            click.echo(
                f"  [coverage {coll_idx}/{coll_count}] {coll_name}: "
                f"{with_doc_id}/{total} covered "
                f"({coverage * 100:.1f}%) in {elapsed:.1f}s",
                err=True,
            )

    # RDR-102 D3: surface orphan-only collections (those that appear in
    # events.jsonl but have ZERO non-orphan ChunkIndexed events). They
    # would otherwise be invisible because the per-coll loop only
    # iterates ``expected`` (non-orphan-bearing collections). For the
    # operator dashboard, orphan-only collections should appear in
    # tables with orphan_ratio=1.0 and total_chunks=0 (no T3 inspection
    # since the verb's strict_not_in_t3 contract doesn't have non-
    # orphan events to anchor against).
    for coll_name in sorted(all_event_collections - set(expected)):
        n_orphans = len(expected_orphans.get(coll_name, set()))
        per_coll[coll_name] = {
            "total_chunks": 0,
            "with_doc_id": 0,
            "expected_chunks": 0,
            "expected_orphans": n_orphans,
            "orphan_ratio": 1.0,
            "missing_doc_id_sample": [],
            "missing_doc_id_count": 0,
            "mismatched_doc_id_sample": [],
            "mismatched_doc_id_count": 0,
            "not_in_t3_sample": [],
            "not_in_t3_count": 0,
            "coverage": 1.0,
            "pass": True,  # nothing to fail on — all events are orphan
        }

    # Global orphan ratio across every event in the log.
    total_orphan_events = sum(len(s) for s in expected_orphans.values())
    total_non_orphan_events = sum(len(d) for d in expected.values())
    total_events = total_orphan_events + total_non_orphan_events
    global_orphan_ratio = (
        total_orphan_events / total_events if total_events else 0.0
    )

    return {
        "pass": overall_pass,
        "events_path": str(log.path),
        "collections_in_log": len(expected),
        "collections_in_log_total": len(all_event_collections),
        "orphan_ratio": round(global_orphan_ratio, 4),
        "strict_not_in_t3": strict_not_in_t3,
        "skipped_superseded": skipped_superseded,
        "tables": per_coll,
    }


_ORPHAN_RATIO_WARN_THRESHOLD = 0.50


def _print_t3_doc_id_coverage_text(report: dict) -> None:
    click.echo("=== T3 doc_id coverage ===")
    click.echo(f"Events path:        {report['events_path']}")
    # RDR-102 D3: clarified header. The original "Collections in log: N"
    # was the count of collections with at least one non-orphan
    # ChunkIndexed event (the slice the PASS gate sees), not the count
    # of distinct coll_id values in events.jsonl. On the host catalog
    # the numbers diverge ~30x (23 vs 783), and operators reading the
    # output would silently believe most collections were covered.
    in_log_total = report.get(
        "collections_in_log_total", report["collections_in_log"],
    )
    click.echo(
        f"Collections with non-orphan ChunkIndexed events: "
        f"{report['collections_in_log']} "
        f"(total in events.jsonl: {in_log_total})"
    )
    skipped = report.get("skipped_superseded", 0)
    if skipped:
        click.echo(f"Skipped (superseded): {skipped}")
    click.echo("")
    for coll_name, diff in report["tables"].items():
        if "skipped" in diff:
            click.echo(
                f"  - {coll_name:<40}  SKIPPED: {diff['skipped']} "
                f"(expected {diff['expected_chunks']} chunks)"
            )
            continue
        if "error" in diff:
            click.echo(
                f"  ✗ {coll_name:<40}  ERROR: {diff['error']} "
                f"(expected {diff['expected_chunks']} chunks)"
            )
            continue
        marker = "✓" if diff["pass"] else "✗"
        click.echo(
            f"  {marker} {coll_name:<40}  "
            f"total={diff['total_chunks']:>6}  "
            f"with_doc_id={diff['with_doc_id']:>6}  "
            f"coverage={diff['coverage']:.2%}"
        )
        if diff["mismatched_doc_id_count"]:
            click.echo(
                f"     mismatched: {diff['mismatched_doc_id_count']} "
                f"(first {len(diff['mismatched_doc_id_sample'])} shown)"
            )
            for m in diff["mismatched_doc_id_sample"]:
                click.echo(
                    f"       {m['chunk_id']}: actual={m['actual']!r} "
                    f"expected={m['expected']!r}"
                )
        if diff["missing_doc_id_count"]:
            click.echo(
                f"     missing doc_id: {diff['missing_doc_id_count']} "
                f"(first {len(diff['missing_doc_id_sample'])} shown): "
                f"{diff['missing_doc_id_sample']}"
            )
        if diff["not_in_t3_count"]:
            click.echo(
                f"     in event log but not in T3: {diff['not_in_t3_count']} "
                f"(first {len(diff['not_in_t3_sample'])} shown): "
                f"{diff['not_in_t3_sample']}"
            )
    click.echo("")
    # RDR-102 D3: orphan-ratio surface. PASS gate stays unchanged
    # (per A4 — tightening would invalidate the host catalog's current
    # PASS); orphan ratio is a SOFT signal alongside the gate. Any
    # collection above the WARN threshold prints a WARN line; the
    # global ratio prints regardless so operators see the headline.
    click.echo("=== Orphan ratio ===")
    global_ratio = report.get("orphan_ratio", 0.0)
    click.echo(f"Global: {global_ratio:.2%}")
    warn_lines: list[str] = []
    for coll_name, diff in report["tables"].items():
        if "error" in diff:
            continue
        ratio = diff.get("orphan_ratio", 0.0)
        if ratio > _ORPHAN_RATIO_WARN_THRESHOLD:
            warn_lines.append(
                f"  WARN: {coll_name:<40}  orphan_ratio={ratio:.2%}  "
                f"(orphans={diff['expected_orphans']}, non-orphans="
                f"{diff['expected_chunks']})"
            )
    if warn_lines:
        for line in warn_lines:
            click.echo(line)
        click.echo(
            "  The synthesize-log and t3-backfill-doc-id remediation "
            "verbs were retired post Phase 5b (nexus-iftc). Re-index the "
            "affected collections to repopulate orphan chunks with "
            "current doc_id metadata; see docs/migration/"
            "rdr-101-phase4-orphan-recovery.md for historical context."
        )
    click.echo("")
    if report["pass"]:
        click.echo("PASS — every non-orphan chunk carries the expected doc_id.")
    else:
        click.echo("FAIL — T3 doc_id metadata diverges from the event log.")
        # Post-iftc (RDR-101 Phase 5b irreversibility): the migrate /
        # synthesize-log / t3-backfill-doc-id verbs are gone. A FAIL
        # today means the catalog holds pre-Phase-4 state; restore by
        # bootstrapping a fresh catalog from current T3.
        click.echo("")
        click.echo("Next step:")
        click.echo(
            "  Delete the catalog directory and re-run 'nx catalog setup' "
            "to bootstrap a fresh event log from current T3 state."
        )
        click.echo(
            "See docs/rdr/post-mortem/101-event-sourced-catalog-migration.md "
            "for the arc record (verbs retired post Phase 5b)."
        )


def register(group: click.Group) -> None:
    """Attach the diagnostics commands to the shared ``catalog`` group."""
    group.add_command(synthesize_log_cmd)
    group.add_command(doctor_cmd)
