# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 Phase 3 Step 1 (nexus-wauo1.22): credential_print_guard.

Deny a Bash command that would print a Claude Code credential --
``conexus/hooks/scripts/routing/credential_print_guard.py``. Structure
mirrors the routing/README.md contract (positive/negative/escape/
malformed) plus RDR-219's own additions: the guard must not deny the
legitimate Phase 2 shapes already on ``develop`` (``claude_credentials.py
run --``/``status``, the harnesses' ``docker -e CLAUDE_CODE_OAUTH_TOKEN``
and tmux launch lines), and NO ESCAPE (unlike the git-write precedent,
a ``# routing-allow:`` comment changes nothing here).

**Environment-DUMP cases moved to ALLOWED after code review round 2**
(nexus-wauo1.22, T2 ``nexus/rdr219-p3-code-review-round2-findings``): the
guard no longer denies a bare ``env``/``env -0``/``set``/``printenv``
dump, or a pipe off one, because Claude Code deletes
``CLAUDE_CODE_OAUTH_TOKEN`` from its own environment before a Bash-tool
child ever starts (T2 ``nexus_rdr/219-research-15``), so a dump from
Claude's own Bash tool can never contain the protected value -- the rule
protected nothing, cost false denials, and (per round 2's critical
findings 1-2) could not be made complete anyway. What stays denied: an
EXPLICIT reference to a protected variable by name (``$VAR``/``${VAR}``
expansion, ``printenv NAME``), and a read of ANOTHER process's
environment (``ps -E``/BSD ``e``, ``/proc/*/environ``), which really can
carry the token.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "credential_print_guard.py"
)


def _run(command: str | None, *, tool_name: str = "Bash") -> subprocess.CompletedProcess:
    """Drive the hook script directly (it is plugin-resident, not ported --
    RDR-219 requires it stay a self-contained stdlib script with no
    ``nexus`` import, so there is no wheel-resident verb twin to go
    through instead).

    ``NX_HOOK_PYTHON`` pins the interpreter ``_interpreter.reexec_if_needed``
    resolves to ``sys.executable`` itself, so the preamble's resolution
    is a guaranteed, fast no-op in these tests rather than a live probe
    of the box's installed generation.
    """
    payload: dict = {}
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if command is not None:
        payload["tool_input"] = {"command": command}
    env = os.environ.copy()
    env["NX_HOOK_PYTHON"] = sys.executable
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def _raw(stdin_text: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["NX_HOOK_PYTHON"] = sys.executable
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def _decision(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, (
        f"hook must exit 0; got {proc.returncode}; stderr={proc.stderr}"
    )
    out = json.loads(proc.stdout)
    return out["hookSpecificOutput"]["permissionDecision"]


def _reason(proc: subprocess.CompletedProcess) -> str:
    out = json.loads(proc.stdout)
    return out["hookSpecificOutput"].get("permissionDecisionReason", "")


# ---------------------------------------------------------------------------
# Positive: one control per denied shape. Each of these must FAIL if the
# matching pattern it exercises is deleted -- that is the point of testing
# the real command shapes rather than synthetic ones.
# ---------------------------------------------------------------------------

DENIED_SHAPES = [
    pytest.param(
        'python3 -c "import os; print(os.environ[\\"CLAUDE_CODE_OAUTH_TOKEN\\"])"',
        id="python-environ-escaped-double-quotes",
    ),
    pytest.param(
        'python3 -c "import os; print(os.getenv(\\"CLAUDE_CODE_OAUTH_TOKEN\\"))"',
        id="python-getenv-escaped-double-quotes",
    ),
    pytest.param(
        'security find-generic-password -s "Claude Code-credentials" -w',
        id="find-generic-password-interactive-login-item",
    ),
    pytest.param(
        'security find-generic-password -a "$USER" -s nexus-automation-oauth-token -w',
        id="find-generic-password-automation-item",
    ),
    pytest.param(
        'security dump-keychain -d | grep "Claude Code-credentials"',
        id="dump-keychain-d-naming-item",
    ),
    pytest.param(
        "cat tests/e2e/.claude-auth/.credentials.json",
        id="cat-credentials-json",
    ),
    pytest.param(
        "jq . ~/nexus-sandbox/.claude/.credentials.json",
        id="jq-credentials-json",
    ),
    pytest.param(
        "less .credentials.json",
        id="less-credentials-json",
    ),
    pytest.param(
        "head -c 40 .credentials.json",
        id="head-credentials-json",
    ),
    pytest.param(
        "tail -f .credentials.json",
        id="tail-credentials-json",
    ),
    pytest.param(
        """python3 -c "print(open('.credentials.json').read())\"""",
        id="python-dash-c-credentials-json",
    ),
    pytest.param(
        "echo $CLAUDE_CODE_OAUTH_TOKEN",
        id="echo-dollar-expansion",
    ),
    pytest.param(
        'printf "%s" "${CLAUDE_CODE_OAUTH_TOKEN}" > leaked.txt',
        id="printf-braced-expansion-redirected-to-file",
    ),
    pytest.param(
        "printenv CLAUDE_CODE_OAUTH_TOKEN",
        id="printenv-named",
    ),
    pytest.param(
        "printenv CLAUDE_CODE_OAUTH_TOKEN PATH",
        id="printenv-named-among-others",
    ),
    pytest.param(
        "sh -c 'printenv CLAUDE_CODE_OAUTH_TOKEN'",
        id="printenv-named-inside-sh-c-wrapper",
    ),
    pytest.param(
        "echo $NX_HARNESS_CLAUDE_OAUTH_TOKEN",
        id="echo-dollar-expansion-harness-name",
    ),
    pytest.param(
        "printenv NX_HARNESS_CLAUDE_OAUTH_TOKEN",
        id="printenv-named-harness-name",
    ),
    pytest.param(
        """python3 -c "import os; print(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\"""",
        id="python-dash-c-os-environ-bracket",
    ),
    pytest.param(
        """python3 -c "import os; print(os.getenv('CLAUDE_CODE_OAUTH_TOKEN'))\"""",
        id="python-dash-c-os-getenv",
    ),
    pytest.param(
        "python3 - <<'EOF'\n"
        "import os\n"
        "print(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\n"
        "EOF\n",
        id="python-heredoc-os-environ",
    ),
    pytest.param(
        "python3 - <<'EOF'\n"
        "print(open('.credentials.json').read())\n"
        "EOF\n",
        id="python-heredoc-credentials-json-open",
    ),
    pytest.param(
        "awk '{print}' .credentials.json",
        id="awk-credentials-json",
    ),
    pytest.param(
        "sed -n '1p' .credentials.json",
        id="sed-credentials-json",
    ),
    pytest.param(
        "more .credentials.json",
        id="more-credentials-json",
    ),
    pytest.param(
        "od -c .credentials.json",
        id="od-credentials-json",
    ),
    pytest.param(
        "xxd .credentials.json",
        id="xxd-credentials-json",
    ),
    pytest.param(
        "strings .credentials.json",
        id="strings-credentials-json",
    ),
    pytest.param(
        "base64 .credentials.json",
        id="base64-credentials-json",
    ),
    pytest.param(
        "ps -E",
        id="ps-dash-capital-e-macos",
    ),
    pytest.param(
        "ps auxeww",
        id="ps-auxeww-bsd-e-flag",
    ),
    pytest.param(
        "ps eww",
        id="ps-eww-bsd-e-flag",
    ),
    pytest.param(
        "cat /proc/1234/environ",
        id="cat-proc-environ",
    ),
    pytest.param(
        "strings /proc/$$/environ",
        id="strings-proc-environ",
    ),
    pytest.param(
        "tr '\\0' '\\n' < /proc/1234/environ",
        id="tr-proc-environ-redirect",
    ),
    pytest.param(
        "$(< /proc/self/environ)",
        id="proc-environ-bash-builtin-redirect-read-no-reader-command",
    ),
    pytest.param(
        "bash -c 'cat /proc/1/environ'",
        id="proc-environ-reader-inside-bash-c-wrapper",
    ),
    pytest.param(
        'eval "ps eww"',
        id="ps-bsd-e-flag-inside-eval-wrapper",
    ),
]


@pytest.mark.parametrize("command", DENIED_SHAPES)
def test_denied_shape_is_denied(command: str) -> None:
    proc = _run(command)
    assert _decision(proc) == "deny", (
        f"expected deny for {command!r}; got {_decision(proc)!r}, "
        f"stdout={proc.stdout!r}"
    )


@pytest.mark.parametrize("command", DENIED_SHAPES)
def test_denied_shape_names_the_helper_in_the_reason(command: str) -> None:
    """The redirect message names both sanctioned invocations (RDR-219:
    'a message naming claude_credentials.py status and
    claude_credentials.py run --')."""
    reason = _reason(_run(command))
    assert "claude_credentials.py status" in reason
    assert "claude_credentials.py run --" in reason


# ---------------------------------------------------------------------------
# Diagnosability: the new python (rule 3/4) and process-environment (rule
# 5) heuristics are the most likely to false-positive, so their reasons
# must name what actually matched, not just a generic label.
# ---------------------------------------------------------------------------


def test_python_env_print_reason_names_the_variable() -> None:
    reason = _reason(_run(
        """python3 -c "import os; print(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\""""
    ))
    assert "CLAUDE_CODE_OAUTH_TOKEN" in reason


def test_process_environment_reason_names_the_matched_flag() -> None:
    reason = _reason(_run("ps -E"))
    assert "-E" in reason


def test_process_environment_reason_names_the_bsd_flag_shape() -> None:
    reason = _reason(_run("ps auxeww"))
    assert "auxeww" in reason


def test_process_environment_reason_names_the_reader_for_proc_environ() -> None:
    reason = _reason(_run("cat /proc/1234/environ"))
    assert "cat" in reason
    assert "/proc/*/environ" in reason


# ---------------------------------------------------------------------------
# NO ESCAPE: unlike the git-write precedent, a `# routing-allow:` comment
# on a denied command changes nothing -- this guard never calls
# `_lib.should_skip_for_reason`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", DENIED_SHAPES)
def test_routing_allow_escape_does_not_apply(command: str) -> None:
    escaped = f"{command}  # routing-allow: reviewed and approved by the user"
    assert _decision(_run(escaped)) == "deny", (
        f"RDR-219 grants this guard NO escape token; {escaped!r} must still "
        "deny"
    )


# ---------------------------------------------------------------------------
# Negative: the legitimate Phase 2 shapes now on develop must not be
# denied. Real invocation text collected from tests/e2e and
# tests/cc-validation (RDR-219 P2 migration, nexus-wauo1.18/.21), not
# synthesized -- a guard that denies these would break every harness.
# ---------------------------------------------------------------------------

ALLOWED_SHAPES = [
    pytest.param("cat .env", id="dotenv-file-read-is-not-an-env-dump"),
    pytest.param("source .env", id="dotenv-source-is-not-an-env-dump"),
    pytest.param("tmux set-environment -g X 1", id="tmux-set-environment"),
    pytest.param("env | grep -e FOO -e BAR", id="env-grep-multiple-unrelated-patterns"),
    pytest.param(
        'python3 "$CRED_TOOL" status',
        id="cred-tool-status-tests-e2e-run-sh",
    ),
    pytest.param(
        'python3 "$CRED_TOOL" status > /dev/null 2>&1',
        id="cred-tool-status-redirected-hook-surface-shakeout",
    ),
    pytest.param(
        'python3 "$CRED_TOOL" run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s e2e -x 220 -y 50',
        id="cred-tool-run-tmux-tests-e2e-run-sh",
    ),
    pytest.param(
        '_cred_tool run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s "$TMUX_SESSION" -x 220 -y 50',
        id="cred-tool-run-tmux-cc-validation-runner-sh",
    ),
    pytest.param(
        'python3 "$CRED_TOOL" run -- docker run --rm -v "$ART:/home/nexus/artifacts" '
        '-e MVV_ARTIFACTS=/home/nexus/artifacts -e CLAUDE_CODE_OAUTH_TOKEN "$IMAGE"',
        id="cred-tool-run-docker-dash-e-bare-name-rdr208-mvv",
    ),
    pytest.param(
        """python3 "$CRED_TOOL" run -- bash -c 'exec docker run --name "$0" -e CLAUDE_CODE_OAUTH_TOKEN "$@"' """
        '"$IMAGE-container" "$IMAGE"',
        id="cred-tool-run-docker-name-wrapper-hook-surface-shakeout",
    ),
    pytest.param(
        'python3 "$CRED_TOOL" run -- docker run --rm "${DOCKER_ARGS[@]}" "$IMAGE"',
        id="cred-tool-run-docker-args-array-hook-surface-shakeout",
    ),
    pytest.param(
        'docker run --rm -e NX_HARNESS_CLAUDE_OAUTH_TOKEN "$IMAGE"',
        id="docker-dash-e-bare-name-harness-token",
    ),
    pytest.param(
        "grep -rlE 'sk-ant-o(a|r)t' /tmp",
        id="rdr219-mandated-token-shape-grep",
    ),
    pytest.param(
        "export CLAUDE_CODE_OAUTH_TOKEN",
        id="bare-export-no-dollar-remote-reader-shape",
    ),
    pytest.param(
        "set -euo pipefail",
        id="set-with-flags-ubiquitous-script-preamble",
    ),
    pytest.param(
        "env -i CLAUDE_CODE_OAUTH_TOKEN=x claude --dangerously-skip-permissions",
        id="env-with-args-execs-a-command-not-a-bare-dump",
    ),
    pytest.param(
        "printenv PATH",
        id="printenv-unprotected-name-path",
    ),
    pytest.param(
        "printenv HOME",
        id="printenv-unprotected-name-home",
    ),
    pytest.param(
        "env | grep -c NAME",
        id="env-piped-grep-count-mode",
    ),
    pytest.param(
        "env | grep -q NAME",
        id="env-piped-grep-quiet-mode",
    ),
    pytest.param(
        "env | grep MYVAR",
        id="env-piped-grep-pattern-not-matching-protected-name",
    ),
    pytest.param(
        "env | wc -l",
        id="env-piped-wc",
    ),
    pytest.param(
        "set -x",
        id="set-with-dash-x-ubiquitous-script-preamble",
    ),
    pytest.param(
        'python3 tests/e2e/lib/claude_credentials.py run -- echo "hi"',
        id="python-script-by-path-not-dash-c-not-heredoc",
    ),
    pytest.param(
        "ps aux",
        id="ps-aux-no-environment-flag",
    ),
    pytest.param(
        "ps -ef",
        id="ps-dash-ef-no-environment-flag",
    ),
    pytest.param(
        "ps -p 123 -o args",
        id="ps-dash-p-pid-dash-o-format",
    ),
]

# ---------------------------------------------------------------------------
# Moved from DENIED_SHAPES after code review round 2 (nexus-wauo1.22, T2
# nexus/rdr219-p3-code-review-round2-findings): environment-DUMP shapes
# (bare env/env -0/set/printenv, and any pipe off one) are no longer
# denied. Reason (T2 nexus_rdr/219-research-15): Claude Code deletes
# CLAUDE_CODE_OAUTH_TOKEN from its own environment before a Bash-tool
# child ever starts, so a dump from Claude's own Bash tool can never
# contain the protected value -- the old rule protected nothing.
# ---------------------------------------------------------------------------

ALLOWED_SHAPES_FORMERLY_DENIED_ENV_DUMPS = [
    pytest.param('echo "$(env)"', id="env-dump-in-command-substitution-now-allowed"),
    pytest.param("x=`env`", id="env-dump-in-backticks-now-allowed"),
    pytest.param(
        "env | grep -e FOO -e TOKEN",
        id="env-grep-second-pattern-matches-protected-name-now-allowed",
    ),
    pytest.param("env", id="bare-env-now-allowed"),
    pytest.param("env | grep TOKEN", id="bare-env-piped-now-allowed"),
    pytest.param("printenv", id="bare-printenv-now-allowed"),
    pytest.param("set", id="bare-set-now-allowed"),
    pytest.param("set | grep TOKEN", id="bare-set-piped-now-allowed"),
    pytest.param("env -0", id="bare-env-dash-0-now-allowed"),
    pytest.param(
        "env > /tmp/leaked.txt", id="bare-env-redirected-to-file-now-allowed"
    ),
    pytest.param(
        "set > /tmp/leaked.txt", id="bare-set-redirected-to-file-now-allowed"
    ),
    pytest.param(
        "env | grep CLAUDE_CODE_OAUTH_TOKEN",
        id="env-piped-grep-exact-name-now-allowed",
    ),
    pytest.param(
        "env | grep OAUTH",
        id="env-piped-grep-substring-of-protected-name-now-allowed",
    ),
    pytest.param("env | cat", id="env-piped-into-unrecognized-filter-now-allowed"),
    # Round 2's own critical findings (1-2): these three defeated the old
    # anchored bare-dump detector even before this removal -- proof the
    # rule "could not be made complete regardless" (module docstring).
    pytest.param("env 2>/dev/null", id="env-redirected-to-devnull-stderr"),
    pytest.param("eval env", id="eval-env-not-command-position-anchored"),
    pytest.param("{ env; }", id="env-inside-a-brace-group"),
    # Round 2 finding 4: command-position anchoring false-positived on a
    # backtick-quoted `env`/`set` inside single-quoted shell text where
    # the shell never interprets the backtick -- a heredoc and a commit
    # message are exactly this project's own routine authoring shapes.
    pytest.param(
        "cat > file.md <<'EOF'\n"
        "This mentions bare `env` and `set` in prose, never executed.\n"
        "EOF\n",
        id="quoted-heredoc-mentioning-backtick-env-and-set",
    ),
    pytest.param(
        "git commit -m 'docs: explain the bare `env`/`set` dump shape'",
        id="commit-message-mentioning-backtick-env-and-set",
    ),
]

ALLOWED_SHAPES = ALLOWED_SHAPES + ALLOWED_SHAPES_FORMERLY_DENIED_ENV_DUMPS


@pytest.mark.parametrize("command", ALLOWED_SHAPES)
def test_legitimate_shape_is_allowed(command: str) -> None:
    proc = _run(command)
    assert _decision(proc) == "allow", (
        f"expected allow for {command!r}; got {_decision(proc)!r}, "
        f"reason={_reason(proc)!r}"
    )


# ---------------------------------------------------------------------------
# Contract: non-Bash tool, empty command, malformed stdin.
# ---------------------------------------------------------------------------


def test_non_bash_tool_is_allowed() -> None:
    proc = _run("security find-generic-password -s nexus-automation-oauth-token -w", tool_name="Edit")
    assert _decision(proc) == "allow"


def test_empty_command_is_allowed() -> None:
    proc = _run("", tool_name="Bash")
    assert _decision(proc) == "allow"


def test_no_tool_input_is_allowed() -> None:
    proc = _run(None, tool_name="Bash")
    assert _decision(proc) == "allow"


def test_malformed_stdin_is_allowed() -> None:
    proc = _raw("not json at all {{{")
    assert _decision(proc) == "allow"


def test_empty_stdin_is_allowed() -> None:
    proc = _raw("")
    assert _decision(proc) == "allow"


# ---------------------------------------------------------------------------
# FAILURE BEHAVIOUR: a forced internal error denies a command carrying a
# protected marker and allows one without, per the module docstring's
# pinned plain-substring fallback.
# ---------------------------------------------------------------------------

#: Runs the guard in a FRESH subprocess (never in-process): the module's
#: own preamble calls `_interpreter.reexec_if_needed()` at import time,
#: and containing that inside a throwaway subprocess -- rather than the
#: pytest process itself -- is what makes monkeypatching its internals
#: safe. `NX_HOOK_PYTHON` pins the resolution to a no-op, same as `_run`.
_FAILURE_DRIVER = """
import importlib.util, sys

spec = importlib.util.spec_from_file_location("credential_print_guard", {script!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def _boom(command):
    raise RuntimeError("induced: the matching logic could not decide")

mod._matched_reason = _boom
mod._lib.run_hook(mod.body, fail_closed=False, rule_name=mod.RULE_NAME)
"""


def _drive_forced_error(command: str) -> tuple[int, dict]:
    env = os.environ.copy()
    env["NX_HOOK_PYTHON"] = sys.executable
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, "-c", _FAILURE_DRIVER.format(script=str(SCRIPT))],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.stdout.strip(), (
        f"the guard emitted NOTHING on a raised body (rc={proc.returncode}). "
        f"Silence is the failure this test exists to catch.\n{proc.stderr}"
    )
    return proc.returncode, json.loads(proc.stdout)


def test_forced_error_denies_a_command_carrying_a_marker() -> None:
    rc, out = _drive_forced_error(
        'security find-generic-password -s "Claude Code-credentials" -w'
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert rc == 0, "the deny is envelope-encoded; the exit code is 0"


def test_forced_error_allows_a_command_with_no_marker() -> None:
    rc, out = _drive_forced_error("echo hello world")
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert rc == 0


@pytest.mark.parametrize("var", ["CLAUDE_CODE_OAUTH_TOKEN", "NX_HARNESS_CLAUDE_OAUTH_TOKEN"])
def test_forced_error_denies_on_each_failsafe_marker(var: str) -> None:
    """One control per marker the module docstring pins
    (:data:`credential_print_guard._FAILSAFE_MARKERS`), so the failure-mode
    substring list can't silently drop one."""
    rc, out = _drive_forced_error(f"do something with {var} unrelated to the regexes")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert rc == 0
