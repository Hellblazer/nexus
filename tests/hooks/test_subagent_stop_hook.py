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
import time
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
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    env.pop("NX_ORCH_STOP_GUARD", None)
    if mode is not None:
        env["NX_ORCH_STOP_GUARD"] = mode
    if extra_env:
        env.update(extra_env)
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


def _blocked_rows(tmp_path: Path, session_id: str = SESSION) -> list[tuple[str, str]]:
    """(agent_id, cause) for every BLOCKED row in the session's
    expectations file; cause is '' when the row carries no 4th field
    (nexus-plycy: expectations_mark_blocked's optional cause param)."""
    f = _expectations_file(tmp_path, session_id)
    if not f.exists():
        return []
    out: list[tuple[str, str]] = []
    for ln in f.read_text().splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3 and parts[1] == "BLOCKED":
            out.append((parts[2], parts[3] if len(parts) >= 4 else ""))
    return out


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
        same-type stops racing 4 units of credit.

        nexus-4b8sz: CI red 2026-08-07 (run 31215840624 shard 1) got
        blocked=5 vs credit=4 under a loaded runner — the hardcoded ~1s
        lock budget (10 x 0.1s) was exhausted by one racer, which then
        proceeded unlocked (fail-open by design) and over-counted. The
        budget was made env-tunable (NX_EXPECT_LOCK_TRIES); this test
        pins a generous one (200 tries =~ 20s).

        nexus-7z7rj round 1: widening the budget alone narrows but does
        not close the window — the SAME test still reproduced blocked=5
        vs credit=4 in CI at 200 tries (PR #1445 run 31244456036). Round
        1 made the decision atomic-under-lock and had exhaustion default
        to "does not owe" (never touch the ledger).

        nexus-plycy (round 2, substantive-critic Critical on round 1):
        a fixed "does not owe" default, combined with the pre-existing
        60s stale-lock reap, could silence the guard SESSION-WIDE for up
        to a minute on an orphaned lock holder — an unsafe (silent-miss)
        failure direction for a guard whose entire purpose is not missing
        undeclared dispatches. Round 2 keeps decision-only-under-lock for
        the CONSUMED append (that half was sound — see THE CONTRACT in
        expectations.sh) but flips the exhaustion default to BLOCK
        (disclosed via a "lock-exhausted" cause, both in the JSON reason
        and the BLOCKED row's 4th field) now that the lockdir is PER-TYPE,
        which removes the cross-type false-accusation risk round 1 was
        avoiding. Consequence for THIS test: "CONSUMED rows never exceed
        credit" is still a MECHANICAL invariant (asserted below), but
        raw "blocked" count is no longer bounded by credit at all — an
        exhaustion-forced block is now a deliberate, safe, DISCLOSED
        over-block, not an accounting entry. So this test asserts the
        ledger-level ceiling (consumed <= credit) plus the disclosure
        invariant (every BLOCKED row is either backed by a matching
        CONSUMED row or explicitly names cause=lock-exhausted — never an
        unexplained block) instead of a raw blocked<=credit ceiling. See
        test_owes_report_lock_exhaustion_never_exceeds_credit_under_forced_contention
        for the version that deterministically forces exhaustion and
        checks the lower bound (the winner still blocks)."""
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
                extra_env={"NX_EXPECT_LOCK_TRIES": "200"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_stop, ids))

        for proc in results:
            assert proc.returncode == 0, proc.stderr

        blocked = sum(1 for proc in results if _decision(proc) is not None)
        assert blocked >= 1, "an always-pass regression would silently satisfy every other assertion here"

        content = _expectations_file(tmp_path).read_text()
        consumed_rows = [ln for ln in content.splitlines() if "\tCONSUMED\t" in ln]
        assert len(consumed_rows) <= credit, consumed_rows

        consumed_ids = {ln.split("\t")[2] for ln in consumed_rows}
        for agent_id, cause in _blocked_rows(tmp_path):
            assert agent_id in consumed_ids or cause == "lock-exhausted", (
                f"BLOCKED row for {agent_id} has neither a matching CONSUMED "
                f"row nor a disclosed cause (cause={cause!r})"
            )

    def test_owes_report_credit_claim_survives_a_lock_that_grants_no_exclusion(
        self, tmp_path: Path
    ) -> None:
        """nexus-7z7rj ROUND 3 — the permanent falsification for the
        double-spend, replacing "hope the runner is loaded enough".

        Rounds 1 and 2 both fixed the CRITICAL SECTION (widen the retry
        budget; then decide only while holding the lock) and both declared
        `CONSUMED <= credit` a mechanical invariant. It was violated again
        on 2026-08-09 (develop f145f3de, local full-parallel run, gw11:
        five CONSUMED rows for four credits, all stamped the same second)
        because the defect is NOT in the critical section. It is in the
        stale-lock reap that runs BEFORE the lock is acquired: `-d` test,
        `find`, `rmdir` are three steps, the lock can be released and
        legitimately re-acquired between them, and the `rmdir` then
        deletes a LIVE holder's lock — putting two racers inside the
        critical section, reading the same credit state, both appending.
        (Deterministically reproduced with injected scheduling delay;
        see the WHY A LOCK CANNOT CARRY THIS INVARIANT block in
        expectations.sh.) Safely stealing a name-based lock needs an
        atomic compare-and-delete that POSIX does not offer, so the fix
        stops making the ceiling depend on the lock at all: a unit of
        credit is claimed by creating a slot SYMLINK, and symlink(2) is
        atomic and fails with EEXIST, so exactly one racer per slot name
        can ever win.

        This test therefore asserts the property rather than an
        interleaving: with NX_EXPECT_LOCK_DISABLE=1 the lock is switched
        off entirely — no reap, no acquire, no release, ZERO mutual
        exclusion, which is a strictly stronger adversary than any stolen
        or leaked lock — and the ceiling must STILL hold.

        DETERMINISM (nexus-ols6a, reviewer Important 3). Disabling the
        lock removes EXCLUSION but not SIMULTANEITY: racers can still
        happen to serialize, so an early version of this test — which
        relied on 8 concurrent hooks colliding by luck — missed a
        deliberately broken claim 1 run in 12, and the miss direction was
        a FALSE PASS. That is the same load-dependent shape the test
        exists to retire, in the one place it does the most damage: this
        is the SOLE permanent falsification for a bug that has recurred
        three times, and rounds 1 and 2 both shipped believing they were
        verified. NX_EXPECT_CLAIM_DELAY_S closes it by widening the
        read->claim gap so EVERY racer provably reads the same credit
        state before ANY racer claims, making the collision structural
        rather than incidental. Re-measured after the change: 0 misses
        in 30 trials against the same neutered claim (was 1 in 12).

        Asserted: exactly `credit` rows (upper bound = the double-spend;
        lower bound = a "concurrent path always returns no" regression),
        distinct claimants, and the disclosure invariant that every
        BLOCKED row is either credit-backed or names its cause. Note the
        blocked set is deliberately NOT required to equal the claimant
        set: a racer that loses the claim while a winner is still
        mid-append re-reads an inconsistent ledger and over-blocks with
        cause=credit-slot-orphan, which is the safe, disclosed direction
        (see ORPHANED CREDIT SLOTS in expectations.sh)."""
        import concurrent.futures

        agent_type = "worker-noexcl"
        credit = 4
        racers = 8
        for _ in range(credit):
            _expect_row(tmp_path, name=agent_type, mode="background")
        t = _transcript(tmp_path, with_sendmessage=False)

        def _stop(i: int) -> subprocess.CompletedProcess[str]:
            return _run_hook(
                _payload(
                    agent_id=f"anoexcl{i:02d}0000000000",
                    agent_type=agent_type,
                    transcript=str(t),
                ),
                tmp_path,
                mode="block",
                extra_env={
                    "NX_EXPECT_LOCK_DISABLE": "1",
                    # every racer reads before any racer claims
                    "NX_EXPECT_CLAIM_DELAY_S": "1.5",
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=racers) as pool:
            results = list(pool.map(_stop, range(racers)))
        for proc in results:
            assert proc.returncode == 0, proc.stderr

        content = _expectations_file(tmp_path).read_text()
        consumed = [ln for ln in content.splitlines() if "\tCONSUMED\t" in ln]
        assert len(consumed) == credit, (
            f"with the lock disabled and every racer decided from the same "
            f"credit state, {racers} racers must still consume exactly "
            f"{credit} units — got {len(consumed)}: {consumed}"
        )
        # Distinct claimants: one unit each, never one agent twice.
        assert len({ln.split("\t")[2] for ln in consumed}) == credit, consumed
        consumed_ids = {ln.split("\t")[2] for ln in consumed}
        blocked_rows = _blocked_rows(tmp_path)
        assert consumed_ids <= {aid for aid, _c in blocked_rows}, (
            "every credit-backed claimant must be blocked"
        )
        for agent_id, cause in blocked_rows:
            assert agent_id in consumed_ids or cause == "credit-slot-orphan", (
                f"BLOCKED row for {agent_id} has neither a matching CONSUMED "
                f"row nor a disclosed cause (cause={cause!r})"
            )

    def test_credit_slot_orphaned_by_a_killed_hook_blocks_instead_of_silently_passing(
        self, tmp_path: Path
    ) -> None:
        """nexus-ols6a — the falsifier for round 3's OWN failure mode,
        which nothing in the suite covered: a hook KILLED between claiming
        a credit slot and writing its CONSUMED row.

        This is not a hypothetical window. conexus/hooks/hooks.json gives
        SubagentStop a 10-SECOND timeout, while the lock budget is 20s in
        CI (NX_EXPECT_LOCK_TRIES=200) and clamps at 60s — so the harness
        SIGKILLs this hook as a routine, load-correlated event, in exactly
        the contention regime the credit mechanism exists for.

        The cost of that kill got WORSE at round 3 before it got better.
        Pre-round-3, a kill in the read->append window wrote nothing: the
        killed agent's stop went unguarded (one miss) but the pool was
        untouched, so a later agent still got the unit. Post-round-3 the
        claim lands BEFORE the row, so the same kill burns a credit
        permanently and a SECOND future agent also slips — doubled damage,
        and less observable, since the orphan is a symlink rather than a
        CONSUMED row a census could read.

        So this test kills a racer in that exact window (via the
        NX_EXPECT_APPEND_DELAY_S seam), proves the orphan really exists
        (slot claimed, no row), and then asserts the property that closes
        it: the NEXT agent of that type must be BLOCKED with a disclosed
        cause rather than silently waved through. Without the ols6a fix
        this fails — the next agent loses its claim, returns "does not
        owe", and stops unblocked with no ledger trace at all."""
        agent_type = "worker-orphan"
        for _ in range(1):  # exactly one unit of credit
            _expect_row(tmp_path, name=agent_type, mode="background")
        t = _transcript(tmp_path, with_sendmessage=False)

        env = {
            **os.environ,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "NX_ORCH_STOP_GUARD": "block",
            "NX_EXPECT_APPEND_DELAY_S": "10",
        }
        victim = subprocess.Popen(
            ["bash", str(SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert victim.stdin is not None
        victim.stdin.write(
            _payload(agent_id="aorphan000000000", agent_type=agent_type, transcript=str(t))
        )
        victim.stdin.close()

        # Wait for the slot claim to land, then kill INSIDE the window.
        slot = Path(str(_expectations_file(tmp_path)) + f".credit.{agent_type}.1")
        for _ in range(200):
            if slot.is_symlink():
                break
            time.sleep(0.05)
        assert slot.is_symlink(), "victim never claimed a credit slot; seam did not engage"
        victim.kill()
        victim.wait(timeout=10)

        content = _expectations_file(tmp_path).read_text()
        assert "\tCONSUMED\t" not in content, (
            "victim must have been killed BEFORE its CONSUMED row — the orphan "
            "window is what this test is about"
        )
        assert slot.is_symlink(), "the orphaned slot must survive the kill"

        # A hook killed mid-claim orphans its LOCKDIR as well as its slot,
        # and for the first ~60s the stale lock masks the orphaned slot:
        # later stops of this type exhaust the budget and block with
        # cause=lock-exhausted (safe, disclosed, and verified separately by
        # test_owes_report_lock_exhaustion_*). The interesting state is the
        # one AFTER the stale-lock reap clears that mask, because from then
        # on the orphaned slot is permanent and nothing else stands between
        # it and a silent miss. Removing the lockdir here is exactly what
        # the reap does at 60s — it makes the test represent the steady
        # state rather than the first minute.
        stale_lock = Path(str(_expectations_file(tmp_path)) + f".owes.{agent_type}.lock")
        if stale_lock.is_dir():
            stale_lock.rmdir()

        # The next agent of this type must NOT be silently waved through.
        proc = _run_hook(
            _payload(agent_id="anext0000000000", agent_type=agent_type, transcript=str(t)),
            tmp_path,
            mode="block",
        )
        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block", (
            "an orphaned credit slot must BLOCK the next agent (disclosed), not "
            f"silently pass it — that silent pass is how ONE killed hook costs "
            f"TWO unguarded stops. stdout={proc.stdout!r}"
        )
        assert "credit-slot-orphan" in proc.stderr, proc.stderr
        assert ("anext0000000000", "credit-slot-orphan") in _blocked_rows(tmp_path), (
            "the block must be recorded with its cause so an operator can tell "
            "it from a credit-verified block"
        )

    def test_owes_report_lock_exhaustion_never_exceeds_credit_under_forced_contention(
        self, tmp_path: Path
    ) -> None:
        """nexus-7z7rj/nexus-plycy falsification harness: rather than
        hoping a CI runner happens to be loaded enough to reproduce the
        double-spend (the bug only surfaced under real load — 2026-08-08,
        PR #1445 run 31244456036 — and was invisible in 8/8 local and
        even 200-try CI runs most of the time), force lock exhaustion
        deterministically. NX_EXPECT_LOCK_HOLD_DELAY_S widens the
        critical section (sleeps after acquiring, before the
        read-decide-append) so a tiny NX_EXPECT_LOCK_TRIES budget
        guarantees every racer but the current lock holder exhausts its
        budget while the holder is still inside its critical section.

        Proves THREE things the round-2 contract requires together:
        (1) the winner still blocks via a verified CONSUMED spend — a
            regression that made the concurrent path always return "no"
            would fail `blocked >= 1` (substantive-critic Significant-3:
            the round-1 tests had no lower bound, so an always-pass
            regression would have passed silently);
        (2) CONSUMED rows never exceed credit — manually reverting
            decision-only-under-lock (moving the awk read + CONSUMED
            append back outside `if [[ -n "$held" ]]`, i.e. 4b8sz's
            shipped shape) reliably reds this with consumed_rows > credit;
        (3) every exhaustion-forced block is DISCLOSED (cause=
            lock-exhausted in the BLOCKED row), never a bare, unexplained
            over-block — manually reverting the round-2 exhaustion
            default back to round 1's silent "does not owe" would fail
            `exhausted > 0` implying no blocks at all from the losing
            racers, the exact silent-miss class nexus-plycy exists to
            prevent."""
        import concurrent.futures

        agent_type = "worker-race-forced"
        credit = 3
        for _ in range(credit):
            _expect_row(tmp_path, name=agent_type, mode="background")
        t = _transcript(tmp_path, with_sendmessage=False)

        ids = [f"aforce{i:02d}0000000000" for i in range(6)]

        def _stop(aid: str) -> subprocess.CompletedProcess[str]:
            return _run_hook(
                _payload(agent_id=aid, agent_type=agent_type, transcript=str(t)),
                tmp_path,
                mode="block",
                extra_env={
                    # ~0.3s budget (3 x 0.1s) against a 0.5s post-acquire
                    # hold: any racer that does not win the very first
                    # mkdir is essentially guaranteed to exhaust its
                    # budget while the winner is still sleeping inside
                    # the (now-widened) critical section.
                    "NX_EXPECT_LOCK_TRIES": "3",
                    "NX_EXPECT_LOCK_HOLD_DELAY_S": "0.5",
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_stop, ids))

        for proc in results:
            assert proc.returncode == 0, proc.stderr

        exhausted = sum(1 for proc in results if "lock budget exhausted" in proc.stderr)
        assert exhausted > 0, (
            "test did not actually force lock exhaustion in any racer -- "
            "not a valid falsification run; widen NX_EXPECT_LOCK_HOLD_DELAY_S "
            "or shrink NX_EXPECT_LOCK_TRIES"
        )

        blocked = sum(1 for proc in results if _decision(proc) is not None)
        # Lower bound (critic Significant-3): the lock WINNER genuinely
        # owes (real background credit, unreported transcript) and must
        # still block via a verified spend, regardless of how many other
        # racers exhaust their budget. An always-pass regression in the
        # concurrent/lock path would satisfy every ceiling-only assertion
        # silently; this is the assertion that catches it.
        assert blocked >= 1, "the lock winner must still block on a genuine, verified owe"

        content = _expectations_file(tmp_path).read_text()
        consumed_rows = [ln for ln in content.splitlines() if "\tCONSUMED\t" in ln]
        assert len(consumed_rows) <= credit, consumed_rows
        assert len(consumed_rows) >= 1, (
            "the lock winner's block must be backed by a verified CONSUMED spend"
        )

        consumed_ids = {ln.split("\t")[2] for ln in consumed_rows}
        exhaustion_forced = 0
        for agent_id, cause in _blocked_rows(tmp_path):
            assert agent_id in consumed_ids or cause == "lock-exhausted", (
                f"BLOCKED row for {agent_id} has neither a matching CONSUMED "
                f"row nor a disclosed cause (cause={cause!r})"
            )
            if cause == "lock-exhausted":
                exhaustion_forced += 1
        # At least one of the exhausted racers must have actually blocked
        # WITH the cause disclosed -- proves the round-2 "block, but name
        # it" contract end to end, not just that exhaustion happened.
        assert exhaustion_forced > 0, "forced exhaustion never produced a disclosed over-block"

    def test_lock_budget_exhaustion_degrades_to_fixed_default_with_warning(
        self, tmp_path: Path
    ) -> None:
        """nexus-plycy (round 2, supersedes nexus-7z7rj round 1's
        "does not owe" default): with NX_EXPECT_LOCK_TRIES=1 and the
        owes-report lockdir already held (fresh, so the reap does not
        remove it), the single try must fail to acquire. Per THE
        CONTRACT in expectations.sh, the CONSUMED-append decision is
        still made ONLY while holding the lock, but the BLOCK decision
        on exhaustion now defaults to "owes" (round 1's "does not owe"
        default, combined with the pre-existing 60s stale-lock reap,
        could silence the guard session-wide for up to a minute — an
        unsafe failure direction a substantive-critic review caught
        before this shipped, T2 nexus/critique-7z7rj-owes-report-
        decision-under-lock). So this agent (genuine unspent background
        credit, unreported transcript) IS blocked even though the lock
        was never acquired — but the block is DISCLOSED as unverified:
        the JSON reason names the lock-contention cause, and the BLOCKED
        row itself carries "lock-exhausted" as a 4th field, distinct
        from a verified CONSUMED-backed block. Separately, the
        exhaustion is still debug-visible: exactly one stderr line
        naming the try budget. CAVEAT: subagent-stop.sh exits 0 on every
        path, and the hook contract only surfaces exit-0 stderr to the
        operator under `claude --debug` (see the LOCKING comment in
        expectations.sh) — this assertion proves the line is emitted,
        not that a normal-session operator sees it unprompted."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)

        # Per-type lockdir (nexus-plycy): NAME has no ':' so the encoding
        # is the identity mapping (see THE CONTRACT's '__'-for-':' note).
        lockdir = Path(str(_expectations_file(tmp_path)) + f".owes.{NAME}.lock")
        lockdir.mkdir(parents=True)  # fresh mtime -- the reap only sweeps stale (>1min) dirs

        proc = _run_hook(
            _payload(transcript=str(t)),
            tmp_path,
            mode="block",
            extra_env={"NX_EXPECT_LOCK_TRIES": "1"},
        )

        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block", decision
        assert "lock contention" in decision["reason"], decision
        assert "expectations: owes-report lock budget exhausted" in proc.stderr
        assert "1 tries" in proc.stderr
        assert "fixed default = owes" in proc.stderr
        assert "cause=lock-exhausted" in proc.stderr
        # The warning is diagnostic-only -- never a ledger row -- but the
        # BLOCKED row IS written (with the disclosed cause), and NO
        # CONSUMED row (never spend credit that was never verified).
        content = _expectations_file(tmp_path).read_text()
        assert "owes-report lock budget exhausted" not in content
        assert "\tCONSUMED\t" not in content
        assert f"\tBLOCKED\t{AGENT_ID}\tlock-exhausted\n" in content

    @pytest.mark.parametrize(
        ("raw", "decimal"),
        [
            ("008", 8),
            ("009", 9),
            ("010", 10),
        ],
    )
    def test_lock_tries_leading_zero_reads_as_decimal(
        self, tmp_path: Path, raw: str, decimal: int
    ) -> None:
        """code-review-expert HIGH (nexus-4b8sz fix round): the
        regex-validated NX_EXPECT_LOCK_TRIES value is used directly in a
        bash arithmetic `((...))` context, where a leading-zero literal is
        parsed as OCTAL — "008"/"009" have no valid octal digit and crash
        the loop init outright (bash: "value too large for base"), and
        "010" silently means 8, not 10. `tries=$((10#$tries))` forces
        base-10 interpretation after the charset guard. This asserts the
        SAFE semantic end to end: the hook never crashes on a leading-zero
        value (exit 0, blocked with a disclosed cause — nexus-plycy's
        round-2 exhaustion-defaults-to-block contract, see
        test_lock_budget_exhaustion_degrades_to_fixed_default_with_warning),
        and the coerced decimal count is exactly what reaches the
        exhaustion warning (proven via the "(N tries)" text — the
        cheapest observable proxy for the loop's actual bound without
        instrumenting the shell)."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)

        lockdir = Path(str(_expectations_file(tmp_path)) + f".owes.{NAME}.lock")
        lockdir.mkdir(parents=True)

        proc = _run_hook(
            _payload(transcript=str(t)),
            tmp_path,
            mode="block",
            extra_env={"NX_EXPECT_LOCK_TRIES": raw},
        )

        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block", decision
        assert f"({decimal} tries)" in proc.stderr, proc.stderr

    def test_lock_tries_clamped_to_ceiling(self, tmp_path: Path) -> None:
        """Reviewer suggestion 1: an oversized NX_EXPECT_LOCK_TRIES must
        not be able to hang the hook — clamped to 600 (~60s) after
        coercion. A real 999999-try exhaustion at 0.1s/try would take
        hours and blow the subprocess timeout, so this test shadows
        `sleep` with a no-op on PATH: the loop still runs its full
        (clamped) iteration count and hits the same mkdir-fails-every-time
        path, just without the wall-clock cost, and the exhaustion
        warning's printed count is the observable proof of the clamp."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)

        lockdir = Path(str(_expectations_file(tmp_path)) + f".owes.{NAME}.lock")
        lockdir.mkdir(parents=True)

        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_sleep = fake_bin / "sleep"
        fake_sleep.write_text("#!/bin/sh\nexit 0\n")
        fake_sleep.chmod(0o755)

        proc = _run_hook(
            _payload(transcript=str(t)),
            tmp_path,
            mode="block",
            extra_env={
                "NX_EXPECT_LOCK_TRIES": "999999",
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            },
        )

        assert proc.returncode == 0, proc.stderr
        assert "(600 tries)" in proc.stderr, proc.stderr

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
    """nexus-suuja/nexus-ahl9v: expectations_undeclared's rc contract is
    four-way: 0 = clean, 1 = recognized==0 blindspot (pre-existing,
    false-clean not a pass), 2 = undeclared>0 — a real declaration-
    completeness deficit that was previously rc-invisible (same exit 0 as
    clean), 3 = no ledger file for this session — also previously
    rc-invisible as the same exit 0 as clean (nexus-ahl9v: a mistyped
    session id audited as rc=0)."""

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

    def test_rc_three_when_no_ledger_file(self, tmp_path: Path) -> None:
        """nexus-ahl9v: a session with no ledger file at all (e.g. a
        mistyped session id) must not audit as rc=0 clean -- it must be
        distinguishable via a dedicated rc plus a stderr NOTE. No
        expectations file is created for this session at all (unlike the
        other cases in this class, which write one)."""
        proc = _run_undeclared(tmp_path, session_id="no-such-session-ever")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "NOTE" in proc.stderr
        assert "no-such-session-ever" in proc.stderr


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
