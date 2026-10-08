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


@pytest.mark.parametrize("cmd", ["git commit -m msg", "git add -A", "git checkout -- x.py"])
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


def test_the_spliced_expansion_deny_names_the_reconstructed_fragment(shared_repo):
    out = _verdict("git re${x}set --hard", shared_repo)
    assert out["permissionDecision"] == "deny"
    assert "reset" in out["permissionDecisionReason"], out["permissionDecisionReason"]


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
