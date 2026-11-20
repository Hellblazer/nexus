# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nexus.hooks.rdr_verb`` after SessionStart progressive disclosure (T2 nexus/design-sessionstart-progressive-disclosure-2026-11-20).

A healthy RDR tree is silent at session start: its count line is the ``rdr`` topic of ``nx brief``
(:func:`nexus.hooks.rdr_verb.brief_lines`), built from the same summary the hook builds its remedy lines from. What still prints
at session start is every line that asks for an action or says a check was cut short: the not-indexed verdict and remedy, the
unknown-index-state verdict, the late-T2 note, and the unchecked-fix pointers. The rest of the verb is tested in
``tests/hooks/test_rdr_verb.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.hooks import rdr_verb as rdr_hook_module

#: ``docs/rdr`` as the hook prints it: relative, with the platform separator.
_RDR_REL = os.path.join("docs", "rdr")


@pytest.fixture()
def rdr_hook_mod():
    return rdr_hook_module


@pytest.fixture(autouse=True)
def _reset_module_state():
    """``rdr_verb`` is one shared module, so its module-level run state must not leak between tests."""
    state = (rdr_hook_module._RESOLUTION_FAILURES, rdr_hook_module._T2_ROWS_CACHE, rdr_hook_module._T2_LATE, rdr_hook_module._T2_PENDING)
    for each in state:
        each.clear()
    yield
    for each in state:
        each.clear()


def _rdr_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "rdr").mkdir(parents=True)
    (root / "docs" / "rdr" / "rdr-001-x.md").write_text("---\nstatus: draft\n---\n# x\n", encoding="utf-8")
    return root


def _stub_tree(mod, monkeypatch, root, *, indexed, statuses=None) -> None:
    monkeypatch.setattr(mod, "_repo_root", lambda: root)
    monkeypatch.setattr(mod, "_repo_name", lambda r: "repo")
    monkeypatch.setattr(mod, "_resolve_rdr_collection", lambda r: "rdr__1-1__voyage-context-3__v1")
    monkeypatch.setattr(mod, "_collection_exists", lambda target: indexed)
    monkeypatch.setattr(mod, "_load_all_t2_statuses", lambda repo: statuses if statuses is not None else {})


def test_a_healthy_start_prints_nothing_and_a_late_t2_still_says_so(rdr_hook_mod, tmp_path, monkeypatch) -> None:
    """Indexed tree, T2 statuses late: the count line is gone from the hook but the late note (a fire-only notice
    that the fix pointers may be incomplete) still prints, unindented now that no count line precedes it."""
    mod = rdr_hook_mod
    root = _rdr_tree(tmp_path)
    _stub_tree(mod, monkeypatch, root, indexed=True)
    monkeypatch.setattr(mod, "_load_all_t2_statuses", lambda repo: (mod._T2_LATE.add(repo), {})[1])
    out = mod.run(None).stdout or ""
    assert out == mod._T2_LATE_NOTE, out


def test_not_indexed_still_prints_the_count_line_and_the_remedy_at_session_start(rdr_hook_mod, tmp_path, monkeypatch) -> None:
    mod = rdr_hook_mod
    root = _rdr_tree(tmp_path)
    _stub_tree(mod, monkeypatch, root, indexed=False)
    out = mod.run(None).stdout or ""
    assert out.startswith(f"RDR: 1 documents (1 RDRs) in {_RDR_REL} but NOT indexed."), out
    assert f"Run: nx index repo {root}" in out


def test_brief_lines_carry_the_count_and_the_remedy_but_not_the_fix_check(rdr_hook_mod, tmp_path, monkeypatch) -> None:
    mod = rdr_hook_mod
    root = _rdr_tree(tmp_path)
    _stub_tree(mod, monkeypatch, root, indexed=False, statuses={"1": "draft"})
    monkeypatch.setattr(mod, "_load_gated_commits", lambda repo: {"1": "abcdef0"})
    monkeypatch.setattr(mod, "_load_gated_blobs", lambda repo: {})
    monkeypatch.setattr(mod, "_unchecked_fix_edits", lambda *a, **k: pytest.fail("the brief does not run the fix check"))
    lines = mod.brief_lines()
    assert lines[0] == f"RDR: 1 documents (1 RDRs: 1 draft) in {_RDR_REL} but NOT indexed.", lines
    assert any("Run: nx index repo" in ln for ln in lines)


def test_brief_lines_are_empty_without_an_rdr_tree_and_leave_no_run_state_behind(rdr_hook_mod, tmp_path, monkeypatch) -> None:
    mod = rdr_hook_mod
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    assert mod.brief_lines() == []
    root = _rdr_tree(tmp_path / "other")
    _stub_tree(mod, monkeypatch, root, indexed=True)
    assert mod.brief_lines()
    assert mod._run_deadline is None and not mod._T2_PENDING


# ── the brief never reconfigures the caller's logging; the hook does ──────────────────────────────────────────


def test_brief_lines_never_configure_hook_logging_and_run_does(rdr_hook_mod, tmp_path, monkeypatch) -> None:
    """``nx brief rdr`` runs in the CLI and, many times, in the MCP server, whose logging sink hook-mode setup would replace.
    Pinned at the caller (``_summary(configure_logging=False)``), not only at the callee: reverting either call to the default
    turns this red."""
    mod = rdr_hook_mod
    root = _rdr_tree(tmp_path)
    _stub_tree(mod, monkeypatch, root, indexed=True)
    configured: list[str] = []
    monkeypatch.setattr(mod, "configure_hook_logging", lambda: configured.append("hook"))
    assert mod.brief_lines()
    assert configured == [], "brief_lines reconfigured the process's logging"
    mod.run(None)
    assert configured == ["hook"], "the hook verb must configure its own logging before it starts workers"


def test_brief_lines_survive_a_configure_hook_logging_that_raises(rdr_hook_mod, tmp_path, monkeypatch) -> None:
    mod = rdr_hook_mod
    root = _rdr_tree(tmp_path)
    _stub_tree(mod, monkeypatch, root, indexed=True)

    def boom() -> None:
        raise AssertionError("configure_hook_logging called from brief_lines")

    monkeypatch.setattr(mod, "configure_hook_logging", boom)
    assert mod.brief_lines()[0].startswith("RDR: 1 documents")
