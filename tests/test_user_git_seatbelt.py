# SPDX-License-Identifier: AGPL-3.0-or-later
"""T4 (nexus-sby49): the git guard's user mode, ``_user_git_seatbelt``.

A user's repository (no nexus marker, no account flag) gets a short deny list
(D1, plan section 2.1) and everything else passes. Two layers:

* ``decide`` called in-process against real throwaway git repos: every verb of
  the table, in a clean tree, a dirty tree and a linked worktree, through
  ``-C``, ``-c``, a path-qualified git, and in command position after
  ``then``/``do``/``&&``/``$(...)``.
* The real hook argv through ``tests/hooks/audience_harness.py`` with an account
  home that has no flag, for the verdict envelope and the deny text.

Git-state rows pass when the state cannot be read; text rows deny regardless.
The documented gaps (``bash -c``, ``xargs``, ``find -exec``, ``eval``) are pinned
as passing so a later change to them is a visible decision.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from tests._module_seam import setattr_in

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "conexus" / "hooks" / "scripts"
ROUTING = SCRIPTS / "routing"
GUARD = ROUTING / "subagent_git_write_requires_orchestrator.py"
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"

for _p in (str(ROUTING), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import _user_git_seatbelt as sb  # noqa: E402

#: Words a user must never meet in a deny: the plan's lexicon (section 5).
LEXICON = re.compile(
    r"\bnexus-[a-z0-9]{4,}\b|RDR-\d+|\bbd\b|\bSam\b|\bHal\b|orchestrator|develop/main|"
    r"lab-remote|lab-run|\bchas\b|\bqwent\b|\bhellmini\b|Hellblazer/nexus|hook-ENFORCED|local AND cloud"
)


def _git(cwd: pathlib.Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, env=env,
    )


@pytest.fixture()
def clean_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "userrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("one\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def dirty_repo(clean_repo: pathlib.Path) -> pathlib.Path:
    (clean_repo / "f").write_text("two\n")
    (clean_repo / "untracked").write_text("u\n")
    return clean_repo


@pytest.fixture()
def pristine_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A second, clean checkout: ``dirty_repo`` is ``clean_repo`` with edits, so a test
    that needs both a clean and a dirty tree cannot use those two together."""
    repo = tmp_path / "pristine"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("one\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def linked_dirty(clean_repo: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    wt = tmp_path / "userwt"
    _git(clean_repo, "worktree", "add", "-q", "-b", "wtb", str(wt))
    (wt / "f").write_text("changed in the worktree\n")
    return wt


# ---------------------------------------------------------------------------
# Text rows: denied from the command alone, in any cwd
# ---------------------------------------------------------------------------

TEXT_DENY = [
    "git push -f",
    "git push --force origin main",
    "git push --force-with-lease",
    "git push --force-if-includes",
    "git push origin +main",
    "git push origin --delete foo",
    "git push origin :foo",
    "git push --mirror",
    "git push --prune origin",
    "git branch -D foo",
    "git branch -M a b",
    "git branch --delete --force foo",
    "git branch -d -f foo",
    "git reflog expire --all",
    "git reflog delete HEAD@{1}",
    "git gc --prune=now",
    "git prune",
    "git update-ref -d refs/heads/x",
    "git filter-branch --all",
    "git filter-repo --path x",
    "git stash drop",
    "git stash clear",
    "git worktree remove --force ../wt",
    "git worktree remove -f ../wt",
    "git config core.hooksPath /tmp/h",
    "git config core.fsmonitor x",
    "git config core.sshCommand 'ssh -v'",
    "git config include.path /tmp/x",
    "git config includeIf.gitdir:/a.path /tmp/x",
    "git config --global user.name x",
    "git config --system core.autocrlf true",
    "git config set --global user.name x",
    "git config --add core.hooksPath /x",
]

#: Each command and the guard's reaction on a user repo in ANY state.
TEXT_ALLOW = [
    "git push",
    "git push origin main",
    "git push -u origin feature",
    "git push --follow-tags",
    "git branch newb",
    "git branch -d foo",
    "git branch -f foo HEAD",
    "git branch -m old new",
    "git checkout -B foo",
    "git switch -C foo",
    "git tag v1",
    "git tag -d v1",
    "git update-ref refs/heads/x abc",
    "git worktree add ../wt",
    "git worktree remove ../wt",
    "git worktree prune",
    "git gc",
    "git stash pop",
    "git stash apply",
    "git stash list",
    "git config user.email a@b",
    "git config --worktree user.email a@b",
    "git config --get user.name",
    "git config --global --get user.name",
    "git config --list",
    "git config get user.name",
    "git -c core.hooksPath=/x status",
    "git -c user.name=x commit -m msg",
    "git commit -am x",
    "git add f",
    "git pull",
    "git fetch origin",
    "git rebase main",
    "git merge topic",
    "git cherry-pick abc",
    "git revert abc",
    "git am p.patch",
    "git mv a b",
    "git rm f",
    "git rm --cached f",
    "git reset --soft HEAD~1",
    "git reset --mixed",
    "git reset HEAD -- f",
    "git restore --staged f",
    "git checkout main",
    "git switch main",
    "git status",
    "git log --grep=reset",
    "git diff",
    "git show HEAD",
    "gh pr merge 5",
    "gh pr checkout 5",
    'echo "git reset --hard"',
    'echo "a && git reset --hard"',
    "grep -rn 'git add' .",
    "echo git push --force",
    "cat <<EOF\ngit reset --hard\nEOF",
    "cat <<'EOF'\ngit branch -D x\nEOF\ngit status",
    "# git reset --hard\ngit status",
    "git commit -m 'git push --force and git reset --hard'",
    # The usual commit-message shape: a heredoc inside a substitution, text with
    # git words and an unbalanced paren in it.
    'git commit -m "$(cat <<\'EOF\'\nfix: stop git push --force and git reset --hard (partly\nEOF\n)"',
]


@pytest.mark.parametrize("cmd", TEXT_DENY)
@pytest.mark.parametrize("where", ["clean", "none", "gone"])
def test_text_rows_deny_in_any_cwd(cmd, where, clean_repo, tmp_path):
    cwd = {"clean": str(clean_repo), "none": None, "gone": str(tmp_path / "nope")}[where]
    hit = sb.decide(cmd, cwd)
    assert hit is not None and hit.kind == "text", f"{cmd!r} in {where} cwd: {hit}"


@pytest.mark.parametrize("cmd", TEXT_ALLOW)
@pytest.mark.parametrize("state", ["clean", "dirty"])
def test_allowed_rows_pass_in_clean_and_dirty_trees(cmd, state, clean_repo, dirty_repo):
    cwd = clean_repo if state == "clean" else dirty_repo
    assert sb.decide(cmd, str(cwd)) is None, cmd


# ---------------------------------------------------------------------------
# Git-state rows: denied only with something to lose
# ---------------------------------------------------------------------------

STATE_DENY = [
    "git checkout -- f",
    "git checkout HEAD -- f",
    "git checkout HEAD f",
    "git checkout f",
    "git checkout .",
    "git checkout -f",
    "git checkout -f main",
    "git switch -f main",
    "git switch --discard-changes main",
    "git restore f",
    "git restore --source=HEAD f",
    "git restore --staged --worktree f",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git reset --merge",
    "git reset --keep HEAD",
    "git rm -f f",
    "git rm --force f",
    "git stash",
    "git stash push -m wip",
    "git stash save wip",
    "git stash -u",
]


@pytest.mark.parametrize("cmd", STATE_DENY)
def test_state_rows_deny_on_a_dirty_tree(cmd, dirty_repo):
    hit = sb.decide(cmd, str(dirty_repo))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"


@pytest.mark.parametrize("cmd", STATE_DENY)
def test_state_rows_pass_on_a_clean_tree(cmd, clean_repo):
    assert sb.decide(cmd, str(clean_repo)) is None, cmd


@pytest.mark.parametrize("cmd", STATE_DENY)
def test_the_dirty_rows_apply_in_a_linked_worktree_of_a_user_repo(cmd, linked_dirty):
    hit = sb.decide(cmd, str(linked_dirty))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"


@pytest.mark.parametrize("cmd", STATE_DENY)
@pytest.mark.parametrize("where", ["none", "gone", "not_a_repo"])
def test_state_rows_pass_when_the_state_cannot_be_read(cmd, where, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cwd = {"none": None, "gone": str(tmp_path / "nope"), "not_a_repo": str(plain)}[where]
    assert sb.decide(cmd, cwd) is None, cmd


@pytest.mark.parametrize("cmd", STATE_DENY)
def test_state_rows_pass_when_a_git_spawn_times_out(cmd, dirty_repo, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    setattr_in(monkeypatch, sb, "subprocess.run", boom)
    assert sb.decide(cmd, str(dirty_repo)) is None, cmd


@pytest.mark.parametrize("cmd", TEXT_DENY[:6])
def test_text_rows_deny_when_a_git_spawn_times_out(cmd, dirty_repo, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    setattr_in(monkeypatch, sb, "subprocess.run", boom)
    assert sb.decide(cmd, str(dirty_repo)) is not None


def test_clean_denies_only_when_its_own_dry_run_lists_something(clean_repo):
    assert sb.decide("git clean -fd", str(clean_repo)) is None
    (clean_repo / "stray").write_text("x")
    assert sb.decide("git clean -fd", str(clean_repo)) is not None
    assert sb.decide("git clean -n", str(clean_repo)) is None
    assert sb.decide("git clean --dry-run -fd", str(clean_repo)) is None
    # -x matters: an ignored file is listed only with it.
    (clean_repo / ".gitignore").write_text("ign\n")
    (clean_repo / "stray").unlink()
    (clean_repo / "ign").write_text("x")
    _git(clean_repo, "add", ".gitignore")
    assert sb.decide("git clean -fd", str(clean_repo)) is None
    assert sb.decide("git clean -fdx", str(clean_repo)) is not None


def test_untracked_files_alone_are_not_something_to_lose_for_a_discard(clean_repo):
    (clean_repo / "only-untracked").write_text("x")
    assert sb.decide("git reset --hard", str(clean_repo)) is None
    assert sb.decide("git stash", str(clean_repo)) is None
    assert sb.decide("git stash -u", str(clean_repo)) is not None


# ---------------------------------------------------------------------------
# The decider: command position, global options, nesting
# ---------------------------------------------------------------------------

POSITION_FORMS = [
    "git branch -D x",
    "cd /tmp && git branch -D x",
    "true; git branch -D x",
    "true || git branch -D x",
    "echo hi | git branch -D x",
    "(git branch -D x)",
    "{ git branch -D x; }",
    "if true; then git branch -D x; fi",
    "for i in 1 2; do git branch -D x; done",
    "if false; then echo a; else git branch -D x; fi",
    "! git branch -D x",
    "echo $(git branch -D x)",
    'echo "$(git branch -D x)"',
    "echo `git branch -D x`",
    "echo $(echo $(git branch -D x))",
    "echo a\ngit branch -D x",
    "echo a \\\n; git branch -D x",
    "X=1 git branch -D x",
    "env GIT_PAGER=cat git branch -D x",
    "env -i git branch -D x",
    "command git branch -D x",
    "exec git branch -D x",
    "/usr/bin/git branch -D x",
    "git.exe branch -D x",
    "git -C /tmp branch -D x",
    "git -c core.pager=cat branch -D x",
    "git --no-pager branch -D x",
    "git --git-dir=/x/.git branch -D x",
    "git -C /tmp -c a=b --no-pager branch -D x",
    "cat <<EOF\nhello\nEOF\ngit branch -D x",
    "git push origin main && git push --force",
    'git commit -m "$(cat <<\'EOF\'\nmsg\nEOF\n)" && git push --force',
    "git branch | while read b; do git branch -D $b; done",
    "(cd /tmp && git push -f)",
    "true;git push -f",
    "true&&git push -f",
    "\\git push --force",
    "'git' push --force",
    "echo $(git push -f)",
    'git -C "$(pwd)" push -f',
]


@pytest.mark.parametrize("cmd", POSITION_FORMS)
def test_a_denied_form_is_found_in_command_position(cmd, tmp_path):
    assert sb.decide(cmd, str(tmp_path)) is not None, repr(cmd)


NOT_COMMAND_POSITION = [
    'echo "git branch -D x"',
    "echo 'git branch -D x'",
    "grep -rn 'git push --force' .",
    "printf '%s' 'git reset --hard'",
    "echo hi # git branch -D x",
    "git log --grep='branch -D'",
    "git commit -m 'never git push --force'",
    "echo 'unbalanced",
    "ls git",
    "cat git.txt",
]


@pytest.mark.parametrize("cmd", NOT_COMMAND_POSITION)
def test_text_that_is_not_in_command_position_passes(cmd, tmp_path):
    assert sb.decide(cmd, str(tmp_path)) is None, repr(cmd)


#: Pinned gaps (module docstring): a later change to any of these is a decision.
DOCUMENTED_GAPS = [
    "bash -c 'git branch -D x'",
    "sh -c \"git push --force\"",
    "eval 'git branch -D x'",
    "echo x | xargs git branch -D",
    "find . -exec git branch -D x {} ;",
    "bash <<EOF\ngit branch -D x\nEOF",
    "g=git; $g push --force",
    "git push $'--force'",
]


@pytest.mark.parametrize("cmd", DOCUMENTED_GAPS)
def test_the_documented_gaps_pass(cmd, tmp_path):
    assert sb.decide(cmd, str(tmp_path)) is None, repr(cmd)


def test_dash_c_names_the_target_checkout(clean_repo, dirty_repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    # cwd is not a repo, -C points at the dirty one.
    assert sb.decide(f"git -C {dirty_repo.as_posix()} reset --hard", str(other)) is not None
    # cwd is the dirty repo, -C points at somewhere with nothing to lose.
    assert sb.decide(f"git -C {other.as_posix()} reset --hard", str(dirty_repo)) is None
    # (Forward slashes: shlex reads a backslash as a shell escape, so a Windows-style -C path is
    # unreadable here and a state row on it passes, like any state that cannot be read.)
    # A relative -C resolves against the cwd.
    assert sb.decide("git -C userrepo reset --hard", str(dirty_repo.parent)) is not None


def test_deny_text_is_short_and_free_of_the_lexicon():
    for hit in (sb.Hit("branch -D", "text"), sb.Hit("reset --hard", "state")):
        text = sb.deny_text(hit)
        assert len(text) < 420, len(text)
        assert hit.label in text
        assert not LEXICON.search(text), LEXICON.search(text)


# ---------------------------------------------------------------------------
# The real hook argv, no flag on the account
# ---------------------------------------------------------------------------


def _hook(cmd: str, cwd: pathlib.Path | None, home: pathlib.Path, *, agent: bool = True) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1"}
    if agent:
        payload["agent_id"] = "aworker-x-1"
        payload["agent_type"] = "general-purpose"
    if cwd is not None:
        payload["cwd"] = str(cwd)
    env = {
        **os.environ,
        "NX_HOOK_PYTHON": sys.executable,
        "NX_ROUTING_LOG_PATH": str(home / "routing.jsonl"),
        "NEXUS_CONFIG_DIR": str(home / "cfg"),
        "NX_DROPPED_WRITES_LOG_PATH": str(home / "dropped.jsonl"),
    }
    env.pop("NX_REMOTE_TEST_HOSTS", None)
    env.pop("NX_SERVICE_URL", None)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(home), str(GUARD)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout else {}


@pytest.fixture()
def no_flag_home(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "acct-home"
    home.mkdir()
    return home


def test_a_user_commit_passes_through_the_real_argv(clean_repo, no_flag_home):
    assert _hook("git commit -am x", clean_repo, no_flag_home) == {}
    assert _hook("git add f && git commit -m x && git push", clean_repo, no_flag_home) == {}


def test_a_dirty_user_checkout_gets_the_short_deny(dirty_repo, no_flag_home):
    out = _hook("git checkout -- f", dirty_repo, no_flag_home)
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert len(reason) < 420 and not LEXICON.search(reason)


def test_a_user_branch_delete_gets_the_short_deny(clean_repo, no_flag_home):
    out = _hook("git branch -D x", clean_repo, no_flag_home)
    assert out["permissionDecision"] == "deny"
    assert "branch -D" in out["permissionDecisionReason"]
    assert len(out["permissionDecisionReason"]) < 420


def test_the_main_conversation_is_never_gated(dirty_repo, no_flag_home):
    assert _hook("git reset --hard", dirty_repo, no_flag_home, agent=False) == {}


def test_user_mode_records_no_telemetry(dirty_repo, no_flag_home):
    first = _hook("git checkout -- f", dirty_repo, no_flag_home)
    second = _hook("git branch -D x", dirty_repo, no_flag_home)
    assert first["permissionDecision"] == "deny" and second["permissionDecision"] == "deny", (first, second)
    assert not (no_flag_home / "routing.jsonl").exists()
    assert not (no_flag_home / "dropped.jsonl").exists()


def test_a_flagged_account_gets_the_full_form_in_a_user_repo(clean_repo, tmp_path):
    home = tmp_path / "flagged"
    flag = home / ".config" / "nexus" / "maintainer-profile"
    flag.parent.mkdir(parents=True)
    flag.write_text("")
    out = _hook("git commit -am x", clean_repo, home)
    assert out["permissionDecision"] == "deny"
    assert "STRUCTURE-AGNOSTIC" in out["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# Fix round 1 (nexus-sby49.2): review findings, each pinned by a case that failed first
# ---------------------------------------------------------------------------

#: A $(...) span is one word to the decider: a flag after it is still the command's flag.
SUBSTITUTED_TEXT_DENY = [
    "git push origin $(git branch --show-current) --force",
    "git push $(echo origin) -f",
    "git push origin `git branch --show-current` --force",
    "git push origin \"$(git branch --show-current)\" --delete",
    "git branch $(date +%s) -D",
    "git branch -m $(echo a) --force",
    "git config $(echo core.hooksPath) /tmp/x --global",
    "git -C $(pwd) branch -D x",
]
SUBSTITUTED_STATE_DENY = [
    "git reset $(git rev-parse HEAD~1) --hard",
    "git reset --hard $(git rev-parse HEAD~1)",
    "git checkout $(git rev-parse HEAD) -- f",
    "git checkout $(echo HEAD) f",
    "git restore --source=$(git rev-parse HEAD) f",
    "git rm $(echo f) -f",
    "git switch $(echo main) --discard-changes",
    "git stash push -m $(date +%s)",
]


@pytest.mark.parametrize("cmd", SUBSTITUTED_TEXT_DENY)
def test_a_flag_after_a_command_substitution_is_still_the_commands_flag(cmd, tmp_path):
    hit = sb.decide(cmd, str(tmp_path))
    assert hit is not None and hit.kind == "text", f"{cmd!r}: {hit}"


@pytest.mark.parametrize("cmd", SUBSTITUTED_STATE_DENY)
def test_a_state_form_with_a_substitution_in_it_still_denies_on_a_dirty_tree(cmd, dirty_repo, pristine_repo):
    hit = sb.decide(cmd, str(dirty_repo))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"
    assert sb.decide(cmd, str(pristine_repo)) is None, f"{cmd!r} denied a clean tree"


@pytest.mark.parametrize("cmd", [
    "git push origin $(git branch --show-current)",
    "git branch $(date +%s)",
    "git reset $(git rev-parse HEAD~1)",
    "git log $(git rev-parse HEAD~3)..HEAD",
    "git push origin \"$(git branch --show-current)\" --follow-tags",
])
def test_a_substitution_alone_does_not_make_a_command_a_denied_form(cmd, dirty_repo):
    assert sb.decide(cmd, str(dirty_repo)) is None, cmd


def test_nested_substitutions_are_scanned_in_linear_time(tmp_path):
    import time

    nested = "git status" + " $(echo" * 60 + " x" + ")" * 60
    t0 = time.monotonic()
    sb.decide(nested, str(tmp_path))
    sb.decide("git branch -D x " + "$(" * 60 + ")" * 60, str(tmp_path))
    assert time.monotonic() - t0 < 1.0


#: Backticks and $(...) inside a QUOTED heredoc are text, not code (plan 1.3: bodies are never command position).
QUOTED_HEREDOC_ALLOW = [
    "git commit -m \"$(cat <<'EOF'\nfix: stop running `git push --force` here\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nfix: $(git branch -D x) is text\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\n- never `git reset --hard` in a user repo\nEOF\n)\"",
    "git commit -F - <<\"EOF\"\nmessage with `git push -f`\nEOF",
    "git commit -F - <<-'EOF'\n\tmessage with `git push -f`\n\tEOF",
    "git commit -F - <<\\EOF\nmessage with `git push -f`\nEOF",
]
#: An UNQUOTED heredoc expands: a $(...) or backtick body runs, so it is still scanned.
UNQUOTED_HEREDOC_DENY = [
    "cat <<EOF\n$(git push --force)\nEOF",
    "cat <<EOF\n`git branch -D x`\nEOF",
    "git commit -F - <<EOF\n$(git push -f)\nEOF",
]


@pytest.mark.parametrize("cmd", QUOTED_HEREDOC_ALLOW)
def test_a_quoted_heredoc_body_is_text_even_with_backticks_and_substitutions(cmd, dirty_repo):
    assert sb.decide(cmd, str(dirty_repo)) is None, repr(cmd)


@pytest.mark.parametrize("cmd", UNQUOTED_HEREDOC_DENY)
def test_an_unquoted_heredoc_body_that_substitutes_is_still_scanned(cmd, tmp_path):
    assert sb.decide(cmd, str(tmp_path)) is not None, repr(cmd)


def test_a_quoted_heredoc_with_no_terminator_hides_nothing(tmp_path):
    cmd = "echo \"<<'X'\"\ngit push --force"
    assert sb.decide(cmd, str(tmp_path)) is not None


#: -q silences the dry run, so the guard must not forward it.
CLEAN_QUIET = ["git clean -fdq", "git clean -f -q", "git clean --force --quiet", "git clean -qfd", "git clean -f --quiet -d"]


@pytest.mark.parametrize("cmd", CLEAN_QUIET)
def test_clean_with_quiet_still_denies_when_the_dry_run_lists_something(cmd, clean_repo):
    assert sb.decide(cmd, str(clean_repo)) is None, f"{cmd!r} denied a tree with nothing to remove"
    (clean_repo / "stray").write_text("x")
    hit = sb.decide(cmd, str(clean_repo))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"


# An untrusted repo's config must not run while the guard reads its state.
def test_reading_the_state_does_not_run_the_repos_fsmonitor(dirty_repo, tmp_path):
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmon.sh"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n")
    hook.chmod(0o755)
    _git(dirty_repo, "config", "core.fsmonitor", hook.as_posix())
    hit = sb.decide("git reset --hard", str(dirty_repo))
    assert hit is not None, "the dirty tree must still deny"
    assert not marker.exists(), "the repo's core.fsmonitor ran during the guard's status"


def test_a_state_probe_pins_the_two_code_running_config_keys(monkeypatch, dirty_repo):
    argvs: list[list[str]] = []
    real = subprocess.run

    def spy(argv, *a, **k):
        argvs.append(list(argv))
        return real(argv, *a, **k)

    setattr_in(monkeypatch, sb, "subprocess.run", spy)
    assert sb.decide("git reset --hard", str(dirty_repo)) is not None
    assert sb.decide("git clean -fd", str(dirty_repo)) is not None
    assert len(argvs) == 2, argvs
    for argv in argvs:
        assert "core.fsmonitor=false" in argv, argv
        assert any(w.startswith("core.hooksPath=") for w in argv), argv


# D1: a config write aimed at a host-wide file with -f/--file is the same hazard as --global.
GLOBAL_FILE_CONFIG_DENY = [
    "git config --file ~/.gitconfig user.name x",
    "git config -f ~/.gitconfig core.hooksPath /x",
    "git config --file=$HOME/.gitconfig user.name x",
    "git config --file ${HOME}/.config/git/config user.name x",
    "git config -f /etc/gitconfig user.name x",
    "git config --file ~/.config/git/config --add safe.directory '*'",
]
GLOBAL_FILE_CONFIG_ALLOW = [
    "git config --file .git/config user.name x",
    "git config -f /tmp/scratch.cfg user.name x",
    "git config --file ~/.gitconfig --get user.name",
    "git config --file ~/.gitconfig --list",
    "git config --file ./local.cfg user.name x",
]


@pytest.mark.parametrize("cmd", GLOBAL_FILE_CONFIG_DENY)
def test_a_config_write_to_a_host_wide_file_denies_in_any_cwd(cmd, tmp_path):
    hit = sb.decide(cmd, str(tmp_path))
    assert hit is not None and hit.kind == "text", f"{cmd!r}: {hit}"


def test_a_config_write_to_the_account_homes_gitconfig_by_absolute_path_denies(tmp_path):
    home = pathlib.Path(os.path.expanduser("~")).as_posix()  # forward slashes: shlex reads a backslash as an escape
    assert sb.decide(f"git config --file {home}/.gitconfig user.name x", str(tmp_path)) is not None


@pytest.mark.parametrize("cmd", GLOBAL_FILE_CONFIG_ALLOW)
def test_a_config_file_aimed_at_a_local_or_scratch_file_or_a_read_passes(cmd, tmp_path):
    assert sb.decide(cmd, str(tmp_path)) is None, cmd


# -C with a path the decider cannot resolve reads the checkout the cwd names.
UNRESOLVABLE_DASH_C = [
    'git -C "$PWD" reset --hard',
    "git -C ~ reset --hard",
    "git -C $(pwd) reset --hard",
    "git -C ${PWD} reset --hard",
    "git -C `pwd` reset --hard",
    "git -C $HOME/x reset --hard",
]


@pytest.mark.parametrize("cmd", UNRESOLVABLE_DASH_C)
def test_an_unresolvable_dash_c_falls_back_to_the_cwd_checkout(cmd, dirty_repo, pristine_repo):
    hit = sb.decide(cmd, str(dirty_repo))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"
    assert sb.decide(cmd, str(pristine_repo)) is None


# Wrappers: timeout hands its command on; sudo -n is a flag, nice -n takes a value.
WRAPPED_DENY = [
    "timeout 30 git push --force",
    "timeout -s KILL 30 git branch -D x",
    "timeout --signal=KILL --kill-after=5 30 git branch -D x",
    "sudo -n git push -f",
    "sudo -u root git push -f",
    "nice -n 5 git push -f",
    "env -u FOO git push -f",
    "nohup git push -f",
]


@pytest.mark.parametrize("cmd", WRAPPED_DENY)
def test_a_wrapper_in_front_of_git_does_not_hide_it(cmd, tmp_path):
    assert sb.decide(cmd, str(tmp_path)) is not None, cmd


def test_wrappers_that_run_something_else_are_not_git(tmp_path):
    assert sb.decide("timeout 30 make test", str(tmp_path)) is None
    assert sb.decide("sudo -n ls", str(tmp_path)) is None
    assert sb.decide("nice -n 5 echo git push -f", str(tmp_path)) is None


def test_the_accepted_gaps_are_named_in_the_module_docstring():
    doc = sb.__doc__ or ""
    for gap in ("abbreviation", "xargs", "bash -c", "eval", "backslash"):
        assert gap in doc, f"the module docstring does not name the gap: {gap}"


# ---------------------------------------------------------------------------
# Fix round 2 (nexus-sby49.2): quote state and heredoc bodies in the substitution scan
# ---------------------------------------------------------------------------

ROUND2_TEXT_DENY = [
    # N1: an apostrophe inside "..." is not a single quote, so the substitution after it is still blanked.
    "git commit -m \"it's done\" && git push origin $(git branch --show-current) --force",
    "echo \"it's\" ; git branch $(date +%s) -D",
]
ROUND2_STATE_DENY = [
    # R2: an unquoted heredoc body with an unbalanced paren must not swallow the command after it.
    "git commit -m \"$(cat <<EOF\nfix (partly\nEOF\n)\" && git reset --hard",
    "git commit -m \"$(cat <<EOF\nfix (partly\nEOF\n)\"\ngit reset --hard",
    "git commit -m \"$(cat <<-EOF\n\tfix (partly\n\tEOF\n)\" && git reset --hard",
    # a heredoc marker inside a quoted string is a string, so it hides nothing
    "echo \"<<'X'\"\ngit reset --hard\nX",
    "echo '<<EOF'\ngit reset --hard\nEOF",
]


@pytest.mark.parametrize("cmd", ROUND2_TEXT_DENY)
def test_round2_text_forms_deny(cmd, tmp_path):
    hit = sb.decide(cmd, str(tmp_path))
    assert hit is not None and hit.kind == "text", f"{cmd!r}: {hit}"


@pytest.mark.parametrize("cmd", ROUND2_STATE_DENY)
def test_round2_state_forms_deny_on_a_dirty_tree_and_pass_on_a_clean_one(cmd, dirty_repo, pristine_repo):
    hit = sb.decide(cmd, str(dirty_repo))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"
    assert sb.decide(cmd, str(pristine_repo)) is None, f"{cmd!r} denied a clean tree"


#: Each prefix opens, closes or fakes a quote, a comment or a heredoc in a different way. None may change what
#: the command after it is: a flag after a substitution is still that command's flag.
QUOTE_PREFIXES = [
    "",
    "echo \"it's\"; ",
    "echo \"it's\" && ",
    "echo 'it'\"'\"'s'; ",
    "echo \"a\\\"b'c\"; ",
    "echo 'a\"b'; ",
    "echo \"x\" 'y' \"z's\"; ",
    "echo \"$(echo \"it's\")\"; ",
    "echo \"$(echo 'a)b')\"; ",
    "echo $(echo \"(\"); ",
    "echo hi # don't (quote\n",
    "echo ${#HOME} \"it's\"; ",
    "cat <<EOF\nit's (partly\nEOF\n",
    "cat <<-EOF\n\tit's (partly\n\tEOF\n",
    "cat <<'EOF'\nit's `x` (\nEOF\n",
    "cat <<\"EOF\"\nit's `x` (\nEOF\n",
    "cat <<\\EOF\nit's `x` (\nEOF\n",
    "git commit -m \"$(cat <<'EOF'\nit's (a `b`\nEOF\n)\" && ",
    "git commit -m \"$(cat <<EOF\nit's (a\nEOF\n)\" && ",
]
SUBSTITUTED_TAILS_DENY = [
    "git push origin $(git branch --show-current) --force",
    "git push origin \"$(git branch --show-current)\" --delete",
    "git push origin `git branch --show-current` -f",
    "git branch $(date +%s) -D",
    "git push $(echo origin) +main",
]
SUBSTITUTED_TAILS_PASS = [
    "git push origin $(git branch --show-current)",
    "git branch $(date +%s)",
    "git log $(git rev-parse HEAD~3)..HEAD",
]


@pytest.mark.parametrize("prefix", QUOTE_PREFIXES, ids=range(len(QUOTE_PREFIXES)))
@pytest.mark.parametrize("tail", SUBSTITUTED_TAILS_DENY)
def test_no_quote_comment_or_heredoc_variant_hides_a_trailing_flag(prefix, tail, tmp_path):
    cmd = prefix + tail
    hit = sb.decide(cmd, str(tmp_path))
    assert hit is not None and hit.kind == "text", f"{cmd!r}: {hit}"


@pytest.mark.parametrize("prefix", QUOTE_PREFIXES, ids=range(len(QUOTE_PREFIXES)))
@pytest.mark.parametrize("tail", SUBSTITUTED_TAILS_PASS)
def test_the_same_variants_with_no_denied_flag_pass(prefix, tail, tmp_path):
    cmd = prefix + tail
    assert sb.decide(cmd, str(tmp_path)) is None, repr(cmd)


@pytest.mark.parametrize("prefix", QUOTE_PREFIXES, ids=range(len(QUOTE_PREFIXES)))
def test_the_same_variants_still_deny_a_state_form_after_them_on_a_dirty_tree(prefix, dirty_repo, pristine_repo):
    cmd = prefix + "git reset --hard $(git rev-parse HEAD~1)"
    hit = sb.decide(cmd, str(dirty_repo))
    assert hit is not None and hit.kind == "state", f"{cmd!r}: {hit}"
    assert sb.decide(cmd, str(pristine_repo)) is None


def test_an_apostrophe_in_a_heredoc_body_or_a_comment_does_not_start_a_quote(tmp_path):
    assert sb.decide("cat <<EOF\ndon't\nEOF\ngit push $(x) -f", str(tmp_path)) is not None
    assert sb.decide("# don't\ngit push $(x) -f", str(tmp_path)) is not None


def test_a_comment_is_not_code(tmp_path):
    assert sb.decide("echo hi # git push --force $(git branch -D x)", str(tmp_path)) is None
    assert sb.decide("echo a#b $(git branch -D x)", str(tmp_path)) is not None
