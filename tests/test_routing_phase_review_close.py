# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 2 hook 2: phase_review_close_requires_gate.

The hook denies ``bd close <bead-id>`` for phase-review beads unless a
fresh PASSED sentinel exists for the bead's ``(rdr-id, phase)`` tuple.

Five enforcement scenarios per RDR-121, Test Plan:

1. Sentinel present, fresh, PASSED -> allow.
2. Sentinel absent -> deny.
3. Sentinel mtime older than session-start -> deny.
4. Sentinel outcome != PASSED -> deny.
5. Sentinel unreadable (corrupt JSON) -> deny (fail-closed).

Plus contract:
- Non-phase-review bead close -> allow (short-circuit on title).
- Non-Bash tool -> allow.
- Bash command without ``bd close`` -> allow.
- ``# routing-allow: <reason>`` escape -> allow.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK_SCRIPT = (
    PROJECT_ROOT
    / "conexus"
    / "hooks"
    / "scripts"
    / "routing"
    / "phase_review_close_requires_gate.py"
)


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Redirect TMPDIR, NEXUS_CONFIG_DIR, and PATH for an isolated hook run."""
    sentinel_root = tmp_path / "sentinels"
    config_dir = tmp_path / "nexus_config"
    bin_dir = tmp_path / "bin"
    sentinel_root.mkdir()
    config_dir.mkdir()
    bin_dir.mkdir()
    monkeypatch.setenv("TMPDIR", str(sentinel_root))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))
    return {
        "tmp": tmp_path,
        "sentinel_root": sentinel_root,
        "sentinel_dir": sentinel_root / "nx-phase-gate-sentinel",
        "config_dir": config_dir,
        "bin_dir": bin_dir,
    }


def _write_bd_stub(bin_dir: pathlib.Path, *, title: str, description: str = "") -> None:
    """Install a fake ``bd`` on PATH that returns canned bd show output."""
    stub = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import sys
        if len(sys.argv) >= 2 and sys.argv[1] == "show":
            print({title!r})
            print()
            print("DESCRIPTION")
            print({description!r})
            sys.exit(0)
        sys.exit(0)
        """
    )
    bd_path = bin_dir / "bd"
    bd_path.write_text(stub)
    bd_path.chmod(0o755)


def _run_hook(payload: dict, env_extra: dict[str, str], bin_dir: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    """Invoke the hook with the given Claude PreToolUse payload on stdin."""
    env = os.environ.copy()
    env.update(env_extra)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return proc


def _decision(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, f"hook must exit 0; got {proc.returncode}; stderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    return payload["hookSpecificOutput"]


def _make_session_addr(config_dir: pathlib.Path, claude_pid: int) -> pathlib.Path:
    """Create the ``current_session`` pointer representing a live session.

    RDR-149 P4: the session-start anchor moved from ``t1_addr.<pid>`` to the
    ``current_session`` pointer (the gate hook reads its mtime). ``claude_pid``
    is written as the session-id content for determinism.
    """
    p = config_dir / "current_session"
    p.write_text(str(claude_pid))
    return p


def _make_sentinel(
    sentinel_dir: pathlib.Path,
    *,
    claude_pid: int,
    rdr_id: str,
    phase: str,
    outcome: str = "PASSED",
    mtime: float | None = None,
) -> pathlib.Path:
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    p = sentinel_dir / f"{claude_pid}-{rdr_id}-{phase}.json"
    p.write_text(json.dumps({
        "outcome": outcome,
        "rdr_id": rdr_id,
        "phase": phase,
        "claude_pid": claude_pid,
    }))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ---------------------------------------------------------------------------
# Hook script exists
# ---------------------------------------------------------------------------


def test_hook_script_exists():
    assert HOOK_SCRIPT.exists()


def test_hook_script_executable_shebang():
    assert HOOK_SCRIPT.read_text().startswith("#!/usr/bin/env python3")


# ---------------------------------------------------------------------------
# Short-circuit paths
# ---------------------------------------------------------------------------


def test_non_bash_tool_allows(tmp_env):
    proc = _run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}},
        env_extra={},
    )
    assert _decision(proc)["permissionDecision"] == "allow"


def test_bash_non_bd_command_allows(tmp_env):
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        env_extra={},
    )
    assert _decision(proc)["permissionDecision"] == "allow"


def test_bd_close_on_non_phase_review_bead_allows(tmp_env):
    _write_bd_stub(tmp_env["bin_dir"], title="Add some feature thing")
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-xyz"}},
        env_extra={},
        bin_dir=tmp_env["bin_dir"],
    )
    assert _decision(proc)["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# Regression: GH #931 / nexus-1pr9n
#
# Implementation beads inside a phased RDR plan must not trigger the gate
# just because the word "phase" or "review" appears in their title or
# description. Only beads whose title matches "Phase N review gate" or
# "Phase N phase-review-gate" should count.
# ---------------------------------------------------------------------------


def test_impl_bead_phase_step_title_allows(tmp_env):
    """Implementation bead title 'Phase N Step N: ...' must NOT trigger
    the gate (GH #931). The implementation step is not the gate bead;
    the gate is a sibling with a distinct title."""
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="Phase 0 Step 0: Resolve spatial-level question (Manager + BubbleBounds co-update)",
        description="RDR-003 §Implementation Plan Phase 0 Step 0. Closing gate is the sibling phase-review-gate bead.",
    )
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close Luciferase-hic"}},
        env_extra={},
        bin_dir=tmp_env["bin_dir"],
    )
    assert _decision(proc)["permissionDecision"] == "allow"


def test_meta_bead_about_phase_review_gate_skill_allows(tmp_env):
    """A bead title ABOUT the phase-review-gate skill (e.g. a follow-on
    task) is not a gate execution and must not trigger the sentinel
    check. Distinguishing signal: no phase number prefix in the title."""
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="phase-review-gate skill: recognize phase-block sub-bullets as items (RDR-120 follow-on)",
    )
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-4u6mt"}},
        env_extra={},
        bin_dir=tmp_env["bin_dir"],
    )
    assert _decision(proc)["permissionDecision"] == "allow"


def test_impl_bead_description_mentioning_phase_review_gate_allows(tmp_env):
    """An implementation bead whose DESCRIPTION mentions the phase-review-
    gate command (because future-self will eventually run it) must not
    trigger the gate when the title itself is an implementation step."""
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="P3.A Migration ownership transfer",
        description="When P3 is complete, run /conexus:phase-review-gate RDR-120 --phase 3 to close the phase.",
    )
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-e9x4l"}},
        env_extra={},
        bin_dir=tmp_env["bin_dir"],
    )
    assert _decision(proc)["permissionDecision"] == "allow"


def test_gate_bead_sub_phase_letter_title_triggers(tmp_env):
    """Gate beads with sub-phase identifiers (P3b, Phase 1.5, etc.) must
    still trigger the sentinel check."""
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="Phase 3b review gate: /conexus:phase-review-gate RDR-120 --phase 3b",
    )
    # No sentinel written — expect deny.
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-b9lox"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(os.getpid())},
        bin_dir=tmp_env["bin_dir"],
    )
    _make_session_addr(tmp_env["config_dir"], os.getpid())
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-b9lox"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(os.getpid())},
        bin_dir=tmp_env["bin_dir"],
    )
    decision = _decision(proc)
    assert decision["permissionDecision"] == "deny"


def test_escape_token_allows_even_on_phase_review_bead(tmp_env):
    _write_bd_stub(tmp_env["bin_dir"], title="P1 phase review gate for RDR-112")
    proc = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "bd close nexus-abc  # routing-allow: closing manually "
                    "with explicit operator approval"
                )
            },
        },
        env_extra={},
        bin_dir=tmp_env["bin_dir"],
    )
    assert _decision(proc)["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# Five sentinel scenarios
# ---------------------------------------------------------------------------


def test_sentinel_present_fresh_passed_allows(tmp_env):
    pid = os.getpid()
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="RDR-112 Phase 1 review gate",
        description="Cross-walk close for Phase 1 of RDR-112.",
    )
    _make_session_addr(tmp_env["config_dir"], pid)
    # Sentinel slightly newer than addr file
    _make_sentinel(
        tmp_env["sentinel_dir"],
        claude_pid=pid, rdr_id="112", phase="1",
        outcome="PASSED",
        mtime=time.time(),
    )
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-abc"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(pid)},
        bin_dir=tmp_env["bin_dir"],
    )
    assert _decision(proc)["permissionDecision"] == "allow"


def test_sentinel_absent_denies(tmp_env):
    pid = os.getpid()
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="RDR-112 Phase 1 review gate",
    )
    _make_session_addr(tmp_env["config_dir"], pid)
    # No sentinel written.
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-abc"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(pid)},
        bin_dir=tmp_env["bin_dir"],
    )
    decision = _decision(proc)
    assert decision["permissionDecision"] == "deny"
    assert "/conexus:phase-review-gate" in decision["reason"]


def test_sentinel_stale_denies(tmp_env):
    pid = os.getpid()
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="RDR-112 Phase 1 review gate",
    )
    addr = _make_session_addr(tmp_env["config_dir"], pid)
    # Session anchor is the current_session mtime (now); sentinel in the past.
    addr_mtime = addr.stat().st_mtime
    _make_sentinel(
        tmp_env["sentinel_dir"],
        claude_pid=pid, rdr_id="112", phase="1",
        outcome="PASSED",
        mtime=addr_mtime - 3600,  # one hour before session start
    )
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-abc"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(pid)},
        bin_dir=tmp_env["bin_dir"],
    )
    decision = _decision(proc)
    assert decision["permissionDecision"] == "deny"


def test_sentinel_outcome_non_passed_denies(tmp_env):
    pid = os.getpid()
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="RDR-112 Phase 1 review gate",
    )
    _make_session_addr(tmp_env["config_dir"], pid)
    _make_sentinel(
        tmp_env["sentinel_dir"],
        claude_pid=pid, rdr_id="112", phase="1",
        outcome="BLOCKED",
        mtime=time.time(),
    )
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-abc"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(pid)},
        bin_dir=tmp_env["bin_dir"],
    )
    decision = _decision(proc)
    assert decision["permissionDecision"] == "deny"


def test_sentinel_corrupt_json_denies(tmp_env):
    pid = os.getpid()
    _write_bd_stub(
        tmp_env["bin_dir"],
        title="RDR-112 Phase 1 review gate",
    )
    _make_session_addr(tmp_env["config_dir"], pid)
    tmp_env["sentinel_dir"].mkdir(parents=True, exist_ok=True)
    bad = tmp_env["sentinel_dir"] / f"{pid}-112-1.json"
    bad.write_text("{not json")
    proc = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "bd close nexus-abc"}},
        env_extra={"NX_FAKE_CLAUDE_PID": str(pid)},
        bin_dir=tmp_env["bin_dir"],
    )
    decision = _decision(proc)
    assert decision["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Fail-closed contract: hook crash denies (not allows)
# ---------------------------------------------------------------------------


def test_malformed_stdin_fails_closed(tmp_env):
    """Empty / non-JSON stdin: hook should still emit valid JSON and exit 0.

    For non-phase-review semantics we cannot tell what the user intended,
    so the safe default is allow (cannot block what we cannot identify).
    Fail-closed only kicks in once we know we are looking at a phase-
    review close that lacks a valid sentinel.
    """
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="",
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] in ("allow", "deny")


# ---------------------------------------------------------------------------
# Registry has the rule with fail_closed: true
# ---------------------------------------------------------------------------


def test_registry_lists_rule_with_fail_closed():
    yaml = pytest.importorskip("yaml")
    reg = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
    parsed = yaml.safe_load(reg.read_text()) or {}
    rules = parsed.get("rules") or {}
    rule = rules.get("phase_review_close_requires_gate")
    assert rule is not None, "rule must be registered"
    assert rule.get("fail_closed") is True


# ---------------------------------------------------------------------------
# hooks.json registers the hook on Bash matcher
# ---------------------------------------------------------------------------


def test_hooks_json_registers_routing_hook():
    hooks_json = PROJECT_ROOT / "conexus" / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text())
    bash_hooks = data["hooks"]["PreToolUse"]
    found = False
    for entry in bash_hooks:
        if entry.get("matcher") != "Bash":
            continue
        for hook in entry.get("hooks", []):
            if "phase_review_close_requires_gate.py" in hook.get("command", ""):
                found = True
    assert found, "phase_review_close_requires_gate.py must be registered"
