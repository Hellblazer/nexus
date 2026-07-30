# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E tests for the upgrade mechanism (RDR-076, Phase 6) — surviving SCs.

RDR-158 P4 Stage 4 (nexus-i711w): SC-1/2/3/7/9 (version table, version-gated
execution, the local dry-run/--force flags, the Migration registry shape, and
existing-install bootstrapping) were subjects of the DELETED
``nexus/db/migrations.py`` chain and its ``nx upgrade`` local leg — they die
with the machinery. What survives here:

  SC-5  MCP version divergence warning never crashes on a missing DB
  SC-6  T2 facade domain stores usable end-to-end (engine substrate)
  SC-8  hooks.json SessionStart ordering + PreToolUse timeout bounds
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


# ── SC-5: MCP version divergence warning ────────────────────────────────────


class TestSC5McpVersionCheck:
    def test_no_crash_on_missing_db(self) -> None:
        from nexus.mcp_infra import check_version_compatibility

        with patch(
            "nexus.mcp_infra.default_db_path",
            return_value=Path("/nonexistent.db"),
        ):
            check_version_compatibility()


# ── SC-6: Domain stores usable post-upgrade ──────────────────────────────────


class TestSC6Delegation:
    """RDR-076 SC-6 originally asserted the SQLite MemoryStore / PlanLibrary
    delegated schema work to migrations.py's module-level functions. Those
    stores are deleted (nexus-i711w Stage 2 sub-stage A3); the surviving
    MEANING of SC-6 is that the T2 facade's domain stores are usable
    end-to-end after construction — now against the engine substrate via
    the Http* stores T2Database builds unconditionally."""

    def test_memory_store_round_trip(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            db.memory.put("test", "title1", "content")
            result = db.memory.get("test", "title1")
            assert result is not None
        finally:
            db.close()

    def test_plan_library_round_trip(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            row_id = db.plans.save_plan("test query", '{"plan": "test"}')
            assert isinstance(row_id, int) and row_id > 0
        finally:
            db.close()


# ── SC-8: hooks.json SessionStart ───────────────────────────────────────────


class TestSC8HooksJson:
    def test_upgrade_auto_first(self) -> None:
        hooks_path = Path(__file__).parent.parent / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        startup_hooks = next(
            h["hooks"]
            for h in data["hooks"]["SessionStart"]
            if "startup" in h["matcher"]
        )
        assert startup_hooks[0]["command"].startswith("nx upgrade --auto")
        assert startup_hooks[0]["timeout"] == 30

    def test_pretooluse_bash_timeout_is_short(self) -> None:
        """PreToolUse Bash timeout must stay short.

        ``pre_close_verification_hook.sh`` is advisory (read stdin, JSON
        out, exit 0); the body completes in <100 ms. A long timeout is a
        footgun: a future bug or filesystem stall would block every
        ``Bash`` tool call by that ceiling. Pinning low so any drift
        toward "minutes" trips this test instead of the user.

        Earlier shape used ``timeout: 300`` which would have masked a
        five-minute stall in the hook with no operator visibility. The
        bound here matches the SessionStart fast-path hooks (5 s).
        """
        hooks_path = Path(__file__).parent.parent / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        bash_blocks = [
            h for h in data["hooks"].get("PreToolUse", [])
            if h.get("matcher") == "Bash"
        ]
        assert bash_blocks, "PreToolUse Bash matcher missing from hooks.json"
        for block in bash_blocks:
            for hook in block.get("hooks", []):
                timeout = hook.get("timeout")
                assert isinstance(timeout, int), (
                    f"PreToolUse Bash hook missing/invalid timeout: {hook!r}"
                )
                assert timeout <= 10, (
                    f"PreToolUse Bash timeout {timeout}s is too high. "
                    f"This hook is advisory and should never need >5 s. "
                    f"A long ceiling masks real stalls — keep it tight "
                    f"(<=10 s)."
                )
