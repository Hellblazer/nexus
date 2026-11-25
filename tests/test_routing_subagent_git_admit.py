# SPDX-License-Identifier: AGPL-3.0-or-later
"""Segment-level admission in the subagent git-write guard (nexus-5y47c).

Sam ruled six fixes to a guard that denied 2,199 subagent Bash calls in three weeks, 97% of
them not the harm it exists for (T2 ``nexus/sam-ruling-subagent-git-guard-fixes-2026-11-25``).
This file pins the four that change a verdict; the deny text and the brief template have their
own tests. Each admission is paired with a refusal table of the SAME shapes carrying a planted
write (the mutation proof): a shape that admits a primary-tree write is a Critical.

Every case drives the real script as a subprocess, the way Claude Code does, from the primary
checkout (a subagent's cwd is its session's start directory). ``_legacy_denies`` is the
unchanged rule; the admitted tables assert that it denies them, so a case the old rule
already passed proves nothing and the test says so.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.hooks.audience_harness import flagged_home

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "conexus" / "hooks" / "scripts"
SCRIPT = SCRIPTS / "routing" / "subagent_git_write_requires_orchestrator.py"
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"
AGENT_ID = "aworker-x-6f59dab8bbb14864"
D = chr(36)  # dollar
BT = chr(96)  # backtick

_HOME: list[pathlib.Path | None] = [None]


@pytest.fixture(autouse=True)
def _account_home(tmp_path):
    _HOME[0] = pathlib.Path(flagged_home(tmp_path / "acct-home"))
    yield
    _HOME[0] = None


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "isolated-nexus-config"))
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_REMOTE_TEST_HOSTS", raising=False)
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "dropped_writes.jsonl"))


def _git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture()
def primary(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "primary"
    repo.mkdir()
    _git("init", "-q", "-b", "trunk", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture()
def worktree(primary: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "wt-branch", str(wt), cwd=primary)
    (wt / "sub").mkdir()
    return wt


@pytest.fixture()
def develop_worktree(primary: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    wt = tmp_path / "wt-develop"
    _git("worktree", "add", "-q", "-b", "develop", str(wt), cwd=primary)
    return wt


@pytest.fixture()
def scratch(tmp_path: pathlib.Path) -> pathlib.Path:
    """A directory under the temp root with no repository in it or above it."""
    box = tmp_path / "box"
    box.mkdir()
    return box


def _verdict(cmd: str, cwd: pathlib.Path) -> dict:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": cmd},
        "agent_id": AGENT_ID, "agent_type": "worker-x", "cwd": str(cwd),
    }
    env = {**os.environ, "NX_HOOK_PYTHON": sys.executable}
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(_HOME[0]), str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return {} if proc.stdout == "" else json.loads(proc.stdout)["hookSpecificOutput"]


def _denied(cmd: str, cwd: pathlib.Path) -> bool:
    return _verdict(cmd, cwd).get("permissionDecision") == "deny"


def _fill(cmds: list[str], **places: object) -> list[str]:
    return [c.format(**{k: str(v) for k, v in places.items()}) for c in cmds]


@pytest.fixture(scope="module")
def legacy(guard_module):
    """The unchanged rule, in process: what the guard said about a text before nexus-5y47c."""
    return guard_module._legacy_denies


@pytest.fixture(scope="module")
def guard_module():
    mp = pytest.MonkeyPatch()
    mp.setenv("NX_HOOK_PYTHON", sys.executable)  # the script's interpreter preamble then does not re-exec
    mp.syspath_prepend(str(SCRIPTS))
    mp.syspath_prepend(str(SCRIPTS / "routing"))
    try:
        spec = importlib.util.spec_from_file_location("guard_under_test", SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        mp.undo()


# ---------------------------------------------------------------------------
# Fix 1: read-only git and text that merely spells a verb
# ---------------------------------------------------------------------------

#: Commands the unchanged rule denied for a verb WORD (an argument, a pattern, a filename, or a
#: neighbour in another segment) and that run no write. {S} is a scratch directory.
_READ_ADMITTED = [
    "git merge-base --is-ancestor v8.5.3 " + D + "m",
    "git merge-base --is-ancestor v8.5.3 " + D + "m && echo ancestor",
    "git stash list",
    "git stash list | head -1",
    "git stash show --stat",
    "git log --grep=commit",
    "git log --grep='git commit' --oneline",
    "git log -S'push' --oneline | head -5",
    "git log --format=%h | grep commit",
    "git grep -n 'git add' -- docs",
    "git grep -n -e 'git checkout' -e 'git reset'",
    "git diff --stat " + D + "(git merge-base A B) B | tail -3",
    "git branch --list '*add*'",
    "git branch -a --contains abc1234",
    "git worktree list",
    "git -C /some/other/repo log --grep=push",
    "git fetch origin pull/190/head 2>&1 | tail -2",
    "git fetch -q origin feature/x",
    "grep -n PUSH_OK scripts/git-push-develop.sh",
    'grep -n "git push" scripts/git-push-develop.sh | head -30',
    "cat scripts/git-push-develop.sh | head -40",
    "ls scripts/git-push-develop.sh",
    "wc -l scripts/git-push-develop.sh",
    "sed -n '100,116p' scripts/git-push-develop.sh",
    "echo 'later run git commit -m x'",
    "cd ~/git && echo add",
    "ls ~/git/ && echo add",
    "for c in a b c; do git merge-base --is-ancestor " + D + "c HEAD && echo yes; done",
    "if git merge-base --is-ancestor A B; then echo anc; fi",
    "(git stash list; git status --short)",
    "S={S}; git show HEAD:f.txt > " + D + "S/copy.txt && diff " + D + "S/copy.txt f.txt",
    "gh pr view 5 && echo pr checkout",
    "git status --short | head -3; git stash list | head -1; git log -1 --format=%h",
]


def test_read_only_git_and_verb_words_in_text_pass(primary, scratch, legacy):
    cmds = _fill(_READ_ADMITTED, S=scratch)
    blocked = [c for c in cmds if _denied(c, primary)]
    assert not blocked, blocked
    # Non-vacuity: most of these are texts the unchanged rule denies; a table the old rule
    # already passed would prove nothing about the new one.
    old_denied = [c for c in cmds if legacy(c)]
    assert len(old_denied) >= 25, (len(old_denied), len(cmds))


#: The members of the old deny catalogue (tests/test_routing_subagent_git_write.py: accepted_false_positives,
#: glued_verbs_accepted_price, stash_read_forms_since_round4 and the last members of
#: exemptions_withheld_when_text_can_be_rebuilt) that nexus-5y47c admits ON PURPOSE. Each runs nothing a verb word
#: could turn into a write: a read-only git command or a text command whose arguments spell the word, with no
#: interpreter, no pipe or file that carries the word to a command, and no config read. They are listed here, by name,
#: so the loosening is a visible decision and the catalogue's remaining families stay fully denied.
CATALOGUE_NOW_ADMITTED = [
    "git log --grep=commit",
    "ls ~/git/ && echo add",
    "cd ~/git && echo add",
    "echo git config user.name x",
    "gh pr view 5 && echo pr checkout",
    "cd ~/bin/gh && echo repo sync",
    "git stash list",
    "git stash show -p",
    "git diff " + D + "(git merge-base A B)",
    "git log --no-commit-id " + D + "(basename /x/commit)",
    "git status; ls x.git/hooks/post-commit",
    "git status; ls .git/hooks/post-commit-x",
    "git log --abbrev-commit-x",
    "git log --no-commit-id=1",
    "git log x--abbrev-commit",
    "git log feature/x-add-thing",
    "git log origin/feature/add-thing",
    "git log docs/add.md",
    "git diff docs/add.md",
    "git show HEAD:src/commit.py",
    "git log -- scripts/nexus-96-restore",
    "git status && ls /Users/x/git/nexus-96-restore",
    "git status && echo ---clean above---",
    "git show a461db0b7 --stat; ls scratchpad/commit-a461db0b7",
    "git status && ls hooks/post-merge",
    "git log ./checkout",
    "git log ~/commit",
    "git diff " + D + "(git merge-base HEAD origin/develop)..HEAD",
    "for d in a b; do git status; ls " + D + "d/.git/hooks/post-commit; done",
    "for s in 1; do git diff-tree --no-commit-id -r " + D + "s; done",
]


def test_the_catalogue_members_admitted_on_purpose_pass_and_were_denied_before(primary, legacy):
    blocked = [c for c in CATALOGUE_NOW_ADMITTED if _denied(c, primary)]
    assert not blocked, blocked
    unchanged = [c for c in CATALOGUE_NOW_ADMITTED if not legacy(c)]
    assert not unchanged, f"already passed before nexus-5y47c, so not a loosening: {unchanged}"
    assert len(CATALOGUE_NOW_ADMITTED) == 30


#: The same shapes with a write planted in each: every one stays denied.
_READ_PLANTS = [
    "git stash list; git stash",
    "git stash list | head -1; git checkout f.txt",
    "git stash list && git stash drop",
    "git stash",
    "git stash pop",
    "git merge-base A B && git reset --hard",
    "git log --grep=commit && git commit -m x",
    "git log --grep=commit; git add -A",
    "git log " + D + "(git checkout f.txt)",
    "git log " + D + "(git stash)",
    "git diff " + D + "(git add -A; echo B) B",
    "git grep -n 'git add' -- docs | sh",
    "grep -n 'git checkout' f | sh",
    "echo 'git checkout f.txt' | sh",
    "echo 'git checkout f.txt' | bash -s",
    "echo 'git checkout f.txt' > /tmp/x.sh",
    "printf 'git%s checkout f.txt' '' | sh",
    "X='git checkout f.txt'; sh -c \"" + D + "X\"",
    "X='git checkout f.txt'; eval " + D + "X",
    "git branch --list; git branch -D x",
    "git branch -D x --list",
    "git worktree list; git worktree remove -f w",
    "git fetch origin a:b",
    "git fetch --prune origin pull/190/head",
    "git fetch -f origin pull/190/head",
    "git fetch -p origin pull/190/head",
    "git fetch --force origin pull/190/head",
    "git fetch origin +main:main",
    "git fetch origin pull/1/head:refs/heads/pr1",
    "git fetch --prune-tags origin",
    "git fetch origin; git checkout f.txt",
    "ls scripts/git-push-develop.sh && git push origin HEAD",
    "bash scripts/git-push-develop.sh --source HEAD",
    "./scripts/git-push-develop.sh",
    "sed -n '1,5p' scripts/git-push-develop.sh; git add f",
    "sed -i 's/a/b/' f && git commit -m x",
    "git diff --output=/tmp/o HEAD",
    "git log --output=/tmp/o",
    "(git stash list; git stash)",
    "if git merge-base A B; then git checkout f.txt; fi",
    "for c in a; do git merge-base " + D + "c HEAD; git reset --hard; done",
    "git stash list | xargs git stash",
    "git -c core.pager=x log --grep=commit",
    "git grep -Osh commit",
    "git grep --open-files-in-pager=sh commit",
    "GIT_EXTERNAL_DIFF=/tmp/x git diff --grep=commit",
    "PATH=/tmp/x:" + D + "PATH git stash list",
    # a renamed git: only the git word and the verb word together say what runs
    "alias g=git; g checkout f.txt",
    "git stash list; alias g=git; g checkout f.txt",
    "ln -s /usr/bin/git /tmp/g && /tmp/g checkout f.txt",
    "cp /usr/bin/git /tmp/gg; /tmp/gg add -A",
    "git status; export V=stash; git " + D + "V",
    "git status; declare -x V=checkout; git " + D + "V f.txt",
    "git status; printf -v V checkout; git " + D + "V f.txt",
]


def test_a_planted_write_in_each_read_shape_stays_denied(primary):
    leaked = [c for c in _READ_PLANTS if not _denied(c, primary)]
    assert not leaked, leaked


# ---------------------------------------------------------------------------
# Fix 2: a worktree the agent was given
# ---------------------------------------------------------------------------

_WT_ADMITTED = [
    "git -C {wt} add -A && git -C {wt} commit -m 'fix(x): y'",
    "git -C {wt} add -- f.txt; git -C {wt} commit --message=msg; git -C {wt} log --oneline -1",
    "git -C {wt} commit --message 'msg' --quiet",
    "git -C {wt} commit --quiet --amend --no-edit",
    "git -C {wt} rm --quiet --cached f.txt",
    "git -C {wt} switch --create step-4 --quiet",
    "git -C {wt} add -A && git -C {wt} commit -F /tmp/msg.txt && git -C {wt} log -1 --format=%h",
    "git -C {wt} add -A 2>&1 | tail -3",
    "git -C {wt} commit -m x 2>&1 | tail -3; git -C {wt} status --short",
    "cd {wt} && git add -A && git commit -m x",
    "cd {wt} && git status --short && git add -A && git commit -q -m 'wip'",
    "cd {wt} && git rebase origin/develop 2>&1 | tail -3",
    "cd {wt} && git merge --ff-only origin/develop",
    "cd {wt} && git reset --soft 'HEAD~1'",
    "cd {wt} && git log --oneline -3 && git cherry-pick abc1234",
    "cd {wt} || exit 1; git add -A; git commit -m x",
    "W={wt}; git -C " + D + "W add -A && git -C " + D + "W commit -m x",
    "W={wt}; cd " + D + "W && git add -A && git commit -m x",
    "cd {wt} && git add f.txt && git commit -m 'a' && cd {wt} && git commit --amend --no-edit",
    "cd {wt} && (git status --short; git add -A) && git commit -m x",
    "git -C {wt} branch step-5 abc1234 && git -C {wt} switch step-5",
]


def test_a_worktree_the_agent_was_given_takes_chained_writes(primary, worktree):
    cmds = _fill(_WT_ADMITTED, wt=worktree)
    blocked = [c for c in cmds if _denied(c, primary)]
    assert not blocked, blocked


def _wt_plants() -> list[str]:
    return [
        # a bare write after a cd that a `;` or a newline may have skipped
        "cd {wt}; git add -A",
        "cd {wt}\ngit add -A",
        "cd {wt} && git status; git add -A",
        "cd {wt} || echo no; git add -A",
        "cd {wt} && git add -A\ngit commit -m x",
        # the cd names something that is not a worktree root
        "cd {primary} && git add -A",
        "cd {wt}/sub && git add -A",
        "cd {wt}/.. && git add -A",
        "cd {wt} && cd {primary} && git add -A",
        "cd {tmp} && git add -A",
        "cd " + D + "UNSET && git add -A",
        "cd .. && git add -A",
        "cd {wt} && git -C {primary} add -A",
        "git -C {primary} add -A",
        "git -C {wt} add -A && git -C {primary} commit -m x",
        "git -C {wt} add -A; git add -A",
        "git -C {wt} add -A; git -C {wt}/sub add -A",
        "git -C {wt} add -A && git commit -m x",
        "git -C {wt} commit -m x | git commit -m y",
        "git -C {wt}/../primary add -A",
        "git -C {wt} -C {primary} add -A",
        "git -C wt add -A",
        "git -C . add -A",
        # verbs the shape never admits, in a worktree or not
        "cd {wt} && git checkout f.txt",
        "cd {wt} && git checkout -b other",
        "cd {wt} && git stash",
        "cd {wt} && git stash pop",
        "cd {wt} && git restore f.txt",
        "cd {wt} && git reset --hard",
        "cd {wt} && git clean -fd",
        "cd {wt} && git rebase --abort",
        "cd {wt} && git rm --force f.txt",
        "cd {wt} && git rm -f f.txt",
        "cd {wt} && git branch -D other",
        "cd {wt} && git tag -d v1",
        "cd {wt} && git commit -am msg",
        "cd {wt} && git commit --mess msg",
        "cd {wt} && git add -u -x",
        "git -C {wt} checkout f.txt",
        "git -C {wt} stash",
        "git -C {wt} restore f.txt",
        "git -C {wt} reset --hard",
        "git -C {wt} add -A && git -C {wt} checkout f.txt",
        "git -C {wt} add -A; git -C {wt} stash",
        # a message that spells a git write, text that rebuilds a word, an env that moves the repo
        "git -C {wt} commit -m 'see git -C {primary} reset --hard'",
        "git -C {wt} commit -m x && echo 'git checkout f' | sh",
        "git -C {wt} commit -m x && git " + D + "(echo checkout) f.txt",
        "git -C {wt} commit -m x; X=checkout; git " + D + "X f.txt",
        "GIT_DIR={primary}/.git git -C {wt} commit -m x",
        "export GIT_DIR={primary}/.git; git -C {wt} commit -m x",
        "git -C {wt} commit -m x > {primary}/out.txt && sh {primary}/out.txt; git add -A",
        "git -C {wt} -c core.worktree={primary} commit -m x",
        "git -C {wt} --git-dir={primary}/.git commit -m x",
        "git -C {wt} config user.name x",
        "git -C {wt} config core.hooksPath /tmp/h",
        # a flag shape the allowlist refuses
        "git -C {wt} rebase --exec 'touch /tmp/x' abc1234",
        "git -C {wt} rebase --abort",
        "git -C {wt} switch --discard-changes other",
        "git -C {wt} merge -s ours abc1234",
        "git -C {wt} push --force origin HEAD",
    ]


def test_a_planted_write_in_each_worktree_shape_stays_denied(primary, worktree, tmp_path):
    cmds = _fill(_wt_plants(), wt=worktree, primary=primary, tmp=tmp_path)
    leaked = [c for c in cmds if not _denied(c, primary)]
    assert not leaked, leaked


def test_the_cd_form_never_reaches_the_primary_checkout_for_real(primary, worktree):
    """The claim behind the cd rule, run with real git: a shell whose cd fails stays in the
    primary, and the plain-write forms are exactly the ones the guard refuses."""
    ok = f"cd {worktree} && git add -A"
    risky = f"cd {worktree}; git add -A"
    assert not _denied(ok, primary)
    assert _denied(risky, primary)
    (worktree / "g.txt").write_text("y")
    done = subprocess.run(["sh", "-c", ok], cwd=primary, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    staged_in_primary = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=primary, capture_output=True, text=True
    ).stdout
    assert "g.txt" not in staged_in_primary


# ---------------------------------------------------------------------------
# Fix 3: fetch, a detached worktree outside every repo, a scratch repository
# ---------------------------------------------------------------------------


_FETCH_PASSES = [
    "git fetch", "git fetch origin", "git fetch -q origin develop", "git fetch --quiet --tags origin",
    "git fetch origin feature/x feature/y", "git fetch origin pull/12/head",
    "git -C /some/repo fetch --all", "git fetch --depth=1 origin develop", "git fetch --depth 1 origin x",
    "git fetch -q origin && git log --oneline -1 origin/develop",
]


def test_a_plain_fetch_passes_and_a_ref_writing_fetch_does_not(primary):
    passes = _FETCH_PASSES
    blocked = [c for c in passes if _denied(c, primary)]
    assert not blocked, blocked
    denied = [
        "git fetch origin a:b", "git fetch origin +a:b", "git fetch --prune-tags",
        "git fetch -P", "git fetch --prune origin pull/1/head", "git fetch -p origin pull/1/head",
        "git fetch -f origin pull/1/head", "git fetch --force origin pull/1/head", "git fetch origin refs/heads/*:refs/remotes/o/*", "git fetch origin tag v1",
        "git fetch --update-head-ok origin develop:develop", "git fetch -u origin develop:develop",
        "git fetch --refmap= origin a:b", "git fetch --set-upstream origin develop",
        "git fetch origin " + D + "BRANCH", "git fetch origin develop:develop",
        "git fetch origin develop; git checkout f.txt",
    ]
    leaked = [c for c in denied if not _denied(c, primary)]
    assert not leaked, leaked


_DETACHED_PASSES = [
    "git worktree add --detach {box}/pr1 origin/develop",
    "git -C {primary} worktree add --detach {box}/pr2 HEAD",
    "git worktree add --detach --quiet {box}/pr3 abc1234",
    "S={box}; git worktree add --detach " + D + "S/pr4 HEAD 2>&1 | tail -1; cd " + D + "S/pr4 && git log --oneline -1",
]


def test_a_detached_worktree_outside_every_repo_passes(primary, scratch, tmp_path):
    passes = _fill(_DETACHED_PASSES, box=scratch, primary=primary)
    blocked = [c for c in passes if _denied(c, primary)]
    assert not blocked, blocked
    denied = _fill([
        "git worktree add {box}/x HEAD",
        "git worktree add -b newbranch {box}/x HEAD",
        "git worktree add --detach -b newbranch {box}/x HEAD",
        "git worktree add -f --detach {box}/x HEAD",
        "git worktree add --detach {primary}/inside HEAD",
        "git worktree add --detach {primary} HEAD",
        "git worktree add --detach {box}/../primary/inside HEAD",
        "git worktree add --detach relative/x HEAD",
        "git worktree add --detach " + D + "UNSET/x HEAD",
        "git worktree add --detach {box}/x --track",
        "git worktree remove {box}/x",
        "git worktree remove --force {primary}",
        "git worktree prune",
        "git worktree add --detach {box}/x HEAD && git checkout f.txt",
        "git worktree add --detach {box}/x HEAD; cd {box}/x && git add -A",
    ], box=scratch, primary=primary)
    leaked = [c for c in denied if not _denied(c, primary)]
    assert not leaked, leaked


_SCRATCH_PASSES = [
    "cd {box} && git init -q && git config user.email a@b && git config user.name t && "
    "git add -A && git commit -q -m init",
    "git init -q {box}/r1 && git -C {box}/r1 config user.name t && git -C {box}/r1 commit -q --allow-empty -m x",
    "mkdir -p {box}/r2 && cd {box}/r2 && git init -q -b main && git config commit.gpgsign false",
    "git clone -q {primary} {box}/c1 && git -C {box}/c1 log --oneline -1",
    "git clone -q --branch trunk --depth 1 {primary} {box}/c2 && cd {box}/c2 && git switch --create t",
    "S={box}; rm -rf " + D + "S/r3 && git init -q " + D + "S/r3 && git -C " + D + "S/r3 add -A",
]


def test_a_scratch_repository_the_command_creates_passes(primary, scratch, tmp_path):
    passes = _fill(_SCRATCH_PASSES, box=scratch, primary=primary)
    blocked = [c for c in passes if _denied(c, primary)]
    assert not blocked, blocked
    denied = _fill([
        # only a repository this very command created
        "cd {box} && git add -A",
        "git -C {box} commit -m x",
        "git -C {box}/gone add -A",
        # not a fresh directory outside every repo under a temp root
        "cd {primary} && git init -q && git add -A",
        "git init -q {primary}/sub && git -C {primary}/sub add -A",
        "git -C {primary} init -q && git -C {primary} add -A",
        "git init -q && git add -A",
        "git init -q . && git add -A",
        "git init -q /Users/someone/not-temp && git -C /Users/someone/not-temp add -A",
        "git clone -q {primary} {primary}/inside && git -C {primary}/inside add -A",
        "git clone -q {primary} relative && git -C relative add -A",
        # config that moves a worktree or runs code
        "cd {box} && git init -q && git config core.worktree {primary}",
        "cd {box} && git init -q && git config core.hooksPath /tmp/h",
        "cd {box} && git init -q && git config --global user.name t",
        "cd {box} && git init -q && git config --worktree user.name t",
        "cd {box} && git init -q && git config alias.x '!sh'",
        # a destructive verb or a push the shape does not list
        "cd {box} && git init -q && git push origin HEAD",
        "cd {box} && git init -q && git checkout f.txt",
        "cd {box} && git init -q && git stash",
        "cd {box} && git init -q && git add -A && git -C {primary} add -A",
        "cd {box} && git init -q && cd {primary} && git add -A",
        "cd {box} && git init -q; cd {primary}; git add -A",
        "cd {box} && git init -q && GIT_DIR={primary}/.git git add -A",
        "cd {box} && git init -q --template=/tmp/t && git add -A",
        "git clone -q --upload-pack=x {primary} {box}/c3 && git -C {box}/c3 add -A",
        "git clone -q -c core.hooksPath=/tmp/h {primary} {box}/c3 && git -C {box}/c3 add -A",
        "git clone -q {primary} {box}/c4 {box}/c5 && git -C {box}/c4 add -A",
    ], box=scratch, primary=primary)
    leaked = [c for c in denied if not _denied(c, primary)]
    assert not leaked, leaked


# ---------------------------------------------------------------------------
# Fix 5: push of the agent's own branch
# ---------------------------------------------------------------------------


_PUSH_PASSES = [
    "git -C {wt} push -u origin HEAD",
    "git -C {wt} push --set-upstream origin HEAD",
    "git -C {wt} push origin HEAD",
    "git -C {wt} push origin wt-branch",
    "git -C {wt} push -u origin wt-branch:wt-branch",
    "git -C {wt} push origin HEAD:wt-branch",
    "git -C {wt} push origin HEAD:refs/heads/wt-branch",
    "git -C {wt} push -q origin HEAD 2>&1 | tail -3",
    "git -C {wt} push --dry-run origin HEAD",
    "cd {wt} && git push -u origin HEAD",
    "cd {wt} && git add -A && git commit -m x && git push origin HEAD",
    "git -C {wt} commit -m x && git -C {wt} push origin wt-branch",
]


def test_an_agent_may_push_its_own_branch_from_a_proven_worktree(primary, worktree):
    passes = _fill(_PUSH_PASSES, wt=worktree)
    blocked = [c for c in passes if _denied(c, primary)]
    assert not blocked, blocked


def test_a_push_that_is_not_the_agents_own_branch_stays_denied(primary, worktree, develop_worktree, tmp_path):
    cmds = _fill([
        "git -C {wt} push --force origin HEAD",
        "git -C {wt} push -f origin HEAD",
        "git -C {wt} push --force-with-lease origin HEAD",
        "git -C {wt} push --force-with-lease=wt-branch origin HEAD",
        "git -C {wt} push origin +HEAD",
        "git -C {wt} push origin +wt-branch",
        "git -C {wt} push origin +HEAD:wt-branch",
        "git -C {wt} push --delete origin wt-branch",
        "git -C {wt} push -d origin wt-branch",
        "git -C {wt} push origin :wt-branch",
        "git -C {wt} push --mirror origin",
        "git -C {wt} push --all origin",
        "git -C {wt} push --tags origin",
        "git -C {wt} push --follow-tags origin HEAD",
        "git -C {wt} push --prune origin HEAD",
        "git -C {wt} push origin HEAD:develop",
        "git -C {wt} push origin HEAD:main",
        "git -C {wt} push origin HEAD:refs/heads/develop",
        "git -C {wt} push origin HEAD:other",
        "git -C {wt} push origin other",
        "git -C {wt} push origin develop",
        "git -C {wt} push origin wt-branch:other",
        "git -C {wt} push origin wt-branch HEAD:other",
        "git -C {wt} push origin HEAD HEAD:other",
        "git -C {wt} push upstream HEAD",
        "git -C {wt} push {primary} HEAD",
        "git -C {wt} push file://{primary} HEAD",
        "git -C {wt} push",
        "git -C {wt} push origin",
        "git -C {wt} push -u",
        "git -C {wt} push --repo={primary} origin HEAD",
        "git -C {wt} push --receive-pack=x origin HEAD",
        "git -C {wt} push --exec=x origin HEAD",
        "git -C {wt} push --push-option=x origin HEAD",
        "git -C {wt} push --no-verify origin HEAD",
        "git -C {wt} push origin HEAD --force",
        "git -C {wt} push -uf origin HEAD",
        "git -C {develop} push origin HEAD",
        "git -C {develop} push -u origin develop",
        "git -C {primary} push origin HEAD",
        "git -C {primary} push origin trunk",
        "git -C {wt}/sub push origin HEAD",
        "cd {primary} && git push origin HEAD",
        "cd {wt}; git push origin HEAD",
        "git -C {wt} push origin HEAD && git -C {primary} push origin trunk",
        "git -C {wt} push origin HEAD && git push origin trunk",
        "git -C {wt} push origin HEAD; git push",
        "git push origin HEAD",
        "git push -u origin HEAD",
        "git -C {wt} push origin HEAD:wt-branch --force",
    ], wt=worktree, develop=develop_worktree, primary=primary)
    leaked = [c for c in cmds if not _denied(c, primary)]
    assert not leaked, leaked


def test_a_detached_worktree_has_no_own_branch_to_push(primary, tmp_path):
    detached = tmp_path / "detached"
    _git("worktree", "add", "-q", "--detach", str(detached), cwd=primary)
    assert _denied(f"git -C {detached} push origin HEAD", primary)
    assert _denied(f"cd {detached} && git push origin HEAD", primary)


# ---------------------------------------------------------------------------
# The chain keeps the unchanged guard's other rules
# ---------------------------------------------------------------------------


def test_the_config_and_side_effect_rules_still_run_on_an_admitted_segment(primary, worktree):
    denied = _fill([
        "git -C {wt} commit -m x && git -C {wt} remote add o /tmp/x",
        "git -C {wt} add -A && git -C {wt} branch -u origin/develop",
        "git -C {wt} add -A && git -C {wt} config user.name x",
        "cd {wt} && git add -A && git log --output=/tmp/o",
        "git -C {wt} commit -m x && git -C {wt} tag v1",
        "git stash list && git remote set-url origin /tmp/x",
        "git stash list | head -1; git branch -u origin/develop",
        "git stash list; git config user.name x",
        "git stash list; git submodule foreach 'echo hi'",
    ], wt=worktree)
    leaked = [c for c in denied if not _denied(c, primary)]
    assert not leaked, leaked


def test_the_adjacency_rule_still_denies_a_glued_expansion(primary):
    assert _denied("git status && echo add${item} to list", primary)
    assert _denied("git log file${i}.txt", primary)
    assert _denied("make i${n}t", primary)


def test_a_command_the_lexer_cannot_read_falls_through_to_the_old_verdict(primary):
    unreadable = [
        "git stash list <<'EOF'\nx\nEOF",
        "git stash list `echo`",
        "git stash list # comment",
        "git stash list \\\n--oneline",
        "git stash list; { git stash; }",
        "git stash list &",
        "git stash list >" + D + "(cat)",
        "echo " + D + "'\\x67it' checkout f.txt",
        "git stash list" + chr(13) + "; git stash",
    ]
    # none of these may be admitted on the strength of the read-only part; the ones that spell
    # a write deny (the rest pass for the reason they always did)
    must_deny = [c for c in unreadable if "stash;" in c.replace(" ", "") or "git stash" in c.split("stash list", 1)[-1]]
    leaked = [c for c in must_deny if not _denied(c, primary)]
    assert not leaked, leaked


def test_a_hostile_long_command_is_refused_quickly(primary):
    cmd = "git stash list | head -1; " + "echo add; " * 4000
    t0 = time.monotonic()
    _verdict(cmd, primary)
    assert time.monotonic() - t0 < 10


# ---------------------------------------------------------------------------
# The mutation proof: every admitted command, with a write planted beside it
# ---------------------------------------------------------------------------


def _deny_flags(cmds: list[str], cwd: pathlib.Path) -> list[bool]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda c: _denied(c, cwd), cmds))


def test_every_admitted_shape_refuses_a_write_planted_beside_it(primary, worktree, scratch, tmp_path):
    """For each command the admission passes, the same command with a primary-tree write added after it, before it
    and behind a ``cd <primary>`` is denied. A shape that lets any of them through admits a write nobody proved."""
    admitted = (
        [c.replace("{S}", str(scratch)) for c in _READ_ADMITTED]
        + list(CATALOGUE_NOW_ADMITTED)
        + _fill(_WT_ADMITTED, wt=worktree)
        + _FETCH_PASSES
        + _fill(_DETACHED_PASSES, box=scratch, primary=primary)
        + _fill(_SCRATCH_PASSES, box=scratch, primary=primary)
        + _fill(_PUSH_PASSES, wt=worktree)
    )
    admitted = [c for c in admitted if "<<" not in c and "\n" not in c]
    assert len(admitted) >= 110, len(admitted)
    p = str(primary)
    plants = [
        "{c} && git checkout f.txt",
        "{c}; git -C " + p + " add -A",
        "git checkout f.txt; {c}",
        "{c} && cd " + p + " && git add -A",
        "{c} && git stash",
        "{c} && git -C " + p + " push origin trunk",
    ]
    leaked: list[str] = []
    for plant in plants:
        mutated = [plant.replace("{c}", c) for c in admitted]
        flags = _deny_flags(mutated, primary)
        leaked += [m for m, hit in zip(mutated, flags) if not hit]
    assert not leaked, leaked[:10]


def test_a_bug_in_the_admission_denies_instead_of_crashing_into_an_allow(guard_module, monkeypatch):
    """``run_hook(fail_closed=False)`` allows when the hook crashes, so the admission may never raise."""
    def boom(*a, **k):
        raise RuntimeError("an admission bug")

    monkeypatch.setattr(guard_module._git_admit, "analyze", boom)
    assert guard_module._admission("git stash list") is None
    monkeypatch.setattr(guard_module, "_git_admit", None)
    assert guard_module._admission("git stash list") is None
