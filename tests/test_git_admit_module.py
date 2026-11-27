# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admission module (``_git_admit``, nexus-5y47c) read in process: its lexer, its
variable tracking and its refusals, with the guard's own proofs replaced by stubs.

The end-to-end tests (``test_routing_subagent_git_admit.py``) drive the real script; these
pin the parts a subprocess verdict cannot name: WHY a command is not admitted.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import shutil
import sys
import tempfile

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
ROUTING = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
D = chr(36)
BT = chr(96)
BS = chr(92)


def _load():
    spec = importlib.util.spec_from_file_location("git_admit_under_test", ROUTING / "_git_admit.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load()

#: A stand-in for the unchanged guard: git as a command word, then a write verb anywhere after.
_LEGACY = re.compile(r"(?<![\w.-])git\b.*\b(?:checkout|add|commit|push|stash|reset|merge|rebase|restore|clean|rm|mv|switch|cherry-pick)\b", re.S)
_VERB_WORD = re.compile(r"\b(?:checkout|add|commit|push|stash|reset|merge|rebase|restore|clean|rm|mv|switch|cherry-pick)\b")
ROOT = "/wt/root"
OTHER = "/wt/other"


def _hooks(*, branch: str | None = "feature-x", roots: tuple[str, ...] = (ROOT, OTHER)):
    return A.Hooks(
        legacy_denies=lambda text: _LEGACY.search(text) is not None,
        is_wt_root=lambda p: p in roots,
        branch_of=lambda p: branch,
        safe_config_key=lambda k: k in ("user.name", "user.email"),
        has_verb_word=lambda text: _VERB_WORD.search(text) is not None,
        has_git_word=lambda text: re.search(r"(?<![\w.-])git(?![\w.-])", text) is not None,
    )


def _kind(cmd: str, **kw) -> str | None:
    adm = A.analyze(cmd, _hooks(**kw))
    return None if adm is None else adm.kind


@pytest.mark.parametrize("cmd", [
    "git stash list <<'EOF'\nx\nEOF",
    "git stash list <<< x",
    "git stash list " + BT + "echo" + BT,
    "git stash list # comment",
    "git stash list " + BS + "\n--oneline",
    "{ git stash list; }",
    "git stash list &",
    "git stash list |& cat",
    "git stash list >" + D + "(cat)",
    "git stash list <(echo x)",
    "git stash list > f 2>&" + "x",
    "git stash list " + D + "((1+1))",
    "git stash list " + D + "'x'",
    "echo " + D + "1",
    "echo " + D + "{x:-y}",
    "echo !x",
    "git stash list;; echo",
    "git stash list" + chr(13),
    "echo caf" + chr(233),
    "git stash list 'unterminated",
    'git stash list "unterminated',
    "git stash list " + D + "(echo",
    "=cmd x",
    "git stash list " + BS + "x",
    "echo " + chr(34) + BT + "x" + BT + chr(34),
    "echo " + chr(34) + BS + "\nx" + chr(34),
    "( ( git stash list ) )x",
    "(git stash list) x",
    "git -C " + D + "W:h log",
    "git log " + D + "x[1]",
    "git log " + chr(34) + D + "x:t" + chr(34),
])
def test_a_construct_the_lexer_does_not_read_admits_nothing(cmd):
    assert _kind(cmd) is None


@pytest.mark.parametrize("cmd", [
    "git stash list",
    "git stash list | head -1",
    "git log --grep='git commit' 2>&1 | tail -3",
    "git merge-base A " + D + "m",
    "git merge-base A " + D + "{m}",
    "git merge-base A " + D + "?",
    "echo " + chr(34) + "git add " + D + "x" + chr(34),
    "grep -n " + chr(34) + "a" + BS + "|b" + chr(34) + " f",
    "git diff " + D + "(git merge-base A B) B",
    "(git stash list; git status)",
    "git stash list >/dev/null 2>&1",
    "git stash list &>/dev/null",
    "git stash list 1>&2",
    "git stash list &&\n  git status",
    "for x in a b; do git merge-base " + D + "x HEAD; done",
    "if git merge-base A B; then echo y; else echo n; fi",
    "git show HEAD:f > /tmp/copy",
    "git show HEAD:f > " + D + "S/copy",
])
def test_a_command_the_lexer_reads_is_admitted_as_a_read(cmd):
    assert _kind(cmd) == "read"


def test_a_variable_is_read_back_into_the_segments_that_use_it():
    assert _kind("X='git checkout f'; sh -c " + chr(34) + D + "X" + chr(34)) is None
    assert _kind("X=checkout; git " + D + "X f") is None
    assert _kind("X=check; Y=out; git " + D + "X" + D + "Y f") is None
    assert _kind("git status && X=checkout; git " + D + "X f") is None
    assert _kind("true || X=checkout; git " + D + "X f") is None
    assert _kind("export X=checkout; git " + D + "X f") is None
    assert _kind("declare -x X=checkout; git " + D + "X f") is None
    assert _kind("for v in checkout; do git " + D + "v f; done") is None
    assert _kind("printf -v X checkout; git " + D + "X f") is None


def test_a_binding_cannot_reach_a_path_it_has_not_proven():
    assert _kind("W=" + ROOT + "; git -C " + D + "W add -A") == "worktree"
    assert _kind("W=" + ROOT + "; git -C " + D + "W add -A && git -C " + D + "W commit -m x") == "worktree"
    assert _kind("git status && W=" + ROOT + "; git -C " + D + "W add -A") is None  # maybe bound
    assert _kind("W=/not/a/worktree; git -C " + D + "W add -A") is None
    assert _kind("git -C " + D + "UNSET add -A") is None
    assert _kind("W=" + ROOT + "; W=" + OTHER + "x; git -C " + D + "W add -A") is None


@pytest.mark.parametrize("name", ["PATH", "IFS", "HOME", "GIT_DIR", "GIT_INDEX_FILE", "LD_PRELOAD", "PYTHONPATH", "BASH_ENV"])
def test_an_assignment_that_changes_how_a_later_command_runs_admits_nothing(name):
    assert _kind(name + "=/x; git stash list") is None


def test_an_executor_anywhere_withholds_the_exemptions():
    assert _kind("git stash list; python3 -c 'x'") is None
    assert _kind("git stash list | python3 -c 'x'") is None
    assert _kind("git stash list; perl -e x") is None
    assert _kind("git stash list; eval foo") is None
    assert _kind("git stash list; xargs foo") is None
    assert _kind("git stash list; bash x.sh") is None
    assert _kind("git stash list\n. ./s") is None
    assert _kind("git stash list; sed -i s/a/b/ f") is None
    assert _kind("git stash list; sed -n '1,5p' f") == "read"


def test_a_pipeline_that_carries_verb_text_into_a_command_is_refused():
    assert _kind("echo 'git checkout f' | sh") is None
    assert _kind("echo 'git checkout f' | grep x | sh") is None
    assert _kind("git stash list | sh") is None
    assert _kind("git merge-base A B | tee out") is None
    assert _kind("git merge-base A B > out; cat out") is None
    assert _kind("git stash list > out") is None
    assert _kind("echo 'git checkout' > out") is None
    assert _kind("git show HEAD:f > out") == "read"


def test_cd_sets_a_directory_only_along_an_and_run():
    assert _kind("cd " + ROOT + " && git add -A") == "worktree"
    assert _kind("cd " + ROOT + " && git status && git add -A && git commit -m x") == "worktree"
    assert _kind("cd " + ROOT + " || exit 1; git add -A; git commit -m x") is None  # Sam's ruling: `&&` only
    assert _kind("cd " + ROOT + " || exit 1\ngit add -A") is None
    assert _kind("cd " + ROOT + "; git add -A") is None
    assert _kind("cd " + ROOT + "\ngit add -A") is None
    assert _kind("cd " + ROOT + " || echo no; git add -A") is None
    assert _kind("cd " + ROOT + " && git status; git add -A") is None
    assert _kind("cd " + ROOT + " || exit 1; cd " + OTHER + " && git status; git add -A") is None
    assert _kind("cd " + ROOT + " | cat; git add -A") is None
    assert _kind("cd " + ROOT + "/sub && git add -A") is None
    assert _kind("cd /elsewhere && git add -A") is None
    assert _kind("cd " + ROOT + " && cd /elsewhere && git add -A") is None
    assert _kind("cd " + ROOT + " && (git add -A && git commit -m x)") == "worktree"
    assert _kind("cd " + ROOT + " && (git add -A; git commit -m x)") is None
    assert _kind("cd " + ROOT + " && (cd /elsewhere; git add -A)") is None
    assert _kind("(cd " + ROOT + " && git add -A); git commit -m x") is None


def test_a_depth_limit_and_a_segment_limit_refuse_rather_than_scan():
    deep = "echo " + D + "(" + D + "(" + D + "(" + D + "(echo x))))"
    assert _kind(deep) is None
    many = "; ".join(["echo x"] * (A.MAX_SEGMENTS + 5))
    assert _kind(many) is None
    assert _kind("echo " + "x" * (A.MAX_COMMAND + 1)) is None


def test_the_push_shape_names_the_checked_out_branch_only():
    ok = "git -C " + ROOT + " push -u origin HEAD"
    assert _kind(ok) == "push"
    assert _kind("git -C " + ROOT + " push origin feature-x") == "push"
    assert _kind("git -C " + ROOT + " push origin feature-x:feature-x") == "push"
    assert _kind("git -C " + ROOT + " push origin HEAD:refs/heads/feature-x") == "push"
    assert _kind(ok, branch="develop") is None
    assert _kind(ok, branch="main") is None
    assert _kind(ok, branch="master") is None
    assert _kind(ok, branch=None) is None
    assert _kind("git -C " + ROOT + " push origin HEAD:other") is None
    assert _kind("git -C " + ROOT + " push origin other") is None
    assert _kind("git -C " + ROOT + " push -f origin HEAD") is None
    assert _kind("git -C " + ROOT + " push upstream HEAD") is None
    assert _kind("git -C /not/a/root push origin HEAD") is None


def test_a_scratch_repository_is_only_one_the_command_created():
    box = tempfile.mkdtemp(prefix="nx-5y47c-box-")  # under the temp root the module reads
    try:
        _scratch_repository_cases(box)
    finally:
        shutil.rmtree(box, ignore_errors=True)


def _scratch_repository_cases(box: str) -> None:
    assert _kind("git init -q " + box + "/r && git -C " + box + "/r add -A") == "scratch"
    assert _kind("cd " + box + " && git init -q && git add -A") == "scratch"
    assert _kind("git -C " + box + "/r add -A") is None
    assert _kind("cd " + box + " && git add -A") is None
    assert _kind("git init -q " + box + "/r && git -C " + box + "/other add -A") is None
    assert _kind("git init -q " + box + "/r && git -C " + box + "/r config core.hooksPath x") is None
    assert _kind("git init -q " + box + "/r && git -C " + box + "/r config user.name t") == "scratch"


def test_a_destroyed_primary_shape_is_never_a_kind():
    for cmd in [
        "git checkout f", "git stash", "git restore f", "git reset --hard", "git clean -fd",
        "git add f", "git commit -m x", "git push origin HEAD", "git -C /primary add f",
        "git -C " + ROOT + " checkout f", "git -C " + ROOT + " stash", "git -C " + ROOT + " restore f",
        "git -C " + ROOT + " reset --hard", "git -C " + ROOT + " clean -fd",
        "cd " + ROOT + " && git checkout f", "cd " + ROOT + " && git stash pop",
    ]:
        assert _kind(cmd) is None, cmd


def test_the_module_stays_pure():
    """Analysis reads paths for the scratch rules and nothing else: no process, no network."""
    tree = ast.parse((ROUTING / "_git_admit.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"__future__", "os", "re", "tempfile", "typing"}, imported
    assert _kind("git stash list | head -1 && cd " + ROOT + " && git add -A") == "worktree"



def test_a_verb_word_in_a_command_beside_a_git_word_elsewhere_is_left_to_the_old_verdict():
    """An alias, a symlink or a copy turns a non-git word into git; only the two words together say so."""
    assert _kind("alias g=git; g checkout f") is None
    assert _kind("ln -s /usr/bin/git /tmp/g && /tmp/g checkout f") is None
    assert _kind("cp /usr/bin/git /tmp/g; /tmp/g add -A") is None
    assert _kind("git status; ./runner commit") is None
    assert _kind("git status; uv run pytest -k commit") is None
    # a verb word in text or in a read-only command's arguments is not that
    assert _kind("git status; echo commit") == "read"
    assert _kind("git status; ls hooks/post-merge") == "read"
    assert _kind("git status; rm -rf /tmp/scratch && mv a b") == "read"  # a verb as the command name is no argument
    # and a command with no git word at all never reached the admission
    assert _kind("uv run pytest -k commit") == "read"


W_ADD = "git -C " + ROOT + " add -A"


@pytest.mark.parametrize("before", [
    # the proof of the worktree is read before these run: each can swap the path or the cwd first
    "rm " + ROOT + "/.git && ln -s /prim/.git " + ROOT + "/.git",
    "mv " + ROOT + " " + ROOT + ".old && ln -s /prim " + ROOT,
    "ln -sfn /prim " + ROOT,
    "export GIT''_DIR=/prim/.git",
    'export GIT"_"DIR=/prim/.git',
    'declare -x GI"T_DIR"=/prim/.git',
    "export P''ATH=/tmp/evil:" + D + "PATH",
    "read x < f",
    "pushd /prim",
    "popd",
    "chdir /prim",
    "builtin cd /prim",
    "command cd /prim",
    "exec cd /prim",
    "eval cd /prim",
    "touch /tmp/y",
    "uv run pytest -q",
    "make test",
    "true || cd " + ROOT,
])
def test_a_write_admits_no_segment_that_can_change_what_the_proof_read(before):
    assert _kind(before + " && " + W_ADD) is None
    assert _kind("cd " + ROOT + " && " + before + " && git add -A") is None
    assert _kind(W_ADD + " && " + before) is None


def test_a_plain_assignment_and_an_inert_head_may_stand_beside_a_write():
    assert _kind("W=" + ROOT + "; git -C " + D + "W add -A") == "worktree"
    assert _kind("ls " + ROOT + " | head -3 && " + W_ADD) == "worktree"
    assert _kind("cd " + ROOT + " && git status && git add -A") == "worktree"


def test_a_file_write_beside_a_write_refuses():
    assert _kind(W_ADD + " && cat f > " + ROOT + "/.git") is None
    assert _kind(": > " + ROOT + "/.git && " + W_ADD) is None
    assert _kind(W_ADD + " 2>&1 | tail -3") == "worktree"
    assert _kind(W_ADD + " > /dev/null 2>&1") == "worktree"


def test_a_directory_named_as_a_command_is_a_cd_in_zsh(tmp_path):
    here = tmp_path / "prim"
    here.mkdir()
    hooks = _hooks()._replace(cwd=str(tmp_path))
    assert A.analyze("cd " + ROOT + " && " + str(here) + " && git add -A", hooks) is None
    assert A.analyze("cd " + ROOT + " && prim && git add -A", hooks) is None
    assert A.analyze("git status && " + str(here) + " && echo add", hooks) is None
    assert A.analyze("cd " + ROOT + " && git add -A", hooks) is not None


@pytest.mark.parametrize("move", [
    "git -C " + ROOT + " switch main",
    "git -C " + ROOT + " switch --create main",
    "git -C " + ROOT + " switch --create=main",
    "git -C " + ROOT + " rebase --onto main feature-x main",
    "git -C " + ROOT + " reset --soft abc1234",
    "git -C " + ROOT + " merge --ff-only abc1234",
    "git -C " + ROOT + " cherry-pick abc1234",
])
def test_a_push_beside_a_verb_that_moves_head_refuses(move):
    push = "git -C " + ROOT + " push origin HEAD"
    assert _kind(move + " && " + push) is None
    assert _kind("cd " + ROOT + " && " + move.replace("git -C " + ROOT + " ", "git ") + " && git push origin HEAD") is None
    assert _kind(push) == "push"


def test_a_value_word_that_could_split_into_flags_refuses():
    base = "git -C " + ROOT + " rebase --onto "
    assert _kind(base + "main HEAD") == "worktree"
    assert _kind(base + chr(34) + D + "X" + chr(34) + " HEAD") == "worktree"
    assert _kind("X='main --exec true'; " + base + D + "X HEAD") is None
    assert _kind(base + D + "(echo 'main --exec=true') HEAD") is None
    assert _kind(base + D + "{X} HEAD") is None
    assert _kind("git -C " + ROOT + " commit -m " + D + "MSG") is None
    assert _kind("git -C " + ROOT + " commit -m " + chr(34) + D + "MSG" + chr(34)) == "worktree"


@pytest.mark.parametrize("args", [
    "ls-remote --upload-pack='git checkout f' .",
    "ls-remote --exec='git stash' .",
    "ls-remote --upload-pa='git stash' .",
    "ls-remote --receive-pack='git add' .",
    "ls-remote ext::git-add .",
    "ls-remote --foo=add .",
    "grep -nO'git checkout f' x",
    "grep -O'git stash' x",
    "grep --open-files-in-pager='git stash' x",
    "grep --open-f='git stash' x",
    "grep --o='git stash' x",
    "grep -n add " + D + "X x",
    "help -m checkout",
    "help -w checkout",
    "help --man checkout",
    "help --web checkout",
    "diff --outp=/tmp/add HEAD",
    "log --outpu=/tmp/add",
    "show --out=/tmp/add",
])
def test_a_read_only_subcommand_that_can_run_a_program_or_write_a_file_refuses(args):
    assert _kind("git " + args) is None


@pytest.mark.parametrize("args", [
    "ls-remote --heads --tags origin",
    "ls-remote --sort=refname --refs origin",
    "ls-remote --exit-code -q origin main",
    "grep -n commit -- docs",
    "grep --only-matching -e push x",
    "grep -ne checkout x",
    "diff --output-indicator-new=+ HEAD",
    "help -a",
])
def test_the_read_only_shapes_that_do_not_take_a_program_still_pass(args):
    assert _kind("git " + args + " && echo commit") == "read"


def test_fetch_depth_and_unshallow_are_admitted_only_into_a_scratch_repository(tmp_path):
    box = tempfile.mkdtemp(prefix="nx-5y47c-box-")
    try:
        assert _kind("git fetch --depth=1 origin add") is None
        assert _kind("git fetch --unshallow origin add") is None
        assert _kind("git -C " + ROOT + " fetch --depth 1 origin add") is None
        assert _kind("git fetch -q origin add") == "read"
        assert _kind("git init -q " + box + "/r && git -C " + box + "/r fetch --depth=1 origin add") == "scratch"
    finally:
        shutil.rmtree(box, ignore_errors=True)


def test_after_an_or_the_cd_context_is_not_set():
    for pre in ("true", "ls", "git status", "cd /prim"):
        assert _kind(pre + " || cd " + ROOT + " && git add -A") is None
    assert _kind("cd " + ROOT + " && git add -A") == "worktree"


def test_only_and_chains_carry_a_write_with_a_separator_the_ruling_names():
    a = "git -C " + ROOT
    assert _kind(a + " add -A && " + a + " commit -m x") == "worktree"
    assert _kind(a + " add -A; " + a + " commit -m x") is None
    assert _kind(a + " add -A\n" + a + " commit -m x") is None
    assert _kind(a + " add -A || " + a + " commit -m x") is None
    assert _kind(a + " add -A | cat") == "worktree"


def test_a_file_write_downstream_of_verb_text_in_a_pipeline_refuses():
    assert _kind("git log --grep=add | cat > /tmp/out") is None
    assert _kind("git log --grep=add > /tmp/out") is None
    assert _kind("git log --format=%h | cat > /tmp/out") == "read"
