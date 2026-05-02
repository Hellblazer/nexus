# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up D (nexus-o6aa.9.9): nx catalog migrate verb.

Tests the one-shot migration verb that sequences synthesize-log + t3-
backfill-doc-id + doctor verification. The verb is idempotent and
pre-checks the bootstrap state to avoid regenerating a perfectly-good
event log.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.commands.catalog import migrate_cmd


def _init_legacy_catalog(tmp_path: Path) -> Path:
    """Bootstrap a pre-RDR-101-style catalog: documents.jsonl populated,
    events.jsonl empty.
    """
    d = tmp_path / "test-catalog"
    Catalog.init(d)
    return d


def _populate_legacy(cat_dir: Path, n_docs: int = 2) -> None:
    """Register *n_docs* under NEXUS_EVENT_SOURCED=0 so writes go to
    legacy JSONL only. Caller must set the env var BEFORE this call.
    """
    cat = Catalog(cat_dir, cat_dir / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    for i in range(n_docs):
        cat.register(
            owner, f"doc-{i}.md", content_type="prose",
            file_path=f"doc-{i}.md",
        )
    cat._db.close()


def _inject_event(events_path: Path, payload_dict: dict) -> None:
    """Append a raw event line to events.jsonl. Used to construct the
    sparse-state that triggers bootstrap_fallback_active.
    """
    if not events_path.exists():
        events_path.touch()
    with events_path.open("a") as f:
        f.write(json.dumps(payload_dict, separators=(",", ":")))
        f.write("\n")


# ─────────────────────────────────────────────────────────────────────
# Idempotency: nothing-to-do paths
# ─────────────────────────────────────────────────────────────────────


def test_migrate_no_op_on_empty_catalog(tmp_path, monkeypatch):
    """An empty catalog (no documents.jsonl) should report "nothing to
    do" and exit 0.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    d = tmp_path / "test-catalog"
    Catalog.init(d)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--no-chunks"])

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output.lower()
    assert "catalog is empty" in result.output.lower()


def test_migrate_no_op_when_already_migrated(tmp_path, monkeypatch):
    """A catalog whose events.jsonl already covers documents.jsonl
    should report "nothing to do" and not regenerate the event log.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    d = _init_legacy_catalog(tmp_path)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    # Populate under ES so events.jsonl is built alongside documents.jsonl
    # — net_registered ≥ legacy_doc_count, no fallback.
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    for i in range(3):
        cat.register(
            owner, f"doc-{i}.md", content_type="prose",
            file_path=f"doc-{i}.md",
        )
    cat._db.close()

    events_path = d / "events.jsonl"
    pre_size = events_path.stat().st_size

    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--no-chunks"])

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output.lower()
    # events.jsonl size unchanged — no regeneration.
    assert events_path.stat().st_size == pre_size


def test_migrate_dry_run_reports_plan(tmp_path, monkeypatch):
    """--dry-run on a catalog that needs migration prints the plan
    without writing anything.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_legacy_catalog(tmp_path)
    _populate_legacy(d, n_docs=3)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    # Inject a stray event to trigger fallback_active.
    _inject_event(d / "events.jsonl", {
        "type": "DocumentRegistered", "v": 0,
        "payload": {
            "doc_id": "1.1.99", "owner_id": "1.1",
            "content_type": "prose", "source_uri": "",
            "coll_id": "", "title": "stray.md", "tumbler": "1.1.99",
            "author": "", "year": 0, "file_path": "stray.md",
            "corpus": "", "physical_collection": "",
            "chunk_count": 0, "head_hash": "", "indexed_at": "",
            "alias_of": "", "meta": {}, "source_mtime": 0.0,
            "indexed_at_doc": "",
        },
        "ts": "2026-05-01T00:00:00+00:00",
    })

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--dry-run", "--no-chunks"])

    assert result.exit_code == 0, result.output
    assert "Migration plan" in result.output
    assert "synthesize-log --force" in result.output
    # --no-chunks: should NOT mention t3-backfill-doc-id in the plan.
    assert "t3-backfill-doc-id" not in result.output


def test_migrate_dry_run_json(tmp_path, monkeypatch):
    """--dry-run --json emits a structured plan."""
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_legacy_catalog(tmp_path)
    _populate_legacy(d, n_docs=2)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    _inject_event(d / "events.jsonl", {
        "type": "DocumentDeleted", "v": 0,
        "payload": {"doc_id": "1.1.99", "tumbler": "1.1.99", "reason": "test"},
        "ts": "2026-05-01T00:00:00+00:00",
    })

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    runner = CliRunner()
    result = runner.invoke(
        migrate_cmd, ["--dry-run", "--no-chunks", "--json"],
    )

    assert result.exit_code == 0, result.output
    out = result.output
    payload = json.loads(out[out.find("{"):])
    assert payload["needs_migration"] is True
    assert payload["fallback_active"] is True
    assert any("synthesize-log" in step for step in payload["would_run"])


# ─────────────────────────────────────────────────────────────────────
# Active migration paths
# ─────────────────────────────────────────────────────────────────────


def test_migrate_proactive_populates_empty_event_log(tmp_path, monkeypatch):
    """Freshly-upgraded catalog (legacy docs, empty events.jsonl).
    The bootstrap-fallback flag is False, but the verb still
    proactively populates events.jsonl so the log catches up before
    any ES write happens.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_legacy_catalog(tmp_path)
    _populate_legacy(d, n_docs=2)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    events_path = d / "events.jsonl"
    assert (
        not events_path.exists() or events_path.stat().st_size == 0
    ), "precondition: events.jsonl empty"

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--no-chunks"])

    assert result.exit_code == 0, result.output
    assert "Migration complete" in result.output
    # events.jsonl now populated.
    assert events_path.exists()
    assert events_path.stat().st_size > 0


def test_migrate_recovers_from_bootstrap_fallback(tmp_path, monkeypatch):
    """The textbook scenario: legacy docs + sparse events.jsonl =
    bootstrap_fallback_active. migrate runs synthesize-log --force,
    rebuilding the log to cover documents.jsonl. Doctor PASS post-
    migration.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_legacy_catalog(tmp_path)
    _populate_legacy(d, n_docs=4)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    # Sparse the event log: 1 stray DocumentDeleted vs 4 legacy docs.
    _inject_event(d / "events.jsonl", {
        "type": "DocumentDeleted", "v": 0,
        "payload": {"doc_id": "1.1.99", "tumbler": "1.1.99", "reason": "stray"},
        "ts": "2026-05-01T00:00:00+00:00",
    })

    # Confirm fallback fires.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    cat = Catalog(d, d / ".catalog.db")
    assert cat.bootstrap_fallback_active is True
    cat._db.close()

    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--no-chunks"])

    assert result.exit_code == 0, result.output
    assert "Migration complete" in result.output

    # Post-migration the fallback flag must clear on a fresh Catalog.
    cat2 = Catalog(d, d / ".catalog.db")
    assert cat2.bootstrap_fallback_active is False
    cat2._db.close()


# ─────────────────────────────────────────────────────────────────────
# Idempotency: re-run after migration
# ─────────────────────────────────────────────────────────────────────


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    """Running migrate twice in a row: the second run reports
    "nothing to do" because the first one converged the log to cover
    documents.jsonl.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_legacy_catalog(tmp_path)
    _populate_legacy(d, n_docs=3)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    runner = CliRunner()
    first = runner.invoke(migrate_cmd, ["--no-chunks"])
    assert first.exit_code == 0
    assert "Migration complete" in first.output

    second = runner.invoke(migrate_cmd, ["--no-chunks"])
    assert second.exit_code == 0
    assert "nothing to do" in second.output.lower()


# ─────────────────────────────────────────────────────────────────────
# Error paths
# ─────────────────────────────────────────────────────────────────────


def test_migrate_fails_loudly_when_catalog_uninitialized(tmp_path, monkeypatch):
    """Catalog not initialized: clean error, no partial state."""
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    d = tmp_path / "missing-catalog"  # never created
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--no-chunks"])

    assert result.exit_code != 0
    assert "not initialized" in result.output.lower()


# ─────────────────────────────────────────────────────────────────────
# nexus-o6aa.9.14: migrate forces ES rebuild before doctor verification
# so live SQLite reflects the post-synthesize-log state. Without the
# rebuild step, doctor's _run_replay_equality opens .catalog.db
# read-only and compares the legacy-rebuild SQLite to a fresh
# projection of events.jsonl — they diverge on any row whose JSONL
# shape differs from what the synthesizer emits.
# ─────────────────────────────────────────────────────────────────────


def test_migrate_syncs_live_sqlite_to_events_for_doctor_pass(
    tmp_path, monkeypatch,
):
    """The end-to-end UX assertion: a row whose legacy JSONL has
    ``"meta": null`` produces the literal SQLite string ``'null'`` on
    legacy rebuild but the synthesizer emits ``meta={}`` (default-
    factory coercion). Pre-fix: migrate runs synth-log + doctor;
    doctor compares un-rebuilt live (``'null'``) to projection
    (``'{}'``) and FAILs. Post-fix: migrate forces a Catalog
    construction between t3-backfill and doctor, triggering the ES
    rebuild that synchronizes live SQLite to events.jsonl. Doctor
    PASSes.
    """
    # Legacy populate.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_legacy_catalog(tmp_path)
    _populate_legacy(d, n_docs=3)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    # Inject a row with ``meta: null`` directly into documents.jsonl
    # to reproduce the shape drift Hal's real catalog has on 11 rows.
    docs_path = d / "documents.jsonl"
    with docs_path.open("a") as f:
        f.write(json.dumps({
            "tumbler": "1.1.99",
            "title": "drift.py",
            "author": "",
            "year": 0,
            "content_type": "code",
            "file_path": "drift.py",
            "corpus": "",
            "physical_collection": "code__drift",
            "chunk_count": 0,
            "head_hash": "",
            "indexed_at": "2026-04-08T16:55:55.193766+00:00",
            "meta": None,
            "source_mtime": 0.0,
            "alias_of": "",
            "source_uri": "file:///tmp/drift.py",
            "_deleted": False,
        }))
        f.write("\n")

    # Force a legacy rebuild so the null-meta row lands in SQLite.
    cat = Catalog(d, d / ".catalog.db")
    drift_meta = cat._db.execute(
        "SELECT metadata FROM documents WHERE tumbler = ?", ("1.1.99",),
    ).fetchone()
    cat._db.close()
    assert drift_meta is not None and drift_meta[0] == "null", (
        f"legacy rebuild should produce 'null' from meta:null JSONL; "
        f"got {drift_meta!r}"
    )

    # Now flip ES on and run migrate. Pre-fix this leaves live SQLite
    # at 'null' while events project to '{}', so doctor would FAIL.
    # Post-fix the migrate verb's explicit Catalog construction
    # triggers _ensure_consistent's ES rebuild → live becomes '{}'.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    runner = CliRunner()
    result = runner.invoke(migrate_cmd, ["--no-chunks"])

    assert result.exit_code == 0, (
        f"migrate did not converge to a clean PASS post-fix; output:\n"
        f"{result.output}"
    )
    assert "Migration complete" in result.output

    # Verify the live SQLite was rebuilt: drift.py's metadata is now
    # '{}' (events.jsonl shape), not 'null' (legacy shape).
    cat2 = Catalog(d, d / ".catalog.db")
    drift_meta_post = cat2._db.execute(
        "SELECT metadata FROM documents WHERE tumbler = ?", ("1.1.99",),
    ).fetchone()
    cat2._db.close()
    assert drift_meta_post[0] == "{}", (
        f"migrate did not sync live SQLite to events.jsonl; the null "
        f"row is still 'null' instead of '{{}}': got {drift_meta_post!r}"
    )
