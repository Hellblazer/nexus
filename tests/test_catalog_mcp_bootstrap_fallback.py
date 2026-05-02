# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up E.2 (nexus-o6aa.9.13): MCP smoke test
under the bootstrap-fallback state.

The bootstrap-fallback state is what catalogs enter when
``events.jsonl`` is non-empty but materially sparser than
``documents.jsonl`` — typically a partially-migrated catalog that
hasn't run ``nx catalog migrate`` yet. The contract under fallback:

* Reads fall back to legacy JSONL — tools still work.
* ``Catalog.bootstrap_fallback_active`` is True; ``doctor`` surfaces
  the state.
* The TTY-gated upgrade prompt
  (``nexus.commands._migration_prompt.maybe_emit_bootstrap_prompt``)
  is suppressed in non-TTY contexts (CI, cron, MCP, scripted runs).

The unit-level TTY suppression is covered by
``tests/test_migration_prompt.py::test_prompt_suppressed_when_not_tty``.
This file is the e2e MCP-side counterpart: it spins up the MCP tool
surface in-process, points it at a fallback-state catalog, invokes a
read-only tool, and asserts:

1. The tool returns a valid response (catalog reads work via fallback).
2. ``Catalog.bootstrap_fallback_active`` is True after construction.
3. No human-readable upgrade banner leaks to stderr from the MCP path
   — the prompt is fired only from ``cli.py`` (the ``nx`` entry point),
   not from ``mcp_server.py``, so the structural separation is the
   load-bearing invariant. This test pins it: if a future refactor
   adds a prompt call to the MCP server, this assertion catches it.

The structlog warning (``catalog_event_log_incomplete_falling_back_
to_legacy``) is implementation detail — the state-level check
(``bootstrap_fallback_active``) is the contract the doctor verb
keys on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.mcp_server import (
    _inject_catalog,
    _reset_singletons,
    catalog_list,
    catalog_search,
    catalog_show,
)


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@test.invalid")


@pytest.fixture(autouse=True)
def _clean_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


def _write_event_line(events_path: Path, payload_dict: dict) -> None:
    """Append a raw event line to events.jsonl. Mirrors the helper in
    tests/test_catalog_bootstrap_guardrail.py — used to simulate
    states the public API doesn't produce directly.
    """
    if not events_path.exists():
        events_path.touch()
    with events_path.open("a") as f:
        f.write(json.dumps(payload_dict, separators=(",", ":")))
        f.write("\n")


@pytest.fixture
def fallback_catalog(tmp_path, monkeypatch):
    """Build a catalog in bootstrap-fallback state and return it.

    Layout: 10 legacy ``DocumentRegistered`` rows in ``documents.jsonl``,
    one stray ``DocumentRegistered`` event in ``events.jsonl`` —
    sparse enough to trip the 95% guardrail.
    """
    # Build legacy state under NEXUS_EVENT_SOURCED=0 so writes go to
    # documents.jsonl, not events.jsonl.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = tmp_path / "test-catalog"
    Catalog.init(d)
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
    for i in range(10):
        cat.register(
            owner, f"doc-{i}.md", content_type="prose",
            file_path=f"doc-{i}.md",
        )
    cat._db.close()

    # Inject one stray DocumentRegistered event so events.jsonl is
    # non-empty but sparse. The guardrail's 95% threshold against the
    # 10-row legacy catalog will trip.
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

    # Re-open under NEXUS_EVENT_SOURCED=1: _ensure_consistent runs,
    # the guardrail trips, fallback is set.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    cat2 = Catalog(d, d / ".catalog.db")
    yield cat2
    cat2._db.close()


# ─────────────────────────────────────────────────────────────────────
# Smoke tests
# ─────────────────────────────────────────────────────────────────────


def test_fallback_state_set_after_construction(fallback_catalog):
    """Sanity guardrail: the fixture must actually produce the
    bootstrap-fallback state. Without this, the rest of the file is
    testing the happy path under a misleading name.
    """
    assert fallback_catalog.bootstrap_fallback_active is True, (
        "fixture failed to produce bootstrap-fallback state — guardrail "
        "did not trip. Either the legacy/event ratio changed or the "
        "guardrail threshold moved. Re-check _event_log_covers_legacy."
    )


def test_mcp_catalog_list_under_fallback_returns_legacy_rows(
    fallback_catalog, capsys,
):
    """``catalog_list`` MCP tool must return all 10 legacy documents
    via the fallback read path, not an error stub.
    """
    _inject_catalog(fallback_catalog)
    capsys.readouterr()  # drain prior output

    result = catalog_list()

    captured = capsys.readouterr()
    assert isinstance(result, list) and len(result) > 0, (
        f"catalog_list returned empty/invalid under fallback: {result!r}"
    )
    # MCP tools wrap their payload in a list of dicts; each dict
    # represents one document. We registered 10 legacy docs.
    rows = result[0] if isinstance(result[0], list) else result
    if isinstance(rows, dict) and "documents" in rows:
        rows = rows["documents"]
    # Lenient: at least the 10 legacy docs are visible.
    assert len(rows) >= 10, (
        f"expected ≥10 docs from fallback read; got {len(rows)}: {rows!r}"
    )
    # No error key in any row.
    for r in rows:
        assert "error" not in r, (
            f"catalog_list returned error under fallback: {r!r}"
        )

    # The MCP path must NOT emit the human-readable upgrade banner.
    # The banner is fired only from cli.py:main() — never from
    # mcp_server.py paths. This assertion is the e2e pin.
    assert "bootstrap-fallback active" not in captured.err, (
        f"upgrade prompt leaked from MCP path — stderr: {captured.err!r}"
    )
    assert "nx catalog migrate" not in captured.err, (
        f"migration verb hint leaked from MCP path — stderr: {captured.err!r}"
    )


def test_mcp_catalog_show_under_fallback_returns_doc(
    fallback_catalog, capsys,
):
    """``catalog_show`` against a known legacy tumbler must return
    the document's full metadata, not an error stub.
    """
    _inject_catalog(fallback_catalog)
    capsys.readouterr()

    # The first registered doc is at 1.1.1.
    result = catalog_show(tumbler="1.1.1")

    captured = capsys.readouterr()
    assert isinstance(result, dict), (
        f"catalog_show returned non-dict under fallback: {result!r}"
    )
    assert "error" not in result, (
        f"catalog_show(1.1.1) returned error under fallback: {result!r}"
    )
    assert result.get("title") == "doc-0.md", (
        f"catalog_show returned wrong/empty data: {result!r}"
    )
    assert "bootstrap-fallback active" not in captured.err


def test_mcp_catalog_search_under_fallback_does_not_crash(
    fallback_catalog, capsys,
):
    """``catalog_search`` must accept a query and return either results
    or an empty list — not crash. The 10 legacy docs were registered
    with predictable titles (``doc-N.md``) so a substring query has
    something to match against.
    """
    _inject_catalog(fallback_catalog)
    capsys.readouterr()

    result = catalog_search(query="doc")

    captured = capsys.readouterr()
    # Search results may be empty if the legacy index isn't populated;
    # the contract for this test is "returns without crashing AND no
    # error stub".
    payload = result[0] if isinstance(result, list) else result
    assert "error" not in payload, (
        f"catalog_search returned error under fallback: {payload!r}"
    )
    assert "bootstrap-fallback active" not in captured.err
