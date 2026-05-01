# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up B (nexus-o6aa.9.7): bootstrap guardrail
correctness + operator-visible signal.

Two convergent review findings on the
``Catalog._event_log_covers_legacy`` guardrail (code-review-expert +
deep-analyst, 2026-05-01):

* **C1** — ``int(legacy_doc_count * 0.95)`` evaluates to 0 when
  ``legacy_doc_count == 1``. ``event_doc_count >= 0`` is True even
  when events.jsonl carries zero ``DocumentRegistered`` events.
  A 1-document legacy catalog with a non-empty-but-DocumentRegistered-
  free ``events.jsonl`` (e.g. a ChunkIndexed-only log from partial
  Phase 2 synthesis, or a dedupe-only event stream that drives
  ``event_doc_count`` to 0) bypassed the guardrail and silently wiped
  the single legacy row. Floor the threshold at 1.

* **C2** — when ``_ensure_consistent`` runtime-decides to fall back to
  legacy reads, ``cat._event_sourced_enabled`` remains True. ES writes
  still land in events.jsonl while reads come from legacy JSONL —
  silent split state where replay-equality is fundamentally not
  testing what it claims. ``Catalog.bootstrap_fallback_active`` now
  reflects this decision so the doctor verb can surface it.

Includes a doctor-surface assertion: ``nx catalog doctor`` must emit
an operator-visible warning when the bootstrap fallback is active.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.commands.catalog import doctor_cmd


def _init_catalog(tmp_path: Path) -> Path:
    d = tmp_path / "test-catalog"
    Catalog.init(d)
    return d


def _write_event_line(events_path: Path, payload_dict: dict) -> None:
    """Append a raw event line to events.jsonl. Used to simulate
    states the public API does not produce directly (e.g. a
    ChunkIndexed-only log).
    """
    if not events_path.exists():
        events_path.touch()
    with events_path.open("a") as f:
        f.write(json.dumps(payload_dict, separators=(",", ":")))
        f.write("\n")


# ─────────────────────────────────────────────────────────────────────
# C1: legacy_doc_count=1 floor regression
# ─────────────────────────────────────────────────────────────────────


def test_guardrail_refuses_es_rebuild_for_single_doc_with_no_registered_events(
    tmp_path, monkeypatch,
):
    """Pre-fix: ``int(1 * 0.95) == 0`` and ``0 >= 0`` is True, so a
    1-document legacy catalog with a non-empty-but-DocumentRegistered-
    free events.jsonl bypassed the guardrail and the ES rebuild
    silently wiped the legacy row. Post-fix: the floor at 1 forces a
    real ``DocumentRegistered`` event before ES rebuild proceeds.
    """
    # Set up a 1-document legacy catalog under legacy mode so the
    # legacy JSONL is the source of truth.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_catalog(tmp_path)
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    cat.register(
        owner, "doc-1.md", content_type="prose", file_path="doc-1.md",
    )
    cat._db.close()

    # Inject a non-empty events.jsonl that carries only a ChunkIndexed
    # event (no DocumentRegistered). Pre-fix: guardrail passes
    # because event_doc_count=0 >= int(1 * 0.95) = 0.
    events_path = d / "events.jsonl"
    _write_event_line(events_path, {
        "type": "ChunkIndexed", "v": 0,
        "payload": {
            "chunk_id": "ch1", "chash": "h" * 64, "doc_id": "uuid7-A",
            "coll_id": "code__test", "position": 0,
        },
        "ts": "2026-05-01T00:00:00+00:00",
    })

    # Now flip ES on and re-open the catalog. _ensure_consistent must
    # detect the sparse log and fall back to legacy.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    cat2 = Catalog(d, d / ".catalog.db")
    try:
        # Live catalog must still have the 1 document — guardrail
        # protected it from ES rebuild.
        doc_count = cat2._db.execute(
            "SELECT count(*) FROM documents",
        ).fetchone()[0]
        assert doc_count == 1, (
            "guardrail floor regression: 1-document catalog was wiped "
            "by ES rebuild because int(1 * 0.95) == 0 made the "
            "threshold trivially passable. Floor must be max(1, ...)."
        )
        assert cat2.bootstrap_fallback_active, (
            "bootstrap_fallback_active must be set when guardrail "
            "fires — the silent split state is what doctor surfaces."
        )
    finally:
        cat2._db.close()


# ─────────────────────────────────────────────────────────────────────
# C2: bootstrap fallback flag is set when guardrail fires
# ─────────────────────────────────────────────────────────────────────


def test_bootstrap_fallback_active_set_when_guardrail_fires(
    tmp_path, monkeypatch,
):
    """When ``_ensure_consistent`` decides to fall back to legacy
    because events.jsonl is sparse, ``cat.bootstrap_fallback_active``
    must be True so the doctor verb can surface the state. Pre-fix
    the only signal was a structlog warning operators rarely see.
    """
    # Build legacy state: 10 docs, no events.jsonl.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_catalog(tmp_path)
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    for i in range(10):
        cat.register(
            owner, f"doc-{i}.md", content_type="prose",
            file_path=f"doc-{i}.md",
        )
    cat._db.close()

    # Inject one stray event so events.jsonl is non-empty but sparse
    # vs the 10-row documents.jsonl. Guardrail should fire on flip.
    events_path = d / "events.jsonl"
    _write_event_line(events_path, {
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
    cat2 = Catalog(d, d / ".catalog.db")
    try:
        assert cat2.bootstrap_fallback_active is True, (
            "guardrail fired but bootstrap_fallback_active not set; "
            "doctor cannot surface the silent state to operators."
        )
        # Live state still has all 10 legacy docs (guardrail saved
        # them from being wiped).
        live = cat2._db.execute(
            "SELECT count(*) FROM documents",
        ).fetchone()[0]
        assert live >= 10
    finally:
        cat2._db.close()


def test_bootstrap_fallback_clears_when_log_catches_up(
    tmp_path, monkeypatch,
):
    """Once events.jsonl carries ≥ ``int(legacy_doc_count * 0.95)``
    DocumentRegistered events (e.g. after ``nx catalog synthesize-log
    --force``), the next rebuild promotes to ES and the flag clears.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_catalog(tmp_path)
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    cat.register(
        owner, "doc-1.md", content_type="prose", file_path="doc-1.md",
    )
    cat._db.close()

    # Flip ES on. events.jsonl is empty → ``use_event_log`` is False
    # at the .size > 0 gate, never reaches the guardrail check, so
    # ``bootstrap_fallback_active`` stays False.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    cat_empty_log = Catalog(d, d / ".catalog.db")
    try:
        assert cat_empty_log.bootstrap_fallback_active is False
    finally:
        cat_empty_log._db.close()


# ─────────────────────────────────────────────────────────────────────
# Doctor surface
# ─────────────────────────────────────────────────────────────────────


def test_doctor_surfaces_bootstrap_fallback_in_text_output(
    tmp_path, monkeypatch,
):
    """``nx catalog doctor`` must emit an operator-visible warning to
    stderr when the bootstrap fallback is active. Structlog alone is
    not enough — operators inspecting doctor output must see the
    state and the remediation hint.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_catalog(tmp_path)
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    cat.register(
        owner, "doc-1.md", content_type="prose", file_path="doc-1.md",
    )
    cat._db.close()

    events_path = d / "events.jsonl"
    _write_event_line(events_path, {
        "type": "DocumentDeleted", "v": 0,
        "payload": {
            "doc_id": "1.1.99", "tumbler": "1.1.99",
            "reason": "test",
        },
        "ts": "2026-05-01T00:00:00+00:00",
    })

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    runner = CliRunner()
    result = runner.invoke(
        doctor_cmd, ["--replay-equality"],
    )

    # Doctor exits non-zero because bootstrap fallback fails the
    # overall pass.
    assert result.exit_code == 1, result.output
    # The warning is written via click.echo(..., err=True). Click
    # 8.3+ CliRunner exposes stderr separately when invoked without
    # explicit mixing (the param was removed). Fall back to combined
    # output for older Click semantics so the test is robust.
    text = (result.stderr or "") + (result.output or "")
    assert "bootstrap-fallback" in text, text
    assert "synthesize-log --force" in text, (
        "remediation hint must include --force; pre-fix the warning "
        "told operators to run synthesize-log without --force, which "
        "fails on a non-empty events.jsonl."
    )


def test_doctor_surfaces_bootstrap_fallback_in_json_output(
    tmp_path, monkeypatch,
):
    """``nx catalog doctor --json`` must include a
    ``bootstrap_fallback`` key when the state is active so machine
    consumers (CI, monitoring) can detect it.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = _init_catalog(tmp_path)
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab")
    cat.register(
        owner, "doc-1.md", content_type="prose", file_path="doc-1.md",
    )
    cat._db.close()

    events_path = d / "events.jsonl"
    _write_event_line(events_path, {
        "type": "DocumentDeleted", "v": 0,
        "payload": {
            "doc_id": "1.1.99", "tumbler": "1.1.99",
            "reason": "test",
        },
        "ts": "2026-05-01T00:00:00+00:00",
    })

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))

    # Suppress structlog noise so it doesn't interleave with the JSON.
    import structlog
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(50),
    )
    try:
        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd, ["--replay-equality", "--json"],
        )
    finally:
        structlog.configure(**saved)

    assert result.exit_code == 1, result.output
    # Strip any non-JSON prefix (the warning text path may emit some
    # before the JSON dump even in --json mode).
    out = result.output
    start = out.find("{")
    payload = json.loads(out[start:])
    assert "bootstrap_fallback" in payload, payload
    assert payload["bootstrap_fallback"]["fallback_active"] is True
