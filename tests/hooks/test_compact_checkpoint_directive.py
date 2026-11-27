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
    # the project: a repository with beads, so the full text (the beads words) is the default here
    (tmp_path / "project" / ".git").mkdir(parents=True)
    (tmp_path / "project" / ".beads").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    directory = tmp_path / "config" / "compact-checkpoints"
    directory.mkdir(parents=True)
    return directory


def _write(directory: Path, working_state: str, *, age_s: float = 5.0, board: bool = True) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(timespec="seconds")
    heading = "## Threads board entry (threads/leg)\n\nsession: x\n\n" if board else ""
    (directory / f"{SID}.md").write_text(
        f"# Compaction checkpoint\n\n- session: {SID}\n- trigger: auto\n- written: {stamp}\n\n{heading}"
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
        assert ctx.count(mod._resume_directive(False, True)) == 1

    def test_a_stub_checkpoint_carries_it_once(self, mod, checkpoints, monkeypatch) -> None:
        (checkpoints / f"{SID}.md").write_text(
            f"# Compaction checkpoint\n\n- session: {SID}\n\n{mod._STUB_BODY}\n", encoding="utf-8"
        )
        ctx = _inject(mod, monkeypatch)
        assert "no working-state checkpoint" in ctx
        assert ctx.count(mod._resume_directive(False, True)) == 1

    def test_a_checkpoint_with_no_usable_stamp_carries_it_once(self, mod, checkpoints, monkeypatch) -> None:
        (checkpoints / f"{SID}.md").write_text(
            f"# Compaction checkpoint\n\n- session: {SID}\n- trigger: auto\n\n## Working state\n\nx\n", encoding="utf-8"
        )
        ctx = _inject(mod, monkeypatch)
        assert "no working-state checkpoint" in ctx
        assert ctx.count(mod._resume_directive(False, True)) == 1

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
        assert ctx.count(mod._resume_directive(False, True)) == 1

    def test_a_resume_start_that_restores_a_checkpoint_carries_no_directive(self, mod, checkpoints, monkeypatch) -> None:
        """"After this compaction" is false on a resume, a fork or a startup: the directive is compact-only."""
        _write(checkpoints, "x")
        ctx = _inject(mod, monkeypatch, source="resume")
        assert DIRECTIVE not in ctx and "After this compaction" not in ctx

    def test_the_notice_still_tells_the_session_to_recover_its_state_before_acting(
        self, mod, checkpoints, monkeypatch
    ) -> None:
        ctx = _inject(mod, monkeypatch)
        assert ctx.index("Recover your state") < ctx.index(mod._resume_directive(False, True))

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


class TestAudienceGate:
    """nexus-sby49.4, S2-2: the board and beads words appear only where the project has them."""

    @pytest.mark.parametrize(("board", "beads", "items"), [
        (True, True, "a board thread, or a summary item the board, git or beads still show open"),
        (True, False, "a board thread, or a summary item the board or git still show open"),
        (False, True, "a summary item git or beads still show open"),
        (False, False, "a summary item git still shows open"),
    ])
    def test_the_directive_names_only_the_places_the_project_has(self, mod, board, beads, items) -> None:
        text = mod._resume_directive(board, beads)
        assert f"act on any open item that has nothing to wait on ({items})." in text
        assert PACING in text and WAIT in text
        assert (mod._resume_directive(True, True) == DIRECTIVE)

    @pytest.mark.parametrize(("board", "beads", "places", "state"), [
        (True, True, "git, beads and the board", "version control, beads and the threads board"),
        (True, False, "git and the board", "version control and the threads board"),
        (False, True, "git and beads", "version control and beads"),
        (False, False, "git", "version control"),
    ])
    def test_the_stale_check_and_the_state_line_follow(self, mod, board, beads, places, state) -> None:
        assert mod._checked_places(board, beads) == places
        assert mod._state_places(board, beads) == state

    def test_the_full_no_restore_notice_is_unchanged(self, mod) -> None:
        text = mod._no_restore_notice(SID)
        assert "read your threads board entry" in text and "git status, the branch and `bd list --status=in_progress`." in text
        assert DIRECTIVE in text

    def test_a_user_with_neither_gets_a_notice_with_no_board_and_no_bd(self, mod, checkpoints, monkeypatch, tmp_path) -> None:
        (tmp_path / "bare" / ".git").mkdir(parents=True)
        ctx = _inject(mod, monkeypatch, cwd=str(tmp_path / "bare"))
        assert "no working-state checkpoint" in ctx
        assert "check git status and the branch." in ctx
        for word in ("bd list", "board", "beads", "threads"):
            assert word not in ctx.lower(), word
        assert ctx.count(mod._resume_directive(False, False)) == 1

    def test_beads_are_named_where_the_project_has_a_beads_directory(self, mod, checkpoints, monkeypatch, tmp_path) -> None:
        ctx = _inject(mod, monkeypatch, cwd=str(tmp_path / "project"))
        assert "check git status, the branch and `bd list --status=in_progress`." in ctx
        assert "board" not in ctx.lower()

    def test_a_subdirectory_of_a_beads_project_counts(self, mod, tmp_path) -> None:
        (tmp_path / "p" / ".beads").mkdir(parents=True)
        (tmp_path / "p" / ".git").mkdir()
        (tmp_path / "p" / "a" / "b").mkdir(parents=True)
        assert mod._has_beads({"cwd": str(tmp_path / "p" / "a" / "b")}) is True
        assert mod._has_beads({"cwd": "relative/path"}) is False

    def test_a_nested_repository_does_not_inherit_its_parents_beads(self, mod, tmp_path) -> None:
        (tmp_path / "p" / ".beads").mkdir(parents=True)
        (tmp_path / "p" / ".git").mkdir()
        (tmp_path / "p" / "sub" / ".git").mkdir(parents=True)
        assert mod._has_beads({"cwd": str(tmp_path / "p" / "sub")}) is False

    def test_a_restored_checkpoint_with_a_board_entry_names_the_board(self, mod, checkpoints, monkeypatch) -> None:
        _write(checkpoints, "BODY-SENTINEL")
        ctx = _inject(mod, monkeypatch)
        assert "the threads board" in ctx and mod._resume_directive(True, True) in ctx

    def test_a_restored_checkpoint_with_no_board_entry_names_none(self, mod, checkpoints, monkeypatch, tmp_path) -> None:
        (tmp_path / "bare" / ".git").mkdir(parents=True)
        (checkpoints / f"{SID}.md").write_text(
            f"# Compaction checkpoint\n\n- session: {SID}\n- trigger: auto\n- written: "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
            "## Threads board entry (threads/x)\n\n[no board entry: nothing found]\n\n## Working state\n\nBODY-SENTINEL\n",
            encoding="utf-8")
        ctx = _inject(mod, monkeypatch, cwd=str(tmp_path / "bare"))
        assert "BODY-SENTINEL" in ctx
        intro = ctx.split("## Threads board entry")[0]
        assert "The state lives in version control, not in the summary" in intro
        assert "board" not in intro and "beads" not in intro
        assert mod._resume_directive(False, False) in ctx

    def test_no_one_gets_the_board_with_no_checkpoint_to_read(self, mod, checkpoints, monkeypatch, tmp_path) -> None:
        """Inject cannot ask the service, so with no checkpoint the board is not named: it is named only when one was found."""
        (tmp_path / "bare" / ".git").mkdir(parents=True)
        ctx = _inject(mod, monkeypatch, cwd=str(tmp_path / "bare"))
        assert "no working-state checkpoint" in ctx
        assert "board" not in ctx.lower() and "bd list" not in ctx

    def test_the_module_reads_no_audience_setting(self, mod) -> None:
        assert not hasattr(mod, "_maintainer") and not hasattr(mod, "_audience")

    def test_a_project_without_beads_loses_the_in_progress_beads_placeholder(self, mod, tmp_path) -> None:
        """``nx command-context continuation`` fills the subsection with ``(bd unavailable or no results)`` for a project that
        has no beads; the checkpoint drops it there and keeps every other subsection, and keeps it all where beads exist."""
        shaped = ("- **branch:** `x`\n\n### In-progress beads\n- (bd unavailable or no results)\n\n"
                  "### Uncommitted\n(clean)\n\n### Recent commits\nabc a commit\n")
        (tmp_path / "bare" / ".git").mkdir(parents=True)
        out = mod._without_beads_section(shaped, {"cwd": str(tmp_path / "bare")})
        assert "In-progress beads" not in out and "bd unavailable" not in out
        assert "### Uncommitted\n(clean)" in out and "### Recent commits\nabc a commit" in out and "- **branch:**" in out
        (tmp_path / "bare" / ".beads").mkdir()
        assert mod._without_beads_section(shaped, {"cwd": str(tmp_path / "bare")}) == shaped
        assert mod._without_beads_section("### In-progress beads\n- x\n", {"cwd": "relative"}) == ""
