# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/qwentescence_local_supervisor_gate.py (bead nexus-u0mcx): the
mechanized release-skill Step 11d local-supervisor leg on qwentescence
(WSL2).

Round 2 (post-review) covers: the interactive-session dispatch mechanism
(never `claude -p`), minted-and-forced session ids (never auto-discovery),
boundary-value validation for every argv/remote-script value, and the
reinstall/downgrade/enable-unit safety gates.

Every test here injects its own command runner (and, where relevant, its
own Popen factory for :class:`DistroHold`) -- no real ssh, no real
qwentescence, no real WSL, per the bead's own instruction that this
script's LOGIC is unit-tested on the Mac without ssh. Only the live run
against the real box (done separately, by hand, as this bead's proof) hits
the network.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "qwentescence_local_supervisor_gate.py"
_spec = importlib.util.spec_from_file_location("qwentescence_local_supervisor_gate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules["qwentescence_local_supervisor_gate"] = gate  # a @dataclass resolves its annotations through sys.modules
_spec.loader.exec_module(gate)

VALID_SID = "aaaaaaaa-1111-2222-3333-444444444444"


def _unpack(call: tuple[list[str], str | None]) -> tuple[list[str], str]:
    """Unpacks a recorded ``ScriptedRunner`` call, asserting its stdin
    payload is present -- every staging call in this script always passes
    ``input=``, so a ``None`` here is a test-routing bug, not a real
    "no input" case worth silently tolerating."""
    argv, text = call
    assert text is not None
    return argv, text


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _kind_of(argv: list[str], input_text: str | None) -> str:
    """Classifies a call the way the real script actually shapes it, purely
    for test routing -- production code never calls this."""
    text = input_text or ""
    if len(argv) >= 3 and argv[0] == "ssh" and argv[2] == "echo":
        return "reachable"
    if str(gate.CLAUDE_CREDENTIALS) in argv:
        return "dispatch"
    if gate._CHECK_SCRIPT_NAME in " ".join(argv):
        return "check"
    if "rm -rf" in text:
        return "cleanup"
    if gate._HARNESS_DIR_NAME in text and "mkdir -p" in text:
        return "harness-stage"
    if gate._DRIVER_SCRIPT_NAME in text and "DRIVER_SCRIPT_EOF" in text:
        return "driver-stage"
    if "systemctl --user enable --now nexus-service" in text:
        return "systemd-enable"
    if "systemctl --user is-active --quiet nexus-service" in text:
        return "systemd-state"
    if "nx daemon service status --json" in text:
        return "status"
    if "uv tool install" in text:
        return "upgrade"
    if text.strip() == (gate.REMOTE_ENV_PREFIX + gate._VERSION_SCRIPT).strip():
        return "version"
    if "POST-PUBLISH DISPATCH CHECK" in text:
        return "check-stage"
    return "unknown"


class ScriptedRunner:
    """A fake :data:`gate.Runner`. ``responses`` maps a call "kind" (see
    :func:`_kind_of`) to a list of :class:`gate.CommandResult` consumed in
    order; the last response repeats once its list is exhausted, so a
    caller only needs to enumerate the DIFFERING calls. Records every call
    for assertions on order/count."""

    def __init__(self, responses) -> None:  # responses: dict[str, list[CommandResult]] (pyright can't type-check a dynamically-loaded module's attrs)
        self._responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
        argv = list(argv)
        self.calls.append((argv, input))
        kind = _kind_of(argv, input)
        bucket = self._responses.get(kind)
        if not bucket:
            raise AssertionError(
                f"ScriptedRunner: no response scripted for kind={kind!r} argv={argv!r}"
            )
        if len(bucket) > 1:
            return bucket.pop(0)
        return bucket[0]

    def count(self, kind: str) -> int:
        return sum(1 for argv, inp in self.calls if _kind_of(argv, inp) == kind)

    def calls_of(self, kind: str) -> list[tuple[list[str], str | None]]:
        return [c for c in self.calls if _kind_of(*c) == kind]


def _ok(stdout: str = "", stderr: str = ""):  # -> CommandResult
    return gate.CommandResult(0, stdout, stderr)


def _fail(rc: int = 1, stdout: str = "", stderr: str = "boom"):  # -> CommandResult
    return gate.CommandResult(rc, stdout, stderr)


class FakePopen:
    """Records terminate()/wait()/kill() calls and reports a controllable
    `poll()` state; never spawns a real process."""

    instances: list["FakePopen"] = []
    #: Class-level override for the NEXT instance's poll() behaviour.
    #: None (default) = "still running". Set an int to simulate an
    #: already-exited process (DistroHold.start's fail-fast check).
    next_poll_rc: int | None = None

    def __init__(self, argv) -> None:
        self.argv = list(argv)
        self.terminated = False
        self.killed = False
        self._poll_rc = FakePopen.next_poll_rc
        FakePopen.next_poll_rc = None
        FakePopen.instances.append(self)

    def poll(self):
        return self._poll_rc

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    FakePopen.instances.clear()
    FakePopen.next_poll_rc = None
    yield
    FakePopen.instances.clear()
    FakePopen.next_poll_rc = None


def _full_pass_responses(target_version: str = "7.60.0"):  # -> dict[str, list[CommandResult]]
    status_json = json.dumps({"port": 49999, "health": "ok", "pg": "up"})
    return {
        "reachable": [_ok("REACHABLE\n")],
        "systemd-state": [_ok("UNIT_STATE=active\nLINGER=yes\n")],
        "status": [_ok(status_json), _ok(status_json)],
        "version": [_ok(f"nx, version {target_version}\n")],
        "harness-stage": [_ok("")],
        "driver-stage": [_ok("")],
        "dispatch": [_ok(f"DRIVER_OK session_id={VALID_SID}\n")],
        "check-stage": [_ok("")],
        "check": [_ok("POST-PUBLISH DISPATCH CHECK PASSED -- session=sess1 violations=0\n")],
        "cleanup": [_ok("")],
    }


# ---------------------------------------------------------------------------
# Finding 7: boundary validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,pattern_attr,good,bad",
    [
        ("host", "_HOST_RE", "qwentescence", "qwen;rm -rf /"),
        ("host", "_HOST_RE", "my-host.example.com", "host name"),
        ("distro", "_DISTRO_RE", "Ubuntu", "Ubuntu (evil)"),
        ("distro", "_DISTRO_RE", "Ubuntu-24.04", "Ubuntu`whoami`"),
        ("remote_user", "_USER_RE", "nexus", "Nexus"),
        ("remote_user", "_USER_RE", "nexus", "nexus; id"),
        ("session_id", "_SESSION_ID_RE", VALID_SID, "sid with spaces"),
        ("session_id", "_SESSION_ID_RE", VALID_SID, "sid(parens)"),
        ("target_version", "_VERSION_RE", "7.63.0", "7.63.0; rm -rf /"),
        ("target_version", "_VERSION_RE", "7.63.0", "$(evil)"),
    ],
)
def test_validate_token_accepts_good_rejects_bad(field, pattern_attr, good, bad):
    pattern = getattr(gate, pattern_attr)
    gate._validate_token(field, good, pattern)  # no raise
    with pytest.raises(gate.GateError, match="refusing an unsafe"):
        gate._validate_token(field, bad, pattern)


def test_validate_options_all_good():
    opts = gate.Options(host="qwentescence", distro="Ubuntu", remote_user="nexus", session_id=VALID_SID)
    gate.validate_options(opts, "7.63.0")  # no raise


def test_validate_options_session_id_none_is_skipped():
    opts = gate.Options(session_id=None)
    gate.validate_options(opts, "7.63.0")  # no raise -- None is fine, only a set value is checked


@pytest.mark.parametrize(
    "kwargs,version",
    [
        ({"host": "qwen;evil"}, "7.63.0"),
        ({"distro": "Ubuntu`id`"}, "7.63.0"),
        ({"remote_user": "Nexus"}, "7.63.0"),
        ({"session_id": "bad sid"}, "7.63.0"),
        ({}, "7.63.0; rm -rf ~"),
    ],
)
def test_validate_options_rejects_hostile_value(kwargs, version):
    opts = gate.Options(**kwargs)
    with pytest.raises(gate.GateError, match="refusing an unsafe"):
        gate.validate_options(opts, version)


def test_mint_session_id_default_factory_is_valid():
    sid = gate.mint_session_id()
    gate._validate_token("session_id", sid, gate._SESSION_ID_RE)  # no raise


def test_mint_session_id_hostile_factory_raises():
    with pytest.raises(gate.GateError, match="refusing an unsafe"):
        gate.mint_session_id(factory=lambda: "not a valid sid!!")


# ---------------------------------------------------------------------------
# Individual steps: reachability, DistroHold
# ---------------------------------------------------------------------------


def test_ensure_reachable_ok():
    runner = ScriptedRunner({"reachable": [_ok("REACHABLE\n")]})
    gate.ensure_reachable(runner, gate.Options())  # no raise


def test_ensure_reachable_unreachable_raises_gate_error():
    runner = ScriptedRunner({"reachable": [_fail(255, stderr="Could not resolve hostname")]})
    with pytest.raises(gate.GateError, match="unreachable"):
        gate.ensure_reachable(runner, gate.Options())


def test_ensure_reachable_wrong_output_raises():
    runner = ScriptedRunner({"reachable": [_ok("something else\n")]})
    with pytest.raises(gate.GateError, match="unreachable"):
        gate.ensure_reachable(runner, gate.Options())


def test_distro_hold_start_stop_terminates_process():
    hold = gate.DistroHold(popen_factory=FakePopen)
    hold.start(gate.Options(hold_seconds=42), startup_check_sleep=lambda s: None)
    assert len(FakePopen.instances) == 1
    proc = FakePopen.instances[0]
    assert proc.argv[:3] == ["ssh", gate.DEFAULT_HOST, "wsl"]
    assert "42" in proc.argv
    hold.stop()
    assert proc.terminated is True


def test_distro_hold_stop_before_start_is_a_noop():
    hold = gate.DistroHold(popen_factory=FakePopen)
    hold.stop()  # must not raise
    assert FakePopen.instances == []


def test_distro_hold_stop_is_idempotent():
    hold = gate.DistroHold(popen_factory=FakePopen)
    hold.start(gate.Options(), startup_check_sleep=lambda s: None)
    hold.stop()
    hold.stop()  # second stop must not raise or double-terminate
    assert FakePopen.instances[0].terminated is True


def test_distro_hold_start_spawn_failure_raises_gate_error():
    def _raising_factory(argv):
        raise OSError("no such file or directory: ssh")

    hold = gate.DistroHold(popen_factory=_raising_factory)
    with pytest.raises(gate.GateError, match="could not start"):
        hold.start(gate.Options(), startup_check_sleep=lambda s: None)


def test_distro_hold_start_immediate_exit_raises_gate_error():
    """Live-finding-adjacent (review item 5): a process that exits before
    holding anything must be a NAMED exit-2 failure, not a silent success
    that later steps discover the hard way."""
    FakePopen.next_poll_rc = 255
    hold = gate.DistroHold(popen_factory=FakePopen)
    with pytest.raises(gate.GateError, match="exited immediately"):
        hold.start(gate.Options(), startup_check_sleep=lambda s: None)
    assert FakePopen.instances[0].terminated is False  # never got that far


# ---------------------------------------------------------------------------
# ensure_systemd_unit_active (finding 2/7)
# ---------------------------------------------------------------------------


def test_ensure_systemd_unit_active_already_active_single_call():
    runner = ScriptedRunner({"systemd-state": [_ok("UNIT_STATE=active\nLINGER=yes\n")]})
    gate.ensure_systemd_unit_active(runner, gate.Options())
    assert runner.count("systemd-state") == 1
    assert runner.count("systemd-enable") == 0


def test_ensure_systemd_unit_active_read_failure_raises():
    runner = ScriptedRunner({"systemd-state": [_fail(1, stderr="no systemctl")]})
    with pytest.raises(gate.GateError, match="could not read"):
        gate.ensure_systemd_unit_active(runner, gate.Options())


def test_ensure_systemd_unit_active_inactive_without_flag_refuses():
    runner = ScriptedRunner({"systemd-state": [_ok("UNIT_STATE=inactive\nLINGER=yes\n")]})
    with pytest.raises(gate.GateError, match="--allow-enable-unit"):
        gate.ensure_systemd_unit_active(runner, gate.Options(allow_enable_unit=False))
    assert runner.count("systemd-enable") == 0


def test_ensure_systemd_unit_active_inactive_with_flag_enables_and_rechecks():
    runner = ScriptedRunner(
        {
            "systemd-state": [
                _ok("UNIT_STATE=inactive\nLINGER=yes\n"),
                _ok("UNIT_STATE=active\nLINGER=yes\n"),
            ],
            "systemd-enable": [_ok("")],
        }
    )
    gate.ensure_systemd_unit_active(runner, gate.Options(allow_enable_unit=True))
    assert runner.count("systemd-enable") == 1
    assert runner.count("systemd-state") == 2


def test_ensure_systemd_unit_active_enable_command_fails_raises():
    runner = ScriptedRunner(
        {
            "systemd-state": [_ok("UNIT_STATE=inactive\nLINGER=yes\n")],
            "systemd-enable": [_fail(1, stderr="Unit not found")],
        }
    )
    with pytest.raises(gate.GateError, match="failed"):
        gate.ensure_systemd_unit_active(runner, gate.Options(allow_enable_unit=True))


def test_ensure_systemd_unit_active_still_inactive_after_enable_raises():
    runner = ScriptedRunner(
        {
            "systemd-state": [
                _ok("UNIT_STATE=inactive\nLINGER=yes\n"),
                _ok("UNIT_STATE=inactive\nLINGER=yes\n"),
            ],
            "systemd-enable": [_ok("")],
        }
    )
    with pytest.raises(gate.GateError, match="still"):
        gate.ensure_systemd_unit_active(runner, gate.Options(allow_enable_unit=True))


def test_ensure_systemd_unit_active_linger_not_yes_raises():
    runner = ScriptedRunner({"systemd-state": [_ok("UNIT_STATE=active\nLINGER=no\n")]})
    with pytest.raises(gate.GateError, match="Linger=no"):
        gate.ensure_systemd_unit_active(runner, gate.Options())


# ---------------------------------------------------------------------------
# read_service_status / wait_for_service_ready / assert_lease_port_stable
# ---------------------------------------------------------------------------


def test_read_service_status_parses_json():
    runner = ScriptedRunner({"status": [_ok('{"port": 123, "health": "ok", "pg": "up"}')]})
    data = gate.read_service_status(runner, gate.Options())
    assert data == {"port": 123, "health": "ok", "pg": "up"}


def test_read_service_status_bad_json_raises():
    runner = ScriptedRunner({"status": [_ok("not json")]})
    with pytest.raises(gate.GateError, match="valid JSON"):
        gate.read_service_status(runner, gate.Options())


def test_read_service_status_command_failure_raises():
    runner = ScriptedRunner({"status": [_fail(1, stderr="No storage service lease found")]})
    with pytest.raises(gate.GateError, match="failed"):
        gate.read_service_status(runner, gate.Options())


def test_wait_for_service_ready_succeeds_immediately():
    runner = ScriptedRunner({"status": [_ok(json.dumps({"port": 1, "health": "ok", "pg": "up"}))]})
    sleeps: list[float] = []
    status = gate.wait_for_service_ready(runner, gate.Options(), sleep_fn=sleeps.append)
    assert status["port"] == 1
    assert sleeps == []


def test_wait_for_service_ready_retries_then_succeeds():
    good = _ok(json.dumps({"port": 2, "health": "ok", "pg": "up"}))
    runner = ScriptedRunner(
        {
            "status": [
                _fail(1, stderr="No storage service lease found"),
                _fail(1, stderr="No storage service lease found"),
                good,
            ]
        }
    )
    sleeps: list[float] = []
    status = gate.wait_for_service_ready(
        runner, gate.Options(), timeout=30.0, poll_interval=3.0, sleep_fn=sleeps.append
    )
    assert status["port"] == 2
    assert sleeps == [3.0, 3.0]


def test_wait_for_service_ready_times_out_raises_gate_error():
    runner = ScriptedRunner({"status": [_fail(1, stderr="No storage service lease found")]})
    with pytest.raises(gate.GateError, match="never succeeded"):
        gate.wait_for_service_ready(
            runner, gate.Options(), timeout=6.0, poll_interval=3.0, sleep_fn=lambda s: None
        )


def test_assert_lease_port_stable_ok():
    j = json.dumps({"port": 5555, "health": "ok", "pg": "up"})
    runner = ScriptedRunner({"status": [_ok(j), _ok(j)]})
    sleeps: list[float] = []
    result = gate.assert_lease_port_stable(runner, gate.Options(), sleep_fn=sleeps.append)
    assert result["port"] == 5555
    assert sleeps == [4.0]
    assert runner.count("status") == 2


def test_assert_lease_port_stable_port_changed_raises():
    runner = ScriptedRunner(
        {
            "status": [
                _ok(json.dumps({"port": 111, "health": "ok", "pg": "up"})),
                _ok(json.dumps({"port": 222, "health": "ok", "pg": "up"})),
            ]
        }
    )
    with pytest.raises(gate.GateError, match="not stable"):
        gate.assert_lease_port_stable(runner, gate.Options(), sleep_fn=lambda s: None)


def test_assert_lease_port_stable_unhealthy_raises():
    j_bad = json.dumps({"port": 1, "health": "down", "pg": "up"})
    runner = ScriptedRunner({"status": [_ok(j_bad), _ok(j_bad)]})
    with pytest.raises(gate.GateError, match="health"):
        gate.assert_lease_port_stable(runner, gate.Options(), sleep_fn=lambda s: None)


def test_assert_lease_port_stable_pg_down_raises():
    j_bad = json.dumps({"port": 1, "health": "ok", "pg": "DOWN"})
    runner = ScriptedRunner({"status": [_ok(j_bad), _ok(j_bad)]})
    with pytest.raises(gate.GateError, match="PG"):
        gate.assert_lease_port_stable(runner, gate.Options(), sleep_fn=lambda s: None)


# ---------------------------------------------------------------------------
# ensure_version (finding 5/7): transition logging + reinstall/downgrade gates
# ---------------------------------------------------------------------------


def test_ensure_version_already_matches_logs_none_action():
    runner = ScriptedRunner({"version": [_ok("nx, version 7.60.0\n")]})
    logs: list[str] = []
    version = gate.ensure_version(runner, gate.Options(), "7.60.0", log=logs.append)
    assert version == "7.60.0"
    assert runner.count("upgrade") == 0
    assert any("action=none" in line for line in logs)


def test_ensure_version_mismatch_without_allow_reinstall_refuses():
    runner = ScriptedRunner({"version": [_ok("nx, version 7.59.0\n")]})
    with pytest.raises(gate.GateError, match="--allow-reinstall"):
        gate.ensure_version(runner, gate.Options(allow_reinstall=False), "7.60.0")
    assert runner.count("upgrade") == 0


def test_ensure_version_mismatch_with_allow_reinstall_upgrades():
    runner = ScriptedRunner(
        {
            "version": [_ok("nx, version 7.59.0\n"), _ok("nx, version 7.60.0\n")],
            "upgrade": [_ok("Installed conexus 7.60.0")],
        }
    )
    logs: list[str] = []
    version = gate.ensure_version(
        runner, gate.Options(allow_reinstall=True), "7.60.0", log=logs.append
    )
    assert version == "7.60.0"
    assert runner.count("upgrade") == 1
    assert any("action=upgrade" in line for line in logs)


def test_ensure_version_upgrade_command_fails_raises():
    runner = ScriptedRunner(
        {
            "version": [_ok("nx, version 7.59.0\n")],
            "upgrade": [_fail(1, stderr="network unreachable")],
        }
    )
    with pytest.raises(gate.GateError, match="failed"):
        gate.ensure_version(runner, gate.Options(allow_reinstall=True), "7.60.0")


def test_ensure_version_silent_noop_upgrade_trap_raises():
    runner = ScriptedRunner(
        {
            "version": [_ok("nx, version 7.59.0\n"), _ok("nx, version 7.59.0\n")],
            "upgrade": [_ok("hint: already installed, bumped transitive deps only")],
        }
    )
    with pytest.raises(gate.GateError, match="upgrade trap"):
        gate.ensure_version(runner, gate.Options(allow_reinstall=True), "7.60.0")


def test_ensure_version_downgrade_without_allow_downgrade_refuses():
    runner = ScriptedRunner({"version": [_ok("nx, version 7.60.0\n")]})
    with pytest.raises(gate.GateError, match="--allow-downgrade"):
        gate.ensure_version(
            runner, gate.Options(allow_reinstall=True, allow_downgrade=False), "7.59.0"
        )
    assert runner.count("upgrade") == 0


def test_ensure_version_downgrade_with_both_flags_succeeds():
    runner = ScriptedRunner(
        {
            "version": [_ok("nx, version 7.60.0\n"), _ok("nx, version 7.59.0\n")],
            "upgrade": [_ok("Installed conexus 7.59.0")],
        }
    )
    logs: list[str] = []
    version = gate.ensure_version(
        runner,
        gate.Options(allow_reinstall=True, allow_downgrade=True),
        "7.59.0",
        log=logs.append,
    )
    assert version == "7.59.0"
    assert any("action=downgrade" in line for line in logs)


def test_version_tuple_orders_numerically():
    assert gate._version_tuple("7.9.0") < gate._version_tuple("7.10.0")
    assert gate._version_tuple("7.60.0") == gate._version_tuple("7.60.0")


# ---------------------------------------------------------------------------
# Interactive-session dispatch (finding 6) -- replaces `claude -p`
# ---------------------------------------------------------------------------


def test_stage_harness_files_embeds_the_real_repo_files():
    runner = ScriptedRunner({"harness-stage": [_ok("")]})
    harness_dir = gate._stage_harness_files(runner, gate.Options())
    assert harness_dir == gate._harness_dir(gate.Options())
    [call] = runner.calls_of("harness-stage")
    _argv, input_text = _unpack(call)
    # The REAL files, verbatim -- never a reimplementation.
    assert "claude_start" in input_text
    assert "claude_fd_exec" in input_text or "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" in input_text
    assert "passed_by_default" in input_text
    assert "chmod +x" in input_text


def test_stage_harness_files_failure_raises_gate_error():
    runner = ScriptedRunner({"harness-stage": [_fail(1, stderr="disk full")]})
    with pytest.raises(gate.GateError, match="could not stage the interactive harness"):
        gate._stage_harness_files(runner, gate.Options())


def test_build_driver_script_forces_session_id_and_embeds_prompt():
    script = gate._build_driver_script(VALID_SID, "/home/nexus/nexus-u0mcx-harness")
    assert f"--session-id {VALID_SID}" in script
    assert gate.DISPATCH_PROMPT in script
    assert f"DRIVER_OK session_id={VALID_SID}" in script
    assert 'source "/home/nexus/nexus-u0mcx-harness/lib.sh"' in script
    assert "claude_start" in script
    assert "claude_prompt" in script
    assert "claude_exit" in script
    assert "trap cleanup EXIT" in script


def test_build_driver_script_two_phase_debounced_wait_never_trusts_a_prompt_echo():
    """Round-2 live fixes, both against the real host:

    1. A marker token NAMED in the prompt text gets echoed by the pane
       before Claude even starts responding, so the completion signal must
       be the busy-indicator's PRESENCE (phase 1, proves Claude actually
       started THIS turn), never a string the prompt itself contains.
    2. The busy indicator is NOT monotonic once phase 1 passes -- a
       background-agent dispatch (Claude Code v2.1.283) flickers it absent
       for a few seconds while genuinely still running, so phase 2 must
       DEBOUNCE (several consecutive absent checks) rather than trust a
       single snapshot."""
    script = gate._build_driver_script(VALID_SID, "/home/nexus/nexus-u0mcx-harness")
    poll_for_idx = script.index(f'poll_for "{gate._BUSY_INDICATOR_PATTERN}"')
    debounce_idx = script.index(f"_stable_clear -ge {gate._IDLE_DEBOUNCE_COUNT}")
    claude_exit_idx = script.rindex("claude_exit")
    assert poll_for_idx < debounce_idx < claude_exit_idx
    # Every consecutive-absence check re-evaluates the SAME pattern.
    assert script.count(gate._BUSY_INDICATOR_PATTERN) >= 2
    assert gate._IDLE_DEBOUNCE_COUNT >= 2  # a single check is exactly the bug this fixes
    # No word of the free-text prompt is a substring `poll_for` searches
    # for -- the busy-indicator pattern shares nothing with DISPATCH_PROMPT.
    assert gate._BUSY_INDICATOR_PATTERN not in gate.DISPATCH_PROMPT


def test_build_driver_script_settles_before_exit_for_a_deferred_completion_hook():
    """Third live finding: even a debounced-idle UI can precede a
    background agent's RDR-184 completion hook (BLOCKED_UNRESOLVED was
    observed live even after the idle debounce passed). A short settle
    window must run between the debounce loop succeeding and `claude_exit`
    tearing the session down."""
    script = gate._build_driver_script(VALID_SID, "/home/nexus/nexus-u0mcx-harness")
    debounce_idx = script.index(f"_stable_clear -ge {gate._IDLE_DEBOUNCE_COUNT}")
    settle_idx = script.index(f"sleep {gate._POST_IDLE_SETTLE_SECONDS}")
    claude_exit_idx = script.rindex("claude_exit")
    assert debounce_idx < settle_idx < claude_exit_idx
    assert gate._POST_IDLE_SETTLE_SECONDS > 0


def test_stage_interactive_driver_ok():
    runner = ScriptedRunner({"driver-stage": [_ok("")]})
    path = gate._stage_interactive_driver(
        runner, gate.Options(), VALID_SID, "/home/nexus/nexus-u0mcx-harness"
    )
    assert path == gate._driver_script_path(gate.Options())
    [call] = runner.calls_of("driver-stage")
    _argv, input_text = _unpack(call)
    assert VALID_SID in input_text


def test_stage_interactive_driver_failure_raises():
    runner = ScriptedRunner({"driver-stage": [_fail(1, stderr="permission denied")]})
    with pytest.raises(gate.GateError, match="could not stage the interactive dispatch driver"):
        gate._stage_interactive_driver(
            runner, gate.Options(), VALID_SID, "/home/nexus/nexus-u0mcx-harness"
        )


def test_dispatch_via_interactive_session_ok():
    runner = ScriptedRunner(
        {
            "harness-stage": [_ok("")],
            "driver-stage": [_ok("")],
            "dispatch": [_ok(f"DRIVER_OK session_id={VALID_SID}\n")],
        }
    )
    result = gate.dispatch_via_interactive_session(runner, gate.Options(), VALID_SID)
    assert result.returncode == 0
    [call] = runner.calls_of("dispatch")
    argv, _input = call
    assert argv[0] == sys.executable
    assert str(gate.CLAUDE_CREDENTIALS) in argv
    assert "--remote" in argv
    assert gate.DEFAULT_HOST in argv
    assert "--remote-shell" in argv
    remote_shell_idx = argv.index("--remote-shell") + 1
    assert gate.DEFAULT_DISTRO in argv[remote_shell_idx]
    assert gate.DEFAULT_REMOTE_USER in argv[remote_shell_idx]
    assert argv[-2] == "bash"
    assert argv[-1] == gate._driver_script_path(gate.Options())
    # No word of the free-text prompt is on this argv -- it lives only in
    # the staged driver script's heredoc, never the command line.
    assert not any("bead" in tok for tok in argv)


def test_dispatch_via_interactive_session_rejects_hostile_session_id_before_any_call():
    runner = ScriptedRunner({})
    with pytest.raises(gate.GateError, match="refusing an unsafe"):
        gate.dispatch_via_interactive_session(runner, gate.Options(), "not a valid sid!!")
    assert runner.calls == []


def test_dispatch_via_interactive_session_process_failure_raises_gate_failure():
    runner = ScriptedRunner(
        {
            "harness-stage": [_ok("")],
            "driver-stage": [_ok("")],
            "dispatch": [_fail(1, stderr="ssh: connection refused")],
        }
    )
    with pytest.raises(gate.GateFailure, match="failed"):
        gate.dispatch_via_interactive_session(runner, gate.Options(), VALID_SID)


def test_dispatch_via_interactive_session_missing_driver_ok_marker_raises():
    """rc=0 alone is not enough -- claude_start could have failed to reach
    the main prompt and the driver script still exit 0 through a code path
    that never prints DRIVER_OK. Must be checked explicitly."""
    runner = ScriptedRunner(
        {
            "harness-stage": [_ok("")],
            "driver-stage": [_ok("")],
            "dispatch": [_ok("some unrelated output, no marker")],
        }
    )
    with pytest.raises(gate.GateFailure, match="failed"):
        gate.dispatch_via_interactive_session(runner, gate.Options(), VALID_SID)


def test_dispatch_via_interactive_session_staging_failure_never_reaches_dispatch():
    runner = ScriptedRunner({"harness-stage": [_fail(1, stderr="disk full")]})
    with pytest.raises(gate.GateError, match="could not stage the interactive harness"):
        gate.dispatch_via_interactive_session(runner, gate.Options(), VALID_SID)
    assert runner.count("dispatch") == 0


# ---------------------------------------------------------------------------
# run_check_script
# ---------------------------------------------------------------------------


def test_run_check_script_passes_session_id_when_given():
    runner = ScriptedRunner(
        {
            "check-stage": [_ok("")],
            "check": [_ok(f"POST-PUBLISH DISPATCH CHECK PASSED -- session={VALID_SID} violations=0")],
        }
    )
    result = gate.run_check_script(runner, gate.Options(session_id=VALID_SID))
    assert result.returncode == 0
    [call] = runner.calls_of("check")
    argv, _input = call
    assert argv[-1] == VALID_SID
    assert gate._check_script_path(gate.Options()) in argv


def test_run_check_script_no_session_id_invokes_staged_script_by_path():
    runner = ScriptedRunner(
        {
            "check-stage": [_ok("")],
            "check": [_ok("POST-PUBLISH DISPATCH CHECK PASSED -- session=auto violations=0")],
        }
    )
    gate.run_check_script(runner, gate.Options())
    [call] = runner.calls_of("check")
    argv, _input = call
    assert argv[-1] == gate._check_script_path(gate.Options())


def test_run_check_script_rejects_hostile_session_id():
    runner = ScriptedRunner({})
    with pytest.raises(gate.GateError, match="refusing an unsafe"):
        gate.run_check_script(runner, gate.Options(session_id="bad sid"))
    assert runner.calls == []


def test_stage_check_script_embeds_the_real_script_and_env_prefix():
    runner = ScriptedRunner({"check-stage": [_ok("")]})
    path = gate._stage_check_script(runner, gate.Options())
    assert path == gate._check_script_path(gate.Options())
    [call] = runner.calls_of("check-stage")
    _argv, input_text = _unpack(call)
    assert "POST-PUBLISH DISPATCH CHECK" in input_text
    assert 'export PATH="$HOME/.local/bin:$PATH"' in input_text
    assert f"chmod +x {path}" in input_text


def test_stage_check_script_failure_raises_gate_error():
    runner = ScriptedRunner({"check-stage": [_fail(1, stderr="disk full")]})
    with pytest.raises(gate.GateError, match="could not stage the check script"):
        gate._stage_check_script(runner, gate.Options())


# ---------------------------------------------------------------------------
# _cleanup_staged
# ---------------------------------------------------------------------------


def test_cleanup_staged_removes_everything_this_gate_staged():
    runner = ScriptedRunner({"cleanup": [_ok("")]})
    gate._cleanup_staged(runner, gate.Options())
    [call] = runner.calls_of("cleanup")
    _argv, input_text = _unpack(call)
    assert gate._harness_dir(gate.Options()) in input_text
    assert gate._driver_script_path(gate.Options()) in input_text
    assert gate._check_script_path(gate.Options()) in input_text


def test_cleanup_staged_swallows_runner_exceptions():
    def _raising_runner(argv, **kwargs):
        raise RuntimeError("network blip")

    gate._cleanup_staged(_raising_runner, gate.Options())  # must not raise


# ---------------------------------------------------------------------------
# fetch_current_published_version
# ---------------------------------------------------------------------------


def test_fetch_current_published_version_ok():
    payload = json.dumps({"info": {"version": "7.61.0"}}).encode()
    version = gate.fetch_current_published_version(fetch=lambda url: payload)
    assert version == "7.61.0"


def test_fetch_current_published_version_missing_raises():
    payload = json.dumps({"info": {}}).encode()
    with pytest.raises(gate.GateError, match="could not determine"):
        gate.fetch_current_published_version(fetch=lambda url: payload)


# ---------------------------------------------------------------------------
# run_gate end-to-end (still zero real subprocesses)
# ---------------------------------------------------------------------------


def _sid_factory(sid: str = VALID_SID):
    return lambda: sid


def test_run_gate_full_pass_mints_own_session_id():
    runner = ScriptedRunner(_full_pass_responses())
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 0
    assert gate.VERDICT_PASS in report
    assert f"session id: {VALID_SID} (minted)" in report
    assert FakePopen.instances[0].terminated is True  # hold always stopped
    # The SAME minted id reached the interactive driver (never the dispatch
    # argv itself -- it lives only in the staged heredoc, finding 6) AND
    # the check script.
    [driver_call] = runner.calls_of("driver-stage")
    assert VALID_SID in (driver_call[1] or "")
    [check_call] = runner.calls_of("check")
    assert check_call[0][-1] == VALID_SID
    assert runner.count("cleanup") == 1


def test_run_gate_rejects_session_id_without_skip_dispatch():
    runner = ScriptedRunner({})
    code, report = gate.run_gate(
        gate.Options(session_id=VALID_SID, skip_dispatch=False),
        "7.60.0",
        runner=runner,
        hold=gate.DistroHold(popen_factory=FakePopen),
        sleep_fn=lambda s: None,
    )
    assert code == 2
    assert "only accepted together with --skip-dispatch" in report
    assert runner.calls == []  # refused before any network call


def test_run_gate_skip_dispatch_requires_session_id():
    runner = ScriptedRunner({})
    code, report = gate.run_gate(
        gate.Options(skip_dispatch=True, session_id=None),
        "7.60.0",
        runner=runner,
        hold=gate.DistroHold(popen_factory=FakePopen),
        sleep_fn=lambda s: None,
    )
    assert code == 2
    assert "--skip-dispatch requires --session-id" in report
    assert runner.calls == []


def test_run_gate_skip_dispatch_with_session_id_reuses_it_no_new_dispatch():
    responses = _full_pass_responses()
    del responses["harness-stage"]
    del responses["driver-stage"]
    del responses["dispatch"]
    runner = ScriptedRunner(responses)
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(skip_dispatch=True, session_id=VALID_SID),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
    )
    assert code == 0
    assert "dispatch SKIPPED" in report
    [check_call] = runner.calls_of("check")
    assert check_call[0][-1] == VALID_SID


def test_run_gate_check_script_reports_failed():
    responses = _full_pass_responses()
    responses["check"] = [
        gate.CommandResult(
            1, f"POST-PUBLISH DISPATCH CHECK FAILED -- session={VALID_SID} violations=2\n", ""
        )
    ]
    runner = ScriptedRunner(responses)
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 1
    assert gate.VERDICT_FAIL in report


def test_run_gate_check_script_prerequisite_absent():
    responses = _full_pass_responses()
    responses["check"] = [gate.CommandResult(2, "", "no ledger file for session")]
    runner = ScriptedRunner(responses)
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 2
    assert "prerequisite absent" in report.lower()


def test_run_gate_unreachable_box_exits_2_without_dispatch():
    runner = ScriptedRunner({"reachable": [_fail(255, stderr="Connection timed out")]})
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 2
    assert "unreachable" in report
    assert runner.count("dispatch") == 0
    assert FakePopen.instances == []  # hold never started


def test_run_gate_systemd_failure_exits_2_and_still_stops_hold():
    responses = _full_pass_responses()
    responses["systemd-state"] = [_fail(1, stderr="no systemctl")]
    runner = ScriptedRunner(responses)
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 2
    assert runner.count("dispatch") == 0
    assert FakePopen.instances[0].terminated is True


def test_run_gate_version_mismatch_without_flag_exits_2():
    responses = _full_pass_responses()
    responses["version"] = [_ok("nx, version 7.1.0\n")]
    runner = ScriptedRunner(responses)
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 2
    assert "--allow-reinstall" in report
    assert runner.count("dispatch") == 0


def test_run_gate_dispatch_failure_exits_1():
    responses = _full_pass_responses()
    responses["dispatch"] = [_fail(1, stderr="claude: authentication failed")]
    runner = ScriptedRunner(responses)
    hold = gate.DistroHold(popen_factory=FakePopen)
    code, report = gate.run_gate(
        gate.Options(),
        "7.60.0",
        runner=runner,
        hold=hold,
        sleep_fn=lambda s: None,
        session_id_factory=_sid_factory(),
    )
    assert code == 1
    assert runner.count("check") == 0  # never reached the assertion half
    assert FakePopen.instances[0].terminated is True
    assert runner.count("cleanup") == 1  # cleanup still runs on a checked failure


# ---------------------------------------------------------------------------
# argv parsing
# ---------------------------------------------------------------------------


def test_parse_args_defaults():
    opts, version = gate.parse_args([])
    assert opts.host == gate.DEFAULT_HOST
    assert opts.distro == gate.DEFAULT_DISTRO
    assert opts.remote_user == gate.DEFAULT_REMOTE_USER
    assert opts.hold_seconds == gate.DEFAULT_HOLD_SECONDS
    assert opts.session_id is None
    assert opts.skip_dispatch is False
    assert opts.allow_reinstall is False
    assert opts.allow_downgrade is False
    assert opts.allow_enable_unit is False
    assert version is None


def test_parse_args_overrides():
    opts, version = gate.parse_args(
        [
            "7.62.0",
            "--host",
            "otherhost",
            "--distro",
            "OtherDistro",
            "--remote-user",
            "bob",
            "--hold-seconds",
            "60",
            "--session-id",
            VALID_SID,
            "--skip-dispatch",
            "--allow-reinstall",
            "--allow-downgrade",
            "--allow-enable-unit",
        ]
    )
    assert version == "7.62.0"
    assert opts.host == "otherhost"
    assert opts.distro == "OtherDistro"
    assert opts.remote_user == "bob"
    assert opts.hold_seconds == 60
    assert opts.session_id == VALID_SID
    assert opts.skip_dispatch is True
    assert opts.allow_reinstall is True
    assert opts.allow_downgrade is True
    assert opts.allow_enable_unit is True


def test_main_uses_pypi_version_when_omitted(monkeypatch):
    monkeypatch.setattr(gate, "fetch_current_published_version", lambda: "7.63.0")
    captured = {}

    def fake_run_gate(opts, version, **kwargs):
        captured["version"] = version
        return 0, "ok"

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    code = gate.main([])
    assert code == 0
    assert captured["version"] == "7.63.0"
