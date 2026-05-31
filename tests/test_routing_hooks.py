# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 1: routing-hook framework tests.

Validates the contract every routing hook must honor:

* JSON envelope shape on allow / deny / warn paths
* ``exit 0`` on every path including unexpected exceptions
* fail-closed opt-in semantics
* ``# routing-allow: <reason>=8 chars>`` escape parsing
* JSONL telemetry append
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
LIB_PATH = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "_lib.py"
REGISTRY_PATH = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
README_PATH = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "README.md"


def _load_lib():
    spec = importlib.util.spec_from_file_location("nx_routing_lib", LIB_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Files exist
# ---------------------------------------------------------------------------


def test_lib_file_exists():
    assert LIB_PATH.exists(), f"missing: {LIB_PATH}"


def test_registry_file_exists():
    assert REGISTRY_PATH.exists(), f"missing: {REGISTRY_PATH}"


def test_readme_exists():
    assert README_PATH.exists(), f"missing: {README_PATH}"


# ---------------------------------------------------------------------------
# JSON envelope shape — allow / deny / warn
# ---------------------------------------------------------------------------


def test_allow_envelope_shape():
    lib = _load_lib()
    env = json.loads(lib.allow_envelope())
    assert env["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_allow_envelope_with_context():
    lib = _load_lib()
    env = json.loads(lib.allow_envelope("extra context"))
    assert env["hookSpecificOutput"]["additionalContext"] == "extra context"


def test_deny_envelope_shape():
    lib = _load_lib()
    env = json.loads(lib.deny_envelope("blocked because"))
    assert env["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert env["hookSpecificOutput"]["reason"] == "blocked because"
    # Current Claude Code reads permissionDecisionReason on a deny; the
    # legacy "reason"-only envelope arrived as a bare "denied" with no
    # cause or remediation. systemMessage surfaces it in the transcript.
    assert env["hookSpecificOutput"]["permissionDecisionReason"] == "blocked because"
    assert env["systemMessage"] == "blocked because"


def test_deny_envelope_summary_decouples_banner_from_model_reason():
    """A multi-line reason reaches the model in full; the transcript
    banner carries only the short summary (or the first line by default)."""
    lib = _load_lib()
    full = "Blocked: do X.\n\nWhy: long remediation essay\nwith many lines."

    # Default: systemMessage is the first line, not the whole essay.
    env = json.loads(lib.deny_envelope(full))
    assert env["hookSpecificOutput"]["permissionDecisionReason"] == full
    assert env["systemMessage"] == "Blocked: do X."

    # Explicit summary overrides the banner; model still gets the full reason.
    env = json.loads(lib.deny_envelope(full, summary="one-line banner"))
    assert env["hookSpecificOutput"]["permissionDecisionReason"] == full
    assert env["systemMessage"] == "one-line banner"


def test_warn_envelope_is_allow():
    lib = _load_lib()
    env = json.loads(lib.warn_envelope("just a warning"))
    # warn() is semantic alias for allow() — same decision, message in additionalContext
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "just a warning" in env["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# allow() / deny() emit JSON to stdout and exit 0
# ---------------------------------------------------------------------------


def _run_stub(body: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Run a Python stub that imports _lib and exercises a code path."""
    stub = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(LIB_PATH.parent)!r})
        import _lib
        {body}
        """
    )
    return subprocess.run(
        [sys.executable, "-c", stub],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_allow_exits_zero_with_json():
    proc = _run_stub("_lib.allow()")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_deny_exits_zero_with_json():
    proc = _run_stub("_lib.deny('because reasons')")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert payload["hookSpecificOutput"]["reason"] == "because reasons"


# ---------------------------------------------------------------------------
# fail-open default / fail-closed opt-in
# ---------------------------------------------------------------------------


def test_fail_open_on_exception_default():
    """run_hook with fail_closed=False emits allow on exception, exits 0."""
    proc = _run_stub(
        "_lib.run_hook(lambda stdin: 1/0, fail_closed=False)",
        stdin="{}",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_fail_closed_on_exception_denies():
    """run_hook with fail_closed=True emits deny on exception, still exits 0."""
    proc = _run_stub(
        "_lib.run_hook(lambda stdin: 1/0, fail_closed=True, rule_name='test_rule')",
        stdin="{}",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "cannot verify" in payload["hookSpecificOutput"]["reason"]
    assert "fail-closed" in payload["hookSpecificOutput"]["reason"]


# ---------------------------------------------------------------------------
# Escape token parsing
# ---------------------------------------------------------------------------


def test_escape_token_recognized_with_long_reason():
    lib = _load_lib()
    cmd = "grep MyClass src/foo.py  # routing-allow: legitimate text search here"
    assert lib.should_skip_for_reason(cmd) is True


def test_escape_token_rejected_when_reason_too_short():
    lib = _load_lib()
    cmd = "grep MyClass src/foo.py  # routing-allow: short"
    assert lib.should_skip_for_reason(cmd) is False


def test_escape_token_rejected_when_absent():
    lib = _load_lib()
    cmd = "grep MyClass src/foo.py"
    assert lib.should_skip_for_reason(cmd) is False


def test_escape_token_rejected_when_no_colon_payload():
    lib = _load_lib()
    cmd = "grep MyClass src/foo.py  # routing-allow:"
    assert lib.should_skip_for_reason(cmd) is False


# ---------------------------------------------------------------------------
# Telemetry JSONL append
# ---------------------------------------------------------------------------


def test_log_routing_event_appends_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "routing_log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log_path))
    lib = _load_lib()
    lib.log_routing_event(rule="rule_a", outcome="allow")
    lib.log_routing_event(rule="rule_b", outcome="deny", tool_name="Bash")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["rule"] == "rule_a"
    assert first["outcome"] == "allow"
    assert "ts" in first
    second = json.loads(lines[1])
    assert second["rule"] == "rule_b"
    assert second["outcome"] == "deny"
    assert second["tool_name"] == "Bash"


def test_log_routing_event_swallows_errors(tmp_path, monkeypatch):
    """Telemetry must never crash the hook. Unwritable path = silent no-op."""
    unwritable = tmp_path / "does-not-exist" / "log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(unwritable))
    lib = _load_lib()
    # If this raises, the test fails — log helper must be defensive.
    lib.log_routing_event(rule="x", outcome="allow")


# ---------------------------------------------------------------------------
# parse_stdin helper — defensive against malformed input
# ---------------------------------------------------------------------------


def test_parse_stdin_returns_empty_on_malformed():
    lib = _load_lib()
    data = lib.parse_stdin("{not valid json")
    assert data == {}


def test_parse_stdin_returns_dict_on_valid():
    lib = _load_lib()
    payload = '{"tool_name": "Bash", "tool_input": {"command": "ls"}}'
    data = lib.parse_stdin(payload)
    assert data["tool_name"] == "Bash"
    assert data["tool_input"]["command"] == "ls"


def test_parse_stdin_empty_string_is_empty_dict():
    lib = _load_lib()
    assert lib.parse_stdin("") == {}


# ---------------------------------------------------------------------------
# Registry shape (empty initially but parseable)
# ---------------------------------------------------------------------------


def test_registry_parses_as_yaml():
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    # Registry is empty initially but must be a dict (or None -> dict).
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Tool short-circuit helpers
# ---------------------------------------------------------------------------


def test_get_bash_command_returns_command():
    lib = _load_lib()
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert lib.get_bash_command(payload) == "git status"


def test_get_bash_command_empty_when_not_bash():
    lib = _load_lib()
    payload = {"tool_name": "Edit", "tool_input": {"command": "ignored"}}
    assert lib.get_bash_command(payload) == ""


def test_get_bash_command_empty_on_missing():
    lib = _load_lib()
    assert lib.get_bash_command({}) == ""
