# SPDX-License-Identifier: AGPL-3.0-or-later
"""A repository with no settings file runs no git process for settings, on any reader path (nexus-sby49.8, the Stage 1 ship-when).

A recording ``git`` is first on ``PATH``; each path of the reader runs in the plain posture (and the user-strict posture, which
has an account file but no project file) and the shim must have recorded nothing. A positive control (a project file that the
read must go and check at ``HEAD``) proves the shim would have recorded a call, so a green run cannot be a probe that went blind.
"""
from __future__ import annotations

import io
import json
import os
import contextlib

import pytest

from tests._audience_fixtures import build_posture
from tests.hooks.git_shim import GitShim, init_repo

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the recording git shim is a POSIX script")


def _every_path(audience, payload: dict) -> list[str]:
    """Run every reader path with the strictest ask (HEAD wanted, the full report); return the names of the paths run."""
    ran = []
    for name, call in {
        "read_settings(head, full)": lambda: audience.read_settings(payload, head=True, full=True),
        "read_settings()": lambda: audience.read_settings(payload),
        "guard_mode(head)": lambda: audience.guard_mode(payload, head=True),
        "guard_mode()": lambda: audience.guard_mode(payload),
        "advice_blocked(head)": lambda: audience.advice_blocked(payload, head=True),
        "advice_blocked()": lambda: audience.advice_blocked(payload),
        "project_settings": lambda: audience.project_settings(payload),
        "user_settings": lambda: audience.user_settings(),
        "project_text": lambda: audience.project_text(payload, "ci_wait"),
        "project_brief_questions": lambda: audience.project_brief_questions(payload),
        "user_brief_questions": lambda: audience.user_brief_questions(payload),
        "integration_branch": lambda: audience.integration_branch(payload),
        "telemetry_enabled": lambda: audience.telemetry_enabled(),
        "t1_nudge_enabled": lambda: audience.t1_nudge_enabled(),
        "forced_strict_clause(head)": lambda: audience.forced_strict_clause(payload, head=True),
        "settings_problems": lambda: audience.settings_problems(payload),
        "remote_test_hosts": lambda: audience.remote_test_hosts(),
        "build_report": lambda: audience.build_report(payload["cwd"], versions={"plugin": "x", "nx": "x"}),
        "show": lambda: _run_show(audience, payload["cwd"], ["show", "--cwd", payload["cwd"]]),
        "show --json": lambda: json.loads(_run_show(audience, payload["cwd"], ["show", "--cwd", payload["cwd"], "--json"])),
    }.items():
        call()
        ran.append(name)
    return ran


def _run_show(audience, cwd: str, argv: list[str]) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = audience.main(argv, versions={"plugin": "x", "nx": "x"})
    assert rc == 0
    return out.getvalue()


@pytest.mark.parametrize("posture", ["plain", "user-strict"])
def test_a_repository_with_no_project_file_runs_no_git_on_any_path(audience_copy, tmp_path, monkeypatch, posture):
    p = build_posture(tmp_path, posture)
    monkeypatch.setattr(audience_copy, "_ACCOUNT_HOME_OVERRIDE", str(p.home))
    shim = GitShim(tmp_path).install(monkeypatch)
    ran = _every_path(audience_copy, p.payload)
    assert len(ran) == 20, ran
    assert shim.calls() == [], "git ran for settings in a repository with no settings file: " + repr(shim.calls())


def test_a_checkout_with_a_legacy_marker_and_no_project_file_runs_no_git(audience_copy, tmp_path, monkeypatch):
    """The marker reads strict from the working tree; the committed copy cannot add to a strict answer."""
    repo = init_repo(tmp_path / "r", {"scripts/agent-worktree-preflight.sh": "#!/bin/sh\n"})
    shim = GitShim(tmp_path).install(monkeypatch)
    assert audience_copy.guard_mode({"cwd": str(repo)}, head=True) == "strict"
    assert shim.calls() == []


def test_a_directory_that_is_not_a_repository_runs_no_git(audience_copy, tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    shim = GitShim(tmp_path).install(monkeypatch)
    _every_path(audience_copy, {"cwd": str(bare)})
    assert shim.calls() == []


def test_the_shim_would_have_recorded_a_read_that_needed_git(audience_copy, tmp_path, monkeypatch):
    """Positive control: a project file that does not say strict makes the guard path ask HEAD, and the shim sees it."""
    repo = init_repo(tmp_path / "r", {".nexus/project.json": json.dumps({"version": 1})})
    shim = GitShim(tmp_path).install(monkeypatch)
    audience_copy.guard_mode({"cwd": str(repo)}, head=True)
    assert shim.verbs() == ["ls-tree", "cat-file"], shim.calls()
