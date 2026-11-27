# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction checkpoint measures "not yet landed" against the project's ``integration_branch`` (nexus-sby49.11).

RDR-259 Decision 2 and its test scenarios: with ``integration_branch: develop`` the count is measured against
``origin/develop``, not ``origin/main``; a value ``origin`` does not have gives the default order and one ``hook.log`` note
per session, and prints nothing. The hook, the one runtime reader, is driven in-process on real repositories with a real
settings file; the unit of git work is tested in ``test_session_facts_default_branch.py``.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "compact_checkpoint.py"


@pytest.fixture()
def mod(plugin_account):
    spec = importlib.util.spec_from_file_location("compact_checkpoint_integration_branch_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._load_siblings()
    return module


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", name, cwd=repo)


def _project(tmp_path: Path, **settings: object) -> Path:
    """A clone whose ``origin/HEAD`` is ``main``, with ``origin/develop`` one commit ahead of it, and a feature branch cut from
    ``origin/develop`` holding one commit of its own, so it is 1 commit unlanded against develop and 2 against main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _commit(seed, "base")
    _git("push", "-q", str(origin), "HEAD:main", cwd=seed)
    _git("checkout", "-q", "-b", "develop", cwd=seed)
    _commit(seed, "develop-only")
    _git("push", "-q", str(origin), "HEAD:develop", cwd=seed)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git("checkout", "-q", "-b", "feature/work", "origin/develop", cwd=clone)
    _commit(clone, "mine")
    if settings:
        (clone / ".nexus").mkdir()
        (clone / ".nexus" / "project.json").write_text(json.dumps({"version": 1, **settings}), encoding="utf-8")
    return clone


def _lines(mod, clone: Path, session: str = "s1") -> list[str]:
    lines, unchecked = mod._worktree_lines("", str(clone), [], time.monotonic() + 30, {"cwd": str(clone), "session_id": session})
    assert unchecked == [], unchecked
    return lines


def test_a_project_that_sets_develop_is_measured_against_origin_develop(mod, tmp_path: Path) -> None:
    clone = _project(tmp_path, integration_branch="develop")
    [line] = _lines(mod, clone)
    assert "1 commit(s) not on the default branch" in line, line


def test_a_project_that_sets_nothing_is_measured_against_origin_head_not_a_guess_at_develop(mod, tmp_path: Path) -> None:
    clone = _project(tmp_path)
    [line] = _lines(mod, clone)
    assert "2 commit(s) not on the default branch" in line, line


def test_a_repository_with_no_origin_head_and_no_setting_is_unknown_not_origin_develop(mod, tmp_path: Path) -> None:
    clone = _project(tmp_path)
    _git("branch", "-D", "-r", "origin/main", cwd=clone)
    subprocess.run(["git", "remote", "set-head", "origin", "--delete"], cwd=clone, capture_output=True)
    lines, unchecked = mod._worktree_lines("", str(clone), [], time.monotonic() + 30, {"cwd": str(clone), "session_id": "s1"})
    assert lines == [] and len(unchecked) == 1 and "git could not read its work" in unchecked[0], (lines, unchecked)


def test_a_value_origin_does_not_have_falls_back_and_notes_once_per_session(mod, plugin_account, tmp_path: Path) -> None:
    home = plugin_account()
    logs = home / ".config" / "nexus" / "logs"
    logs.mkdir(parents=True)
    clone = _project(tmp_path, integration_branch="nope")
    for _ in range(2):
        [line] = _lines(mod, clone, session="s-note")
        assert "2 commit(s) not on the default branch" in line, "the default order answered"
    notes = [ln for ln in (logs / "hook.log").read_text(encoding="utf-8").splitlines() if "integration_branch" in ln]
    assert len(notes) == 1, notes
    assert 'origin has no branch "nope"' in notes[0] and "origin/HEAD, then main, then master" in notes[0]
    _lines(mod, clone, session="s-other")
    again = [ln for ln in (logs / "hook.log").read_text(encoding="utf-8").splitlines() if "integration_branch" in ln]
    assert len(again) == 2, "one note per session"


def test_the_note_is_not_written_where_the_log_directory_is_absent(mod, plugin_account, tmp_path: Path) -> None:
    home = plugin_account()
    clone = _project(tmp_path, integration_branch="nope")
    _lines(mod, clone)
    assert not (home / ".config").exists()


def test_a_helper_that_cannot_import_gives_the_default_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_account) -> None:
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "_audience", None)  # an import of it now raises ImportError
    spec = importlib.util.spec_from_file_location("compact_checkpoint_no_helper", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._load_siblings()
    assert module._integration_branch({"cwd": "/nowhere"}) is None
    assert module._integration_base({"cwd": "/nowhere"}) == (None, None)
    clone = _project(tmp_path, integration_branch="develop")
    [line] = _lines(module, clone)
    assert "2 commit(s) not on the default branch" in line, "no settings read: origin/HEAD answered"
