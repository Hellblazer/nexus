# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart half of the audience model (nexus-sby49.3, plan section 7 Phase 2 change 3; reworked in nexus-sby49.4).

* I1a/I1c: the T1 block's heading and lifetime line are every user's; the retired-``share_t1`` parenthetical is gone.
* I1b: the dispatch-protocol sentence is process: every session gets it, whoever the user is and whatever the project's settings.
* I2: the index line names ``beads`` and ``rdr`` only where the project has beads (a ``.beads/`` directory or ``bd``)
  or an RDR directory; with both it is the line this module always carried.
* There is no marker line and no account flag: nothing in this hook asks who the user is.

Everything is in-process and stubs the live sections, so it reaches no engine and mints no tenant.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from nexus.hooks import board_due, identity_notes, session_context


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


def test_every_session_gets_the_heading_the_dispatch_sentence_and_the_lifetime_line(project: Path) -> None:
    out = _out({"cwd": str(project)})
    assert out.split("\n")[:3] == [
        session_context._T1_HEADING, session_context._T1_DISPATCH_PROTOCOL, session_context._T1_LIFETIME,
    ]


def test_the_dispatch_sentence_does_not_depend_on_any_setting(project: Path, project_strict_repo: Path, src_user_strict) -> None:
    """A project that turns the strict guard on and a user who does get the same T1 block as the plain session."""
    plain = _out({"cwd": str(project)})
    assert session_context._T1_DISPATCH_PROTOCOL in plain
    assert session_context._T1_DISPATCH_PROTOCOL in _out({"cwd": str(project_strict_repo)})
    assert _out({"cwd": str(project)}) == plain


def test_the_retired_share_t1_note_is_gone_for_everyone(project: Path) -> None:
    assert "share_t1" not in session_context._T1_LIFETIME
    assert "share_t1" not in _out({"cwd": str(project)})


def test_the_block_is_the_old_block_less_the_parenthetical() -> None:
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
    assert session_context._T1_DISPATCH_PROTOCOL in session_context._T1_TOP_LINES


# --- I2: the index line -------------------------------------------------------------------------


def _topics(line: str) -> set[str]:
    return {t for t in session_context.BRIEF_TOPICS if f"{t} (" in line}


def test_a_project_with_no_beads_and_no_rdr_directory_gets_neither_topic(project: Path) -> None:
    line = session_context.brief_index_line(project)
    assert _topics(line) == {"memory", "caps", "map", "me"}
    assert "bd usage" not in line and "RDR" not in line
    assert line.startswith("More on demand (`nx brief <topic>` or the `brief` MCP tool): ")
    assert _out({"cwd": str(project)}).endswith(line)


def test_a_beads_directory_with_bd_on_path_adds_the_beads_topic(project: Path, monkeypatch) -> None:
    (project / ".beads").mkdir()
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
    assert _topics(session_context.brief_index_line(project)) == {"memory", "beads", "caps", "map", "me"}


def test_a_beads_directory_without_bd_does_not(project: Path) -> None:
    """Plan S2-1: beads are named where the project tracks work in them, a ``.beads/`` directory with ``bd`` on PATH. A
    directory with no tool to run is no tracker the session can use."""
    (project / ".beads").mkdir()
    assert "beads" not in _topics(session_context.brief_index_line(project))


def test_bd_on_path_without_a_beads_directory_does_not(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
    assert "beads" not in _topics(session_context.brief_index_line(project))


def test_an_rdr_directory_adds_the_rdr_topic(project: Path) -> None:
    (project / "docs" / "rdr").mkdir(parents=True)
    assert _topics(session_context.brief_index_line(project)) == {"memory", "caps", "map", "rdr", "me"}


def test_a_project_with_both_gets_the_line_this_module_always_carried(project: Path, monkeypatch) -> None:
    (project / ".beads").mkdir()
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
    (project / "docs" / "rdr").mkdir(parents=True)
    assert session_context.brief_index_line(project) == session_context.BRIEF_INDEX_LINE


def test_a_gate_that_cannot_be_read_leaves_the_topics_in(project: Path, monkeypatch) -> None:
    """The data gates remove text, so on error they keep it (the line is then what it always was)."""

    def boom(*_a, **_k):
        raise OSError("unreadable")

    (project / ".beads").mkdir()  # the directory is there, so the gate has to ask for bd
    monkeypatch.setattr(session_context, "which", boom)
    monkeypatch.setattr("nexus.hooks.rdr_verb._rdr_dir", boom)
    assert session_context.brief_index_line(project) == session_context.BRIEF_INDEX_LINE


# --- no marker line, no flag -----------------------------------------------------------------------


def test_no_session_gets_a_line_about_a_marker_or_a_maintainer_flag(project: Path, project_strict_repo: Path, src_user_strict) -> None:
    for cwd in (project, project_strict_repo):
        out = _out({"cwd": str(cwd)})
        assert "maintainer flag" not in out and "nexus marker" not in out and "maintainer-profile" not in out
    assert not hasattr(session_context, "_TRIPWIRE_LINE")
    assert not hasattr(session_context, "_account_flag") and not hasattr(session_context, "_maintainer")


def test_the_module_imports_no_audience_helper() -> None:
    import ast

    tree = ast.parse(Path(session_context.__file__).read_text(encoding="utf-8"))
    imported = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("_audience")]
    assert imported == [], "SessionStart reads no setting"
