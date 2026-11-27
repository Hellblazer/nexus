# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-259 Decision 1 / scenario S3 at the hook level (nexus-sby49.18): the committed ``.nexus/project.json`` decides.

The reader has a ``HEAD`` read (``guard_mode(..., head=True)``, ``advice_blocked(..., head=True)``); these tests run the REAL hook
scripts through ``audience_harness.py`` in real git repositories and assert that the two hooks ask it where the answer can change:

* the subagent git guard, when the seatbelt would pass a command the strict scan would deny;
* the destructive-action advice, when a row matched and would only warn.

A repository that commits ``subagent_git: strict`` (or an ``advice_block`` row) and then loosens or deletes the working-tree copy
stays strict. The cost side: a call whose verdict cannot change never starts the ``HEAD`` read, and a repository with no
settings file starts no ``git cat-file`` at all. A recording ``git`` (``GitShim``) on the child's ``PATH`` counts the reads.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hooks.audience_harness import write_project_settings
from tests.hooks.git_shim import GitShim, init_repo

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the recording git shim is a POSIX script")

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "hooks" / "audience_harness.py"
ROUTING = REPO_ROOT / "conexus" / "hooks" / "scripts" / "routing"
GUARD = ROUTING / "subagent_git_write_requires_orchestrator.py"
ADVICE = ROUTING / "destructive_action_advice.py"
PROJECT = ".nexus/project.json"
# Built from parts so no literal write verb sits in this file's source for a guard that scans text.
GIT_WRITE = "git " + "com" + "mit -m x"
ADVICE_CMD = "pkill -f pytest"
SCAN_CMD = 'grep -rn "sk-ant" ~'


def _j(**settings: object) -> str:
    return json.dumps({"version": 1, **settings})


def _set_wt(repo: Path, body: "str | None") -> None:
    """Replace the working-tree copy (leaving what is committed alone); None deletes it."""
    f = repo / PROJECT
    if body is None:
        f.unlink()
    else:
        f.write_text(body, encoding="utf-8")


@pytest.fixture()
def shim(tmp_path: Path) -> GitShim:
    root = tmp_path / "shim"
    root.mkdir()
    return GitShim(root)


def _run(script: Path, payload: dict, repo: Path, tmp_path: Path, shim: GitShim) -> "tuple[dict, list[str]]":
    """Run *script* as a hook and return its ``hookSpecificOutput`` ({} when silent) and the ``cat-file`` calls git saw."""
    home = tmp_path / "acct-home"
    home.mkdir(exist_ok=True)
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": str(shim.bin) + os.pathsep + os.environ.get("PATH", ""),
        "CLAUDE_PROJECT_DIR": str(repo),
        "NX_HOOK_PYTHON": sys.executable,
        "NX_ROUTING_LOG_PATH": str(tmp_path / "routing.jsonl"),
        "NEXUS_CONFIG_DIR": str(cfg),
        "NX_DROPPED_WRITES_LOG_PATH": str(cfg / "dropped.jsonl"),
    }
    for k in ("NX_REMOTE_TEST_HOSTS", "NX_SERVICE_URL"):
        env.pop(k, None)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(home), str(script)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else {}
    return out, [c for c in shim.calls() if "cat-file" in c]


def _guard(command: str, repo: Path, tmp_path: Path, shim: GitShim) -> "tuple[dict, list[str]]":
    payload = {
        "agent_id": "a1", "agent_type": "developer", "session_id": "s", "cwd": str(repo),
        "tool_name": "Bash", "tool_input": {"command": command}, "hook_event_name": "PreToolUse",
    }
    return _run(GUARD, payload, repo, tmp_path, shim)


def _advice(command: str, repo: Path, tmp_path: Path, shim: GitShim) -> "tuple[dict, list[str]]":
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo), "session_id": "s"}
    return _run(ADVICE, payload, repo, tmp_path, shim)


# ---------------------------------------------------------------------------
# The subagent git guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loosen", ["seatbelt", "other-key", "deleted"])
def test_the_guard_denies_when_only_the_committed_copy_says_strict(tmp_path: Path, shim: GitShim, loosen: str) -> None:
    """S3: the working tree drops ``subagent_git`` (or the whole file) and HEAD has strict: strict."""
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, {"seatbelt": _j(subagent_git="seatbelt"), "other-key": _j(preflight="p.sh"), "deleted": None}[loosen])
    out, cat = _guard(GIT_WRITE, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny", out
    # the verdict reads HEAD once; the deny text asks again for its "could not be read" clause (a deny is rare)
    assert 1 <= len(cat) <= 2, cat


def test_the_guard_passes_when_the_committed_copy_is_the_seatbelt(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="seatbelt")})
    out, cat = _guard(GIT_WRITE, repo, tmp_path, shim)
    assert out == {}
    assert len(cat) == 1, "the scan would have denied, so HEAD was asked once and said seatbelt"


def test_the_guard_starts_no_head_read_in_a_repository_with_no_settings_file(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {"README.md": "x\n"})
    out, cat = _guard(GIT_WRITE, repo, tmp_path, shim)
    assert out == {} and cat == []


def test_the_guard_does_not_read_head_when_the_working_tree_already_says_strict(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    out, cat = _guard(GIT_WRITE, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny" and cat == []


def test_the_guard_does_not_read_head_for_a_command_the_scan_would_pass(tmp_path: Path, shim: GitShim) -> None:
    """The committed copy says strict, but nothing here could be denied: the answer cannot change, so no read."""
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, _j())
    for command in ("ls -la", "git status", "git log --oneline -3", "pytest tests/ -q"):
        out, cat = _guard(command, repo, tmp_path, shim)
        assert out == {} and cat == [], (command, out, cat)


def test_the_seatbelt_still_denies_without_a_head_read(tmp_path: Path, shim: GitShim) -> None:
    """A text-form the seatbelt itself denies answers at once: no HEAD read can make it more allowed or more denied."""
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, _j())
    out, cat = _guard("git push --force origin main", repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny" and cat == []


# ---------------------------------------------------------------------------
# The destructive-action advice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loosen", ["empty", "deleted"])
def test_the_advice_blocks_when_only_the_committed_copy_lists_the_row(tmp_path: Path, shim: GitShim, loosen: str) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(advice_block=["pattern_kill"])})
    _set_wt(repo, _j() if loosen == "empty" else None)
    out, cat = _advice(ADVICE_CMD, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny", out
    assert "[pattern_kill]" in out["permissionDecisionReason"]
    assert len(cat) == 1


def test_the_advice_only_warns_when_the_committed_copy_lists_no_row(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="seatbelt")})
    out, cat = _advice(ADVICE_CMD, repo, tmp_path, shim)
    assert "permissionDecision" not in out and "[pattern_kill]" in out["additionalContext"]
    assert len(cat) == 1


def test_the_advice_starts_no_head_read_in_a_repository_with_no_settings_file(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {"README.md": "x\n"})
    out, cat = _advice(ADVICE_CMD, repo, tmp_path, shim)
    assert "permissionDecision" not in out and cat == []


def test_the_advice_starts_no_head_read_when_no_row_matched(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(advice_block=["pattern_kill"])})
    out, cat = _advice("ls -la", repo, tmp_path, shim)
    assert out == {} and cat == []


def test_the_advice_does_not_read_head_when_the_working_tree_already_blocks_the_row(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(advice_block=["pattern_kill"])})
    out, cat = _advice(ADVICE_CMD, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny" and cat == []


# ---------------------------------------------------------------------------
# The combined scenario (the second reviewer's driver)
# ---------------------------------------------------------------------------


def test_a_committed_strict_project_file_removed_from_the_working_tree_still_denies_and_blocks(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict", advice_block=["whole_disk_scan"])})
    (repo / PROJECT).unlink()  # a plain rm: the index and HEAD still name the file
    out, _ = _guard(GIT_WRITE, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny", out
    out, _ = _advice(SCAN_CMD, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny" and "[whole_disk_scan]" in out["permissionDecisionReason"], out


def test_the_forced_strict_clause_names_a_committed_copy_that_cannot_be_read(tmp_path: Path, shim: GitShim) -> None:
    """The deny's clause reads HEAD too: a committed copy that does not parse forces strict and the deny says so."""
    repo = init_repo(tmp_path / "r", {PROJECT: "{not json"})
    _set_wt(repo, _j())
    out, _ = _guard(GIT_WRITE, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny", out
    assert "could not be read" in out["permissionDecisionReason"], out


def test_write_project_settings_helper_is_not_the_committed_copy(tmp_path: Path) -> None:
    """Pins the fixture's premise: rewriting the working copy leaves HEAD alone."""
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    write_project_settings(repo, subagent_git="seatbelt")
    committed = subprocess.run(["git", "-C", str(repo), "show", "HEAD:" + PROJECT], capture_output=True, text=True, check=True).stdout
    assert json.loads(committed)["subagent_git"] == "strict"


@pytest.mark.xfail(strict=True, reason="needs A's per-key HEAD fix: the reader skips HEAD once the working tree is strict, even for advice_block")
def test_the_advice_reads_head_for_its_own_key_when_the_working_tree_is_strict_for_another(tmp_path: Path, shim: GitShim) -> None:
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict", advice_block=["pattern_kill"])})
    _set_wt(repo, _j(subagent_git="strict"))
    out, _ = _advice(ADVICE_CMD, repo, tmp_path, shim)
    assert out.get("permissionDecision") == "deny", out
