# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart half of the audience split (nexus-sby49.3, plan section 7 Phase 2 change 3).

* I1a/I1c: the T1 block's heading and lifetime line are every user's; the retired-``share_t1`` parenthetical is gone.
* I1b: the dispatch-protocol sentence (nexus-mtheb, Sam's own working rule) goes only to ``maintainer()``.
* I2: the index line names ``beads`` and ``rdr`` only where the project has beads (a ``.beads/`` directory or ``bd``)
  or an RDR directory; with both it is the line this module always carried.
* The tripwire: a session that is a maintainer's only because the checkout carries the nexus marker gets one line.

Everything is in-process and stubs the live sections, so it reaches no engine and mints no tenant.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from nexus.hooks import board_due, identity_notes, session_context
from tests._audience_fixtures import broken_audience_import


@pytest.fixture()
def project(monkeypatch, tmp_path: Path) -> Path:
    """A bare project directory (no beads, no RDR directory, no ``bd`` on PATH) with every live section stubbed."""
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.chdir(proj)
    monkeypatch.setattr(board_due, "due_lines", lambda sid, **k: [])
    monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: [])
    monkeypatch.setattr(session_context, "_render_bd_prime", lambda project_dir: [])
    monkeypatch.setattr(session_context, "which", lambda cmd: False)
    return proj


def _out(payload: dict | None = None) -> str:
    return session_context.run(payload).stdout or ""


# --- I1a, I1b, I1c -----------------------------------------------------------------------------


def test_a_user_gets_the_heading_and_the_lifetime_line_and_no_dispatch_sentence(project: Path) -> None:
    out = _out({"cwd": str(project)})
    lines = out.split("\n")
    assert lines[0] == session_context._T1_HEADING
    assert lines[1] == session_context._T1_LIFETIME
    assert "Before a dispatch" not in out and "design-of-record" not in out


def test_the_retired_share_t1_note_is_gone_for_everyone(project: Path, src_maintainer_flag) -> None:
    assert "share_t1" not in session_context._T1_LIFETIME
    assert "share_t1" not in _out({"cwd": str(project)})


def test_the_account_flag_gets_the_dispatch_sentence(project: Path, src_maintainer_flag) -> None:
    out = _out({"cwd": str(project)})
    assert out.split("\n")[:3] == [
        session_context._T1_HEADING, session_context._T1_DISPATCH_PROTOCOL, session_context._T1_LIFETIME,
    ]


def test_a_marked_checkout_gets_the_dispatch_sentence_without_the_flag(project: Path, nexus_marked_repo: Path) -> None:
    assert session_context._T1_DISPATCH_PROTOCOL in _out({"cwd": str(nexus_marked_repo)})


def test_the_maintainers_block_is_the_old_block_less_the_parenthetical() -> None:
    old_lifetime = (
        "T1 survives compaction; a `/clear`, `/resume` or fresh restart starts a new scope, "
        "an idle session is swept after 24 h, and flagged entries land in T2 at session end "
        "and on a move to a new id. A `claude -p` child always has its own T1 "
        "(`share_t1=True` is retired and raises). Working notes you want after `/clear` go to `scratch` with "
        "`scope=identity` and tag `working`; session scope stays the layer your subagents share."
    )
    assert session_context._T1_LIFETIME == old_lifetime.replace(" (`share_t1=True` is retired and raises).", ".")
    assert session_context._T1_TOP_LINES[0] == session_context._T1_HEADING
    assert session_context._T1_TOP_LINES[-1] == ""


# --- I2: the index line -------------------------------------------------------------------------


def _topics(line: str) -> set[str]:
    return {t for t in session_context.BRIEF_TOPICS if f"{t} (" in line}


def test_a_project_with_no_beads_and_no_rdr_directory_gets_neither_topic(project: Path) -> None:
    line = session_context.brief_index_line(project)
    assert _topics(line) == {"memory", "caps", "map", "me"}
    assert "bd usage" not in line and "RDR" not in line
    assert line.startswith("More on demand (`nx brief <topic>` or the `brief` MCP tool): ")
    assert _out({"cwd": str(project)}).endswith(line)


def test_a_beads_directory_adds_the_beads_topic(project: Path) -> None:
    (project / ".beads").mkdir()
    assert _topics(session_context.brief_index_line(project)) == {"memory", "beads", "caps", "map", "me"}


def test_bd_on_path_adds_the_beads_topic(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
    assert "beads" in _topics(session_context.brief_index_line(project))


def test_an_rdr_directory_adds_the_rdr_topic(project: Path) -> None:
    (project / "docs" / "rdr").mkdir(parents=True)
    assert _topics(session_context.brief_index_line(project)) == {"memory", "caps", "map", "rdr", "me"}


def test_a_project_with_both_gets_the_line_this_module_always_carried(project: Path) -> None:
    (project / ".beads").mkdir()
    (project / "docs" / "rdr").mkdir(parents=True)
    assert session_context.brief_index_line(project) == session_context.BRIEF_INDEX_LINE


def test_a_gate_that_cannot_be_read_leaves_the_topics_in(project: Path, monkeypatch) -> None:
    """The data gates remove text, so on error they keep it (the line is then what it always was)."""

    def boom(*_a, **_k):
        raise OSError("unreadable")

    monkeypatch.setattr(session_context, "which", boom)
    monkeypatch.setattr("nexus.hooks.rdr_verb._rdr_dir", boom)
    assert session_context.brief_index_line(project) == session_context.BRIEF_INDEX_LINE


# --- the tripwire -------------------------------------------------------------------------------


def test_a_marked_checkout_on_an_account_with_no_flag_gets_one_tripwire_line(project: Path, nexus_marked_repo: Path) -> None:
    out = _out({"cwd": str(nexus_marked_repo)})
    assert out.count(session_context._TRIPWIRE_LINE) == 1
    assert out.index(session_context._T1_LIFETIME) < out.index(session_context._TRIPWIRE_LINE) < out.index("More on demand")


def test_a_flagged_account_in_a_marked_checkout_gets_no_tripwire(project: Path, nexus_marked_repo: Path, src_maintainer_flag) -> None:
    assert session_context._TRIPWIRE_LINE not in _out({"cwd": str(nexus_marked_repo)})


def test_a_user_repo_gets_no_tripwire(project: Path) -> None:
    assert session_context._TRIPWIRE_LINE not in _out({"cwd": str(project)})


def test_the_flag_alone_outside_nexus_gets_no_tripwire(project: Path, src_maintainer_flag) -> None:
    assert session_context._TRIPWIRE_LINE not in _out({"cwd": str(project)})


def test_the_tripwire_line_carries_no_internal_name() -> None:
    import re

    assert re.search(r"\bnexus-[a-z0-9]{4,}\b|RDR-\d+|\bSam\b|\bHal\b", session_context._TRIPWIRE_LINE) is None
    assert "\n" not in session_context._TRIPWIRE_LINE
    # a conditional line of its own, on top of the bounded SessionStart ceilings (tests/hooks/test_session_start_combined_budget.py)
    assert len(session_context._TRIPWIRE_LINE.encode("utf-8")) <= 260


# --- T8: a broken helper never thins the maintainer's output ---------------------------------------


def test_a_helper_that_cannot_import_gives_the_maintainer_block_to_everyone(tmp_path: Path) -> None:
    with broken_audience_import(session_context) as reloaded:
        assert reloaded._maintainer({"cwd": str(tmp_path)}) is True
        assert reloaded._t1_top_lines({"cwd": str(tmp_path)}) == reloaded._T1_TOP_LINES
        assert reloaded._account_flag() is False  # a loosening predicate answers False
