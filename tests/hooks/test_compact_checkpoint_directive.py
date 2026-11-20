# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checkpoint's injected text tells a resumed session what to do first about open work.

The transcript census (T2 nexus/post-compaction-resumption-census-2026-11-19, "Follow-up cut") traced the
real post-compaction misses to one pattern: every board thread blocked, and the one piece of unblocked work
named only in the summary. A smaller one: a session waiting on a background task went silent without saying
what it waited on. Split from tests/hooks/test_compact_checkpoint.py, which is at its file-size ceiling.
"""
from __future__ import annotations

import importlib.util
import io
import os
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "compact_checkpoint.py"
SID = "11111111-2222-3333-4444-555555555555"

#: The three sentences, as the hook must say them. Pinned in full: a reword is a change to the directive.
ACT = ("act on any open item that has nothing to wait on (a board thread, or a summary item the board, git or "
       "beads still show open).")
WAIT = "If all you are doing is waiting on a running task, say what you are waiting on."
PACING = ("Never start work the user held back or asked you to wait on, work that needs the user's approval, or work "
          "another session owns.")
DIRECTIVE = f"After this compaction, once you have done what the user last asked: {ACT} {WAIT} {PACING}"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("compact_checkpoint_directive_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._load_siblings()
    return module


@pytest.fixture()
def checkpoints(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    directory = tmp_path / "config" / "compact-checkpoints"
    directory.mkdir(parents=True)
    return directory


def _write(directory: Path, working_state: str, *, age_s: float = 5.0) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(timespec="seconds")
    (directory / f"{SID}.md").write_text(
        f"# Compaction checkpoint\n\n- session: {SID}\n- trigger: auto\n- written: {stamp}\n\n"
        f"## Working state\n\n{working_state}\n",
        encoding="utf-8",
    )


def _inject(mod, monkeypatch, **payload) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SID, "source": "compact", **payload})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert mod.main(["inject"]) == 0
    return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


class TestOpenWorkDirective:
    def test_the_restored_checkpoint_carries_the_directive_once(self, mod, checkpoints, monkeypatch) -> None:
        _write(checkpoints, "- **branch:** `feature/x`\n\n### In-progress beads\n◐ nexus-aaaaa a bead\n")
        ctx = _inject(mod, monkeypatch)
        assert ctx.count(DIRECTIVE) == 1
        assert "feature/x" in ctx, "the checkpoint body is still injected"

    def test_the_directive_comes_before_the_checkpoint_body(self, mod, checkpoints, monkeypatch) -> None:
        """First read, and first kept when the injection is over its cap (a cut takes the tail)."""
        _write(checkpoints, "BODY-SENTINEL")
        ctx = _inject(mod, monkeypatch)
        assert ctx.index(DIRECTIVE) < ctx.index("BODY-SENTINEL")

    @pytest.mark.parametrize("age_s,extra", [(5.0, {}), (3600.0, {}), (3600.0, {"precomputed": True})])
    def test_every_age_framing_carries_it_once(self, mod, checkpoints, monkeypatch, age_s, extra) -> None:
        """The just-compacted, stale and idle-precomputed intros differ; the directive is in all of them."""
        _write(checkpoints, "BODY-SENTINEL", age_s=age_s)
        assert _inject(mod, monkeypatch, **extra).count(DIRECTIVE) == 1

    def test_the_user_pacing_clause_is_there_and_names_the_user(self, mod, checkpoints, monkeypatch) -> None:
        _write(checkpoints, "BODY-SENTINEL")
        ctx = _inject(mod, monkeypatch)
        assert PACING in ctx
        assert "held back" in ctx and "wait on" in ctx

    def test_the_waiting_clause_is_there(self, mod, checkpoints, monkeypatch) -> None:
        _write(checkpoints, "BODY-SENTINEL")
        assert WAIT in _inject(mod, monkeypatch)

    def test_a_no_checkpoint_compact_start_carries_it_once(self, mod, checkpoints, monkeypatch) -> None:
        """The empty-board path: with nothing restored the summary is all the session has, which is where the
        census found the unblocked work, so the directive applies most here."""
        ctx = _inject(mod, monkeypatch)
        assert "no working-state checkpoint" in ctx
        assert ctx.count(DIRECTIVE) == 1

    def test_a_stub_checkpoint_carries_it_once(self, mod, checkpoints, monkeypatch) -> None:
        (checkpoints / f"{SID}.md").write_text(
            f"# Compaction checkpoint\n\n- session: {SID}\n\n{mod._STUB_BODY}\n", encoding="utf-8"
        )
        ctx = _inject(mod, monkeypatch)
        assert "no working-state checkpoint" in ctx
        assert ctx.count(DIRECTIVE) == 1

    def test_a_checkpoint_with_no_usable_stamp_carries_it_once(self, mod, checkpoints, monkeypatch) -> None:
        (checkpoints / f"{SID}.md").write_text(
            f"# Compaction checkpoint\n\n- session: {SID}\n- trigger: auto\n\n## Working state\n\nx\n", encoding="utf-8"
        )
        ctx = _inject(mod, monkeypatch)
        assert "no working-state checkpoint" in ctx
        assert ctx.count(DIRECTIVE) == 1

    def test_a_checkpoint_an_earlier_compaction_already_injected_carries_it_once(self, mod, checkpoints, monkeypatch) -> None:
        _write(checkpoints, "x")
        first = _inject(mod, monkeypatch)
        assert first.count(DIRECTIVE) == 1
        sidecar = checkpoints / f"{SID}{mod._INJECTED_SUFFIX}"
        assert sidecar.is_file()
        old = sidecar.stat().st_mtime - 3600
        os.utime(sidecar, (old, old))                    # an EARLIER compaction's inject, not this one's
        ctx = _inject(mod, monkeypatch)
        assert "no working-state checkpoint" in ctx
        assert ctx.count(DIRECTIVE) == 1

    def test_a_resume_start_that_restores_a_checkpoint_carries_no_directive(self, mod, checkpoints, monkeypatch) -> None:
        """"After this compaction" is false on a resume, a fork or a startup: the directive is compact-only."""
        _write(checkpoints, "x")
        ctx = _inject(mod, monkeypatch, source="resume")
        assert DIRECTIVE not in ctx and "After this compaction" not in ctx

    def test_the_notice_still_tells_the_session_to_recover_its_state_before_acting(
        self, mod, checkpoints, monkeypatch
    ) -> None:
        ctx = _inject(mod, monkeypatch)
        assert ctx.index("Recover your state") < ctx.index(DIRECTIVE)

    def test_the_notice_stays_a_notice(self, mod, checkpoints, monkeypatch) -> None:
        """tests/hooks/test_compact_checkpoint.py bounds it under 1 KiB; the directive must fit there."""
        assert len(_inject(mod, monkeypatch).encode("utf-8")) < 1024

    def test_over_the_cap_the_injection_still_fits_and_keeps_the_directive_once(
        self, mod, checkpoints, monkeypatch
    ) -> None:
        _write(checkpoints, "é" * 20000)
        ctx = _inject(mod, monkeypatch)
        assert len(ctx.encode("utf-8")) <= mod._INJECT_CAP_BYTES
        assert "[truncated for size" in ctx and ctx.splitlines()[-1].startswith("Full checkpoint")
        assert ctx.count(DIRECTIVE) == 1
