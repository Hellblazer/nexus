# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 Phase 3 Step 1 (nexus-wauo1.22): credential_print_guard.

Deny a Bash command that would print a Claude Code credential --
``conexus/hooks/scripts/routing/credential_print_guard.py``. A plugin-resident,
stdlib-only script with one copy and no wheel twin (RDR-219 requires it stay
free of any ``nexus`` import).

The guard denies an EXPLICIT reference to a protected variable by name
(``$VAR``/``${VAR}`` expansion, ``printenv NAME``, ``os.environ[...]``), a
keychain or ``.credentials.json`` read, and a read of ANOTHER process's
environment (``ps -E``/BSD ``e``, ``/proc/*/environ``). It does NOT deny a
bare ``env``/``set``/``printenv`` dump: Claude Code deletes
``CLAUDE_CODE_OAUTH_TOKEN`` from its own environment before a Bash-tool
child starts, so such a dump can never contain the protected value (code
review round 2, T2 ``nexus_rdr/219-research-15``). It must not deny the
legitimate Phase 2 shapes (``claude_credentials.py run --``/``status``, the
harnesses' ``docker -e CLAUDE_CODE_OAUTH_TOKEN`` and tmux launch lines).
There is NO escape: a ``# routing-allow:`` comment changes nothing.

Each table below is one collected case; the offending shape is in the
assertion message.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "credential_print_guard.py"
)


def _spawn(stdin_text: str) -> subprocess.CompletedProcess:
    """``NX_HOOK_PYTHON`` pins the interpreter the script's preamble
    resolves to ``sys.executable``, so the re-exec is a no-op and the case
    exercises this checkout rather than the box's installed generation."""
    env = os.environ.copy()
    env["NX_HOOK_PYTHON"] = sys.executable
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text, capture_output=True, text=True, timeout=10, env=env,
    )


def _run(command: str | None, *, tool_name: str = "Bash") -> subprocess.CompletedProcess:
    payload: dict = {"tool_name": tool_name}
    if command is not None:
        payload["tool_input"] = {"command": command}
    return _spawn(json.dumps(payload))


def _hso(proc: subprocess.CompletedProcess) -> dict:
    """``hookSpecificOutput``, or ``{}`` for a no-decision (empty stdout)
    verdict: a pass-through emits NOTHING rather than an explicit allow."""
    assert proc.returncode == 0, f"hook must exit 0; got {proc.returncode}; stderr={proc.stderr}"
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout else {}


def _decision(proc: subprocess.CompletedProcess) -> str | None:
    return _hso(proc).get("permissionDecision")


def _reason(proc: subprocess.CompletedProcess) -> str:
    return _hso(proc).get("permissionDecisionReason", "")


def _each(shapes: list[tuple[str, str]], fn) -> list[tuple[str, str, object]]:
    """``fn(command)`` for every ``(label, command)``, in parallel."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: fn(s[1]), shapes))
    return [(label, cmd, r) for (label, cmd), r in zip(shapes, results)]


# ---------------------------------------------------------------------------
# Denied: one control per shape. Each must FAIL if the pattern it exercises
# is deleted, which is why these are real command shapes, not synthetic ones.
# ---------------------------------------------------------------------------

DENIED_SHAPES: list[tuple[str, str]] = [
    ('python-environ-escaped-double-quotes', 'python3 -c "import os; print(os.environ[\\"CLAUDE_CODE_OAUTH_TOKEN\\"])"'),
    ('python-getenv-escaped-double-quotes', 'python3 -c "import os; print(os.getenv(\\"CLAUDE_CODE_OAUTH_TOKEN\\"))"'),
    ('find-generic-password-interactive-login-item', 'security find-generic-password -s "Claude Code-credentials" -w'),
    ('find-generic-password-automation-item', 'security find-generic-password -a "$USER" -s nexus-automation-oauth-token -w'),
    ('dump-keychain-d-naming-item', 'security dump-keychain -d | grep "Claude Code-credentials"'),
    ('cat-credentials-json', 'cat tests/e2e/.claude-auth/.credentials.json'),
    ('jq-credentials-json', 'jq . ~/nexus-sandbox/.claude/.credentials.json'),
    ('less-credentials-json', 'less .credentials.json'),
    ('head-credentials-json', 'head -c 40 .credentials.json'),
    ('tail-credentials-json', 'tail -f .credentials.json'),
    ('python-dash-c-credentials-json', 'python3 -c "print(open(\'.credentials.json\').read())"'),
    ('echo-dollar-expansion', 'echo $CLAUDE_CODE_OAUTH_TOKEN'),
    ('printf-braced-expansion-redirected-to-file', 'printf "%s" "${CLAUDE_CODE_OAUTH_TOKEN}" > leaked.txt'),
    ('printenv-named', 'printenv CLAUDE_CODE_OAUTH_TOKEN'),
    ('printenv-named-among-others', 'printenv CLAUDE_CODE_OAUTH_TOKEN PATH'),
    ('printenv-named-inside-sh-c-wrapper', "sh -c 'printenv CLAUDE_CODE_OAUTH_TOKEN'"),
    ('echo-dollar-expansion-harness-name', 'echo $NX_HARNESS_CLAUDE_OAUTH_TOKEN'),
    ('printenv-named-harness-name', 'printenv NX_HARNESS_CLAUDE_OAUTH_TOKEN'),
    ('python-dash-c-os-environ-bracket', 'python3 -c "import os; print(os.environ[\'CLAUDE_CODE_OAUTH_TOKEN\'])"'),
    ('python-dash-c-os-getenv', 'python3 -c "import os; print(os.getenv(\'CLAUDE_CODE_OAUTH_TOKEN\'))"'),
    ('python-heredoc-os-environ', "python3 - <<'EOF'\nimport os\nprint(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\nEOF\n"),
    ('python-heredoc-credentials-json-open', "python3 - <<'EOF'\nprint(open('.credentials.json').read())\nEOF\n"),
    ('awk-credentials-json', "awk '{print}' .credentials.json"),
    ('sed-credentials-json', "sed -n '1p' .credentials.json"),
    ('more-credentials-json', 'more .credentials.json'),
    ('od-credentials-json', 'od -c .credentials.json'),
    ('xxd-credentials-json', 'xxd .credentials.json'),
    ('strings-credentials-json', 'strings .credentials.json'),
    ('base64-credentials-json', 'base64 .credentials.json'),
    ('ps-dash-capital-e-macos', 'ps -E'),
    ('ps-auxeww-bsd-e-flag', 'ps auxeww'),
    ('ps-eww-bsd-e-flag', 'ps eww'),
    ('cat-proc-environ', 'cat /proc/1234/environ'),
    ('strings-proc-environ', 'strings /proc/$$/environ'),
    ('tr-proc-environ-redirect', "tr '\\0' '\\n' < /proc/1234/environ"),
    ('proc-environ-bash-builtin-redirect-read-no-reader-command', '$(< /proc/self/environ)'),
    ('proc-environ-reader-inside-bash-c-wrapper', "bash -c 'cat /proc/1/environ'"),
    ('ps-bsd-e-flag-inside-eval-wrapper', 'eval "ps eww"'),
]

ALLOWED_SHAPES: list[tuple[str, str]] = [
    ('dotenv-file-read-is-not-an-env-dump', 'cat .env'),
    ('dotenv-source-is-not-an-env-dump', 'source .env'),
    ('tmux-set-environment', 'tmux set-environment -g X 1'),
    ('env-grep-multiple-unrelated-patterns', 'env | grep -e FOO -e BAR'),
    ('cred-tool-status-tests-e2e-run-sh', 'python3 "$CRED_TOOL" status'),
    ('cred-tool-status-redirected-hook-surface-shakeout', 'python3 "$CRED_TOOL" status > /dev/null 2>&1'),
    ('cred-tool-run-tmux-tests-e2e-run-sh', 'python3 "$CRED_TOOL" run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s e2e -x 220 -y 50'),
    ('cred-tool-run-tmux-cc-validation-runner-sh', '_cred_tool run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s "$TMUX_SESSION" -x 220 -y 50'),
    ('cred-tool-run-docker-dash-e-bare-name', 'python3 "$CRED_TOOL" run -- docker run --rm -v "$ART:/home/nexus/artifacts" -e MVV_ARTIFACTS=/home/nexus/artifacts -e CLAUDE_CODE_OAUTH_TOKEN "$IMAGE"'),
    ('cred-tool-run-docker-name-wrapper-hook-surface-shakeout', 'python3 "$CRED_TOOL" run -- bash -c \'exec docker run --name "$0" -e CLAUDE_CODE_OAUTH_TOKEN "$@"\' "$IMAGE-container" "$IMAGE"'),
    ('cred-tool-run-docker-args-array-hook-surface-shakeout', 'python3 "$CRED_TOOL" run -- docker run --rm "${DOCKER_ARGS[@]}" "$IMAGE"'),
    ('docker-dash-e-bare-name-harness-token', 'docker run --rm -e NX_HARNESS_CLAUDE_OAUTH_TOKEN "$IMAGE"'),
    ('rdr219-mandated-token-shape-grep', "grep -rlE 'sk-ant-o(a|r)t' /tmp"),
    ('bare-export-no-dollar-remote-reader-shape', 'export CLAUDE_CODE_OAUTH_TOKEN'),
    ('set-with-flags-ubiquitous-script-preamble', 'set -euo pipefail'),
    ('env-with-args-execs-a-command-not-a-bare-dump', 'env -i CLAUDE_CODE_OAUTH_TOKEN=x claude --dangerously-skip-permissions'),
    ('printenv-unprotected-name-path', 'printenv PATH'),
    ('printenv-unprotected-name-home', 'printenv HOME'),
    ('env-piped-grep-count-mode', 'env | grep -c NAME'),
    ('env-piped-grep-quiet-mode', 'env | grep -q NAME'),
    ('env-piped-grep-pattern-not-matching-protected-name', 'env | grep MYVAR'),
    ('env-piped-wc', 'env | wc -l'),
    ('set-with-dash-x-ubiquitous-script-preamble', 'set -x'),
    ('python-script-by-path-not-dash-c-not-heredoc', 'python3 tests/e2e/lib/claude_credentials.py run -- echo "hi"'),
    ('ps-aux-no-environment-flag', 'ps aux'),
    ('ps-dash-ef-no-environment-flag', 'ps -ef'),
    ('ps-dash-p-pid-dash-o-format', 'ps -p 123 -o args'),
    ('env-dump-in-command-substitution-now-allowed', 'echo "$(env)"'),
    ('env-dump-in-backticks-now-allowed', 'x=`env`'),
    ('env-grep-second-pattern-matches-protected-name-now-allowed', 'env | grep -e FOO -e TOKEN'),
    ('bare-env-now-allowed', 'env'),
    ('bare-env-piped-now-allowed', 'env | grep TOKEN'),
    ('bare-printenv-now-allowed', 'printenv'),
    ('bare-set-now-allowed', 'set'),
    ('bare-set-piped-now-allowed', 'set | grep TOKEN'),
    ('bare-env-dash-0-now-allowed', 'env -0'),
    ('bare-env-redirected-to-file-now-allowed', 'env > /tmp/leaked.txt'),
    ('bare-set-redirected-to-file-now-allowed', 'set > /tmp/leaked.txt'),
    ('env-piped-grep-exact-name-now-allowed', 'env | grep CLAUDE_CODE_OAUTH_TOKEN'),
    ('env-piped-grep-substring-of-protected-name-now-allowed', 'env | grep OAUTH'),
    ('env-piped-into-unrecognized-filter-now-allowed', 'env | cat'),
    ('env-redirected-to-devnull-stderr', 'env 2>/dev/null'),
    ('eval-env-not-command-position-anchored', 'eval env'),
    ('env-inside-a-brace-group', '{ env; }'),
    ('quoted-heredoc-mentioning-backtick-env-and-set', "cat > file.md <<'EOF'\nThis mentions bare `env` and `set` in prose, never executed.\nEOF\n"),
    ('commit-message-mentioning-backtick-env-and-set', "git commit -m 'docs: explain the bare `env`/`set` dump shape'"),
]


def test_every_denied_shape_is_denied_and_names_the_helper() -> None:
    """The redirect names both sanctioned invocations (RDR-219: a message
    naming ``claude_credentials.py status`` and ``claude_credentials.py run --``)."""
    bad = []
    for label, cmd, proc in _each(DENIED_SHAPES, _run):
        reason = _reason(proc)
        if (
            _decision(proc) != "deny"
            or "claude_credentials.py status" not in reason
            or "claude_credentials.py run --" not in reason
        ):
            bad.append(f"{label}: {cmd!r} -> {_decision(proc)!r} reason={reason!r}")
    assert not bad, "denied shape(s) not denied with the helper redirect:\n" + "\n".join(bad)


def test_routing_allow_escape_does_not_apply() -> None:
    """RDR-219 grants this guard NO escape token: a ``# routing-allow:``
    comment on a denied shape must still deny."""
    escaped = [
        (label, f"{cmd}  # routing-allow: reviewed and approved by the user")
        for label, cmd in DENIED_SHAPES
    ]
    bad = [f"{label}: {cmd!r}" for label, cmd, proc in _each(escaped, _run) if _decision(proc) != "deny"]
    assert not bad, "escape token weakened the guard for:\n" + "\n".join(bad)


def test_every_legitimate_shape_is_allowed() -> None:
    """Real invocation text from tests/e2e and tests/cc-validation (RDR-219
    P2), plus the environment-dump shapes round 2 moved out of the deny set.
    A guard that denied these would break every harness."""
    bad = [
        f"{label}: {cmd!r} -> {_decision(proc)!r} reason={_reason(proc)!r}"
        for label, cmd, proc in _each(ALLOWED_SHAPES, _run)
        if _decision(proc) is not None
    ]
    assert not bad, "legitimate shape(s) wrongly denied:\n" + "\n".join(bad)


def test_a_deny_reason_names_what_matched() -> None:
    """The python and process-environment heuristics are the likeliest to
    false-positive, so their reasons name the match, not a generic label."""
    cases = [
        (
            """python3 -c "import os; print(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\"""",
            ["CLAUDE_CODE_OAUTH_TOKEN"],
        ),
        ("ps -E", ["-E"]),
        ("ps auxeww", ["auxeww"]),
        ("cat /proc/1234/environ", ["cat", "/proc/*/environ"]),
    ]
    for cmd, needles in cases:
        reason = _reason(_run(cmd))
        for needle in needles:
            assert needle in reason, f"{cmd!r}: reason lacks {needle!r}: {reason!r}"


def test_inputs_that_are_not_a_bash_command_pass_through() -> None:
    cred = "security find-generic-password -s nexus-automation-oauth-token -w"
    probes = {
        "non-Bash tool": _run(cred, tool_name="Edit"),
        "empty command": _run("", tool_name="Bash"),
        "no tool_input": _run(None, tool_name="Bash"),
        "malformed stdin": _spawn("not json at all {{{"),
        "empty stdin": _spawn(""),
    }
    for label, proc in probes.items():
        assert _decision(proc) is None, f"{label} was not a pass-through: {proc.stdout!r}"


# ---------------------------------------------------------------------------
# FAILURE BEHAVIOUR: a forced internal error denies a command carrying a
# protected marker and passes one without, per the module docstring's pinned
# plain-substring fallback.
# ---------------------------------------------------------------------------

#: Runs the guard in a FRESH subprocess: the module's own preamble calls
#: ``_interpreter.reexec_if_needed()`` at import time, and containing that in
#: a throwaway subprocess is what makes patching its internals safe.
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


def _drive_forced_error(command: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["NX_HOOK_PYTHON"] = sys.executable
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        [sys.executable, "-c", _FAILURE_DRIVER.format(script=str(SCRIPT))],
        input=payload, capture_output=True, text=True, timeout=10, env=env,
    )


@pytest.mark.parametrize(
    "command",
    [
        'security find-generic-password -s "Claude Code-credentials" -w',
        "do something with CLAUDE_CODE_OAUTH_TOKEN unrelated to the regexes",
        "do something with NX_HARNESS_CLAUDE_OAUTH_TOKEN unrelated to the regexes",
    ],
    ids=["keychain-item", "oauth-token-marker", "harness-token-marker"],
)
def test_a_forced_error_still_denies_a_command_carrying_a_failsafe_marker(command: str) -> None:
    """One control per marker in ``_FAILSAFE_MARKERS`` so the list cannot
    silently drop one. Silence here is the failure this test exists to catch."""
    proc = _drive_forced_error(command)
    assert proc.returncode == 0, "the deny is envelope-encoded; the exit code is 0"
    assert proc.stdout.strip(), f"no output on a raised body with a marker present\n{proc.stderr}"
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_forced_error_with_no_marker_passes_through() -> None:
    """Fail-open with no marker is NO decision (empty stdout), not an explicit
    allow: an explicit allow would bypass Claude Code's own permission prompt
    and its auto-mode classifier (nexus-452oy)."""
    proc = _drive_forced_error("echo hello world")
    assert proc.returncode == 0
    assert proc.stdout == "", f"expected empty stdout, got: {proc.stdout!r}"
