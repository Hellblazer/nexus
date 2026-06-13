# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P1b.T (nexus-ue6g7.7) — read surfaces degrade LOUD while migrating.

RDR-159 §"Read-surface coverage (enumerated, locked for P2)". While the
``migration.state`` sentinel reports ``phase == migrating``, EACH of the five
enumerated read surfaces must prepend a LOUD banner ("knowledge migrating: N/M
collections done, results incomplete") and return whatever has landed — NEVER a
bare empty result. ``phase == migrated-failed`` keeps the banner and points at
the report + rollback. ``not-migrating`` (the default) leaves every surface
untouched.

The five surfaces (locked, cover EACH — not CLI-only):

1. ``mcp/core.py`` ``search``
2. ``mcp/core.py`` ``store_get``
3. ``mcp/core.py`` ``store_get_many``
4. ``mcp/core.py`` ``nx_answer`` (banner at the ENTRY POINT, not inside the
   plan runner — per RDR-159 + follow-up nexus-3g05n)
5. ``nx search`` CLI (``commands/search_cmd.py``)

Each surface is exercised down a CHEAP deterministic path (a mocked T3 or an
early validation return) so the test pins the banner-wrapping contract, not the
retrieval engine. Both result shapes are covered: str surfaces get the banner
text prepended; structured (dict) surfaces get a ``migration_warning`` key.

Isolation: ``NEXUS_CONFIG_DIR`` is redirected to ``tmp_path`` so the real
sentinel is never read or written.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.migration.banner import (
    degrade_loud_when_migrating,
    migration_banner,
    with_migration_banner,
)
from nexus.migration.state import (
    begin_migration,
    clear_state,
    mark_failed,
    record_progress,
)

_FIXED_STARTED_AT = "2026-06-13T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _set_migrating(done: int, total: int) -> None:
    begin_migration(collections_total=total, started_at=_FIXED_STARTED_AT)
    record_progress(collections_done=done, collections_total=total)


# --------------------------------------------------------------------------
# Banner helper (the shared mechanism the surfaces call)
# --------------------------------------------------------------------------


def test_banner_none_when_not_migrating() -> None:
    assert migration_banner() is None  # absent sentinel == not-migrating


def test_banner_text_while_migrating() -> None:
    _set_migrating(done=2, total=7)
    banner = migration_banner()
    assert banner is not None
    assert "knowledge migrating:" in banner
    assert "2/7" in banner
    assert "results incomplete" in banner


def test_banner_text_while_migrated_failed_points_at_rollback() -> None:
    begin_migration(collections_total=7, started_at=_FIXED_STARTED_AT)
    record_progress(collections_done=4, collections_total=7)
    mark_failed("3 collections unsupported")
    banner = migration_banner()
    assert banner is not None
    assert "FAILED" in banner
    assert "4/7" in banner
    assert "nx migrate-to-service" in banner  # re-run / rollback pointer
    assert "report" in banner.lower()


def test_banner_cleared_after_unlock() -> None:
    _set_migrating(done=7, total=7)
    assert migration_banner() is not None
    clear_state()  # UNLOCK
    assert migration_banner() is None


def test_with_migration_banner_prepends_to_str() -> None:
    _set_migrating(done=1, total=4)
    out = with_migration_banner("3 results found")
    assert isinstance(out, str)
    assert out.startswith(migration_banner())
    assert out.endswith("3 results found")


def test_with_migration_banner_adds_key_to_dict() -> None:
    _set_migrating(done=1, total=4)
    out = with_migration_banner({"ids": ["a", "b"], "distances": [0.1, 0.2]})
    assert isinstance(out, dict)
    assert out["migration_warning"] == migration_banner()
    assert out["ids"] == ["a", "b"]  # landed payload preserved, never emptied


def test_with_migration_banner_noop_when_not_migrating() -> None:
    assert with_migration_banner("untouched") == "untouched"
    payload = {"ids": ["a"]}
    assert with_migration_banner(payload) == payload
    assert "migration_warning" not in with_migration_banner(payload)


def test_decorator_wraps_sync_and_async() -> None:
    _set_migrating(done=2, total=5)

    @degrade_loud_when_migrating
    def sync_surface() -> str:
        return "sync-body"

    @degrade_loud_when_migrating
    async def async_surface() -> str:
        return "async-body"

    assert sync_surface().endswith("sync-body")
    assert "2/5" in sync_surface()
    awaited = asyncio.run(async_surface())
    assert awaited.endswith("async-body")
    assert "2/5" in awaited


# --------------------------------------------------------------------------
# Surface 1: mcp/core.py search
# --------------------------------------------------------------------------


def test_search_surface_degrades_loud() -> None:
    from nexus.mcp.core import search

    with patch("nexus.mcp.core._get_t3", return_value=MagicMock()), patch(
        "nexus.mcp.core._get_collection_names", return_value=[]
    ):
        # not-migrating: untouched (cheap "no collections" path).
        baseline = search("q", corpus="zzznope")
        assert isinstance(baseline, str)
        assert "knowledge migrating" not in baseline

        _set_migrating(done=2, total=6)
        migrating = search("q", corpus="zzznope")
        assert "knowledge migrating: 2/6" in migrating
        # The landed body (the real surface message) is still present.
        assert "No collections match corpus" in migrating


# --------------------------------------------------------------------------
# Surface 2: mcp/core.py store_get
# --------------------------------------------------------------------------


def test_store_get_surface_degrades_loud() -> None:
    from nexus.mcp.core import store_get

    # Cheap early-return path (empty doc_id) — no T3 needed.
    assert "knowledge migrating" not in store_get("")
    _set_migrating(done=3, total=3)
    out = store_get("")
    assert "knowledge migrating: 3/3" in out
    assert "doc_id is required" in out


# --------------------------------------------------------------------------
# Surface 3: mcp/core.py store_get_many (structured dict shape)
# --------------------------------------------------------------------------


def test_store_get_many_surface_degrades_loud_structured() -> None:
    from nexus.mcp.core import store_get_many

    mock_t3 = MagicMock()
    mock_t3.get_by_id = lambda col, doc_id: {"content": "landed body"}

    with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
        _set_migrating(done=1, total=2)
        out = store_get_many(ids=["d1"], collections="knowledge", structured=True)
    assert isinstance(out, dict)
    assert out["migration_warning"].startswith("⚠")
    assert "1/2" in out["migration_warning"]
    # Landed content preserved — never a bare-empty result.
    assert len(out["contents"]) == 1


# --------------------------------------------------------------------------
# Surface 4: mcp/core.py nx_answer (entry point, async)
# --------------------------------------------------------------------------


def test_nx_answer_surface_degrades_loud() -> None:
    from nexus.mcp.core import nx_answer

    # Cheap deterministic path: an out-of-range min_confidence returns the
    # validation string at the entry point — NO claude -p subprocess.
    baseline = asyncio.run(nx_answer("q", min_confidence=5.0))
    assert "knowledge migrating" not in baseline

    _set_migrating(done=4, total=9)
    migrating = asyncio.run(nx_answer("q", min_confidence=5.0))
    assert "knowledge migrating: 4/9" in migrating
    assert "min_confidence must be in" in migrating  # landed body preserved


def test_nx_answer_structured_degrades_loud_top_level_key() -> None:
    # Machine-consumer path: nx_answer(structured=True) returns a dict, and the
    # outer decorator must attach migration_warning as a TOP-LEVEL key (not bury
    # it in final_text). Driven down the same cheap early-return path, which in
    # structured mode returns the _result() dict.
    from nexus.mcp.core import nx_answer

    _set_migrating(done=4, total=9)
    out = asyncio.run(nx_answer("q", min_confidence=5.0, structured=True))
    assert isinstance(out, dict)
    assert "knowledge migrating: 4/9" in out["migration_warning"]
    assert "final_text" in out  # landed structured payload preserved


# --------------------------------------------------------------------------
# Surface 5: nx search CLI
# --------------------------------------------------------------------------


def test_nx_search_cli_degrades_loud(_isolate_config_dir: Path) -> None:
    from nexus.commands.search_cmd import search_cmd

    runner = CliRunner()

    # The banner is emitted to stderr (so it never corrupts --json stdout).
    # not-migrating: no banner on the CLI surface.
    baseline = runner.invoke(search_cmd, ["zzznope"], catch_exceptions=True)
    assert "knowledge migrating" not in baseline.stderr

    _set_migrating(done=5, total=8)
    migrating = runner.invoke(search_cmd, ["zzznope"], catch_exceptions=True)
    assert "knowledge migrating: 5/8" in migrating.stderr
