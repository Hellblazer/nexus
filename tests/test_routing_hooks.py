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

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time as _time
import urllib.error
import urllib.parse

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


def test_log_path_fallback_resolves_home_at_call_time_not_import_time(tmp_path, monkeypatch):
    """nexus-pfuns / T2 nexus/gc-purge-marker-xdist-leak-2026-08-20: the
    fallback used to be a module-level constant (``_DEFAULT_LOG_PATH =
    pathlib.Path.home() / ...``) frozen at import. A patch to
    ``pathlib.Path.home`` applied AFTER the module is already loaded (as
    ``_load_lib()`` does, re-exec'ing fresh each call -- but a patch
    applied to THIS already-loaded instance, mirroring how a real
    long-lived process would behave) could never take effect. Same
    import-time-default class already fixed once for
    ``gc_purge_marker.py``."""
    monkeypatch.delenv("NX_ROUTING_LOG_PATH", raising=False)
    # NEXUS_CONFIG_DIR now takes precedence over the home fallback (it is what
    # isolates this log from the real config dir under test). Clear it so this
    # test exercises the home-fallback branch it is actually about; the
    # override's own precedence is pinned separately below.
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    lib = _load_lib()
    new_home = tmp_path / "new-home"
    new_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", lambda: new_home)
    resolved = lib._log_path()
    assert resolved == new_home / ".config" / "nexus" / "routing_log.jsonl"


# ---------------------------------------------------------------------------
# log_routing_event writer swap (nexus-gjv9b PART 2): best-effort HTTP POST
# to the engine's routing_events table, metered-drop fallback -- never a
# JSONL append any more (that machinery stays in place, unused from this
# function, for PART 3's deferred deletion; see the rotation tests below).
# ---------------------------------------------------------------------------


def _drop_records(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _isolate_endpoint_discovery(tmp_path, monkeypatch):
    """nexus-gjv9b PART 2 CRITICAL review fix: ``_engine_endpoint`` now
    also reads a ServiceRegistry lease file and ``config.yml`` under
    ``NEXUS_CONFIG_DIR`` (t2_prefix_scan.py-style discovery), not just
    env vars. Without isolating that directory, these tests would
    resolve against whatever is REALLY configured on the box running
    them (a live lease, a real service_url) instead of the scenario
    each test constructs -- the identical class of leak the routing-log/
    dropped-writes/pre-close-verification isolation fixes in this same
    bead already closed for their own env surfaces."""
    cfg_dir = tmp_path / "isolated-nexus-config"
    cfg_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    return cfg_dir


def test_log_routing_event_no_engine_env_drops_to_meter(tmp_path, monkeypatch):
    """The common case: NX_SERVICE_HOST/PORT/TOKEN unset, no lease file,
    no config.yml -- no network attempt at all, straight to the metered
    drop, and the JSONL log is never touched."""
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    log_path = tmp_path / "routing_log.jsonl"
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log_path))
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    lib = _load_lib()

    lib.log_routing_event(rule="rule_a", outcome="allow")

    assert not log_path.exists(), "log_routing_event must never fall back to the JSONL log"
    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["hook"] == "routing_events"
    assert drops[0]["rows"] == 1


def test_log_routing_event_http_success_no_drop(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    log_path = tmp_path / "routing_log.jsonl"
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log_path))
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "9999")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    lib = _load_lib()

    sent: list[dict] = []

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        sent.append({
            "url": req.full_url,
            "body": json.loads(req.data.decode("utf-8")),
            "headers": dict(req.header_items()),
        })
        return _FakeResponse()

    # _lib._post_routing_event_http imports urllib.request lazily, inside
    # the function body -- it fetches the same process-wide sys.modules
    # entry patched here.
    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_b", outcome="deny", tool_name="Bash", session_id="sess-1")

    assert len(sent) == 1
    assert sent[0]["url"] == "http://127.0.0.1:9999/v1/telemetry/routing_events/record"
    assert sent[0]["body"]["rule"] == "rule_b"
    assert sent[0]["body"]["outcome"] == "deny"
    assert sent[0]["body"]["session_id"] == "sess-1"
    assert sent[0]["headers"]["Authorization"] == "Bearer test-token"
    assert not log_path.exists()
    assert _drop_records(drop_path) == []


def test_log_routing_event_resolves_from_lease_file_with_no_env_set(tmp_path, monkeypatch):
    """nexus-gjv9b PART 2 CRITICAL review fix: the routing hook must be
    able to reach a normal local install's engine WITHOUT any
    NX_SERVICE_* env exported -- nothing sets those into an interactive
    Claude Code process (.mcp.json, storage_service_daemon._spawn_service,
    and the install scripts all checked by the reviewer carry no such
    export). A live ServiceRegistry lease file is what every OTHER T2/T3
    client actually resolves through; this proves the routing hook does
    too now, mirroring t2_prefix_scan.py's identical discovery."""
    import time as _time

    cfg_dir = _isolate_endpoint_discovery(tmp_path, monkeypatch)
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    lease_path = cfg_dir / f"storage_service_addr.{os.getuid()}"
    lease_path.write_text(json.dumps({
        "status": "live",
        "heartbeat_epoch": _time.time(),
        "ttl": 60.0,
        "endpoint": {"host": "127.0.0.1", "port": 4242, "token": "lease-bearer-token"},
    }))
    lib = _load_lib()

    sent: list[dict] = []

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        sent.append({"url": req.full_url, "headers": dict(req.header_items())})
        return _FakeResponse()

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_lease", outcome="allow")

    assert len(sent) == 1
    assert sent[0]["url"] == "http://127.0.0.1:4242/v1/telemetry/routing_events/record"
    assert sent[0]["headers"]["Authorization"] == "Bearer lease-bearer-token"


def test_log_routing_event_expired_lease_is_ignored(tmp_path, monkeypatch):
    """A heartbeat older than its own TTL must not be trusted -- same
    stale-lease-is-absent contract as
    nexus.db.service_endpoint.discover_lease's local-supervisor leg."""
    import time as _time

    cfg_dir = _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    lease_path = cfg_dir / f"storage_service_addr.{os.getuid()}"
    lease_path.write_text(json.dumps({
        "status": "live",
        "heartbeat_epoch": _time.time() - 120.0,
        "ttl": 60.0,
        "endpoint": {"host": "127.0.0.1", "port": 4242, "token": "stale-bearer-token"},
    }))
    lib = _load_lib()

    lib.log_routing_event(rule="rule_stale", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1, "an expired lease must be treated as absent -- no attempt, straight to the drop meter"


def test_log_routing_event_resolves_from_config_yml_service_url(tmp_path, monkeypatch):
    """nexus-gjv9b PART 2 CRITICAL review fix: the managed-cloud onboarding
    path (`nx config set service_url/service_token`) persists ONLY to
    config.yml, never an env var -- a Desktop .mcpb install has no other
    credential source at all. The routing hook must read it."""
    cfg_dir = _isolate_endpoint_discovery(tmp_path, monkeypatch)
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    (cfg_dir / "config.yml").write_text(
        "credentials:\n"
        "  service_url: https://api.example-managed.test\n"
        "  service_token: managed-bearer-token\n"
    )
    lib = _load_lib()

    sent: list[dict] = []

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        sent.append({"url": req.full_url, "headers": dict(req.header_items())})
        return _FakeResponse()

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_cfg", outcome="allow")

    assert len(sent) == 1
    assert sent[0]["url"] == "https://api.example-managed.test/v1/telemetry/routing_events/record"
    assert sent[0]["headers"]["Authorization"] == "Bearer managed-bearer-token"


def test_log_routing_event_http_failure_drops_to_meter(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "9999")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    lib = _load_lib()

    import urllib.request as _ur

    def _boom(req, timeout=None):  # noqa: ARG001
        raise TimeoutError("simulated: engine unreachable")

    monkeypatch.setattr(_ur, "urlopen", _boom)

    lib.log_routing_event(rule="rule_c", outcome="allow")  # must not raise

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["hook"] == "routing_events"


def test_log_routing_event_swallows_errors(tmp_path, monkeypatch):
    """Telemetry must never crash the hook, even when the metered-drop
    fallback's own log path is unwritable."""
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    unwritable = tmp_path / "does-not-exist" / "dropped.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(unwritable))
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
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


# nexus-gjv9b PART 2 writer swap: the two integration tests formerly
# here (rotation-before-append, rotation-failure-never-breaks-append)
# exercised _rotate_log_if_oversized through log_routing_event -- a call
# chain that no longer exists (see that function's own docstring:
# rotation has no caller from this module any more, kept in place only
# for PART 3's deferred deletion). _rotate_log_if_oversized itself is
# still fully covered, directly, by the tests above and by the TOCTOU
# suite below.


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


# ── NEXUS_CONFIG_DIR isolation (2026-08-22) ──────────────────────────────────
#
# This was the ONE append log in the tree that ignored NEXUS_CONFIG_DIR. A test
# suite sets that var to isolate itself; the routing log ignored it and wrote to
# the REAL ~/.config/nexus on every routed tool call, so the nexus-pfuns
# real-config-dir mutation guard failed the whole run — twice on 2026-08-22,
# each time surfacing as `rc=1` with 14000+ passed and ZERO failing tests. The
# per-session capability census (same append-log shape) already resolved through
# nexus.config.nexus_config_dir; this brings the routing log into line.

def test_default_log_path_honours_nexus_config_dir(tmp_path, monkeypatch):
    lib = _load_lib()
    monkeypatch.delenv("NX_ROUTING_LOG_PATH", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    assert lib._default_log_path() == tmp_path / "routing_log.jsonl"


def test_default_log_path_falls_back_to_home_without_the_override(tmp_path, monkeypatch):
    lib = _load_lib()
    monkeypatch.delenv("NX_ROUTING_LOG_PATH", raising=False)
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert lib._default_log_path() == tmp_path / ".config" / "nexus" / "routing_log.jsonl"


def test_blank_nexus_config_dir_is_ignored(tmp_path, monkeypatch):
    """An empty/whitespace override must not resolve the log to a bare filename."""
    lib = _load_lib()
    monkeypatch.delenv("NX_ROUTING_LOG_PATH", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "   ")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert lib._default_log_path() == tmp_path / ".config" / "nexus" / "routing_log.jsonl"


def test_explicit_log_path_still_wins_over_config_dir(tmp_path, monkeypatch):
    lib = _load_lib()
    explicit = tmp_path / "explicit.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(explicit))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))

    assert lib._log_path() == explicit


# ---------------------------------------------------------------------------
# Data-token lease branch (nexus-gjv9b review fold-in round 3, critique
# CRITICAL 1): a fresh data-token lease must win over ANY static token
# (config.yml service_token, NX_SERVICE_TOKEN, or the ServiceRegistry
# lease's own token) once the base URL is known -- the RDR-005-armed-box
# case, where the static config.yml service_token is a scope=mint-locked
# credential that gets a real 401 if ever sent to a data path. Before
# this test, `_read_data_token_lease`'s own return value was exercised
# only indirectly through `_engine_endpoint()` unit tests -- nothing
# proved `log_routing_event`'s FULL path (resolve -> POST) actually sends
# the data token over the wire, which is the exact case the critique
# named as unverified.
# ---------------------------------------------------------------------------


def _write_data_token_lease(cfg_dir, *, base_url, tenant, token, expires_in=3600.0):
    host = urllib.parse.urlsplit(base_url).netloc or base_url
    digest = hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()
    lease_path = cfg_dir / f"data_token_lease.{digest}"
    lease_path.write_text(json.dumps({
        "format_version": 1,
        "token": token,
        "tenant": tenant,
        "base_url_digest": digest,
        "expires_at": _time.time() + expires_in,
        "ttl_seconds": expires_in,
        "minted_by_pid": os.getpid(),
    }))
    return lease_path


def test_log_routing_event_prefers_data_token_lease_over_config_yml_static_token(tmp_path, monkeypatch):
    """The RDR-005-armed-box case the critique named: a static
    config.yml service_token exists (the mint-locked credential) AND a
    fresh data-token lease exists for the same host -- the wire request
    must carry the DATA token, never the static one."""
    cfg_dir = _isolate_endpoint_discovery(tmp_path, monkeypatch)
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    (cfg_dir / "config.yml").write_text(
        "credentials:\n"
        "  service_url: https://api.example-managed.test\n"
        "  service_token: mint-locked-static-token\n"
    )
    _write_data_token_lease(
        cfg_dir,
        base_url="https://api.example-managed.test",
        tenant="default",
        token="fresh-data-token",
    )
    lib = _load_lib()

    sent: list[dict] = []

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        sent.append({"url": req.full_url, "headers": dict(req.header_items())})
        return _FakeResponse()

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_data_token", outcome="allow")

    assert len(sent) == 1
    assert sent[0]["headers"]["Authorization"] == "Bearer fresh-data-token", (
        "a fresh data-token lease must win over the static config.yml "
        "service_token -- sending the static (mint-locked) token here is "
        "the exact 401-swallowed-into-a-generic-drop the critique named"
    )


def test_log_routing_event_expired_data_token_lease_falls_back_to_static_token(tmp_path, monkeypatch):
    """The converse: an EXPIRED data-token lease must not be used at all
    -- falls back to the static config.yml token, same as no lease
    existing (never a mint attempt; this hook never mints)."""
    cfg_dir = _isolate_endpoint_discovery(tmp_path, monkeypatch)
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    (cfg_dir / "config.yml").write_text(
        "credentials:\n"
        "  service_url: https://api.example-managed.test\n"
        "  service_token: static-fallback-token\n"
    )
    _write_data_token_lease(
        cfg_dir,
        base_url="https://api.example-managed.test",
        tenant="default",
        token="stale-data-token",
        expires_in=-60.0,
    )
    lib = _load_lib()

    sent: list[dict] = []

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        sent.append({"headers": dict(req.header_items())})
        return _FakeResponse()

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_expired_data_token", outcome="allow")

    assert len(sent) == 1
    assert sent[0]["headers"]["Authorization"] == "Bearer static-fallback-token"


# ---------------------------------------------------------------------------
# Non-2xx cause classification (nexus-gjv9b review fold-in round 3,
# critique CRITICAL 1/2): a real auth or server failure must be metered
# with a distinguishing cause, not a generic "POST failed" the doctor
# check cannot tell apart from a transient connection blip.
# ---------------------------------------------------------------------------


def test_log_routing_event_401_response_meters_with_cause_401(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4242")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "wrong-scope-token")
    lib = _load_lib()

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_401", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "401"


def test_log_routing_event_404_response_meters_with_cause_route_absent(tmp_path, monkeypatch):
    """nexus-gjv9b review fold-in round 4: a plugin cut can ship this
    hook ahead of the paired engine tag -- the cloud engine has no
    routing_events route yet, so every hook decision 404s until the
    engine catches up. Must classify as route_absent (version skew),
    never a generic failure."""
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4242")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "some-token")
    lib = _load_lib()

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_404", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "route_absent"


def test_log_routing_event_405_response_meters_with_cause_route_absent(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4242")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "some-token")
    lib = _load_lib()

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_405", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "route_absent"


def test_log_routing_event_5xx_response_meters_with_cause_5xx(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4242")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "some-token")
    lib = _load_lib()

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_5xx", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "5xx"


def test_log_routing_event_connect_failure_meters_with_cause_connect(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4242")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "some-token")
    lib = _load_lib()

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_connect", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "connect"


def test_log_routing_event_timeout_meters_with_cause_timeout(tmp_path, monkeypatch):
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4242")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "some-token")
    lib = _load_lib()

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        raise TimeoutError("timed out")

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    lib.log_routing_event(rule="rule_timeout", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "timeout"


def test_log_routing_event_unresolvable_endpoint_meters_with_cause_unresolvable(tmp_path, monkeypatch):
    """No env, no lease, no config.yml -- the pre-existing 'common case'
    test above already proves the drop; this pins the CAUSE label on it."""
    _isolate_endpoint_discovery(tmp_path, monkeypatch)
    drop_path = tmp_path / "dropped_writes.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    lib = _load_lib()

    lib.log_routing_event(rule="rule_unresolvable", outcome="allow")

    drops = _drop_records(drop_path)
    assert len(drops) == 1
    assert drops[0]["cause"] == "unresolvable"


# ---------------------------------------------------------------------------
# Parity with t2_prefix_scan.py (nexus-gjv9b review fold-in round 3,
# code-review item 2): _read_service_lease/_read_lease,
# _read_data_token_lease, and _read_config_yml_credentials were all
# "ported verbatim" from t2_prefix_scan.py, and one of the three
# docstrings already CLAIMED this suite existed before it did. The two
# files use different Path-import conventions (t2_prefix_scan.py:
# `from pathlib import Path`; routing/_lib.py: `import pathlib`), so a
# byte-diff of the source would false-positive on that alone -- this
# runs BOTH implementations against the SAME on-disk lease/config
# layout instead and asserts identical return values, function by
# function, across every branch each one documents (fresh, expired,
# malformed, missing, wrong digest).
# ---------------------------------------------------------------------------

T2_PREFIX_SCAN_PATH = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "t2_prefix_scan.py"


def _load_t2_prefix_scan():
    spec = importlib.util.spec_from_file_location("nx_t2_prefix_scan", T2_PREFIX_SCAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parity_read_service_lease_fresh(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    lease_path = tmp_path / f"storage_service_addr.{os.getuid()}"
    lease_path.write_text(json.dumps({
        "status": "live",
        "heartbeat_epoch": _time.time(),
        "ttl": 60.0,
        "endpoint": {"host": "127.0.0.1", "port": 4242, "token": "tok"},
    }))
    assert lib._read_service_lease(tmp_path) == scan._read_lease(tmp_path)


def test_parity_read_service_lease_expired(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    lease_path = tmp_path / f"storage_service_addr.{os.getuid()}"
    lease_path.write_text(json.dumps({
        "status": "live",
        "heartbeat_epoch": _time.time() - 120.0,
        "ttl": 60.0,
        "endpoint": {"host": "127.0.0.1", "port": 4242, "token": "tok"},
    }))
    assert lib._read_service_lease(tmp_path) is None
    assert scan._read_lease(tmp_path) is None


def test_parity_read_service_lease_malformed(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    lease_path = tmp_path / f"storage_service_addr.{os.getuid()}"
    lease_path.write_text("not json")
    assert lib._read_service_lease(tmp_path) is None
    assert scan._read_lease(tmp_path) is None


def test_parity_read_service_lease_missing(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    assert lib._read_service_lease(tmp_path) is None
    assert scan._read_lease(tmp_path) is None


def test_parity_read_data_token_lease_fresh_match(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    _write_data_token_lease(tmp_path, base_url="http://127.0.0.1:4242", tenant="default", token="tok")
    base_url = "http://127.0.0.1:4242"
    assert lib._read_data_token_lease(tmp_path, base_url) == "tok"
    assert scan._read_data_token_lease(tmp_path, base_url) == "tok"


def test_parity_read_data_token_lease_wrong_digest(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    _write_data_token_lease(tmp_path, base_url="http://127.0.0.1:4242", tenant="default", token="tok")
    other_url = "http://127.0.0.1:9999"
    assert lib._read_data_token_lease(tmp_path, other_url) is None
    assert scan._read_data_token_lease(tmp_path, other_url) is None


def test_parity_read_data_token_lease_expired(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    _write_data_token_lease(
        tmp_path, base_url="http://127.0.0.1:4242", tenant="default", token="tok", expires_in=-1.0,
    )
    base_url = "http://127.0.0.1:4242"
    assert lib._read_data_token_lease(tmp_path, base_url) is None
    assert scan._read_data_token_lease(tmp_path, base_url) is None


def test_parity_read_data_token_lease_missing(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    base_url = "http://127.0.0.1:4242"
    assert lib._read_data_token_lease(tmp_path, base_url) is None
    assert scan._read_data_token_lease(tmp_path, base_url) is None


def test_parity_read_config_yml_credentials_present(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    (tmp_path / "config.yml").write_text(
        "credentials:\n"
        "  service_url: https://api.example.test\n"
        "  service_token: tok-123\n"
    )
    assert lib._read_config_yml_credentials(tmp_path) == scan._read_config_yml_credentials(tmp_path)


def test_parity_read_config_yml_credentials_absent(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    assert lib._read_config_yml_credentials(tmp_path) == {}
    assert scan._read_config_yml_credentials(tmp_path) == {}


def test_parity_read_config_yml_credentials_no_credentials_block(tmp_path):
    scan = _load_t2_prefix_scan()
    lib = _load_lib()
    (tmp_path / "config.yml").write_text("install:\n  mode: managed\n")
    assert lib._read_config_yml_credentials(tmp_path) == {}
    assert scan._read_config_yml_credentials(tmp_path) == {}
