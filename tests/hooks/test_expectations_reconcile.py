# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for expectations_reconcile (RDR-184 hardening, bead nexus-2v0v7,
epic nexus-qkbo7).

Cross-checks the ledger's outstanding background STARTs against the
harness's OWN background-task ground truth, now available in Stop/
SubagentStop hook input (CC 2.1.145: ``background_tasks``). The exact
per-task field schema is NOT independently verified as of this bead (see
the function's own SCHEMA CAUTION docstring in expectations.py) — these
tests pin the CURRENT best-effort candidate-field behavior and the
fields-absent no-op contract, not a confirmed harness schema.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.hooks import expectations
from nexus.hooks import stop_verification

REPO = Path(__file__).resolve().parents[2]

SESSION = "sess-reconcile"


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


class _Result:
    """The stdout/returncode surface these tests already assert against."""

    def __init__(self, stdout: str, returncode: int) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _reconcile_script(state: Path, session_id: str, payload: str, monkeypatch) -> _Result:
    """Drive ``expectations_reconcile`` with a RAW payload string."""
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    report = expectations.expectations_reconcile(session_id, payload)
    return _Result("".join(line + "\n" for line in report.lines), report.code)


def _reconcile(state: Path, payload: dict, monkeypatch, session_id: str = SESSION) -> _Result:
    return _reconcile_script(state, session_id, json.dumps(payload), monkeypatch)


class TestFieldsAbsentIsANoOp:
    """Absent/malformed background_tasks must never change behavior on an
    older harness — zero output, rc 0, unconditionally."""

    def test_no_background_tasks_key_at_all(self, state, monkeypatch):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile(state, {"session_id": SESSION}, monkeypatch)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_background_tasks_is_null(self, state, monkeypatch):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": None}, monkeypatch)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_background_tasks_is_not_a_list(self, state, monkeypatch):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": "oops"}, monkeypatch)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_unparseable_payload_never_crashes(self, state, monkeypatch):
        _write_ledger(state, ["a\tSTART\ta1\tconexus:developer"])
        proc = _reconcile_script(state, SESSION, "not json at all", monkeypatch)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_missing_session_id_or_payload_is_a_noop(self, state, monkeypatch):
        proc = _reconcile_script(state, "", "", monkeypatch)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_no_ledger_file_is_a_noop(self, state, monkeypatch):
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []}, monkeypatch)
        assert proc.returncode == 0
        assert proc.stdout == ""


class TestStrandedDetection:
    """The new detection class: ledger outstanding, harness no longer
    tracks it."""

    def test_outstanding_start_absent_from_harness_is_stranded(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "aOTHER"}],
        }, monkeypatch)
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "STRANDED\ta1\tconexus:developer" in proc.stdout

    def test_empty_background_tasks_list_strands_every_outstanding_start(self, state, monkeypatch):
        # The load-bearing edge case: an EMPTY harness list is not the same
        # as an ABSENT key, and must not be silently treated as a no-op.
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []}, monkeypatch)
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "STRANDED\ta1\tconexus:developer" in proc.stdout
        assert "SUMMARY\toutstanding=1 harness_tasks=0 unidentified=0 stranded=1 undeclared_tasks=0" in proc.stdout

    def test_matched_agent_id_is_not_stranded(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "a1"}],
        }, monkeypatch)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "STRANDED" not in proc.stdout

    def test_reported_agent_is_not_outstanding_even_if_harness_forgot_it(self, state, monkeypatch):
        # A START with a terminal row (REPORTED/BLOCKED/WOULDBLOCK) already
        # resolved through the normal ledger path and must never be flagged
        # STRANDED regardless of what the harness's task list says.
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
            "t\tREPORTED\ta1",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []}, monkeypatch)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "STRANDED" not in proc.stdout

    def test_blocked_agent_is_not_outstanding(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
            "t\tBLOCKED\ta1",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": []}, monkeypatch)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "STRANDED" not in proc.stdout


class TestUndeclaredTaskCorroboration:
    """Harness knows about a task with no ledger START row at all — rc=2,
    reused deliberately from expectations_undeclared's vocabulary."""

    def test_harness_only_task_is_flagged_and_reuses_rc2(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
            "t\tREPORTED\ta1",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "a1"}, {"agent_id": "aNEVER-STARTED"}],
        }, monkeypatch)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNDECLARED_TASK\taNEVER-STARTED" in proc.stdout

    def test_stranded_takes_priority_over_undeclared_when_both_present(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "aNEVER-STARTED"}],
        }, monkeypatch)
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "STRANDED\ta1\tconexus:developer" in proc.stdout
        assert "UNDECLARED_TASK\taNEVER-STARTED" in proc.stdout


class TestCleanCase:
    def test_fully_matched_is_clean(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"agent_id": "a1"}],
        }, monkeypatch)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "SUMMARY\toutstanding=1 harness_tasks=1 unidentified=0 stranded=0 undeclared_tasks=0" in proc.stdout


class TestCandidateFieldFallbackAndUnidentified:
    """The candidate field list is best-effort (schema not yet confirmed).
    A task entry with none of the known id fields must degrade to
    'unidentified' rather than crashing or silently matching."""

    @pytest.mark.parametrize("field", ["agent_id", "id", "task_id", "taskId", "subagent_id"])
    def test_each_candidate_field_is_read(self, state, field, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{field: "a1"}],
        }, monkeypatch)
        assert proc.returncode == 0, f"field={field}: {proc.stdout + proc.stderr}"

    def test_bare_string_entries_are_taken_verbatim(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {"session_id": SESSION, "background_tasks": ["a1"]}, monkeypatch)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_unrecognized_shape_counts_as_unidentified_not_a_false_match(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        proc = _reconcile(state, {
            "session_id": SESSION,
            "background_tasks": [{"weird_unrecognized_field": "a1"}],
        }, monkeypatch)
        # a1 is still outstanding and unmatched (the unidentified entry
        # never resolves to "a1"), so it must still be STRANDED, not
        # silently waved through by an accidental match.
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "unidentified=1" in proc.stdout


class TestNeverMutatesTheLedger:
    def test_ledger_content_unchanged_after_reconcile(self, state, monkeypatch):
        original = "t\tEXPECT\tconexus:developer\tbackground\nt\tSTART\ta1\tconexus:developer\n"
        _ledger(state).write_text(original)
        _reconcile(state, {"session_id": SESSION, "background_tasks": []}, monkeypatch)
        assert _ledger(state).read_text() == original


class TestStopHookWiring:
    """The Stop-hook site: WARN-ONLY, never blocks, gated on
    NX_ORCH_STOP_GUARD, degrades silently on any missing/malformed input.

    Re-pointed at ``nexus.hooks.stop_verification`` (bead nexus-q02nx.21):
    the bash original is no longer wired into hooks.json (the Stop event
    dispatches to the ``hook_stop_verification`` mcp_tool) and is retired
    along with the shell ledger library it sourced. ``stop_verification.run``
    is the RDR-215 bead nexus-q02nx.13 port and calls
    ``expectations.expectations_reconcile`` directly, so this class now
    drives the same reconcile integration one
    layer closer to the real call than a subprocess ever did.
    """

    def _run_stop_hook(
        self, state: Path, monkeypatch, stdin: str, guard: str | None = "block"
    ) -> _Result:
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        monkeypatch.setenv("HOME", str(state / "home"))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO / "conexus"))
        if guard is None:
            monkeypatch.delenv("NX_ORCH_STOP_GUARD", raising=False)
        else:
            monkeypatch.setenv("NX_ORCH_STOP_GUARD", guard)
        # chdir to the isolated state dir, NOT the repo (nexus-2v0v7
        # follow-up). The hook's advisory checks shell out to `git status
        # --porcelain` and `bd list --status=in_progress` in whatever cwd
        # the process is in when on_stop is enabled. Pointed at the live
        # checkout those read AMBIENT DEVELOPER STATE (an untracked file,
        # an in-progress bead) for reasons that have nothing to do with
        # reconciliation. CI never saw either: fresh checkout, no local
        # settings file, no beads. From the state dir both degrade to
        # empty exactly as the hook intends, and the test observes
        # reconciliation alone. The default config has on_stop=False, so
        # neither check runs at all here, but the isolation is kept for
        # robustness against that default changing out from under this
        # file.
        monkeypatch.chdir(state)
        try:
            payload = json.loads(stdin)
        except (json.JSONDecodeError, TypeError):
            payload = None
        result = stop_verification.run(payload)
        return _Result(result.stdout or "", result.exit_code)

    def test_stranded_agent_produces_a_warning_but_still_approves(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({
            "session_id": SESSION,
            "hook_event_name": "Stop",
            "background_tasks": [],
        })
        proc = self._run_stop_hook(state, monkeypatch, payload)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["decision"] == "approve"
        assert "nexus-2v0v7" in out.get("reason", "")
        assert "a1" in out.get("reason", "")

    def test_clean_reconciliation_produces_plain_approve(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({
            "session_id": SESSION,
            "hook_event_name": "Stop",
            "background_tasks": [{"agent_id": "a1"}],
        })
        proc = self._run_stop_hook(state, monkeypatch, payload)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {"decision": "approve"}

    def test_older_harness_payload_without_background_tasks_is_unaffected(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({"session_id": SESSION, "hook_event_name": "Stop"})
        proc = self._run_stop_hook(state, monkeypatch, payload)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {"decision": "approve"}

    def test_guard_off_skips_reconciliation_entirely(self, state, monkeypatch):
        _write_ledger(state, [
            "t\tEXPECT\tconexus:developer\tbackground",
            "t\tSTART\ta1\tconexus:developer",
        ])
        payload = json.dumps({
            "session_id": SESSION,
            "hook_event_name": "Stop",
            "background_tasks": [],
        })
        proc = self._run_stop_hook(state, monkeypatch, payload, guard="off")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {"decision": "approve"}

    def test_junk_stdin_never_breaks_the_hook(self, state, monkeypatch):
        proc = self._run_stop_hook(state, monkeypatch, "not json at all")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["decision"] == "approve"

    def test_registered_in_hooks_json(self):
        hooks = json.loads((REPO / "conexus" / "hooks" / "hooks.json").read_text())
        stop_entries = hooks["hooks"].get("Stop", [])
        tools = [
            h.get("tool")
            for entry in stop_entries
            for h in entry.get("hooks", [])
            if h.get("type") == "mcp_tool"
        ]
        assert "hook_stop_verification" in tools, (
            "the Stop event must dispatch to the ported hook_stop_verification mcp_tool"
        )
