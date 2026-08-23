# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for expectations_reconcile (RDR-184 hardening, bead nexus-2v0v7,
epic nexus-qkbo7).

Cross-checks the ledger's outstanding background STARTs against the
harness's OWN background-task ground truth, now available in Stop/
SubagentStop hook input (CC 2.1.145: ``background_tasks``). The exact
per-task field schema is NOT independently verified as of this bead (see
the function's own SCHEMA CAUTION docstring in expectations.sh) — these
tests pin the CURRENT best-effort candidate-field behavior and the
fields-absent no-op contract, not a confirmed harness schema.
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "tests" / "e2e" / "lib" / "expectations.sh"
PLUGIN_LIB = REPO / "conexus" / "hooks" / "scripts" / "expectations.sh"
STOP_HOOK = REPO / "conexus" / "hooks" / "scripts" / "stop_verification_hook.sh"

SESSION = "sess-reconcile"


def _bash(script: str, state: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ, XDG_STATE_HOME=str(state), HOME=str(state / "home"))
    full_env.pop("NX_ORCH_STOP_GUARD", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f"source {LIB}\n{textwrap.dedent(script)}"],
        capture_output=True, text=True, env=full_env,
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    st = tmp_path / "state"
    (st / "nexus" / "orchestration").mkdir(parents=True)
    (st / "home").mkdir()
    return st


def _ledger(state: Path, session_id: str = SESSION) -> Path:
    return state / "nexus" / "orchestration" / f"{session_id}.expectations"


def _write_ledger(state: Path, rows: list[str], session_id: str = SESSION) -> None:
    _ledger(state, session_id).write_text("".join(r + "\n" for r in rows))


def _reconcile(state: Path, payload: dict, session_id: str = SESSION) -> subprocess.CompletedProcess:
    payload_json = json.dumps(payload)
    return _bash(
        f"expectations_reconcile {session_id!r} {payload_json!r}",
        state,
    )


class TestFieldsAbsentIsANoOp:
    """Absent/malformed background_tasks must never change behavior on an
    older harness — zero output, rc 0, unconditionally."""

    def test_no_background_tasks_key_at_all(self, state):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile(state, {"session_id": SESSION})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_background_tasks_is_null(self, state):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": None})
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_background_tasks_is_not_a_list(self, state):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": "oops"})
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_unparseable_payload_never_crashes(self, state):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _bash(f"expectations_reconcile {SESSION!r} 'not json at all'", state)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_missing_session_id_or_payload_is_a_noop(self, state):
        proc = _bash("expectations_reconcile", state)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_no_ledger_file_is_a_noop(self, state):
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []})
        assert proc.returncode == 0
        assert proc.stdout == ""


class TestStrandedDetection:
    """The new detection class: ledger outstanding, harness no longer
    tracks it."""

    def test_outstanding_start_absent_from_harness_is_stranded(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "aOTHER"}],
        })
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "STRANDED\ta1\tconexus:developer" in proc.stdout

    def test_empty_background_tasks_list_strands_every_outstanding_start(self, state):
        # The load-bearing edge case: an EMPTY harness list is not the same
        # as an ABSENT key, and must not be silently treated as a no-op.
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []})
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "STRANDED\ta1\tconexus:developer" in proc.stdout
        assert "SUMMARY\toutstanding=1 harness_tasks=0 unidentified=0 stranded=1 undeclared_tasks=0" in proc.stdout

    def test_matched_agent_id_is_not_stranded(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "a1"}],
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "STRANDED" not in proc.stdout

    def test_reported_agent_is_not_outstanding_even_if_harness_forgot_it(self, state):
        # A START with a terminal row (REPORTED/BLOCKED/WOULDBLOCK) already
        # resolved through the normal ledger path and must never be flagged
        # STRANDED regardless of what the harness's task list says.
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
            "t\tREPORTED\ta1",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "STRANDED" not in proc.stdout

    def test_blocked_agent_is_not_outstanding(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
            "t\tBLOCKED\ta1",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "STRANDED" not in proc.stdout


class TestUndeclaredTaskCorroboration:
    """Harness knows about a task with no ledger START row at all — rc=2,
    reused deliberately from expectations_undeclared's vocabulary."""

    def test_harness_only_task_is_flagged_and_reuses_rc2(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
            "t\tREPORTED\ta1",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "a1"}, {"agent_id": "aNEVER-STARTED"}],
        })
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNDECLARED_TASK\taNEVER-STARTED" in proc.stdout

    def test_stranded_takes_priority_over_undeclared_when_both_present(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "aNEVER-STARTED"}],
        })
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "STRANDED\ta1\tconexus:developer" in proc.stdout
        assert "UNDECLARED_TASK\taNEVER-STARTED" in proc.stdout


class TestCleanCase:
    def test_fully_matched_is_clean(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "a1"}],
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "SUMMARY\toutstanding=1 harness_tasks=1 unidentified=0 stranded=0 undeclared_tasks=0" in proc.stdout


class TestCandidateFieldFallbackAndUnidentified:
    """The candidate field list is best-effort (schema not yet confirmed).
    A task entry with none of the known id fields must degrade to
    'unidentified' rather than crashing or silently matching."""

    @pytest.mark.parametrize("field", ["agent_id", "id", "task_id", "taskId", "subagent_id"])
    def test_each_candidate_field_is_read(self, state, field):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{field: "a1"}],
        })
        assert proc.returncode == 0, f"field={field}: {proc.stdout + proc.stderr}"

    def test_bare_string_entries_are_taken_verbatim(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": ["a1"]})
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_unrecognized_shape_counts_as_unidentified_not_a_false_match(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"weird_unrecognized_field": "a1"}],
        })
        # a1 is still outstanding and unmatched (the unidentified entry
        # never resolves to "a1"), so it must still be STRANDED, not
        # silently waved through by an accidental match.
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "unidentified=1" in proc.stdout


class TestNeverMutatesTheLedger:
    def test_ledger_content_unchanged_after_reconcile(self, state):
        original = "t\tEXPECT\tconexus:developer\tbackground\nt\tSTART\ta1\tconexus:developer\n"
        _ledger(state).write_text(original)
        _reconcile(state, {"session_id": SESSION, "background_tasks": []})
        assert _ledger(state).read_text() == original


class TestStopHookWiring:
    """The Stop-hook site: WARN-ONLY, never blocks, gated on
    NX_ORCH_STOP_GUARD, degrades silently on any missing/malformed input."""

    def _run_stop_hook(self, state: Path, stdin: str, guard: str | None = "block") -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            XDG_STATE_HOME=str(state),
            HOME=str(state / "home"),
            CLAUDE_PLUGIN_ROOT=str(REPO / "conexus"),
        )
        env.pop("NX_ORCH_STOP_GUARD", None)
        if guard is not None:
            env["NX_ORCH_STOP_GUARD"] = guard
        # cwd is the isolated state dir, NOT the repo (nexus-2v0v7 follow-up).
        # The hook's advisory checks shell out to `git status --porcelain` and
        # `bd list --status=in_progress` in whatever cwd it inherits. Pointed at
        # the live checkout those read AMBIENT DEVELOPER STATE, so the
        # plain-approve assertions below fail for reasons that have nothing to
        # do with reconciliation:
        #   - any untracked file makes Check 1 emit "Uncommitted changes". This
        #     bites even on a clean tree, because `env` above overrides HOME and
        #     git then loses the user's ~/.config/git/ignore, so files ignored
        #     ONLY globally (`.claude/settings.local.json` is the standing one)
        #     resurface as untracked.
        #   - any in-progress bead makes Check 3 emit its own warning.
        # CI never saw either: fresh checkout, no local settings file, no beads.
        # Running from the state dir means neither command finds a repo or a
        # beads db, both degrade to empty exactly as the hook intends, and the
        # test observes reconciliation alone.
        return subprocess.run(
            ["bash", str(STOP_HOOK)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(state),
        )

    def test_stranded_agent_produces_a_warning_but_still_approves(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({
            "session_id": SESSION,
            "hook_event_name": "Stop",
            "background_tasks": [],
        })
        proc = self._run_stop_hook(state, payload)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["decision"] == "approve"
        assert "nexus-2v0v7" in out.get("reason", "")
        assert "a1" in out.get("reason", "")

    def test_clean_reconciliation_produces_plain_approve(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({
            "session_id": SESSION,
            "hook_event_name": "Stop",
            "background_tasks": [{"agent_id": "a1"}],
        })
        proc = self._run_stop_hook(state, payload)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {"decision": "approve"}

    def test_older_harness_payload_without_background_tasks_is_unaffected(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({"session_id": SESSION, "hook_event_name": "Stop"})
        proc = self._run_stop_hook(state, payload)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {"decision": "approve"}

    def test_guard_off_skips_reconciliation_entirely(self, state):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({
            "session_id": SESSION,
            "hook_event_name": "Stop",
            "background_tasks": [],
        })
        proc = self._run_stop_hook(state, payload, guard="off")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {"decision": "approve"}

    def test_junk_stdin_never_breaks_the_hook(self, state):
        proc = self._run_stop_hook(state, "not json at all")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["decision"] == "approve"

    def test_hook_is_bash_clean(self):
        proc = subprocess.run(["bash", "-n", str(STOP_HOOK)], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr

    def test_registered_in_hooks_json(self):
        hooks = json.loads((REPO / "conexus" / "hooks" / "hooks.json").read_text())
        stop_entries = hooks["hooks"].get("Stop", [])
        commands = [h["command"] for entry in stop_entries for h in entry.get("hooks", [])]
        assert any("stop_verification_hook.sh" in c for c in commands)


class TestBothLibraryCopiesStayIdentical:
    """CLAUDE.md: edit tests/e2e/lib/expectations.sh, copy it over, never
    the reverse. Byte-identity is the drift tripwire."""

    def test_plugin_copy_is_byte_identical(self):
        assert LIB.read_bytes() == PLUGIN_LIB.read_bytes()

    def test_both_copies_define_the_reconcile_function(self):
        for path in (LIB, PLUGIN_LIB):
            assert "expectations_reconcile()" in path.read_text(), path
