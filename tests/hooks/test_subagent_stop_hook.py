# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the SubagentStop orchestration-guard hook (RDR-184 Gap 1, bead
nexus-ccs9v.9).

The hook consults the P1.1 expectations file (bead nexus-ccs9v.7): agents NOT
listed are NEVER blocked; listed (named, background) agents are blocked at
most once when their transcript shows no SendMessage report. Ships
DEFAULT-OFF: NX_ORCH_STOP_GUARD = off (default) | observe | block.
Every uncertain path fails OPEN (never block on missing evidence).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop.sh"
PLUGIN_EXPECTATIONS = REPO_ROOT / "conexus" / "hooks" / "scripts" / "expectations.sh"
REFERENCE_EXPECTATIONS = REPO_ROOT / "tests" / "e2e" / "lib" / "expectations.sh"

SESSION = "sess-testorch"
NAME = "worker-a"
AGENT_ID = f"a{NAME}-6f59dab8bbb14864"


def _payload(
    *,
    session_id: str = SESSION,
    agent_id: str = AGENT_ID,
    agent_type: str = NAME,
    transcript: str = "",
    stop_hook_active: bool = False,
) -> str:
    return json.dumps(
        {
            "session_id": session_id,
            "hook_event_name": "SubagentStop",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "agent_transcript_path": transcript,
            "stop_hook_active": stop_hook_active,
        }
    )


def _transcript(tmp_path: Path, *, with_sendmessage: bool) -> Path:
    """A minimal agent transcript JSONL, optionally containing a SendMessage
    tool_use (shaped like real Claude Code transcript entries)."""
    lines = [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "true"}}
                ],
            },
        },
    ]
    if with_sendmessage:
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "SendMessage",
                            "input": {"to": "main", "content": "done: report"},
                        }
                    ],
                },
            }
        )
    lines.append(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "finished"}]},
        }
    )
    p = tmp_path / "agent_transcript.jsonl"
    p.write_text("\n".join(json.dumps(entry) for entry in lines) + "\n")
    return p


def _run_hook(
    stdin: str,
    tmp_path: Path,
    *,
    mode: str | None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    env.pop("NX_ORCH_STOP_GUARD", None)
    if mode is not None:
        env["NX_ORCH_STOP_GUARD"] = mode
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _expectations_file(tmp_path: Path, session_id: str = SESSION) -> Path:
    return tmp_path / "state" / "nexus" / "orchestration" / f"{session_id}.expectations"


def _expect_row(tmp_path: Path, name: str = NAME, mode: str = "background", session_id: str = SESSION) -> None:
    f = _expectations_file(tmp_path, session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(f"2026-07-17T00:00:00Z\tEXPECT\t{name}\t{mode}\n")


def _decision(proc: subprocess.CompletedProcess[str]) -> dict | None:
    """Parse a {"decision": ...} JSON object from hook stdout, if any."""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "decision" in obj:
            return obj
    return None


class TestDefaultMode:
    def test_unset_mode_blocks_owing_agent(self, tmp_path: Path) -> None:
        """DEFAULT-ON (P1.G flipped 2026-07-17, bead .15): with
        NX_ORCH_STOP_GUARD unset, an owing unreported agent IS blocked."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode=None)
        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"

    def test_unset_mode_still_failopen_for_unlisted(self, tmp_path: Path) -> None:
        """Default-ON must not change the fail-open floor: no EXPECT row =>
        no block, even with the guard defaulted on."""
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode=None)
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_explicit_off(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="off")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_unknown_mode_is_off(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="banana")
        assert proc.returncode == 0
        assert _decision(proc) is None


class TestBlockMode:
    def test_owing_unreported_agent_is_blocked_with_reason(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None, f"expected a block decision, stdout: {proc.stdout!r}"
        assert decision["decision"] == "block"
        assert "SendMessage" in decision["reason"]

    def test_block_records_blocked_row(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        content = _expectations_file(tmp_path).read_text()
        assert f"\tBLOCKED\t{AGENT_ID}\n" in content

    def test_stop_hook_active_never_reblocks(self, tmp_path: Path) -> None:
        """21c round-trip guard: the re-fired stop after a block carries
        stop_hook_active=true and must pass through untouched."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(transcript=str(t), stop_hook_active=True), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_blocked_row_suppresses_second_block(self, tmp_path: Path) -> None:
        """Belt to stop_hook_active's braces: a pre-existing BLOCKED row for
        this agent_id suppresses any further block."""
        _expect_row(tmp_path)
        f = _expectations_file(tmp_path)
        with f.open("a") as fh:
            fh.write(f"2026-07-17T00:00:01Z\tBLOCKED\t{AGENT_ID}\n")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_unlisted_agent_never_blocked(self, tmp_path: Path) -> None:
        """Sync dispatches stay unblockable by construction: no EXPECT row =>
        no block, regardless of transcript content."""
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_sync_expect_row_not_blocked(self, tmp_path: Path) -> None:
        _expect_row(tmp_path, mode="sync")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_unnamed_dispatch_with_background_credit_is_blocked(self, tmp_path: Path) -> None:
        """nexus-hbr4x widening fix: an UNNAMED dispatch (agent_id
        'a<hash>', agent_type = subagent_type — the ONLY shape the Agent
        tool can produce, since it has no name parameter) now DOES match a
        background EXPECT row for its type. Before this fix owes_report
        required named-agent morphology the harness can never produce, so
        the SubagentStop guard never fired for any real dispatch (18/18,
        19/19 no_terminal, T2 nexus/subagent-reliability-burndown-2026-08-03).
        This supersedes the old test_unnamed_morphology_not_blocked, which
        asserted the pre-fix (never-fires) behavior this bead exists to
        kill."""
        _expect_row(tmp_path, name="general-purpose")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(agent_id="a16b397f79df79c42", agent_type="general-purpose", transcript=str(t)),
            tmp_path,
            mode="block",
        )
        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"

    def test_unnamed_background_dispatch_produces_terminal_row(self, tmp_path: Path) -> None:
        """(+) background EXPECT + START, then a stop through
        subagent-stop.sh, produces a terminal row (BLOCKED here, transcript
        unreported) — the completion-report guard now writes ledger
        evidence for a real (unnamed) background dispatch, not just the
        unreachable named-morphology shape."""
        agent_type = "general-purpose"
        agent_id = "a16b397f79df79c42"
        _expect_row(tmp_path, name=agent_type, mode="background")
        f = _expectations_file(tmp_path)
        with f.open("a") as fh:
            fh.write(f"2026-08-03T00:00:00Z\tSTART\t{agent_id}\t{agent_type}\n")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(agent_id=agent_id, agent_type=agent_type, transcript=str(t)),
            tmp_path,
            mode="block",
        )
        assert proc.returncode == 0, proc.stderr
        content = _expectations_file(tmp_path).read_text()
        assert f"\tBLOCKED\t{agent_id}\n" in content

    def test_mixed_type_session_never_blocks_regardless_of_order(self, tmp_path: Path) -> None:
        """nexus-rkigh (Wave-1 round-1 CRITICAL, fix round): the hybrid
        design gates owes_report on per-type UNMIXED-ness FIRST. When a
        type has BOTH a background AND a sync EXPECT row in the same
        session, owes_report returns "does not owe" unconditionally for
        every agent_id of that type — order-INDEPENDENT, zero false-block
        by construction (burndown exit criterion 1, satisfied literally).
        This supersedes the old
        test_sync_stop_after_background_credit_consumed_not_blocked, which
        code review Finding 1 correctly flagged as order-rigged to the
        favorable case (it never wrote an actual sync EXPECT row, so the
        session it built was never genuinely mixed). Order A here: the
        sync-shaped agent stops first."""
        agent_type = "worker-mixed"
        _expect_row(tmp_path, name=agent_type, mode="background")
        _expect_row(tmp_path, name=agent_type, mode="sync")
        unreported_t = _transcript(tmp_path, with_sendmessage=False)

        sync_id = "async3333333333333"
        proc1 = _run_hook(
            _payload(agent_id=sync_id, agent_type=agent_type, transcript=str(unreported_t)),
            tmp_path,
            mode="block",
        )
        assert proc1.returncode == 0, proc1.stderr
        assert _decision(proc1) is None

        background_id = "abg4444444444444"
        proc2 = _run_hook(
            _payload(agent_id=background_id, agent_type=agent_type, transcript=str(unreported_t)),
            tmp_path,
            mode="block",
        )
        assert proc2.returncode == 0, proc2.stderr
        # Accepted price of the hybrid gate: detection miss for a mixed
        # type extends to the genuinely-owing background agent too.
        assert _decision(proc2) is None

    def test_mixed_type_session_never_blocks_reverse_order(self, tmp_path: Path) -> None:
        """Same invariant as above, opposite stop order — the
        background-labeled agent stops first, the sync-shaped agent
        second. Neither is blocked either way, proving the gate is
        order-INDEPENDENT (not just favorable-order lucky, the exact gap
        code review Finding 1 identified in round 1)."""
        agent_type = "worker-mixed-rev"
        _expect_row(tmp_path, name=agent_type, mode="background")
        _expect_row(tmp_path, name=agent_type, mode="sync")
        unreported_t = _transcript(tmp_path, with_sendmessage=False)

        background_id = "abg5555555555555"
        proc1 = _run_hook(
            _payload(agent_id=background_id, agent_type=agent_type, transcript=str(unreported_t)),
            tmp_path,
            mode="block",
        )
        assert proc1.returncode == 0, proc1.stderr
        assert _decision(proc1) is None

        sync_id = "async6666666666666"
        proc2 = _run_hook(
            _payload(agent_id=sync_id, agent_type=agent_type, transcript=str(unreported_t)),
            tmp_path,
            mode="block",
        )
        assert proc2.returncode == 0, proc2.stderr
        assert _decision(proc2) is None

    def test_consumed_settlement_n_of_type(self, tmp_path: Path) -> None:
        """UNMIXED session: CONSUMED settlement is N-of-type as before the
        rkigh fix round (this scenario has no sync EXPECT row for the type
        at all, so the unmixed-ness gate is a pass-through and settlement
        behaves exactly as in v3). Two background EXPECT rows for one type
        give exactly two units of credit — two stops owe (get blocked,
        unreported transcript), a third stop of the same type finds no
        credit left and is not blocked."""
        agent_type = "pool-worker"
        _expect_row(tmp_path, name=agent_type, mode="background")
        _expect_row(tmp_path, name=agent_type, mode="background")
        t = _transcript(tmp_path, with_sendmessage=False)

        ids = ["apool0000000000001", "apool0000000000002", "apool0000000000003"]
        decisions = []
        for aid in ids:
            proc = _run_hook(
                _payload(agent_id=aid, agent_type=agent_type, transcript=str(t)),
                tmp_path,
                mode="block",
            )
            assert proc.returncode == 0, proc.stderr
            decisions.append(_decision(proc))

        assert decisions[0] is not None and decisions[0]["decision"] == "block"
        assert decisions[1] is not None and decisions[1]["decision"] == "block"
        assert decisions[2] is None

    def test_owes_report_concurrent_stops_never_exceed_credit(self, tmp_path: Path) -> None:
        """nexus-bk974 (Wave-1 round-1 SIGNIFICANT, fix round): the bounded
        mkdir lockdir around owes_report's read-decide-append (mirrors
        agent-dispatch-expect.sh's _expect_if_absent / nexus-3h0u6
        precedent) must hold N-of-type exactness under REAL concurrency,
        not just the sequential test_consumed_settlement_n_of_type. 8
        same-type stops racing 4 units of credit: at most 4 owe (get
        blocked), and the CONSUMED row count for the type never exceeds
        the credit pool — without the lock, a race window lets more than
        4 threads observe unspent credit simultaneously."""
        import concurrent.futures

        agent_type = "worker-race"
        credit = 4
        for _ in range(credit):
            _expect_row(tmp_path, name=agent_type, mode="background")
        t = _transcript(tmp_path, with_sendmessage=False)

        ids = [f"arace{i:02d}00000000000" for i in range(8)]

        def _stop(aid: str) -> subprocess.CompletedProcess[str]:
            return _run_hook(
                _payload(agent_id=aid, agent_type=agent_type, transcript=str(t)),
                tmp_path,
                mode="block",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_stop, ids))

        for proc in results:
            assert proc.returncode == 0, proc.stderr

        blocked = sum(1 for proc in results if _decision(proc) is not None)
        assert blocked == credit, f"expected exactly {credit} blocked, got {blocked}"

        content = _expectations_file(tmp_path).read_text()
        consumed_rows = [ln for ln in content.splitlines() if "\tCONSUMED\t" in ln]
        assert len(consumed_rows) == credit, consumed_rows

    def test_reported_agent_not_blocked(self, tmp_path: Path) -> None:
        """A SendMessage tool_use in the agent transcript counts as the
        report — no block."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_reported_agent_gets_reported_row(self, tmp_path: Path) -> None:
        """The found-report path records a REPORTED row — the .11 missed-block
        census needs it (EXPECT x REPORTED x WOULDBLOCK; critic S1)."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        content = _expectations_file(tmp_path).read_text()
        assert f"\tREPORTED\t{AGENT_ID}\n" in content

    def test_sendmessage_inside_tool_result_does_not_count(self, tmp_path: Path) -> None:
        """A SendMessage-shaped tool_use embedded in a tool_result (e.g. the
        agent READ a transcript containing one) is not the agent's own
        report — only assistant-message tool_use blocks count (critic S1
        compounding factor, reproduced pre-fix)."""
        _expect_row(tmp_path)
        p = tmp_path / "agent_transcript.jsonl"
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t9",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "embedded",
                                "name": "SendMessage",
                                "input": {"to": "main", "content": "decoy from a read file"},
                            }
                        ],
                    }
                ],
            },
        }
        p.write_text(json.dumps(entry) + "\n")
        proc = _run_hook(_payload(transcript=str(p)), tmp_path, mode="block")
        assert proc.returncode == 0
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"

    def test_sendmessage_as_text_mention_does_not_count(self, tmp_path: Path) -> None:
        """Merely SAYING the word SendMessage in text is not a report — only
        a tool_use block named SendMessage counts."""
        _expect_row(tmp_path)
        p = tmp_path / "agent_transcript.jsonl"
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I could use SendMessage but will not"}],
            },
        }
        p.write_text(json.dumps(entry) + "\n")
        proc = _run_hook(_payload(transcript=str(p)), tmp_path, mode="block")
        assert proc.returncode == 0
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"


class TestFailOpen:
    def test_directory_transcript_fails_open(self, tmp_path: Path) -> None:
        """A directory-shaped agent_transcript_path passes -r but crashes a
        naive open(); the crash must fail OPEN (no block), never fall
        through to the block branch (critic S2, reproduced pre-fix)."""
        _expect_row(tmp_path)
        d = tmp_path / "transcript_dir"
        d.mkdir()
        proc = _run_hook(_payload(transcript=str(d)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_missing_transcript_fails_open(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        proc = _run_hook(
            _payload(transcript=str(tmp_path / "nope.jsonl")), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_empty_transcript_path_fails_open(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        proc = _run_hook(_payload(transcript=""), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_junk_stdin_fails_open(self, tmp_path: Path) -> None:
        proc = _run_hook("this is not json {", tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_missing_expectations_file_fails_open(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_junk_ledger_content_fails_open(self, tmp_path: Path) -> None:
        """A ledger file containing malformed/non-TSV junk (missing fields,
        stray tabs, no trailing newline) must never crash the CONSUMED-
        settlement awk consult and must never block — fail-open holds for
        junk CONTENT, not just a missing file."""
        f = _expectations_file(tmp_path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("garbage line without tabs\nEXPECT\n\tEXPECT\tsomething\nnot\ta\tvalid\trow\tat\tall")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0, proc.stderr
        assert _decision(proc) is None

    def test_traversal_session_id_fails_open(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(session_id="../../evil", transcript=str(t)), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None


class TestPostBlockResolution:
    """nexus-hybv1: a BLOCKED row must stop being terminal-forever. Both
    once-guard exits (stop_hook_active round-trip; already-blocked later
    stop) re-scan the transcript and stamp REPORTED when the report has
    since appeared — forensics over bfbfa2fe + b819e8f3 showed all 7
    recorded blocks resolved with a real SendMessage within ~20s, yet the
    ledger read them as failures."""

    def _blocked_agent(self, tmp_path: Path) -> None:
        """EXPECT row + BLOCKED row already on file for the standard agent."""
        _expect_row(tmp_path)
        f = _expectations_file(tmp_path)
        with f.open("a") as fh:
            fh.write(f"2026-07-17T00:01:00Z\tBLOCKED\t{AGENT_ID}\n")

    def test_stop_hook_active_stamps_resolution_when_reported(self, tmp_path: Path) -> None:
        """The immediate post-block re-stop: agent heeded the nudge, sent
        SendMessage, stopped again with stop_hook_active=true — the ledger
        gets a REPORTED row (BLOCKED then REPORTED = guard success)."""
        self._blocked_agent(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        proc = _run_hook(
            _payload(transcript=str(t), stop_hook_active=True), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None
        content = _expectations_file(tmp_path).read_text()
        assert f"REPORTED\t{AGENT_ID}\timmediate" in content

    def test_stop_hook_active_no_stamp_when_still_unreported(self, tmp_path: Path) -> None:
        """Round-trip stop with STILL no SendMessage: nothing stamped, never
        re-blocked — a bare BLOCKED stays honestly unresolved."""
        self._blocked_agent(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(transcript=str(t), stop_hook_active=True), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None
        assert "REPORTED" not in _expectations_file(tmp_path).read_text()

    def test_stop_hook_active_never_stamps_unblocked_agent(self, tmp_path: Path) -> None:
        """The resolution stamp is scoped to owing+blocked agents only — a
        round-trip stop for a never-blocked agent records nothing (the
        normal FOUND path already covers it on its ordinary stop)."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        proc = _run_hook(
            _payload(transcript=str(t), stop_hook_active=True), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert "REPORTED" not in _expectations_file(tmp_path).read_text()

    def test_later_stop_of_blocked_agent_stamps_resolution(self, tmp_path: Path) -> None:
        """A previously-blocked multi-round teammate stops again later
        (stop_hook_active=false): the once-guard still never re-blocks, and
        the delivered report is stamped (the gh1414-critic round-2/3 class,
        which previously left no trace)."""
        self._blocked_agent(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None
        content = _expectations_file(tmp_path).read_text()
        assert f"REPORTED\t{AGENT_ID}\tlater" in content
        assert content.count("BLOCKED") == 1  # never re-blocked

    def test_repeat_stops_stamp_resolution_exactly_once(self, tmp_path: Path) -> None:
        """Critique 2026-07-22 repro: the scan is whole-transcript, so
        without the consecutive-duplicate guard every idle re-stop of a
        resolved agent appended another REPORTED row forever (3 calls ->
        3 rows in the live repro). With the guard: exactly one."""
        self._blocked_agent(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        for _ in range(3):
            proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
            assert proc.returncode == 0
            assert _decision(proc) is None
        content = _expectations_file(tmp_path).read_text()
        assert content.count(f"REPORTED\t{AGENT_ID}") == 1

    def test_later_stop_of_blocked_agent_unreported_stays_bare(self, tmp_path: Path) -> None:
        self._blocked_agent(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None
        assert "REPORTED" not in _expectations_file(tmp_path).read_text()

    def test_resolution_missing_transcript_fails_open(self, tmp_path: Path) -> None:
        self._blocked_agent(tmp_path)
        proc = _run_hook(
            _payload(transcript=str(tmp_path / "nope.jsonl"), stop_hook_active=True),
            tmp_path,
            mode="block",
        )
        assert proc.returncode == 0
        assert _decision(proc) is None
        assert "REPORTED" not in _expectations_file(tmp_path).read_text()


class TestObserveMode:
    def test_observe_never_blocks_but_records(self, tmp_path: Path) -> None:
        """Observe mode is the .11 measurement vehicle: no decision output,
        but a WOULDBLOCK row lands in the expectations file."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="observe")
        assert proc.returncode == 0
        assert _decision(proc) is None
        content = _expectations_file(tmp_path).read_text()
        assert f"\tWOULDBLOCK\t{AGENT_ID}\n" in content

    def test_observe_does_not_mark_blocked(self, tmp_path: Path) -> None:
        """A WOULDBLOCK observation must not consume the real once-guard: no
        BLOCKED row from observe mode."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="observe")
        content = _expectations_file(tmp_path).read_text()
        assert "\tBLOCKED\t" not in content

    def test_observe_reported_agent_records_nothing(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="observe")
        content = _expectations_file(tmp_path).read_text()
        assert "\tWOULDBLOCK\t" not in content


def _run_undeclared(tmp_path: Path, session_id: str = SESSION) -> subprocess.CompletedProcess[str]:
    """Source the reference lib directly and invoke expectations_undeclared,
    propagating its own exit code as the subprocess's returncode (not the
    trailing `echo`'s) so tests can assert on rc precisely."""
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    script = (
        f'source "{REFERENCE_EXPECTATIONS}"; '
        f'expectations_undeclared "{session_id}"; rc=$?; echo "RC=$rc"; exit $rc'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, env=env
    )


class TestUndeclaredExitCodes:
    """nexus-suuja: expectations_undeclared's rc contract is now three-way:
    0 = clean, 1 = recognized==0 blindspot (pre-existing, false-clean not a
    pass), 2 = undeclared>0 — a real declaration-completeness deficit that
    was previously rc-invisible (same exit 0 as clean)."""

    def test_rc_zero_when_clean(self, tmp_path: Path) -> None:
        agent_type = "worker-clean"
        agent_id = f"a{agent_type}-abc123"
        _expect_row(tmp_path, name=agent_type, mode="background")
        f = _expectations_file(tmp_path)
        with f.open("a") as fh:
            fh.write(f"2026-08-03T00:00:00Z\tSTART\t{agent_id}\t{agent_type}\n")
        proc = _run_undeclared(tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "undeclared=0" in proc.stdout

    def test_rc_one_blindspot_when_nothing_recognized(self, tmp_path: Path) -> None:
        f = _expectations_file(tmp_path)
        f.parent.mkdir(parents=True, exist_ok=True)
        # Unnamed START, no EXPECT row for its type -- unrecognizable by
        # either key (name-morphology or agent-type-in-EXPECT).
        with f.open("a") as fh:
            fh.write("2026-08-03T00:00:00Z\tSTART\ta9f8e7d6c5b4a3\tworker-unseen\n")
        proc = _run_undeclared(tmp_path)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "BLINDSPOT" in proc.stdout

    def test_rc_two_when_undeclared_deficit(self, tmp_path: Path) -> None:
        agent_type = "worker-undeclared"
        agent_id = f"a{agent_type}-def456"
        f = _expectations_file(tmp_path)
        f.parent.mkdir(parents=True, exist_ok=True)
        # Named START row (recognized by morphology) with NO EXPECT row at
        # all for this name -> a genuine undeclared deficit.
        with f.open("a") as fh:
            fh.write(f"2026-08-03T00:00:00Z\tSTART\t{agent_id}\t{agent_type}\n")
        proc = _run_undeclared(tmp_path)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNDECLARED" in proc.stdout
        assert "undeclared=1" in proc.stdout


class TestPluginWiring:
    def test_shellib_parity_with_reference(self) -> None:
        """The plugin ships a COPY of the reference shellib (plugin surface
        rides a release; tests/e2e/lib is the reference implementation +
        test bed). Byte-identity is the drift tripwire — same pattern as the
        version-lockstep manifests."""
        assert PLUGIN_EXPECTATIONS.exists(), "plugin copy of expectations.sh missing"
        assert PLUGIN_EXPECTATIONS.read_bytes() == REFERENCE_EXPECTATIONS.read_bytes(), (
            "conexus/hooks/scripts/expectations.sh has drifted from "
            "tests/e2e/lib/expectations.sh — edit the reference, then copy it over"
        )

    def test_registered_in_hooks_json(self) -> None:
        hooks = json.loads((REPO_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
        subagent_stop = hooks["hooks"].get("SubagentStop", [])
        commands = [
            h["command"]
            for entry in subagent_stop
            for h in entry.get("hooks", [])
        ]
        assert any("subagent-stop.sh" in c for c in commands), (
            "subagent-stop.sh not registered under SubagentStop in hooks.json"
        )

    def test_script_is_bash_clean(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10
        )
        assert proc.returncode == 0, proc.stderr
