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
        "git config \"user.name\"",
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
        # a command substitution of a literal is resolved before the scan, so
        # this text spells a plain read
        "git config --get " + D + "(echo user.name)",
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
        "git config --worktree core.hooksPath /tmp/x",
        "git config --worktree --unset user.name",
        "git config --worktree --add a.b c",
        "git config --worktree user.name ';x'",
        "git config --worktree user.name 'a b'",
        "git status; git config --worktree user.name x",
        "git config --get user.name",
        "git config user.name",
        "git config --global --get user.name",
        "git config --list",
        "git config --local --get remote.origin.url",
        "git config get 'user.name'",
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
    ("git log " + "config " * 8_000, False),
    ("git grep " + "config " * 4_000 + "; git status; " * 2_000, False),
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
    assert elapsed < max(5 * baseline + 0.5, 1.0) and elapsed < 3.0, (
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
    import importlib.util

    spec = importlib.util.spec_from_file_location("guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for cmd in _CHAS_ALLOWED:
        assert guard._primary_match(guard._normalize_for_primary_scan(cmd)), cmd
