# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seven bypasses of the subagent git-write guard, closed together.

The guard (``conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py``)
denies a subagent's git write verbs in the primary checkout. The 8.4.1 review
found seven writes it allowed there:

1. ``git branch -f|--force <name> <start>``  moves a branch ref
2. ``git tag <newname>`` (any create form) and ``git tag -f``  write a tag ref
3. ``git revert <commit>`` (and ``--skip`` / ``--abort`` / ``--continue``)  commits
   or discards a stopped revert
4. ``git submodule foreach '<cmd>'``  runs any command in every submodule
5. ``git pull`` (any form)  fetches and merges or rebases
6. ``git fetch <remote> <src>:<dst>``  writes the local ref ``<dst>``
7. ``git diff|log|show --output=<path>``  writes a file anywhere

Gaps 1, 2, 3, 5 and 6 deny in the primary and pass in a proven linked worktree,
as every other write verb does (the verb scan does not apply there). Gaps 4 and
7 deny EVERYWHERE: ``foreach`` runs arbitrary commands and ``--output`` writes
outside the tree, so they ride the side-effect rule (the one that also denies
config writes from a worktree). Only the ``# routing-allow:`` escape passes them.

Every command below is run for real in a sandbox at least once, so a "denied"
verdict is a verdict on a command that changes state. Each case drives the real
script as a subprocess, the way Claude Code does.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.hooks.audience_harness import flagged_home

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "subagent_git_write_requires_orchestrator.py"
)
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"
AGENT_ID = "aworker-x-6f59dab8bbb14864"


#: nexus-sby49: the guard's full rule is the maintainer's. These tests build their
#: own repos with no nexus marker, so they run under an account home that carries
#: the flag (``tests/hooks/audience_harness.py`` is the only seam; the box's real
#: home never decides a verdict). The marker path is covered in
#: ``test_routing_subagent_git_write.py`` and ``test_audience.py``.
_HOME: list[pathlib.Path | None] = [None]


@pytest.fixture(autouse=True)
def _account_home(tmp_path):
    home = tmp_path / "acct-home"
    _HOME[0] = pathlib.Path(flagged_home(home))
    yield
    _HOME[0] = None


def _guard_argv() -> list[str]:
    assert _HOME[0] is not None
    return [sys.executable, str(HARNESS), str(_HOME[0]), str(SCRIPT)]


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "isolated-nexus-config"))
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_REMOTE_TEST_HOSTS", raising=False)
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "dropped_writes.jsonl"))


def _git_env(box: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        HOME=str(box), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    )
    env.pop("GIT_DIR", None)
    return env


def _git(cwd: pathlib.Path, env: dict[str, str], *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True,
    )
    return done.stdout


class Sandbox:
    """A primary checkout on ``main`` with an ``origin`` that is one commit ahead
    (so ``pull`` and ``fetch`` have something to write), a branch ``topic``, a
    tag ``oldtag``, a dirty file, and a linked worktree ``wt``."""

    def __init__(self, box: pathlib.Path) -> None:
        self.box = box
        self.env = _git_env(box)
        box.mkdir()
        origin = box / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=self.env,
        )
        self.main = box / "main"
        self.main.mkdir()
        g = self.g
        g(self.main, "init", "-q", "-b", "main")
        for i in (1, 2):
            (self.main / "f.txt").write_text(f"v{i}\n")
            g(self.main, "add", "f.txt")
            g(self.main, "commit", "-q", "-m", f"c{i}")
        g(self.main, "remote", "add", "origin", str(origin))
        g(self.main, "push", "-q", "-u", "origin", "main")
        g(self.main, "branch", "topic", "HEAD")
        g(self.main, "tag", "oldtag", "HEAD")
        peer = box / "peer"
        g(box, "clone", "-q", str(origin), str(peer))
        (peer / "f.txt").write_text("v3\n")
        g(peer, "commit", "-q", "-am", "c3")
        g(peer, "tag", "peer-tag", "HEAD")
        g(peer, "push", "-q", "origin", "main", "peer-tag", "main:other")
        (self.main / "dirty.txt").write_text("d\n")
        self.wt = box / "wt"
        g(self.main, "worktree", "add", "-q", "-b", "wtb", str(self.wt))
        (self.main / "f.txt").write_text("v2\n")

    def g(self, cwd: pathlib.Path, *args: str) -> str:
        return _git(cwd, self.env, *args)

    def state(self) -> tuple[str, str, tuple[str, ...]]:
        """Refs, HEAD and the files in the box: what a write changes."""
        refs = self.g(self.main, "for-each-ref", "--format=%(refname) %(objectname)")
        head = self.g(self.main, "rev-parse", "HEAD").strip()
        files = tuple(sorted(
            str(p.relative_to(self.box)) for p in self.box.rglob("*")
            if ".git" not in p.parts and "origin.git" not in p.parts and p.is_file()
        ))
        return refs, head, files

    def run(self, cmd: str, cwd: pathlib.Path | None = None) -> None:
        subprocess.run(
            ["bash", "-c", cmd], cwd=cwd or self.main, env=self.env, capture_output=True, text=True,
            timeout=60, check=False,
        )


@pytest.fixture()
def sandbox(tmp_path: pathlib.Path) -> Sandbox:
    return Sandbox(tmp_path / "box")


def _verdict(cmd: str, cwd: pathlib.Path) -> dict:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": cmd},
        "agent_id": AGENT_ID, "agent_type": "worker-x", "cwd": str(cwd),
    }
    env = {**os.environ, "NX_HOOK_PYTHON": sys.executable}
    proc = subprocess.run(
        _guard_argv(), input=json.dumps(payload), capture_output=True,
        text=True, timeout=20, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout else {}


def _denied(cmd: str, cwd: pathlib.Path) -> bool:
    return _verdict(cmd, cwd).get("permissionDecision") == "deny"


def _denied_among(cmds: list[str], cwd: pathlib.Path) -> list[str]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        flags = list(pool.map(lambda c: _denied(c, cwd), cmds))
    return [c for c, hit in zip(cmds, flags) if hit]


def _not_denied(cmds: list[str], cwd: pathlib.Path) -> list[str]:
    denied = set(_denied_among(cmds, cwd))
    return [c for c in cmds if c not in denied]


# ---------------------------------------------------------------------------
# The command tables, one family per gap. `{out}` is a path outside the tree.
# ---------------------------------------------------------------------------

#: Gap 1.
_BRANCH_FORCE = [
    "git branch -f topic HEAD~1",
    "git branch --force topic HEAD~1",
    "git branch topic HEAD~1 -f",
    "git branch topic HEAD~1 --force",
    "git branch -vf topic HEAD~1",
    "git branch -fq topic HEAD~1",
    "git branch --forc topic HEAD~1",
    "git -c core.pager=cat branch -f topic HEAD~1",
]
#: Gap 2.
_TAG_WRITES = [
    "git tag newtag",
    "git tag 'newtag'",
    "git tag '-f' oldtag",
    "git tag newtag HEAD~1",
    "git tag -a newtag -m msg",
    "git tag -a -m msg newtag",
    "git tag -s newtag",
    "git tag -m msg newtag",
    "git tag -F notes.txt newtag",
    "git tag -u key newtag",
    "git tag --annotate newtag",
    "git tag --sign newtag",
    "git tag --message=msg newtag",
    "git tag -f oldtag HEAD~1",
    "git tag --force oldtag HEAD~1",
    "git tag -fa oldtag -m msg",
    "git tag -i newtag",
    "git tag --create-reflog newtag",
    "git tag -- newtag",
    "git tag oldtag -d",
    "git tag --delete oldtag",
    "git -c core.pager=cat tag newtag",
    "git --no-pager tag newtag",
    "git -C . tag newtag",
    "git tag $(date +%F)",
    "git tag --sort=-creatordate newtag",
    "git tag -l && git tag newtag",
]
#: Gap 3.
_REVERT = [
    "git revert HEAD",
    "git revert abc123",
    "git revert --no-edit HEAD",
    "git revert -n HEAD~2..HEAD",
    "git revert -m 1 abc123",
    "git revert --continue",
    "git revert --skip",
    "git revert --abort",
    "git revert --quit",
    "git -C . revert HEAD",
    "git -c core.pager=cat revert HEAD",
]
#: Gap 4. Denied in a linked worktree too.
_FOREACH = [
    "git submodule foreach 'echo hi'",
    "git submodule 'foreach' x",
    "git submodule foreach --recursive 'pwd'",
    "git submodule foreach 'git status --short'",
    "git submodule foreach 'rm -rf build'",
    "git submodule --quiet foreach 'true'",
    "git submodule --quiet foreach --recursive 'true'",
    "git -C sub submodule foreach 'make'",
    "git -c core.pager=cat submodule foreach 'echo hi'",
    "git submodule foreach \"touch {out}\"",
    "git submodule foreach 'touch {out}' && git status",
    "cd sub && git submodule foreach 'echo hi'",
    # the internal helper runs the same command (measured: `git submodule--helper
    # foreach 'touch x'` runs it), and the dashed spelling is the same program
    "git submodule--helper foreach true",
    "git submodule--helper foreach --quiet true",
    "git submodule--helper foreach --recursive 'touch {out}'",
    "git -C sub submodule--helper foreach 'make'",
    "git -c core.pager=cat submodule--helper foreach true",
    "git-submodule--helper foreach true",
]
#: `git submodule--helper create-branch` creates or moves a branch ref (measured:
#: `-f` resets `topic` to the given oid), so it is `branch -f`'s twin: denied in the
#: shared tree in any form, passed from a linked worktree.
_HELPER_CREATE_BRANCH = [
    "git submodule--helper create-branch -f topic abc123 main",
    "git submodule--helper create-branch --force topic abc123 main",
    "git submodule--helper create-branch newb abc123 main",
    "git submodule--helper create-branch --create-reflog newb abc123 main",
    "git submodule--helper create-branch -n newb abc123 main",
    "git submodule--helper create-branch -t newb abc123 main",
    "git submodule--helper --quiet create-branch newb abc123 main",
    "git submodule--helper 'create-branch' newb abc123 main",
    "git -C sub submodule--helper create-branch newb abc123 main",
    "git-submodule--helper create-branch newb abc123 main",
]
#: The helper gets the write set `git submodule` has (init, sync, add, deinit,
#: set-url, set-branch, absorbgitdirs, update --init) plus its own `clone`. Denied
#: in a linked worktree too, like `git submodule init`: they write the common config.
_SUBMODULE_HELPER = [
    "git submodule--helper init",
    "git submodule--helper sync",
    "git submodule--helper deinit --all",
    "git submodule--helper add u p",
    "git submodule--helper set-url p u",
    "git submodule--helper set-branch -b x p",
    "git submodule--helper absorbgitdirs",
    "git submodule--helper update --init",
    "git submodule--helper clone --url u --path p",
    "git -C sub submodule--helper init",
    "git submodule--helper --quiet init",
    "git-submodule--helper init",
]
#: Gap 5.
_PULL = [
    "git pull",
    "git pull origin main",
    "git pull origin",
    "git pull --rebase",
    "git pull --rebase origin main",
    "git pull -r",
    "git pull --ff-only",
    "git pull --no-commit",
    "git pull --no-rebase origin main",
    "git -C . pull",
    "git -c core.pager=cat pull",
    "git-pull",
    "git pull; git status",
]
#: Gap 6: a refspec with a local destination.
_FETCH_REFSPEC = [
    "git fetch origin main:fetched",
    "git fetch origin +main:topic",
    "git fetch origin +refs/heads/*:refs/heads/*",
    "git fetch origin refs/heads/main:refs/remotes/origin/main",
    "git fetch origin a:b c:d",
    "git fetch origin main main:fetched",
    "git fetch --update-head-ok origin main:main",
    "git fetch -u origin main:main",
    "git fetch -- origin main:fetched",
    "git fetch --depth 1 origin main:fetched",
    "git fetch --depth=1 origin main:fetched",
    "git fetch -p origin main:fetched",
    "git fetch --filter blob:none origin main:fetched",
    "git fetch origin tag peer-tag",
    "git fetch origin 'refs/tags/*:refs/tags/*'",
    "git fetch --prune-tags origin",
    "git fetch -P origin",
    "git -c core.pager=cat fetch origin main:fetched",
    "git -C . fetch origin main:fetched",
    "git fetch . main:fetched",
    # a short option fetch does not have (merge's `-s`, `-X`) takes no value here
    "git fetch -s origin main:fetched",
    "git fetch -X origin main:fetched",
    "git fetch -o x origin main:fetched",
    "git fetch -j4 origin main:fetched",
    # --refmap and --stdin hand fetch a refspec (measured: both write a local branch);
    # git takes a unique prefix of each (`--refm`, `--st`)
    "git fetch --refmap='+refs/heads/*:refs/heads/*' origin other",
    "git fetch --refmap '+refs/heads/*:refs/heads/*' origin other",
    "git fetch --refma=+refs/heads/*:refs/heads/* origin other",
    "git fetch --refm=+refs/heads/*:refs/heads/* origin other",
    "git fetch origin other --refmap=+a:b",
    "git fetch --refmap= origin other",
    "git fetch --stdin origin",
    "git fetch --stdi origin",
    "git fetch --std origin",
    "git fetch --st origin",
    "echo other:viastdin | git fetch --stdin origin",
    "git fetch --stdin origin < refs.txt",
    # a refspec held in a variable or substitution is unreadable text
    'git fetch origin "$R"',
    "git fetch origin $R",
    "git fetch origin ${R}",
    'git fetch "$REMOTE" "$BRANCH"',
    "git fetch origin $(git rev-parse --abbrev-ref HEAD)",
    'git fetch origin "$(git rev-parse --abbrev-ref HEAD)"',
    "git fetch -q origin $B",
    "git fetch --depth 1 origin $B",
    "R=main:fetched; git fetch origin $R",
    # a remote's own fetch/push refspec set on the command line or in the environment
    "git -c remote.origin.fetch=+refs/heads/*:refs/heads/* fetch origin",
    "git -c 'remote.origin.fetch=+refs/heads/other:refs/heads/zz' fetch origin",
    'git -c "remote.origin.fetch=+refs/heads/other:refs/heads/zz" fetch origin',
    "git -c remote.origin.FETCH=+refs/heads/o:refs/heads/z fetch origin",
    "git -c remote.a.b.fetch=+refs/heads/o:refs/heads/z fetch a.b",
    "git -c remote.origin.push=HEAD:refs/heads/x push origin",
    "git -c remote.origin.push=HEAD:refs/heads/x fetch origin",
    "git -c core.pager=cat -c remote.origin.fetch=+a:b fetch origin",
    "git --config-env=remote.origin.fetch=V fetch origin",
    "git --config-env remote.origin.fetch=V fetch origin",
    "git --config-env=remote.origin.push=V push origin",
    "git -c remote.origin.fetch=+refs/heads/*:refs/heads/* pull",
    "git -c remote.origin.fetch=+refs/heads/*:refs/heads/* remote update",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.fetch GIT_CONFIG_VALUE_0=+refs/heads/o:refs/heads/z git fetch origin",
    "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push; git push origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.fetch GIT_CONFIG_VALUE_0=+refs/heads/o:refs/heads/z git-fetch origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.fetch GIT_CONFIG_VALUE_0=+refs/heads/o:refs/heads/z /usr/lib/git-core/git-fetch origin",
    "git -c remote.origin.fetch=+refs/heads/o:refs/heads/z /usr/lib/git-core/git-fetch origin",
    "GIT_CONFIG_PARAMETERS=\"'remote.origin.fetch=+refs/heads/o:refs/heads/z'\" git fetch origin",
]
#: Gap 7. Denied in a linked worktree too.
_OUTPUT = [
    "git diff --output={out}",
    "git diff --output {out}",
    "git diff --stat --output={out}",
    "git diff HEAD~1 --output={out}",
    "git diff HEAD~1 -- f.txt --output={out}",
    "git log --output={out}",
    "git log -p --output {out}",
    "git log --oneline -3 --output={out}",
    "git show --output={out} HEAD",
    "git show HEAD --output={out}",
    "git show --output {out} HEAD",
    "git diff-tree -p --output={out} HEAD",
    "git stash show -p --output={out}",
    "git archive --output={out} HEAD",
    "git -C . diff --output={out}",
    "git -c core.pager=cat log --output={out}",
    # quoting: the shell removes the quotes, and so must the rule
    'git diff "--output={out}"',
    "git diff '--output' {out}",
    'git diff --output"={out}"',
    'git diff --out"put"={out}',
    "git diff --'output'={out}",
    "git log -p '--output={out}'",
    "git show \"--output\" {out} HEAD",
    # `--` ends the options, but a reviewer counted a `--output` after it as a hold:
    # it stays denied for diff, log and show
    "git log -- --output={out}",
    "git diff -- --output {out}",
    # a value-taking option hides only the word after it
    "git log -S x --output={out}",
    "git log --grep x --output {out}",
    "git log -S 'a b' --output={out}",
    'git log --grep "a b" --output={out}',
    "git log --author x --format=%h --output={out}",
    "git log -pS x --output={out}",
    # `--format` and `--pretty` take an attached value only: the next word is an option
    "git log --format --output={out}",
    "git log --pretty --output {out}",
    "git log --format '--output'",
    # N1: a `git grep` carries no --output of its own, but a substitution inside it
    # runs a command of its own (measured: each writes the file)
    "git grep -n 1 $(git diff --output={out})",
    "git grep -n 1 `git diff --output={out}`",
    'git grep -e "$(git diff --output={out})"',
    "git grep x -- $(git log --output={out})",
    "git grep x <(git diff --output={out})",
    "echo $(git grep x) $(git diff --output={out})",
    "git grep x $(git diff --output={out})",
    "git grep x; git diff --output={out}",
    "git grep x && git diff --output={out}",
    "git grep x | git diff --output={out}",
    "(git grep x; git diff --output={out})",
    "{ git grep x; git diff --output={out}; }",
    # N2: the value-skip table was measured on log and show only. These commands
    # really write (git diff -L / --decorate-refs do not take the next word), so
    # any --output word denies there, a value-taking option's value included
    "git diff -L --output={out}",
    "git diff --decorate-refs --output={out}",
    "git diff --cached -L --output={out}",
    "git diff --no-index f f --default --output={out}",
    "git diff-tree HEAD -L --output={out}",
    "git diff-tree HEAD --decorate-refs --output={out}",
    "git diff-index HEAD -L --output={out}",
    "git diff-index HEAD --decorate-refs --output={out}",
    "git diff-files -L --output={out}",
    "git difftool -L --output={out}",
    "git difftool --decorate-refs --output={out}",
    "git range-diff HEAD~1 HEAD HEAD --default --output={out}",
    "git diff -S --output={out}",
    "git diff -G '--output'",
    "git stash show -S '--output'",
]

#: Pass in the shared tree and in a worktree: nothing here writes a ref, a
#: commit, a file or a command.
_STAYS_ALLOWED = [
    "git branch",
    "git branch -a",
    "git branch -r",
    "git branch -vv",
    "git branch --list 'feat*'",
    "git branch --contains HEAD",
    "git branch --sort=-committerdate",
    "git branch --format='%(refname:short)'",
    "git branch --show-current",
    "git tag",
    "git tag -l",
    "git tag --list",
    "git tag -l 'v*'",
    "git tag --list 'v*' --sort=-v:refname",
    "git tag -n",
    "git tag -n5",
    "git tag -l -n1",
    "git tag --contains HEAD",
    "git tag --no-contains HEAD",
    "git tag --merged main",
    "git tag --no-merged main",
    "git tag --points-at HEAD",
    "git tag --sort=-creatordate",
    "git tag --sort -creatordate",
    "git tag --format='%(refname:short)'",
    "git tag -v oldtag",
    "git tag --verify oldtag",
    "git tag | head",
    "git tag --column",
    "git log --oneline",
    "git log --oneline -5 -- docs",
    "git log -p --stat",
    "git diff --stat",
    "git diff",
    "git diff HEAD~1 -- f.txt",
    "git show HEAD",
    "git show HEAD:f.txt",
    "git show --stat HEAD",
    "git diff --output-indicator-new=+ --output-indicator-old=-",
    "git log -p --output-indicator-new=+",
    "git show --output-indicator-context=' ' HEAD",
    "git log -- docs/output.md",
    # `git grep` has no --output option, and a value-taking option's value is data
    "git grep -n '--output'",
    "git grep -n -e '--output' -- docs",
    "git grep -- --output",
    "git grep --output",
    "git grep \"--output=x y\"",
    "git -C . grep -e --output",
    "git log -S '--output'",
    "git log -S--output",
    "git log -pS '--output'",
    "git log --grep '--output' --oneline",
    "git log -G '--output' --oneline",
    "git log --author '--output'",
    "git log --grep \"--output x\" --oneline",
    "git show -S '--output' HEAD",
    'git log "--format=%h %s" -3',
    "git log --grep=--output --oneline",
    "git log --format='%h %s' --output-indicator-new=+",
    "hub pull-request",
    "hub pull-request -m 'x'",
    "git fetch",
    "git fetch origin",
    "git fetch --all",
    "git fetch --tags origin",
    "git fetch origin main",
    "git fetch --depth 1 origin",
    "git fetch --depth=1 origin main",
    "git fetch --filter=blob:none origin",
    "git fetch --filter blob:none origin",
    "git fetch $REMOTE",
    'git fetch "$REMOTE"',
    "git fetch --refetch origin",  # not a prefix of --refmap
    "git fetch --shallow-since=2020-01-01 origin",
    # only remote.<name>.fetch|push keys are read; the general `-c` class is not
    "git -c remote.origin.tagopt=--tags fetch origin",
    "git -c remote.origin.url=x fetch origin",
    "git -c remote.origin.pushurl=x fetch origin",
    "git -c core.pager=cat fetch origin",
    "git -c core.pager=cat log --oneline",
    "git submodule--helper status",
    "git submodule--helper summary",
    "git fetch git@github.com:Hellblazer/nexus.git",
    "git fetch https://example.invalid/r.git main",
    "git submodule status",
    "git submodule summary",
    "git status",
    "git rev-parse HEAD",
]


# ---------------------------------------------------------------------------
# Shared tree: all seven deny.
# ---------------------------------------------------------------------------

_FAMILIES = {
    "1_branch_force": _BRANCH_FORCE,
    "2_tag_writes": _TAG_WRITES,
    "3_revert": _REVERT,
    "4_submodule_foreach": _FOREACH,
    "5_pull": _PULL,
    "6_fetch_refspec": _FETCH_REFSPEC,
    "7_output": _OUTPUT,
    "9_helper_create_branch": _HELPER_CREATE_BRANCH,
}
#: Denied in a linked worktree too.
_EVERYWHERE = {
    "4_submodule_foreach": _FOREACH,
    "7_output": _OUTPUT,
    "8_submodule_helper": _SUBMODULE_HELPER,
}


@pytest.mark.parametrize("family", sorted({**_FAMILIES, **_EVERYWHERE}))
def test_each_gap_denies_in_the_shared_tree(family, sandbox):
    cmds = [c.replace("{out}", "/tmp/out.txt") for c in {**_FAMILIES, **_EVERYWHERE}[family]]
    leaked = _not_denied(cmds, sandbox.main)
    assert not leaked, f"{family}: not denied in the shared tree: {leaked!r}"


# ---------------------------------------------------------------------------
# Linked worktree: 1, 2, 3, 5, 6 pass (the cwd rule); 4 and 7 still deny.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", [
    "1_branch_force", "2_tag_writes", "3_revert", "5_pull", "6_fetch_refspec",
    "9_helper_create_branch",
])
def test_gaps_that_are_the_worktrees_own_pass_in_a_linked_worktree(family, sandbox):
    cmds = list(_FAMILIES[family])
    # `git fetch --prune-tags` and the like are text the worktree rule never sees
    blocked = _denied_among(cmds, sandbox.wt)
    assert not blocked, f"{family}: wrongly denied in a linked worktree: {blocked!r}"


@pytest.mark.parametrize("family", sorted(_EVERYWHERE))
def test_foreach_output_and_the_submodule_helper_deny_in_a_linked_worktree_too(family, sandbox):
    cmds = [c.replace("{out}", "/tmp/out.txt") for c in _EVERYWHERE[family]]
    leaked = _not_denied(cmds, sandbox.wt)
    assert not leaked, f"{family}: not denied in a linked worktree: {leaked!r}"


def test_no_false_denials_in_the_shared_tree_or_a_linked_worktree(sandbox):
    assert _denied_among(_STAYS_ALLOWED, sandbox.main) == []
    assert _denied_among(_STAYS_ALLOWED, sandbox.wt) == []


def test_an_undeterminable_tree_fails_closed_for_revert_and_pull(tmp_path):
    """Revert and pull change the tree, so "could not prove this is not shared"
    does not earn a pass: a cwd that is no repository is denied like the primary."""
    nowhere = tmp_path / "not-a-repo"
    nowhere.mkdir()
    for cmd in ("git revert HEAD", "git revert --abort", "git pull", "git pull --rebase origin main"):
        verdict = _verdict(cmd, nowhere)
        assert verdict.get("permissionDecision") == "deny", cmd
        assert "FAIL CLOSED" in verdict["permissionDecisionReason"], cmd


# ---------------------------------------------------------------------------
# The `# routing-allow:` escape.
# ---------------------------------------------------------------------------


def test_the_routing_allow_escape_passes_every_gap_in_both_trees(sandbox):
    """The escape sits before the worktree rule, so it passes foreach and
    --output too, even from a linked worktree: it is the orchestrator's
    deliberate exception, auditable in the routing log, as for every verb."""
    cmds = [
        "git branch -f topic HEAD~1  # routing-allow: orchestrator asked",
        "git tag newtag  # routing-allow: orchestrator asked",
        "git revert HEAD  # routing-allow: orchestrator asked",
        "git submodule foreach 'echo hi'  # routing-allow: orchestrator asked",
        "git pull  # routing-allow: orchestrator asked",
        "git fetch origin main:fetched  # routing-allow: orchestrator asked",
        "git diff --output=/tmp/out.txt  # routing-allow: orchestrator asked",
    ]
    assert _denied_among(cmds, sandbox.main) == []
    assert _denied_among(cmds, sandbox.wt) == []
    # control: the same text without the escape denies in the worktree for 4 and 7
    assert _denied("git submodule foreach 'echo hi'", sandbox.wt)
    assert _denied("git diff --output=/tmp/out.txt", sandbox.wt)


# ---------------------------------------------------------------------------
# The `git -C <linked worktree>` carve-out is NOT widened.
# ---------------------------------------------------------------------------


def test_the_dash_c_carve_out_admits_revert_continue_only_and_none_of_the_new_forms(sandbox):
    """`_DASH_C_FLAGS` is unchanged. `revert` was already listed with `--continue`
    (not `--abort`/`--skip`/`--quit`, which discard a stopped revert's work);
    `branch` is create-only, `tag`, `pull` and `fetch` are not listed, and
    `foreach` / `--output` are refused by the side-effect rule inside the carve-out."""
    wt = str(sandbox.wt)
    assert not _denied(f"git -C {wt} revert --continue", sandbox.main)
    assert not _denied(f"git -C {wt} revert HEAD", sandbox.main)
    denied = [
        f"git -C {wt} revert --abort",
        f"git -C {wt} revert --skip",
        f"git -C {wt} revert --quit",
        f"git -C {wt} branch -f topic HEAD~1",
        f"git -C {wt} branch --force topic HEAD~1",
        f"git -C {wt} tag newtag",
        f"git -C {wt} tag -f oldtag",
        f"git -C {wt} pull",
        f"git -C {wt} fetch origin main:fetched",
        f"git -C {wt} submodule foreach 'echo hi'",
        f"git -C {wt} submodule--helper foreach true",
        f"git -C {wt} submodule--helper init",
        f"git -C {wt} submodule--helper create-branch -f topic abc123 main",
        f"git -C {wt} diff -L --output=/tmp/out.txt",
        f"git -C {wt} fetch --refmap=+a:b origin other",
        f"git -C {wt} fetch --stdin origin",
        f"git -C {wt} -c remote.origin.fetch=+a:b fetch origin",
        f"git -C {wt} diff --output=/tmp/out.txt",
        f"git -C {wt} log --output=/tmp/out.txt",
    ]
    assert _not_denied(denied, sandbox.main) == []


# ---------------------------------------------------------------------------
# Each denied form, run for real, changes state; each control changes none.
# ---------------------------------------------------------------------------

_REAL_WRITES = [
    "git branch -f topic HEAD~1",
    "git branch --force topic HEAD~1",
    "git tag newtag",
    "git tag -a -m msg annot",
    "git tag -f oldtag HEAD~1",
    "git revert --no-edit HEAD",
    "git pull --ff-only",
    "git pull --rebase",
    "git fetch origin main:fetched",
    "git fetch origin +main:topic",
    "git fetch origin tag peer-tag",
    "git fetch --prune-tags origin",  # fetches every tag; with --prune it deletes local ones
    # fetch handed a refspec by an option, the environment or a variable
    "git fetch --refmap='+refs/heads/*:refs/heads/*' origin other",
    "git fetch --refmap '+refs/heads/*:refs/heads/*' origin other",
    "git fetch --refm='+refs/heads/*:refs/heads/*' origin other",
    "printf 'other:viastdin\\n' | git fetch --stdin origin",
    "printf 'other:viastdin\\n' | git fetch --st origin",
    "git -c 'remote.origin.fetch=+refs/heads/other:refs/heads/zz' fetch origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.fetch GIT_CONFIG_VALUE_0='+refs/heads/other:refs/heads/yy' git fetch origin",
    "V='+refs/heads/other:refs/heads/ww' git --config-env=remote.origin.fetch=V fetch origin",
    "git -c remote.origin.fetch=+refs/heads/other:refs/heads/zz remote update",
    'R=main:fetched; git fetch origin "$R"',
    "git diff --output={out}",
    "git diff --output {out}",
    'git diff "--output={out}"',
    'git diff --output"={out}"',
    "git log -p --output={out}",
    "git log --output {out}",
    "git log --format --output={out}",
    "git log --pretty --output {out}",
    "git grep -n 1 $(git diff --output={out})",
    "git grep -n 1 `git diff --output={out}`",
    "git diff -L --output={out}",
    "git diff --decorate-refs --output={out}",
    "git diff-tree HEAD -L --output={out}",
    "git submodule--helper create-branch -f topic $(git rev-parse HEAD~1) main",
    "git submodule--helper create-branch newbr HEAD main",
    "git show --output={out} HEAD",
    "git show HEAD --output {out}",
]


@pytest.mark.parametrize("cmd", _REAL_WRITES, ids=[c.replace(" ", "_")[:48] for c in _REAL_WRITES])
def test_each_denied_form_really_changes_state(cmd, sandbox):
    real = cmd.replace("{out}", str(sandbox.box / "out.txt"))
    before = sandbox.state()
    sandbox.run(real)
    assert sandbox.state() != before, f"{real!r} changed nothing in the sandbox"
    assert _denied(real, sandbox.main), f"{real!r} was not denied in the shared tree"


def test_foreach_really_runs_a_command_in_a_submodule_and_denies(sandbox):
    sub = sandbox.box / "sub"
    sub.mkdir()
    sandbox.g(sub, "init", "-q", "-b", "main")
    (sub / "s.txt").write_text("s\n")
    sandbox.g(sub, "add", "s.txt")
    sandbox.g(sub, "commit", "-q", "-m", "s")
    sandbox.g(
        sandbox.main, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sm",
    )
    for i, verb in enumerate(("submodule foreach", "submodule--helper foreach")):
        marker = sandbox.box / f"ran{i}.txt"
        cmd = f"git {verb} 'touch {marker}'"
        sandbox.run(cmd)
        assert marker.exists(), f"{verb} did not run the command"
        for cwd in (sandbox.main, sandbox.wt):
            assert _denied(cmd, cwd), f"{verb} was not denied in {cwd.name}"
    # and the helper's `init` really writes the common config, as `submodule init` does
    cfg = sandbox.main / ".git" / "config"
    sandbox.g(sandbox.main, "config", "--remove-section", "submodule.sm")
    before = cfg.read_text()
    sandbox.run("git submodule--helper init")
    assert cfg.read_text() != before, "submodule--helper init wrote nothing"
    for cwd in (sandbox.main, sandbox.wt):
        assert _denied("git submodule--helper init", cwd), f"helper init was not denied in {cwd.name}"


def test_plain_fetch_writes_only_remote_tracking_refs_and_followed_tags_and_passes(sandbox):
    """The verdict on `git fetch origin` is unchanged (allowed): it writes
    `refs/remotes/origin/*`, and git auto-follows tags that point into the
    fetched history. A refspec with a local destination is what this change
    denies; an explicit `--tags` is left allowed with the auto-follow."""
    before = dict(line.split(" ") for line in sandbox.state()[0].splitlines())
    head = sandbox.state()[1]
    sandbox.run("git fetch origin")
    after = dict(line.split(" ") for line in sandbox.state()[0].splitlines())
    changed = {r for r in after if after.get(r) != before.get(r)}
    assert changed, "the fetch changed nothing: the sandbox has no news to fetch"
    assert all(r.startswith(("refs/remotes/", "refs/tags/")) for r in changed), changed
    assert sandbox.state()[1] == head
    assert not _denied("git fetch origin", sandbox.main)
    assert not _denied("git fetch origin", sandbox.wt)


_REAL_CONTROLS = [
    "git branch",
    "git branch -a",
    "git tag",
    "git tag -l",
    "git tag -l 'old*'",
    "git tag -n",
    "git tag --contains HEAD",
    "git log --oneline",
    "git diff --stat",
    "git show HEAD",
    "git diff --output-indicator-new=+ --output-indicator-old=-",
    "git log -p --output-indicator-new=+",
    # `git grep` has no --output; a value-taking option eats the word after it, so
    # a `--output` there is data, not the file writer (measured, not assumed)
    "git grep -n -e '--output'",
    "git grep -n -- '--output'",
    "git log -S '--output' --oneline",
    "git log -S --output={out}",
    "git log --grep '--output' --oneline",
    "git log --grep --output={out}",
    "git log --author --output={out}",
    "git log -G '--output' --oneline",
]


@pytest.mark.parametrize("cmd", _REAL_CONTROLS, ids=[c.replace(" ", "_")[:48] for c in _REAL_CONTROLS])
def test_each_control_changes_nothing_and_passes(cmd, sandbox):
    cmd = cmd.replace("{out}", str(sandbox.box / "out.txt"))
    before = sandbox.state()
    sandbox.run(cmd)
    assert sandbox.state() == before, f"control {cmd!r} changed the sandbox"
    assert not _denied(cmd, sandbox.main), f"control {cmd!r} was wrongly denied"
    assert not _denied(cmd, sandbox.wt), f"control {cmd!r} was wrongly denied in a worktree"


def test_git_really_takes_only_the_exact_output_spelling(sandbox):
    """The rule matches `--output` exactly because git does: a prefix is an
    error, not an abbreviation. Measured on git, not assumed."""
    target = sandbox.box / "abbrev.txt"
    for flag in ("--outp", "--out", "--ou"):
        sandbox.run(f"git diff {flag}={target}")
        assert not target.exists(), flag
    sandbox.run(f"git diff --output {target}")
    assert target.exists()


def test_a_command_word_held_in_a_variable_is_a_documented_limit(sandbox):
    """Known limit, pinned so closing it is deliberate: a whole-word variable that
    holds the verb or an option (`V=tag; git $V v1`) is not read, as for every verb
    of this guard. Only a fetch refspec word is read as opaque (above)."""
    cmds = ["V=tag; git $V v1", 'V=tag; git "$V" v1', "F=-f; git branch $F topic HEAD~1"]
    assert _denied_among(cmds, sandbox.main) == []
    assert _denied_among(cmds, sandbox.wt) == []


def test_the_deny_message_names_foreach_and_output_and_no_longer_lists_foreach_as_a_read(sandbox):
    foreach = _verdict("git submodule foreach 'echo hi'", sandbox.main)["permissionDecisionReason"]
    assert "submodule foreach" in foreach, foreach
    assert "status|summary|foreach" not in foreach, "foreach is no longer a read"
    in_wt = _verdict("git submodule foreach 'echo hi'", sandbox.wt)["permissionDecisionReason"]
    assert "foreach" in in_wt and "runs" in in_wt, in_wt
    assert "shares this config" not in in_wt, "foreach is not a config-sharing verb"
    out = _verdict("git diff --output=/tmp/o", sandbox.wt)["permissionDecisionReason"]
    assert "--output" in out and "writes a file" in out, out
    assert "shares this config" not in out, out
    assert "git's own file writer" in out and "redirection" in out, out
    assert "redirect the output" not in out and "`>` into your scratch" not in out, out


def test_a_named_fetch_passes_and_a_variable_fetch_says_to_write_the_name_out(sandbox):
    """A literal named fetch (no `src:dst`) stays allowed in the shared tree; only an unexpanded word after the
    remote denies, and its message tells the agent the passing shape (halcyon, 2026-11-19: the guard must not
    cost ordinary fetches)."""
    for cmd in ("git fetch origin develop", "git fetch -q origin main", "git fetch origin feature/nexus-abc-x",
                "git fetch origin refs/heads/develop", "git fetch origin develop main"):
        assert _verdict(cmd, sandbox.main).get("permissionDecision") != "deny", cmd
    for cmd in ('git fetch origin "$BRANCH"', "git fetch origin $B", "git fetch origin ${B}"):
        reason = _verdict(cmd, sandbox.main)["permissionDecisionReason"]
        assert "write the branch name out" in reason, (cmd, reason)
        assert "git fetch origin develop" in reason, reason
    plain = _verdict("git fetch origin a:b", sandbox.main)["permissionDecisionReason"]
    assert "write the branch name out" not in plain, plain
