# SPDX-License-Identifier: AGPL-3.0-or-later
"""The subagent git guard's linked-worktree probe, ``_in_linked_worktree``.

It asks one ``git rev-parse --git-dir --git-common-dir`` and reads a line per answer. True (a linked worktree)
exempts the agent's own tree from the guard's verb gate, so every reply that is not exactly two lines must be
None, which the guard fails CLOSED on. The fake-git cases pin that parse; the real-repository cases pin the
three answers git gives (primary checkout, linked worktree, bare repository).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
_GUARD = _SCRIPTS / "routing" / "subagent_git_write_requires_orchestrator.py"


def _load_guard() -> types.ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("subagent_git_write_probe_under_test", _GUARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard() -> types.ModuleType:
    return _load_guard()


class _Reply:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _with_git_reply(guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch, reply: _Reply) -> list[list[str]]:
    """Rebind the guard's own ``subprocess`` name to a copy whose ``run`` returns *reply* (the shared module stays
    untouched, see tests/hooks/test_exec_off_cwd.py's ``_fake_subprocess``)."""
    seen: list[list[str]] = []

    def run(argv, *_a, **_k):
        seen.append(list(argv))
        return reply

    ns = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess) if not k.startswith("__")})
    ns.run = run
    monkeypatch.setattr(guard, "subprocess", ns)
    monkeypatch.setattr(guard._exec_path, "which_off_cwd", lambda name: "/usr/bin/git")
    return seen


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        ("/r/.git\n", "one line: the second answer is missing"),
        ("/r/.git\n/r/.git\n/r/.git\n", "three lines: a path holding a newline cannot be told from the answers"),
        ("", "exit 0 with no output"),
    ],
)
def test_a_reply_that_is_not_exactly_two_lines_is_undeterminable(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, why: str,
) -> None:
    seen = _with_git_reply(guard, monkeypatch, _Reply(0, stdout))
    assert guard._in_linked_worktree(str(tmp_path)) is None, why
    assert seen == [["/usr/bin/git", "rev-parse", "--git-dir", "--git-common-dir"]], "one git process, both questions"


def test_a_failed_rev_parse_is_undeterminable(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _with_git_reply(guard, monkeypatch, _Reply(128, "/r/.git/worktrees/wt\n/r/.git\n"))
    assert guard._in_linked_worktree(str(tmp_path)) is None


def test_a_crlf_two_line_reply_reads_as_two_answers(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A CRLF reply (text mode turns it into LF on a real pipe; the parse must not depend on that) is still two
    answers, and a git dir under the common dir's worktrees/ is a linked worktree."""
    common = tmp_path / "r" / ".git"
    reply = f"{common / 'worktrees' / 'wt'}\r\n{common}\r\n"
    _with_git_reply(guard, monkeypatch, _Reply(0, reply))
    assert guard._in_linked_worktree(str(tmp_path)) is True


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, encoding="utf-8",
    )


def test_real_repositories_primary_linked_worktree_and_bare(guard: types.ModuleType, tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    (primary / "f.txt").write_text("x", encoding="utf-8")
    _git("add", "f.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "wt", str(linked), cwd=primary)
    (primary / "sub").mkdir()
    bare = tmp_path / "bare.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert guard._in_linked_worktree(str(primary)) is False, "the primary checkout is not a linked worktree"
    assert guard._in_linked_worktree(str(primary / "sub")) is False, "nor is a directory inside it"
    assert guard._in_linked_worktree(str(linked)) is True, "a linked worktree is proven"
    assert guard._in_linked_worktree(str(bare)) is False, "a bare repository's git dir is its common dir"
    # Not a repository: git fails, which is undeterminable unless a parent of tmp_path is a repository; either
    # way it is never a linked worktree.
    assert guard._in_linked_worktree(str(outside)) is not True
