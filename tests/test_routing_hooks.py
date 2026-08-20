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


def test_lib_copies_are_byte_identical():
    """The routing framework lib is shipped in BOTH plugins. They must stay
    byte-identical; a silent divergence makes one plugin's hooks behave
    differently from the other's. Enforce parity here so an edit to one without
    the other fails CI rather than shipping a latent split."""
    sn_lib = PROJECT_ROOT / "sn" / "hooks" / "scripts" / "routing" / "_lib.py"
    conexus_lib = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "_lib.py"
    assert sn_lib.exists() and conexus_lib.exists()
    assert sn_lib.read_bytes() == conexus_lib.read_bytes(), (
        "sn/ and conexus/ routing _lib.py have diverged — they must be identical; "
        "copy one over the other."
    )


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


def test_deny_envelope_whitespace_or_empty_reason_does_not_crash():
    """deny_envelope is on every routing hook's deny path — it must never raise.
    A whitespace-only reason is truthy, so the first-line slice would IndexError
    unless reason is stripped before the default-guard."""
    lib = _load_lib()
    for raw in ("   ", "\n\n", "", "\t"):
        env = json.loads(lib.deny_envelope(raw))
        assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert env["systemMessage"], f"empty systemMessage for {raw!r}"
        assert env["hookSpecificOutput"]["permissionDecisionReason"]


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
    """Run a Python stub that imports _lib and exercises a code path.

    nexus-mzvwa.9: the stub subprocess MUST NOT inherit the default
    routing-log path — pre-fix, every suite run deposited a
    test_rule/unknown fail-ladder pair into the LIVE
    ~/.config/nexus/routing_log.jsonl (312 pairs over the 48-day soak).
    """
    stub = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(LIB_PATH.parent)!r})
        import _lib
        {body}
        """
    )
    import os as _os
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        env = {**_os.environ, "NX_ROUTING_LOG_PATH": str(pathlib.Path(td) / "routing_log.jsonl")}
        return subprocess.run(
            [sys.executable, "-c", stub],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
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
        "_lib.run_hook(lambda stdin: 1/0, fail_closed=False, rule_name='selftest_fail_open')",
        stdin="{}",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_fail_closed_on_exception_denies():
    """run_hook with fail_closed=True emits deny on exception, still exits 0."""
    proc = _run_stub(
        "_lib.run_hook(lambda stdin: 1/0, fail_closed=True, rule_name='selftest_fail_closed')",
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
# Telemetry JSONL rotation (Sam-directed fix pass, 2026-08-20)
#
# ROTATION, NOT TRIM-IN-PLACE: routing_log.jsonl is appended to by MANY
# concurrent routing-hook processes (every gated Bash/Agent/git command,
# across however many Claude Code sessions are live at once) -- a
# read-modify-write to keep only the newest N lines races those
# concurrent line-atomic appends and can lose the file outright on a
# crash mid-rewrite. Rotation by atomic rename never reads the file's
# content at all.
# ---------------------------------------------------------------------------


def test_rotate_log_if_oversized_leaves_undersize_log_untouched(tmp_path):
    lib = _load_lib()
    log_path = tmp_path / "routing_log.jsonl"
    log_path.write_text('{"rule": "small"}\n')

    lib._rotate_log_if_oversized(log_path)

    assert log_path.read_text() == '{"rule": "small"}\n'
    assert not log_path.with_name(log_path.name + ".1").exists()


def test_rotate_log_if_oversized_rotates_via_atomic_rename(tmp_path, monkeypatch):
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path = tmp_path / "routing_log.jsonl"
    log_path.write_text("x" * 100)

    lib._rotate_log_if_oversized(log_path)

    assert not log_path.exists()
    rotated = tmp_path / "routing_log.jsonl.1"
    assert rotated.exists()
    assert rotated.read_text() == "x" * 100


def test_rotate_log_if_oversized_clobbers_prior_generation(tmp_path, monkeypatch):
    """Exactly one older generation retained -- ``.1`` clobbered, never
    accumulated into ``.2``."""
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path = tmp_path / "routing_log.jsonl"
    (tmp_path / "routing_log.jsonl.1").write_text("STALE-OLD-GENERATION")
    log_path.write_text("FRESH" * 5)

    lib._rotate_log_if_oversized(log_path)

    rotated = tmp_path / "routing_log.jsonl.1"
    assert rotated.read_text() == "FRESH" * 5
    assert not (tmp_path / "routing_log.jsonl.2").exists()


def test_rotate_log_if_oversized_tolerates_concurrent_rotation_race(tmp_path, monkeypatch):
    """A second process's rename hitting FileNotFoundError is expected and
    silently fine -- the file is rotated either way."""
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path = tmp_path / "routing_log.jsonl"
    log_path.write_text("x" * 100)

    def _simulated_concurrent_rotation(_src, _dst):
        raise FileNotFoundError("simulated: another process rotated first")

    monkeypatch.setattr(lib.os, "replace", _simulated_concurrent_rotation)

    lib._rotate_log_if_oversized(log_path)  # must not raise


def test_log_routing_event_rotates_before_append(tmp_path, monkeypatch):
    """Integration: an oversize routing_log.jsonl rotates, and the new
    event lands in the fresh (post-rotation) file."""
    log_path = tmp_path / "routing_log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log_path))
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path.write_text("x" * 100)

    lib.log_routing_event(rule="rule_a", outcome="allow")

    rotated = tmp_path / "routing_log.jsonl.1"
    assert rotated.read_text() == "x" * 100
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["rule"] == "rule_a"


def test_log_routing_event_rotation_failure_never_breaks_the_append(tmp_path, monkeypatch):
    """A non-ENOENT rotation failure (e.g. a permission error on the
    rename) must still let the append proceed."""
    log_path = tmp_path / "routing_log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log_path))
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    # Trailing newline: real log content is always line-terminated (every
    # log_routing_event write ends in "\n") -- this keeps the pre-existing
    # oversize content and the new append parseable as separate JSONL
    # lines even though rotation is simulated to fail.
    log_path.write_text("x" * 100 + "\n")

    def _boom(_src, _dst):
        raise PermissionError("simulated rotation failure")

    monkeypatch.setattr(lib.os, "replace", _boom)

    lib.log_routing_event(rule="rule_b", outcome="allow")  # must not raise

    assert log_path.exists()
    # The pre-existing "x" * 100 filler line is deliberately not valid
    # JSON (synthetic oversize content); only the newly appended LAST
    # line is asserted on, since that is the actual write under test.
    last_line = log_path.read_text().splitlines()[-1]
    assert json.loads(last_line)["rule"] == "rule_b"


# ---------------------------------------------------------------------------
# TOCTOU double-rotation clobber (code-review Critical, nexus-g3jw6,
# fix pass 2026-08-20) -- same fix as nexus._session_end_census's twin
# bug, ported here since this file rotates routing_log.jsonl the same
# way. See _session_end_census.py's TestRotationTOCTOUSerialization
# docstring for the full mechanism explanation.
# ---------------------------------------------------------------------------


def test_stale_oversize_observation_does_not_clobber_a_fresher_rotation(tmp_path, monkeypatch):
    """Deterministic simulation: fakes ONLY the FIRST ``Path.stat()``
    call against the log path (P2's cheap, pre-lock decision) as
    stale-oversize; every subsequent call sees the TRUE current size,
    exactly as the fix's re-check-under-the-lock would in a real
    interleaving. Must fail against the pre-fix code."""
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path = tmp_path / "routing_log.jsonl"
    rotated = tmp_path / "routing_log.jsonl.1"

    log_path.write_text("OLD" * 50)  # 150 bytes, genuinely oversize

    # P1: a real, correct rotation.
    lib._rotate_log_if_oversized(log_path)
    assert not log_path.exists()
    assert rotated.read_text() == "OLD" * 50

    # P1 reopens + appends -- the live file is small again.
    log_path.write_text("x\n")
    fresh_live_content = log_path.read_text()

    real_stat = pathlib.Path.stat
    call_count = {"n": 0}

    class _StaleStatResult:
        st_size = 999

    def _stat_first_call_on_log_path_is_stale(self, *args, **kwargs):
        if self == log_path:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _StaleStatResult()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", _stat_first_call_on_log_path_is_stale)

    lib._rotate_log_if_oversized(log_path)  # P2's rotation attempt

    assert rotated.read_text() == "OLD" * 50, (
        "P2 clobbered P1's real rotated history with a stale oversize "
        "observation (the TOCTOU double-rotation bug, nexus-g3jw6)"
    )
    assert log_path.exists()
    assert log_path.read_text() == fresh_live_content
    assert call_count["n"] >= 2, (
        "the fix must re-stat AT LEAST once more under the lock"
    )


def test_rotation_skips_entirely_when_lock_is_already_held(tmp_path, monkeypatch):
    """Non-blocking acquire: a rotator that loses the lock race must
    skip immediately (no wait, no retry, no raise)."""
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path = tmp_path / "routing_log.jsonl"
    log_path.write_text("x" * 100)

    lock_path = tmp_path / "routing_log.jsonl.rotate.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    held = os.fdopen(fd, "r+")
    lib._lock_file(held, blocking=True)  # simulate another process mid-rotation
    try:
        lib._rotate_log_if_oversized(log_path)  # must not raise, must not block

        assert log_path.read_text() == "x" * 100
        assert not (tmp_path / "routing_log.jsonl.1").exists()
    finally:
        lib._unlock_file(held)
        held.close()


def test_normal_rotation_still_works_under_the_lock(tmp_path, monkeypatch):
    """Regression pin: the lock must not itself prevent an ordinary,
    uncontended rotation from happening."""
    lib = _load_lib()
    monkeypatch.setattr(lib, "_ROUTING_LOG_ROTATION_MAX_BYTES", 10)
    log_path = tmp_path / "routing_log.jsonl"
    log_path.write_text("x" * 100)

    lib._rotate_log_if_oversized(log_path)

    assert not log_path.exists()
    assert (tmp_path / "routing_log.jsonl.1").read_text() == "x" * 100


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
