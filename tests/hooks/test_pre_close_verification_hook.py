# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the PreToolUse close verification hook script.

nexus-4av2n scope (a): the hook BLOCKS a `bd close`/`bd done` when the
bead(s) being closed carry no review-completed marker, and only ever
stamps `verification=passed` on the marker-found branch. It used to be
advisory-only (never denied) and stamped `verification=passed`
unconditionally regardless of whether a marker existed -- a false audit
record, which is what this fix removes.

ROUND 2 (this file): after code-review-expert (T2 [21539]) and
substantive-critic (T2 [21540], not-justified) both returned round 1, this
file added coverage for the DUAL-SOURCE fix (T1 scratch OR T2 memory),
per-id differentiated stamping, loud stamp-failure warnings, and the
tightened bd-verb matcher.

nexus-fgekf (2026-08-30): the T2 leg is RETIRED — the divergence it routed
around is fixed (nexus-d76vc handoff re-lease, nexus-f7xyq loud dead-lease
failure; measured converged on a live session) and a durable marker must
not satisfy a close in a session that performed no review. The old
dual-source acceptance fixtures survive INVERTED as retirement pins; the
`fake_nx` memory stub survives to prove memory is never consulted.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil as _shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "pre_close_verification_hook.sh"
)

# A dedicated directory holding ONLY a `python3` symlink -- NOT the parent
# directory of the resolved interpreter. This repo's venv bin/ (where
# `python3` typically resolves in a dev shell) also ships the `nx` CLI
# alongside it, so putting that whole directory on PATH would silently
# defeat every "nx is unreachable" test below by exposing a real `nx`.
_PYTHON3_ISOLATED_DIR = Path(tempfile.mkdtemp(prefix="nx-hook-test-python3-"))
_PYTHON3_REAL = _shutil.which("python3") or sys.executable
if _PYTHON3_REAL:
    (_PYTHON3_ISOLATED_DIR / "python3").symlink_to(_PYTHON3_REAL)
_SAFE_PATH = f"{_PYTHON3_ISOLATED_DIR}:/usr/bin:/bin"

# nexus-pfuns: the override-escape path (NX_REVIEW_GATE_OVERRIDE=1) shells
# out to `routing/_lib.py`'s `log_routing_event`, which falls back to the
# REAL `~/.config/nexus/routing_log.jsonl` whenever NX_ROUTING_LOG_PATH is
# unset (T2 nexus/gc-purge-marker-xdist-leak-2026-08-20). Every `_run_hook`
# call gets an isolated default here so an override test that forgets to
# set NX_ROUTING_LOG_PATH explicitly can never leak into the real path --
# mirrors the `_PYTHON3_ISOLATED_DIR` pattern just above.
_ROUTING_LOG_ISOLATED_DIR = Path(tempfile.mkdtemp(prefix="nx-hook-test-routing-log-"))
_ISOLATED_ROUTING_LOG_PATH = _ROUTING_LOG_ISOLATED_DIR / "routing_log.jsonl"

# nexus-gjv9b PART 2 writer swap: `log_routing_event` no longer writes
# `routing_log.jsonl` at all in this subprocess (no NX_SERVICE_HOST/PORT/
# TOKEN reaches it here) -- it degrades straight to a METERED DROP, which
# falls back to the REAL `~/.config/nexus/dropped_writes.jsonl` whenever
# NX_DROPPED_WRITES_LOG_PATH is unset. Same isolation discipline as the
# routing-log default above: every `_run_hook` call gets its own isolated
# drop-meter path, or this file reintroduces the exact real-home-dir leak
# class the routing-log isolation was built to close.
_DROPPED_WRITES_ISOLATED_DIR = Path(tempfile.mkdtemp(prefix="nx-hook-test-dropped-writes-"))
_ISOLATED_DROPPED_WRITES_LOG_PATH = _DROPPED_WRITES_ISOLATED_DIR / "dropped_writes.jsonl"


def _make_payload(
    tool_name: str = "Bash",
    command: str = "bd close nexus-4yit",
    session_id: str = "test-session",
) -> str:
    return json.dumps({
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    })


@pytest.fixture
def mock_config_env(tmp_path):
    def _make(config: dict) -> dict[str, str]:
        scripts_dir = tmp_path / "hooks" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script = scripts_dir / "read_verification_config.py"
        config_json = json.dumps(config)
        script.write_text(f"print({repr(config_json)})\n")
        return {"CLAUDE_PLUGIN_ROOT": str(tmp_path)}

    return _make


@pytest.fixture
def fake_nx(tmp_path):
    """Install a fake `nx` on PATH covering BOTH subcommands the hook now
    calls: `nx scratch list` (T1) and `nx memory search <query>` (T2
    fallback, nexus-4av2n round 2 Critical-1). Python, not bash -- avoids
    shell-quoting hazards for an embedded per-query case table.

    `memory_by_query` maps a QUERY SUBSTRING to the raw stdout returned for
    that call (first match wins); anything unmatched gets `memory_default`.
    `*_unreachable=True` makes every call to that subcommand fail (nonzero
    exit) regardless of configured text -- the CAPABILITY-gap path, distinct
    from a reachable call that legitimately finds nothing.
    """

    def _make(
        scratch: str = "No scratch entries.",
        *,
        scratch_rc: int = 0,
        scratch_unreachable: bool = False,
        memory_by_query: dict[str, str] | None = None,
        memory_default: str = "No results found.",
        memory_rc: int = 0,
        memory_unreachable: bool = False,
        sleep_seconds: float = 0.0,
        call_log: Path | None = None,
    ) -> Path:
        """`sleep_seconds` (nexus-4av2n round 3): every invocation sleeps
        before responding -- deterministically reproduces a slow-but-
        working `nx` for the deadline-trip tests, without depending on
        real subprocess-spawn latency. `call_log`: every invocation
        appends one line (subcommand + query) so tests can assert call
        COUNT deterministically instead of timing."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir(exist_ok=True)
        nx_script = fake_bin / "nx"
        cases = memory_by_query or {}
        log_line = (
            f"with open({str(call_log)!r}, 'a') as _f:\n"
            "    _f.write(' '.join(args) + chr(10))\n"
            if call_log is not None else ""
        )
        nx_script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "args = sys.argv[1:]\n"
            f"{log_line}"
            f"time.sleep({sleep_seconds!r})\n"
            "if args[:2] == ['scratch', 'list']:\n"
            f"    if {scratch_unreachable!r}:\n"
            "        sys.exit(1)\n"
            f"    sys.stdout.write({scratch!r})\n"
            f"    sys.exit({scratch_rc!r})\n"
            "if args[:2] == ['memory', 'search']:\n"
            f"    if {memory_unreachable!r}:\n"
            "        sys.exit(1)\n"
            "    query = args[2] if len(args) > 2 else ''\n"
            f"    cases = {cases!r}\n"
            "    for k, v in cases.items():\n"
            "        if k in query:\n"
            "            sys.stdout.write(v)\n"
            f"            sys.exit({memory_rc!r})\n"
            f"    sys.stdout.write({memory_default!r})\n"
            f"    sys.exit({memory_rc!r})\n"
            "sys.exit(0)\n"
        )
        nx_script.chmod(0o755)
        return fake_bin

    return _make


@pytest.fixture
def fake_bd(tmp_path):
    """Install a fake `bd` on PATH that logs every `set-state` call to a
    file, so tests can assert which verification state got stamped (or
    that none did)."""

    def _make() -> tuple[Path, Path]:
        fake_bin = tmp_path / "bdbin"
        fake_bin.mkdir(exist_ok=True)
        log = tmp_path / "bd_calls.log"
        bd_script = fake_bin / "bd"
        bd_script.write_text(
            "#!/bin/bash\n"
            f'echo "$*" >> "{log}"\n'
            "exit 0\n"
        )
        bd_script.chmod(0o755)
        return fake_bin, log

    return _make


def _run_hook(
    stdin: str,
    *,
    path_prefix: str = "",
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    path = f"{path_prefix}:{_SAFE_PATH}" if path_prefix else _SAFE_PATH
    env = {
        **os.environ,
        "PATH": path,
        "NX_ROUTING_LOG_PATH": str(_ISOLATED_ROUTING_LOG_PATH),
        "NX_DROPPED_WRITES_LOG_PATH": str(_ISOLATED_DROPPED_WRITES_LOG_PATH),
        **(env_overrides or {}),
    }
    # Never let a real NX_SERVICE_HOST/PORT/URL/TOKEN leak in from the outer
    # shell and cause the routing hook to actually attempt a network call
    # in a test that didn't ask for one (nexus-gjv9b PART 2's endpoint
    # resolution reads these verbatim). NX_SERVICE_URL joined the strip
    # list here (nexus-a2qhz round-2 fold-in): stripping HOST/PORT/TOKEN
    # while leaving a test's own NX_SERVICE_URL (t2_service_env sets it,
    # not HOST/PORT) ambiently inherited produced a HALF-resolved
    # credential -- service_url present, no token -- which fails loudly
    # with a confusing "service_url is set but no service_token is
    # resolvable" rather than either fully resolving or failing the clean
    # "not configured" way. Callers that want the real test substrate
    # (TestF5RemedyRoundTripReal) now forward NX_SERVICE_URL/TOKEN
    # explicitly via env_overrides, same as every other credential this
    # helper strips.
    for _service_var in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
        if _service_var not in (env_overrides or {}):
            env.pop(_service_var, None)
    # Never let a real NX_REVIEW_GATE_OVERRIDE leak in from the outer shell
    # into a test that didn't ask for it.
    if "NX_REVIEW_GATE_OVERRIDE" not in (env_overrides or {}):
        env.pop("NX_REVIEW_GATE_OVERRIDE", None)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _get_decision(parsed: dict) -> str:
    return parsed.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _get_context(parsed: dict) -> str:
    return parsed.get("hookSpecificOutput", {}).get("additionalContext", "")


def _get_reason(parsed: dict) -> str:
    return parsed.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


# A marker entry pair in the exact `nx scratch list` shape
# (src/nexus/commands/scratch.py list_cmd): two lines, header + content.
#: nexus-e3mak: a marker counts only when it NAMES the full required
#: reviewer set. Appended by default so the many call sites below keep
#: expressing "a valid, complete marker" without each restating the roster;
#: pass reviewers="" (or a partial string) to build the incomplete marker the
#: gate must now refuse.
_FULL_REVIEWERS = "reviewers=code-review-expert,substantive-critic"


def _marker(tags: str, content: str, reviewers: str = _FULL_REVIEWERS) -> str:
    body = f"{content} {reviewers}".rstrip()
    return f"[abcd1234] {tags}  flagged=False\n  {body}\n"


# A marker entry pair in the exact `nx memory search` shape
# (src/nexus/commands/memory.py search_cmd): two lines, header + content.
def _t2_marker(project_title: str, content: str, reviewers: str = _FULL_REVIEWERS) -> str:
    body = f"{content} {reviewers}".rstrip()
    return f"[1] {project_title}  (developer, 2026-08-06T00:00:00Z)\n  {body}\n"


class TestRunHookIsolatesRoutingLog:
    """nexus-pfuns: `_run_hook` must never let a call reach the hook
    subprocess without an isolated NX_ROUTING_LOG_PATH -- the override-
    escape path (`_log_override_escape` in
    `pre_close_verification_hook.sh`) shells out to
    `routing/_lib.py:log_routing_event`, which writes the REAL
    `~/.config/nexus/routing_log.jsonl` whenever that env var is absent.
    Asserted by intercepting `subprocess.run`'s env kwarg directly --
    no real subprocess is spawned, so this cannot itself leak."""

    def test_default_env_always_carries_isolated_routing_log_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, dict[str, str]] = {}

        def _fake_run(*args, **kwargs):
            captured["env"] = kwargs.get("env") or {}

            class _Result:
                returncode = 0
                stdout = '{"hookSpecificOutput": {"permissionDecision": "allow"}}'
                stderr = ""

            return _Result()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        _run_hook(_make_payload())

        assert "NX_ROUTING_LOG_PATH" in captured["env"]
        routing_log_path = captured["env"]["NX_ROUTING_LOG_PATH"]
        real_path = str(Path.home() / ".config" / "nexus" / "routing_log.jsonl")
        assert routing_log_path != real_path
        assert routing_log_path == str(_ISOLATED_ROUTING_LOG_PATH)

    def test_explicit_override_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller-supplied NX_ROUTING_LOG_PATH in env_overrides must not be
        clobbered by the default -- env_overrides is applied last."""
        captured: dict[str, dict[str, str]] = {}

        def _fake_run(*args, **kwargs):
            captured["env"] = kwargs.get("env") or {}

            class _Result:
                returncode = 0
                stdout = '{"hookSpecificOutput": {"permissionDecision": "allow"}}'
                stderr = ""

            return _Result()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        _run_hook(_make_payload(), env_overrides={"NX_ROUTING_LOG_PATH": "/tmp/explicit-override.jsonl"})

        assert captured["env"]["NX_ROUTING_LOG_PATH"] == "/tmp/explicit-override.jsonl"

    def test_default_env_always_carries_isolated_dropped_writes_log_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nexus-gjv9b PART 2: log_routing_event's service-down fallback is
        now a metered drop (NX_DROPPED_WRITES_LOG_PATH), not the JSONL log
        -- this default must be just as isolated as NX_ROUTING_LOG_PATH's,
        or this file reintroduces the real-home-dir leak class the
        routing-log isolation above was built to close."""
        captured: dict[str, dict[str, str]] = {}

        def _fake_run(*args, **kwargs):
            captured["env"] = kwargs.get("env") or {}

            class _Result:
                returncode = 0
                stdout = '{"hookSpecificOutput": {"permissionDecision": "allow"}}'
                stderr = ""

            return _Result()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        _run_hook(_make_payload())

        assert "NX_DROPPED_WRITES_LOG_PATH" in captured["env"]
        drop_path = captured["env"]["NX_DROPPED_WRITES_LOG_PATH"]
        real_path = str(Path.home() / ".config" / "nexus" / "dropped_writes.jsonl")
        assert drop_path != real_path
        assert drop_path == str(_ISOLATED_DROPPED_WRITES_LOG_PATH)


class TestFastNoops:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists()
        assert os.access(SCRIPT, os.X_OK)

    def test_exits_zero_always(self) -> None:
        assert _run_hook(_make_payload()).returncode == 0

    def test_outputs_valid_json(self) -> None:
        parsed = json.loads(_run_hook(_make_payload()).stdout)
        assert "hookSpecificOutput" in parsed

    def test_fast_noop_non_bash_tool(self) -> None:
        result = _run_hook(_make_payload(tool_name="Write"))
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_fast_noop_non_matching_bash(self) -> None:
        result = _run_hook(_make_payload(command="ls -la"))
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_fast_noop_bd_list(self) -> None:
        result = _run_hook(_make_payload(command="bd list --status=in_progress"))
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_allow_when_on_close_false(self, mock_config_env) -> None:
        env = mock_config_env({"on_close": False})
        result = _run_hook(_make_payload(), env_overrides=env)
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_allow_when_config_reader_fails(self) -> None:
        result = _run_hook(
            _make_payload(),
            env_overrides={"CLAUDE_PLUGIN_ROOT": "/nonexistent/path"},
        )
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_graceful_empty_stdin(self) -> None:
        result = _run_hook("")
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_bd_done_pattern_matches(self, mock_config_env, fake_nx) -> None:
        """`bd done` is recognized the same as `bd close`."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx(_marker("review-completed,nexus-xyz", "review-completed: nexus-xyz"))
        result = _run_hook(
            _make_payload(command="bd done nexus-xyz"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "allow"


class TestMatcherTightening:
    """nexus-4av2n round 2 Important-4 (code-review-expert): the prior
    blanket grep matched `bd close|done|create` ANYWHERE in the raw command
    text, including inside an unrelated quoted argument. Now that a match
    can DENY the Bash call outright, a false match has real cost."""

    def test_bd_verb_mentioned_in_a_commit_message_does_not_trigger(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command='git commit -m "docs: bd close workflow notes"'),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        # Fast no-op path: no additionalContext at all (never reached the
        # coverage machinery).
        assert _get_context(parsed) == ""

    def test_bd_verb_mentioned_in_a_description_string_does_not_trigger(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command='echo "remember to bd close this later"'),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert _get_context(parsed) == ""

    def test_real_bd_close_in_a_compound_command_still_triggers(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command="echo starting && bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"


class TestDenyOnMissingMarker:
    """nexus-4av2n item 1: the hook must BLOCK, not advise, on a missing
    marker. Round 2: "missing" now requires BOTH T1 and T2 to be reachable
    and neither to cover -- a real absence, not a capability gap."""

    def test_denies_when_no_marker_anywhere(self, mock_config_env, fake_nx) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")  # T2 defaults to "No results found."
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        assert "nexus-abc12" in _get_reason(parsed)
        assert "review-completed" in _get_reason(parsed).lower()
        assert "NX_REVIEW_GATE_OVERRIDE" in _get_reason(parsed)

    def test_denies_when_marker_exists_for_a_different_bead_in_either_source(
        self, mock_config_env, fake_nx
    ) -> None:
        """Cross-marker vacuity guard (datum iii): a review-completed
        marker for an UNRELATED bead in EITHER T1 or T2 must not satisfy
        this bead's check."""
        env = mock_config_env({"on_close": True})
        scratch = _marker("review-completed,nexus-other", "review-completed: nexus-other")
        fake_bin = fake_nx(
            scratch,
            memory_by_query={"nexus-other": _t2_marker("nexus/x", "review-completed: nexus-other")},
        )
        result = _run_hook(
            _make_payload(command="bd close nexus-target"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"

    def test_denies_when_bead_id_present_but_not_tagged_review_completed(
        self, mock_config_env, fake_nx
    ) -> None:
        """An entry mentioning the bead id in an unrelated (non-review)
        context must not count -- this is the two-independent-greps bug
        the entry-anchored match replaces."""
        env = mock_config_env({"on_close": True})
        scratch = _marker("misc,nexus-target", "nexus-target mentioned but not a review marker")
        fake_bin = fake_nx(scratch)
        result = _run_hook(
            _make_payload(command="bd close nexus-target"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"

    def test_deny_does_not_stamp_any_verification_state(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """nexus-4av2n item 2 core: a blocked close must never acquire a
        verification record, false or otherwise."""
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx("No scratch entries.")
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"
        assert not log.exists() or log.read_text().strip() == ""


class TestAllowOnCoveredMarker:
    def test_allows_and_stamps_passed_when_t1_marker_covers_bead(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        scratch = _marker("review-completed,nexus-cotmr", "review-completed: nexus-cotmr — clean")
        fake_nx_bin = fake_nx(scratch)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-cotmr"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "nexus-cotmr" in _get_context(parsed)
        assert log.exists()
        assert "nexus-cotmr verification=passed" in log.read_text()

    def test_combined_marker_covers_multiple_bead_ids(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """nexus-4av2n datum (ii): one marker whose tags list several bead
        ids covers each of them (tag-contains, not exact per-bead key)."""
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-cotmr,nexus-tafjk",
            "review-completed: nexus-cotmr + nexus-tafjk — 2 rounds",
        )
        fake_nx_bin = fake_nx(scratch)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="for b in nexus-cotmr nexus-tafjk; do bd close $b; done"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        calls = log.read_text()
        assert "nexus-cotmr verification=passed" in calls
        assert "nexus-tafjk verification=passed" in calls


class TestT1OnlyCoverage:
    """nexus-fgekf (2026-08-30): the T2 memory leg is RETIRED. It existed
    for a CLI/MCP T1 scope divergence (nexus-4av2n round 2) whose both
    halves are since fixed (nexus-d76vc handoff re-lease; nexus-f7xyq dead
    lease fails loud), measured converged on a live session. A durable T2
    marker could satisfy a close in a much later session for a review
    nobody performed — every property that makes T2 right for knowledge
    makes it wrong for an attestation."""

    def test_t2_marker_no_longer_covers_when_t1_empty(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """THE retirement pin: the exact fixture that used to ALLOW via the
        T2 fallback (a perfect full-set marker in memory, T1 empty) now
        DENIES — a durable marker is not an attestation from this session."""
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx(
            "No scratch entries.",
            memory_by_query={
                "nexus-abc12": _t2_marker("nexus/review-nexus-abc12", "review-completed: nexus-abc12 clean"),
            },
        )
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        assert "T2" in _get_reason(parsed)  # the deny names the retirement
        assert not log.exists() or log.read_text().strip() == ""

    def test_t1_unreachable_allows_unverified_regardless_of_t2(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """T1 unreachable is a capability gap (post-f7xyq: a dead CLI lease
        fails loud): loud allow + unverified stamp, NEVER passed — and a T2
        marker changes nothing about it."""
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx(
            scratch_unreachable=True,
            memory_by_query={
                "nexus-abc12": _t2_marker("nexus/review-nexus-abc12", "review-completed: nexus-abc12 clean"),
            },
        )
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "WARNING" in _get_context(parsed) or "WARNING" in _get_reason(parsed)
        calls = log.read_text()
        assert "nexus-abc12 verification=unverified" in calls
        assert "nexus-abc12 verification=passed" not in calls

    def test_t1_covers_and_memory_is_never_consulted(
        self, mock_config_env, fake_nx, fake_bd, tmp_path
    ) -> None:
        """Positive path. Honest scope note (review [23832]): the COVERED
        path never consulted T2 even under the old dual-source hook (lazy
        short-circuit), so this test alone does not pin the retirement —
        test_uncovered_bead_makes_no_memory_call_either is the pin (the
        old hook demonstrably spawned `nx memory search` there)."""
        env = mock_config_env({"on_close": True})
        call_log = tmp_path / "nx_calls.log"
        fake_nx_bin = fake_nx(
            _marker("review-completed,nexus-abc12", "review-completed: nexus-abc12"),
            memory_unreachable=True,
            call_log=call_log,
        )
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "nexus-abc12 verification=passed" in log.read_text()
        assert "memory search" not in call_log.read_text()

    def test_uncovered_bead_makes_no_memory_call_either(
        self, mock_config_env, fake_nx, tmp_path
    ) -> None:
        """The old shape's per-uncovered-id `nx memory search` spawns are
        gone: an uncovered bead denies after the single scratch read."""
        env = mock_config_env({"on_close": True})
        call_log = tmp_path / "nx_calls.log"
        fake_bin = fake_nx("No scratch entries.", call_log=call_log)
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"
        calls = call_log.read_text()
        assert "memory search" not in calls
        assert calls.count("scratch list") == 1

    def test_t1_down_with_empty_t2_allows_unverified_the_named_corner(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """THE deliberately accepted corner change (critique [23831]):
        under the old dual-source gate, T1-down + T2-reachable-and-EMPTY
        was a DENY (both sources had to strike out). T1-only collapses
        every T1-down shape into allow-unverified — wider allow, never
        silent (stamps unverified, never passed). Named in the hook's
        header; pinned here so the widening stays a decision, not drift."""
        env = mock_config_env({"on_close": True})
        # memory defaults to reachable-and-"No results found." — the exact
        # old-deny fixture.
        fake_nx_bin = fake_nx(scratch_unreachable=True)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        calls = log.read_text()
        assert "nexus-abc12 verification=unverified" in calls
        assert "verification=passed" not in calls

    def test_t1_reachable_and_empty_denies(
        self, mock_config_env, fake_nx
    ) -> None:
        """Deny-on-absence stays: reachable-and-empty is a real absence."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"

    def test_mixed_bead_ids_covered_and_uncovered_denies_naming_only_uncovered(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx(
            _marker("review-completed,nexus-covered", "review-completed: nexus-covered"),
        )
        result = _run_hook(
            _make_payload(command="for b in nexus-covered nexus-uncov; do bd close $b; done"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        assert "nexus-uncov" in _get_reason(parsed)


class TestLoopVariableDatum:
    """nexus-4av2n datum (i), Hal 08-03: `for b in ...; do bd close $b; done`
    must resolve the REAL bead ids (from the `for ... in` list), not the
    literal string "$b"."""

    def test_loop_variable_resolves_real_ids_not_the_variable(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-cotmr,nexus-tafjk",
            "review-completed: nexus-cotmr + nexus-tafjk",
        )
        fake_bin = fake_nx(scratch)
        result = _run_hook(
            _make_payload(command="for b in nexus-cotmr nexus-tafjk; do bd close $b; done"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        # Pre-fix this would have looked up the literal "$b" and found
        # nothing -- a false-positive advisory (under the old model) or a
        # false DENY (under the new blocking model). Both real ids are
        # covered, so this must ALLOW.
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_loop_variable_with_one_uncovered_id_still_denies(
        self, mock_config_env, fake_nx
    ) -> None:
        """The loop-scan recovers real ids; if one of them genuinely lacks
        a marker, the close must still be blocked."""
        env = mock_config_env({"on_close": True})
        scratch = _marker("review-completed,nexus-cotmr", "review-completed: nexus-cotmr")
        fake_bin = fake_nx(scratch)
        result = _run_hook(
            _make_payload(command="for b in nexus-cotmr nexus-uncov; do bd close $b; done"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        assert "nexus-uncov" in _get_reason(parsed)
        assert "nexus-cotmr" not in _get_reason(parsed).split("Remedy")[0].split("found in T1 scratch for:")[1]

    def test_no_literal_bead_id_is_indeterminate_not_denied(
        self, mock_config_env, fake_nx
    ) -> None:
        """A truly dynamic id (no literal nexus-* anywhere) cannot be
        statically verified -- allow without stamping, not deny."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command="bd close $(cat /tmp/id.txt)"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "INDETERMINATE" in _get_context(parsed)


class TestOverride:
    """nexus-4av2n item 4: an explicit, auditable override."""

    def test_override_env_var_allows_and_stamps_overridden(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx("No scratch entries.")
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides={**env, "NX_REVIEW_GATE_OVERRIDE": "1"},
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "OVERRIDE" in _get_context(parsed)
        assert "nexus-abc12 verification=overridden" in log.read_text()

    def test_no_override_denies(self, mock_config_env, fake_nx) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"

    def test_override_value_other_than_1_does_not_bypass(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides={**env, "NX_REVIEW_GATE_OVERRIDE": "true"},
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"


class TestCapabilityHonestBothSourcesDown:
    """nexus-4av2n item 3(iv), narrowed at nexus-fgekf (T2 leg retired):
    'uncertain' is T1 unreachable — the nx binary absent, or `nx scratch list`
    failing (post-f7xyq that includes a dead CLI lease failing loud).
    Never brick the close, but never claim 'passed' either."""

    def test_t1_unreachable_allows_with_loud_warning_and_unverified_stamp(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx(scratch_unreachable=True, memory_unreachable=True)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "unreachable" in _get_context(parsed).lower() or "not verify" in _get_context(parsed).lower()
        assert "nexus-abc12 verification=unverified" in log.read_text()

    def test_nx_missing_entirely_allows_with_loud_warning(
        self, mock_config_env, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bd_bin),  # nx is NOT on this PATH at all
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "nexus-abc12 verification=unverified" in log.read_text()

    def test_t1_unreachable_plus_override_stamps_overridden_not_unverified(
        self, mock_config_env, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bd_bin),
            env_overrides={**env, "NX_REVIEW_GATE_OVERRIDE": "1"},
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        calls = log.read_text()
        assert "nexus-abc12 verification=overridden" in calls
        assert "unverified" not in calls


class TestDeadlineBudget:
    """nexus-4av2n round 3: wall-clock deadline
    (NX_CLOSE_GATE_DEADLINE_SECONDS test seam) the hook enforces on
    itself, denying deterministically rather than ever risking a harness
    kill mid-check. nexus-fgekf update: with the T2 leg retired, the
    single subprocess left is `nx scratch list`, so the deadline trips
    when THAT read blows the budget (its timeout is clamped to the
    remaining budget → TimeoutExpired → status 'deadline', distinct from
    the rc!=0 capability gap)."""

    def test_deadline_exceeded_denies_deterministically(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """(a) deadline-trip deny path via a stubbed slow `nx`."""
        env = mock_config_env({"on_close": True})
        # nx sleeps LONGER than the whole 0.5s budget: the scratch read's
        # clamped timeout expires -> TimeoutExpired -> deadline flavor.
        fake_nx_bin = fake_nx(scratch="No scratch entries.", sleep_seconds=0.9)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="for b in nexus-bud01 nexus-bud02; do bd close $b; done"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides={**env, "NX_CLOSE_GATE_DEADLINE_SECONDS": "0.5"},
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        reason = _get_reason(parsed)
        assert "0.5" in reason or "wall-clock" in reason.lower()
        assert not log.exists() or log.read_text().strip() == ""  # deny stamps nothing

    def test_deadline_exceeded_names_override_remedy(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx(scratch="No scratch entries.", sleep_seconds=0.9)
        result = _run_hook(
            _make_payload(command="for b in nexus-bud01 nexus-bud02; do bd close $b; done"),
            path_prefix=str(fake_bin),
            env_overrides={**env, "NX_CLOSE_GATE_DEADLINE_SECONDS": "0.5"},
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        assert "NX_REVIEW_GATE_OVERRIDE" in _get_reason(parsed)

    def test_deadline_override_downgrades_to_loud_allow(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx(scratch="No scratch entries.", sleep_seconds=0.9)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="for b in nexus-bud01 nexus-bud02; do bd close $b; done"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides={
                **env,
                "NX_CLOSE_GATE_DEADLINE_SECONDS": "0.5",
                "NX_REVIEW_GATE_OVERRIDE": "1",
            },
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        assert "OVERRIDE" in _get_context(parsed)
        assert "verification=overridden" in log.read_text()

    def test_fast_path_single_call_when_t1_covers(
        self, mock_config_env, fake_nx, tmp_path
    ) -> None:
        """(b) wall-clock ceiling test on the fast path, via call-COUNTING
        with the stub, not timing (deterministic)."""
        env = mock_config_env({"on_close": True})
        call_log = tmp_path / "calls.log"
        fake_bin = fake_nx(
            _marker("review-completed,nexus-cov00", "review-completed: nexus-cov00"),
            call_log=call_log,
        )
        result = _run_hook(
            _make_payload(command="bd close nexus-cov00"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"
        calls = call_log.read_text().splitlines() if call_log.exists() else []
        assert len(calls) == 1, calls
        assert calls[0].startswith("scratch"), calls

    def test_multiple_uncovered_ids_resolve_within_default_deadline_as_genuine_absence(
        self, mock_config_env, fake_nx
    ) -> None:
        """(c) several uncovered bead ids (the loop-close shape) with a
        REALISTICALLY FAST (not sleep-stubbed) nx must resolve WITHIN the
        default deadline and deny as a genuine absence -- not trip the
        deadline path. Asserts deterministically which of the two deny
        flavors fires."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")  # no sleep -- fast
        result = _run_hook(
            _make_payload(
                command="for b in nexus-bud01 nexus-bud02 nexus-bud03 nexus-bud04 nexus-bud05; do bd close $b; done"
            ),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        reason = _get_reason(parsed)
        for i in range(1, 6):
            assert f"nexus-bud{i:02d}" in reason, reason
        # Genuine-absence flavor, NOT the deadline flavor.
        assert "wall-clock" not in reason.lower()
        assert "VERIFIED within the hook's" not in reason


class TestStampFailureIsLoud:
    """nexus-4av2n round 2 Significant-d (both reviewers): a failed `bd
    set-state` must be observable (stderr), never silently swallowed."""

    def test_bd_set_state_failure_prints_warning_to_stderr(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        scratch = _marker("review-completed,nexus-cotmr", "review-completed: nexus-cotmr")
        fake_nx_bin = fake_nx(scratch)
        # A `bd` that always fails.
        fake_bd_bin = fake_nx_bin.parent / "bdfailbin"
        fake_bd_bin.mkdir(exist_ok=True)
        (fake_bd_bin / "bd").write_text("#!/bin/bash\nexit 1\n")
        (fake_bd_bin / "bd").chmod(0o755)
        result = _run_hook(
            _make_payload(command="bd close nexus-cotmr"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"  # never crashes/bricks the close
        assert "FAILED" in result.stderr or "WARNING" in result.stderr


class TestF2EnvPrefixOverride:
    """nexus-cr4lp F2: an inline ``NX_REVIEW_GATE_OVERRIDE=1`` prefix on
    the command itself must be PARSED (the bd verb recognized despite the
    leading env-assignment token, rules apply) and HONORED as an override
    -- not a silent, unaudited no-op via a parser miss (B2: pre-fix,
    the bd-verb matcher required ``tokens[0] == 'bd'``, so the env-
    prefixed form left ``has_close_or_done`` False and the WHOLE hook
    fast-no-op'd, never running the coverage check at all)."""

    def test_inline_env_prefixed_close_is_recognized_and_overridden(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx("No scratch entries.")
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="NX_REVIEW_GATE_OVERRIDE=1 bd close nexus-abc12"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed
        assert "OVERRIDE" in _get_context(parsed), parsed
        assert "nexus-abc12 verification=overridden" in log.read_text()

    def test_inline_override_emits_an_escape_routing_event(
        self, mock_config_env, fake_nx, tmp_path
    ) -> None:
        """nexus-gjv9b PART 2 writer swap: this subprocess has no
        NX_SERVICE_HOST/PORT/TOKEN, so `log_routing_event` degrades
        straight to a metered drop rather than the JSONL log
        (:func:`_record_dropped_routing_event`) -- the routing_log.jsonl
        assertion this test used to make no longer applies; a dropped
        write for hook="routing_events" is the observable proxy that an
        escape event was attempted."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        drop_path = tmp_path / "dropped_writes.jsonl"
        result = _run_hook(
            _make_payload(command="NX_REVIEW_GATE_OVERRIDE=1 bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides={**env, "NX_DROPPED_WRITES_LOG_PATH": str(drop_path)},
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed
        assert drop_path.exists(), "no routing event drop was recorded for the override"
        drops = [json.loads(l) for l in drop_path.read_text().splitlines() if l.strip()]
        assert any(d.get("hook") == "routing_events" for d in drops), drops

    def test_ambient_env_override_also_emits_an_escape_routing_event(
        self, mock_config_env, fake_nx, tmp_path
    ) -> None:
        """See test_inline_override_emits_an_escape_routing_event's
        docstring for why this checks the drop meter, not routing_log.jsonl."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        drop_path = tmp_path / "dropped_writes.jsonl"
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides={
                **env,
                "NX_DROPPED_WRITES_LOG_PATH": str(drop_path),
                "NX_REVIEW_GATE_OVERRIDE": "1",
            },
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed
        drops = [json.loads(l) for l in drop_path.read_text().splitlines() if l.strip()]
        assert any(d.get("hook") == "routing_events" for d in drops), drops


class TestF3ReasonBlindIdHarvesting:
    """nexus-cr4lp F3 (T2 nexus/guard-evidence-cluster-root-cause-2026-08-
    18, LEG D1): a bead id appearing ONLY inside a --reason/--description/
    --notes/-m OPTION VALUE must not be harvested as a required-coverage
    close target -- but genuine close targets (positional args, loop
    variables) must still be."""

    def test_id_only_in_reason_value_is_not_required_for_coverage(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        # nexus-target is a covered close target; nexus-lemv5 appears ONLY
        # inside the --reason prose and must not be demanded.
        scratch = _marker(
            "review-completed,nexus-target",
            "review-completed: nexus-target -- clean",
        )
        fake_bin = fake_nx(scratch)
        result = _run_hook(
            _make_payload(
                command=(
                    'bd close nexus-target --reason="residue tracked as '
                    'nexus-lemv5, not a close target"'
                )
            ),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed

    def test_id_only_in_description_equals_value_is_not_required(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-target",
            "review-completed: nexus-target -- clean",
        )
        fake_bin = fake_nx(scratch)
        result = _run_hook(
            _make_payload(
                command=(
                    "bd close nexus-target && bd create "
                    '--description="was blocked, see nexus-lemv5 for residue"'
                )
            ),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed

    def test_id_in_reason_value_is_still_a_denial_target_if_it_is_ALSO_the_close_positional(
        self, mock_config_env, fake_nx
    ) -> None:
        """The flag-value skip must not swallow an id that ALSO appears as
        the genuine positional close target elsewhere in the same
        command -- only the VALUE occurrence is skipped."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command='bd close nexus-uncov --reason="mentions nexus-uncov again"'),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny", parsed
        assert "nexus-uncov" in _get_reason(parsed)

    def test_loop_variable_ids_still_harvested_alongside_a_reason_flag(
        self, mock_config_env, fake_nx
    ) -> None:
        """datum (i)'s loop-variable recovery must survive: those ids sit
        as bare positional tokens in the `for ... in` segment, never a
        flag value, so F3's flag-value skip must not touch them."""
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-cotmr,nexus-tafjk",
            "review-completed: nexus-cotmr + nexus-tafjk",
        )
        fake_bin = fake_nx(scratch)
        result = _run_hook(
            _make_payload(
                command=(
                    "for b in nexus-cotmr nexus-tafjk; do bd close $b "
                    '--reason="batch close, ref nexus-unrelated"; done'
                )
            ),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed


class TestF4RemedySeparateCallWarning:
    """nexus-cr4lp F4: every denial's Remedy block must lead with the
    separate-tool-call warning -- the shared root cause of every report
    in the guard-evidence cluster."""

    def test_denial_remedy_opens_with_separate_call_warning(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command="bd close nexus-abc12"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny", parsed
        reason = _get_reason(parsed)
        assert "SEPARATE tool call" in reason, reason
        assert reason.index("Remedy") < reason.index("SEPARATE tool call")


class TestB3T2TitleOnlyMarker:
    """nexus-cr4lp B3's title-only T2 acceptance shape, INVERTED at
    nexus-fgekf: with the T2 leg retired, even the hook's OWN former
    printed ``-t review-<bead-id>`` T2 form must no longer satisfy
    coverage — the strongest historical T2 shape is the right regression
    fixture for the retirement."""

    def test_former_t2_title_form_no_longer_covers(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx(
            "No scratch entries.",
            memory_by_query={
                "nexus-b3ttl": _t2_marker(
                    "nexus/review-nexus-b3ttl",
                    "review-completed: clean, no bead id restated here",
                )
            },
        )
        result = _run_hook(
            _make_payload(command="bd close nexus-b3ttl"),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny", parsed


class TestF5RemedyRoundTripReal:
    """nexus-cr4lp F5: each remedy string the hook PRINTS, executed via
    the REAL ``nx`` CLI (THIS checkout's dev build -- never the live
    production install) as its OWN subprocess call, must actually satisfy
    the hook's own T1/T2 lookup afterward. Uses the real engine-backed
    T2/T1 substrate (``t2_service_env``); skips cleanly if the dev ``nx``
    console script cannot be found next to ``sys.executable``."""

    @staticmethod
    def _real_nx_dir() -> Path | None:
        d = Path(sys.executable).parent
        return d if (d / "nx").exists() else None

    # nexus-fgekf: the t2_memory remedy form is retired with the leg — the
    # hook prints only the T1 scratch form now, so that is the only remedy
    # left to round-trip.
    @pytest.mark.parametrize("remedy_kind", ["t1_scratch"])
    def test_printed_remedy_satisfies_the_hooks_own_lookup(
        self, remedy_kind, mock_config_env, t2_service_env, tmp_path: Path
    ) -> None:
        real_nx_dir = self._real_nx_dir()
        if real_nx_dir is None:
            pytest.skip("dev checkout `nx` console script not found next to sys.executable")
        real_nx = str(real_nx_dir / "nx")

        # nexus-gjv9b fix-pass (coordinator-flagged): TWO independent
        # sources of flake, both fixed here.
        #
        # (1) Fixed session/bead ids: this test writes a real T1 scratch
        # marker via the real `nx` CLI with no teardown, so a second run
        # against the same live substrate found its own prior marker and
        # failed its own "no marker -> deny" precondition. A fresh uuid
        # suffix per invocation makes every run start against a marker
        # that provably cannot exist yet. ``[a-z0-9]+`` only (no hyphen)
        # keeps bead_id inside the single `\bnexus-[a-z0-9]+\b` token the
        # hook's own bead-id regex expects
        # (pre_close_verification_hook.sh:357) -- a hyphenated suffix
        # would sit OUTSIDE that token and be silently ignored.
        #
        # (2) Real mint_token bleed-through: this test's subprocesses
        # inherited the REAL ``~/.config/nexus`` (no NEXUS_CONFIG_DIR
        # override existed before this fix), so on a box with a mint_token
        # credential actually configured (RDR-005 step (d)) the shared-
        # CLI-dedicated-scope fallback tries to mint a fresh T1 session
        # using that REAL credential against THIS TEST's ephemeral
        # engine (t2_service_env) and gets a 401 -- "T1 unreachable ...
        # closing anyway" -- which ALSO satisfies 'allow' where the
        # precondition expects 'deny', independent of (1). Isolating
        # NEXUS_CONFIG_DIR to an empty per-test directory means
        # get_credential("mint_token") resolves to nothing, so the T1
        # lookup falls back to the NX_SERVICE_TOKEN this test's
        # t2_service_env fixture already provides -- the same isolation
        # discipline test_session_end_capability_census.py and
        # test_routing_hooks.py already apply for their own env leaks.
        run_suffix = uuid.uuid4().hex[:8]
        bead_id = f"nexus-r5a01{run_suffix}"  # single remedy form post-fgekf; dead t2 branch removed
        session_id = f"cr4lp-close-f5-{remedy_kind}-{run_suffix}"
        isolated_cfg_dir = str(tmp_path / "isolated-nexus-config")

        write_env = os.environ.copy()
        write_env["NX_SESSION_ID"] = session_id
        write_env["NX_T1_ALLOW_SHARED_FALLBACK"] = "1"
        # nexus-a2qhz round-2 review: the suite-wide production-write-guard
        # exemption is an IN-PROCESS override now (service_endpoint.
        # _test_only_opt_in_reason), never an env var, precisely so it
        # cannot leak into a subprocess's inherited os.environ the way
        # NX_ALLOW_PROD_WRITE used to. This subprocess IS a real
        # dev-checkout `nx` (this checkout's editable install, confirmed by
        # _real_nx_dir()) performing a real T1 write, so it must carry the
        # opt-in explicitly -- exactly like NX_SESSION_ID/
        # NX_T1_ALLOW_SHARED_FALLBACK above -- rather than inherit it.
        write_env["NX_ALLOW_PROD_WRITE"] = (
            "nexus-cr4lp F5 remedy round-trip test: writes to the "
            "hermetic t2_service_env test engine only, never production"
        )
        # nexus-gjv9b review fold-in teardown fix: isolate NEXUS_CONFIG_DIR
        # on the write subprocess too, same rationale as the module-level
        # _ROUTING_ENGINE_ISOLATED_CONFIG_DIR default in _run_hook --
        # without it a real mint_token credential configured on this box
        # (RDR-005 step (d)) makes the T1 write's own session resolution
        # attempt a real mint against THIS test's ephemeral engine and get
        # a 401, masking the very precondition this test exists to prove.
        write_env["NEXUS_CONFIG_DIR"] = isolated_cfg_dir

        # nexus-a2qhz round-2 fold-in: _run_hook now strips NX_SERVICE_URL
        # (alongside HOST/PORT/TOKEN) unconditionally unless a caller
        # forwards it explicitly -- the hook probes below need the REAL
        # test substrate's credentials (t2_service_env set these on THIS
        # process's os.environ), so pull them forward by hand rather than
        # relying on ambient inheritance.
        _service_url = os.environ.get("NX_SERVICE_URL", "")
        _service_token = os.environ.get("NX_SERVICE_TOKEN", "")
        assert _service_url and _service_token, (
            "t2_service_env did not set NX_SERVICE_URL/NX_SERVICE_TOKEN on "
            "this process -- the hook probes below would silently resolve "
            "nothing"
        )

        # nexus-e3mak: EXTRACT the command from what the hook actually
        # PRINTS, rather than keeping a hand-copied duplicate of it here.
        # This test's docstring already claimed it round-trips the printed
        # remedy; it did not -- it re-stated the remedy, so the two could
        # drift apart silently, and they DID the moment the required marker
        # form changed. Parsing the real output makes the claim true and
        # makes this test follow any future change to the remedy for free.
        env0 = mock_config_env({"on_close": True})
        probe = _run_hook(
            _make_payload(command=f"bd close {bead_id}", session_id=session_id),
            path_prefix=str(real_nx_dir),
            env_overrides={
                **env0,
                "NX_T1_ALLOW_SHARED_FALLBACK": "1",
                "NX_SERVICE_URL": _service_url,
                "NX_SERVICE_TOKEN": _service_token,
                # nexus-a2qhz round-2: this "nx scratch list" probe is ALSO
                # a real dev-checkout `nx` invocation, and its own T1
                # session resolution may mint a session token (a guarded
                # WRITE) as a bootstrap step even though the visible
                # operation is a read. Without this, the mint is refused,
                # `nx scratch list` exits nonzero, and the hook's own
                # fail-open T1-unreachable path silently turns the expected
                # "deny" into "allow" -- this env var is what keeps this
                # precondition probe hitting the real T1 codepath instead
                # of failing open.
                "NX_ALLOW_PROD_WRITE": write_env["NX_ALLOW_PROD_WRITE"],
                "NEXUS_CONFIG_DIR": isolated_cfg_dir,
                "NX_CLOSE_GATE_DEADLINE_SECONDS": "15",
            },
        )
        reason = _get_reason(json.loads(probe.stdout))
        assert _get_decision(json.loads(probe.stdout)) == "deny", (
            "precondition: with no marker written the hook must deny, or this "
            f"test never exercises the remedy it is here to verify -- got: {probe.stdout}"
        )

        want = "nx scratch put"  # the only remedy form post-fgekf
        line = next(
            (ln.strip() for ln in reason.splitlines() if ln.strip().startswith(want)),
            None,
        )
        assert line, f"the hook printed no {want!r} remedy to round-trip:\n{reason}"

        project = "nexus-cr4lp-f5-close-test"
        line = line.replace("<bead-id>", bead_id).replace("<project>", project)
        write_cmd = shlex.split(line)
        assert write_cmd[0] == "nx", write_cmd
        write_cmd[0] = real_nx

        # The remedy write is ITS OWN subprocess call -- never bundled
        # with the gated `bd close`.
        wproc = subprocess.run(
            write_cmd, env=write_env, capture_output=True, text=True, timeout=60,
        )
        assert wproc.returncode == 0, wproc.stderr

        env = mock_config_env({"on_close": True})
        result = _run_hook(
            _make_payload(command=f"bd close {bead_id}", session_id=session_id),
            path_prefix=str(real_nx_dir),
            env_overrides={
                **env,
                "NX_T1_ALLOW_SHARED_FALLBACK": "1",
                "NX_SERVICE_URL": _service_url,
                "NX_SERVICE_TOKEN": _service_token,
                "NX_ALLOW_PROD_WRITE": write_env["NX_ALLOW_PROD_WRITE"],
                "NEXUS_CONFIG_DIR": isolated_cfg_dir,
                "NX_CLOSE_GATE_DEADLINE_SECONDS": "15",
            },
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow", parsed


class TestSessionIdExport:
    """nexus-36q84: the hook is detached from any live nx-mcp process and
    cannot rely on env-var inheritance from a parent Claude session. It
    must extract ``session_id`` from its own stdin JSON payload (present
    on every hook invocation per the standard hook contract — see
    ``_make_payload``) and export it as ``NX_SESSION_ID`` before invoking
    ``nx scratch list``, so the CLI resolves the CORRECT session's T1 data
    instead of falling through to the machine-wide (and possibly
    clobbered-by-a-sibling-session) ``current_session`` flat file.
    """

    @staticmethod
    def _make_fake_nx(tmp_path: Path) -> Path:
        """A fake `nx` on PATH that logs the NX_SESSION_ID it observed
        for every invocation, then emits harmless scratch-list-shaped
        output so the hook's downstream marker checks don't blow up."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        nx_script = fake_bin / "nx"
        nx_script.write_text(
            "#!/bin/bash\n"
            'echo "NX_SESSION_ID=${NX_SESSION_ID:-<unset>}" >> "$NX_CALL_LOG"\n'
            'if [[ "$1" == "scratch" ]]; then echo "No scratch entries."; fi\n'
            'if [[ "$1" == "memory" ]]; then echo "No results found."; fi\n'
            "exit 0\n"
        )
        nx_script.chmod(0o755)
        return fake_bin

    def test_exports_session_id_from_stdin_payload_for_review_check(
        self, tmp_path, mock_config_env
    ) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"
        env = mock_config_env({"on_close": True})

        result = _run_hook(
            _make_payload(command="bd close nexus-4yit"),
            path_prefix=str(fake_bin),
            env_overrides={
                "NX_CALL_LOG": str(log_file),
                **env,
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=test-session" in log_contents, log_contents

    def test_exports_session_id_from_stdin_payload_for_rdr_close_check(
        self, tmp_path
    ) -> None:
        """The `bd create` / rdr-close-active branch also calls
        `nx scratch list` (to look up the active-close marker) — it must
        see the same exported NX_SESSION_ID."""
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        payload = json.dumps({
            "session_id": "rdr-close-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd create --title foo --description bar"},
        })

        result = _run_hook(
            payload,
            path_prefix=str(fake_bin),
            env_overrides={"NX_CALL_LOG": str(log_file)},
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=rdr-close-session" in log_contents, log_contents

    def test_missing_session_id_in_payload_preserves_ambient_env(
        self, tmp_path, mock_config_env
    ) -> None:
        """Defensive: if the stdin payload has no session_id field, the
        hook must NOT clobber a legitimate pre-existing NX_SESSION_ID
        with an empty value."""
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"
        env = mock_config_env({"on_close": True})

        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-4yit"},
        })

        result = _run_hook(
            payload,
            path_prefix=str(fake_bin),
            env_overrides={
                "NX_CALL_LOG": str(log_file),
                "NX_SESSION_ID": "pre-existing-ambient-value",
                **env,
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=pre-existing-ambient-value" in log_contents, log_contents


class TestMalformedQuotingNeverBypasses:
    """nexus-2e874: an unbalanced quote in any argument (e.g. a --reason
    value) used to make shlex reject the segment inside the BD_VERBS
    matcher, which silently SKIPPED it -- has_close_or_done stayed False
    and the whole hook fast-no-op'd with a bare allow, fully bypassing the
    close gate. The matcher now degrades to a quote-blanked whitespace
    split, so the leading `bd close` anchor still matches."""

    def test_unbalanced_quote_in_reason_still_triggers_the_gate(
        self, mock_config_env, fake_nx
    ) -> None:
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command='bd close nexus-abc12 --reason="unterminated'),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny"
        assert "nexus-abc12" in _get_reason(parsed)

    def test_unbalanced_quoted_mention_does_not_false_match(
        self, mock_config_env, fake_nx
    ) -> None:
        """The degraded split stays anchored: `bd close` inside an
        unterminated quoted string mid-segment never matches (the segment's
        first token is not `bd`)."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command='git commit -m "notes about bd close nexus-abc12'),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "allow"


    def test_quote_inside_the_verb_still_triggers_the_gate(
        self, mock_config_env, fake_nx
    ) -> None:
        """Review Important-1 (nexus-2e874): a quote INSIDE the verb
        fractures the quote-as-space variant ('b', 'd', ...) -- the
        quote-removed variant must still anchor `bd close`."""
        env = mock_config_env({"on_close": True})
        fake_bin = fake_nx("No scratch entries.")
        result = _run_hook(
            _make_payload(command='b"d close nexus-abc12'),
            path_prefix=str(fake_bin),
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny"


class TestE3makCompleteReviewerSet:
    """A marker must NAME the full required reviewer set (nexus-e3mak).

    THE INCIDENT, 2026-08-26 (RG-C, nexus-utpuw.23). The dispatched
    code-review-expert finished reviewer 1 of 2 and wrote a T1 handoff note for
    its sibling. The gate matched on the literal string ``review-completed``
    plus the bead id, so that note WAS coverage: ``bd close nexus-utpuw.23``
    would have passed with the substantive-critic never dispatched, while the
    gate's own text says the critic is never optional. The note was honest --
    it said "reviewer 1/2" -- which is the point: honesty is not a property a
    gate can rest on.

    WHY POSITIVE NAMING RATHER THAN "REFUSE A PARTIAL CLAIM". A rule that
    denied only when a marker named SOME of the roster was drafted and
    discarded: the incident marker names reviewers by COUNT ("1/2"), not by
    agent type, so such a rule would not have caught the very incident this
    bead is about. ``test_the_verbatim_incident_marker_is_refused`` is the
    guard against that mistake being made again.

    NO CROSS-VERSION CARVE-OUT, and none is needed. Markers land in T1, which
    is session-scoped, and the marker write and the ``bd close`` happen in one
    session -- so a T1 marker is always written under whatever instructions
    were live in that session, and the hook and the remedy text it prints ship
    together in one pinned plugin. The only window at all is a durable T2
    marker outliving a pin advance, and the T2 marker leg is itself a
    workaround for the T1 lease split-brain rather than a designed path, so it
    is not a case worth softening the rule for. Its cost is one denied close
    carrying the exact command to rewrite.
    """

    def test_the_verbatim_incident_marker_is_refused(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """The 2026-08-26 note, reproduced as written."""
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-utpuw.23",
            "review-completed bead=nexus-utpuw.23 (RG-C reviewer 1/2: findings clean)",
            reviewers="",
        )
        fake_nx_bin = fake_nx(scratch)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-utpuw.23"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny", (
            "the reviewer's own handoff note still satisfies the gate it is "
            "one half of — this IS nexus-e3mak"
        )
        assert not log.exists() or log.read_text().strip() == "", (
            "a denied close must acquire no verification record"
        )

    def test_naming_only_one_required_reviewer_is_refused(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-e3m01",
            "review-completed: nexus-e3m01",
            reviewers="reviewers=code-review-expert",
        )
        fake_nx_bin = fake_nx(scratch)
        fake_bd_bin, _log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-e3m01"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        parsed = json.loads(result.stdout)
        assert _get_decision(parsed) == "deny", parsed
        assert "substantive-critic" in _get_reason(parsed), (
            "the refusal must name what is missing, not just refuse"
        )

    def test_naming_the_full_set_is_accepted(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        env = mock_config_env({"on_close": True})
        scratch = _marker("review-completed,nexus-e3m02", "review-completed: nexus-e3m02")
        fake_nx_bin = fake_nx(scratch)
        fake_bd_bin, log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-e3m02"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "allow"
        assert log.exists(), "a passed close must acquire its verification record"

    def test_a_t2_marker_must_also_name_the_full_set(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """Parity, so the workaround is not a softer door.

        The T2 marker leg is not a designed feature: the hook's own write-side
        contract says "T2-alone is the correct choice specifically when the
        CLI T1 lease is known-stale", i.e. it exists to route around the T1
        CLI/MCP lease split-brain. It is enforced identically here for exactly
        that reason — a leg added to dodge a T1 problem must not become the
        cheap way past a review gate. Retiring it is tracked separately; while
        it exists it holds the same bar."""
        env = mock_config_env({"on_close": True})
        fake_nx_bin = fake_nx(
            "No scratch entries.",
            memory_by_query={
                "nexus-e3m03": _t2_marker(
                    "nexus/review-nexus-e3m03",
                    "review-completed: nexus-e3m03",
                    reviewers="reviewers=substantive-critic",
                ),
            },
        )
        fake_bd_bin, _log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-e3m03"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides=env,
        )
        assert _get_decision(json.loads(result.stdout)) == "deny", (
            "the T2 leg accepted a marker naming one reviewer"
        )

    def test_override_still_escapes_an_incomplete_marker(
        self, mock_config_env, fake_nx, fake_bd
    ) -> None:
        """The audited override must cover the new refusal too — otherwise a
        genuine cross-version marker would have no escape at all."""
        env = mock_config_env({"on_close": True})
        scratch = _marker(
            "review-completed,nexus-e3m04", "review-completed: nexus-e3m04", reviewers="",
        )
        fake_nx_bin = fake_nx(scratch)
        fake_bd_bin, _log = fake_bd()
        result = _run_hook(
            _make_payload(command="bd close nexus-e3m04"),
            path_prefix=f"{fake_nx_bin}:{fake_bd_bin}",
            env_overrides={**env, "NX_REVIEW_GATE_OVERRIDE": "1"},
        )
        assert _get_decision(json.loads(result.stdout)) == "allow"
