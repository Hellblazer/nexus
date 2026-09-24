# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-184 ledger, ported to Python (RDR-215 bead nexus-q02nx.9).

Written BEFORE the module, against fixture ledgers, because the bead's Test
Plan names exit codes that nothing outside the tests asserts today — and two
of them (``expect`` and ``start`` returning 2 on invalid input) the existing
bash e2e suite never drives at all.

**Why this file is adversarial about concurrency rather than merely
thorough.** ``conexus/hooks/scripts/expectations.sh`` carries, in its own
comments, three successive rounds of concurrency fixes, each of which moved
the unsafe window somewhere that round's own test could not see: round 1 the
critical section, round 2 the pre-lock stale reap, round 3 the gap between
claiming a slot and writing its row. The lesson the file draws about itself
is that a test which only re-asserts the ceiling under load will keep
passing while the defect relocates. So the ceiling here is asserted with the
LOCK DISABLED — if correctness needs the lock, these fail.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus.hooks import expectations as exp


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated XDG state root, so no test touches the real ledger dir."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _rows(path: Path) -> list[list[str]]:
    return [line.split("\t") for line in path.read_text().splitlines() if line]


# ── expectations_file: path safety ────────────────────────────────────────

class TestFile:
    def test_a_session_id_becomes_a_path_under_the_private_dir(self, state):
        path = Path(exp.expectations_file("sess-A"))
        assert path.name == "sess-A.expectations"
        assert path.parent.name == "orchestration"

    def test_the_state_dir_is_private(self, state):
        path = Path(exp.expectations_file("sess-A"))
        assert (path.parent.stat().st_mode & 0o777) == 0o700

    @pytest.mark.parametrize("bad", ["", "../../escape", "a/b", "with space", "x" * 129])
    def test_a_path_unsafe_session_id_is_refused(self, state, bad):
        """The id is interpolated into a filesystem path, so a traversal
        must never escape the private dir."""
        with pytest.raises(exp.ExpectationsUsageError):
            exp.expectations_file(bad)


# ── expect / start: the write path, and exit code 2 on invalid input ──────

class TestExpect:
    def test_a_background_row_is_appended(self, state):
        exp.expectations_expect("s", "conexus:code-review-expert", "background")
        rows = _rows(Path(exp.expectations_file("s")))
        assert len(rows) == 1
        assert rows[0][1:4] == ["EXPECT", "conexus:code-review-expert", "background"]

    def test_a_colon_qualified_type_is_admitted(self, state):
        """nexus-qc4p1: subagent_type is a legal name and plugin-qualified
        types carry a colon. It stays inert in the readers."""
        exp.expectations_expect("s", "conexus:substantive-critic", "background")
        assert _rows(Path(exp.expectations_file("s")))[0][2] == "conexus:substantive-critic"

    def test_an_optional_dispatch_id_is_carried_uninterpreted(self, state):
        exp.expectations_expect("s", "x", "sync", "dispatch-77")
        assert _rows(Path(exp.expectations_file("s")))[0][4] == "dispatch-77"

    @pytest.mark.parametrize(
        "args",
        [
            ("s", "", "background"),
            ("s", "x", ""),
            ("s", "x", "neither"),
            ("s", "bad name", "background"),
            ("s", "tab\tname", "background"),
            ("s", ":leading-colon", "background"),
        ],
    )
    def test_invalid_input_raises_the_usage_error(self, state, args):
        """Exit code 2 on the shell side. Asserted by no file outside these
        tests today, which is why the bead names it as owed coverage."""
        with pytest.raises(exp.ExpectationsUsageError):
            exp.expectations_expect(*args)

    def test_a_tab_in_dispatch_id_is_refused(self, state):
        """Tab would split a TSV field and silently reshape the row."""
        with pytest.raises(exp.ExpectationsUsageError):
            exp.expectations_expect("s", "x", "sync", "has\ttab")


class TestStart:
    def test_a_start_row_is_appended(self, state):
        exp.expectations_start("s", "a123", "conexus:developer")
        rows = _rows(Path(exp.expectations_file("s")))
        assert rows[0][1:4] == ["START", "a123", "conexus:developer"]

    @pytest.mark.parametrize(
        "args",
        [
            ("s", "", "t"),
            ("s", "a", ""),
            ("s", "tab\tid", "t"),
            ("s", "a", "tab\ttype"),
            ("s", "nl\nid", "t"),
        ],
    )
    def test_invalid_input_raises_the_usage_error(self, state, args):
        """The existing bash e2e suite never drives start with invalid input
        at all -- this is new coverage the port owes."""
        with pytest.raises(exp.ExpectationsUsageError):
            exp.expectations_start(*args)


# ── the atomic credit claim: the invariant three rounds failed to hold ────

class TestCreditClaimIsAtomic:
    """The ceiling is: CONSUMED rows for a type never exceed that type's
    credit. A lock cannot carry it (a name-based lock is stealable through
    the three-step stale reap, and POSIX has no atomic compare-and-delete on
    a path), so the unit of credit is an ``os.symlink``: for each slot name
    exactly one racer on the host can ever win, because link creation is
    atomic create-or-fail.
    """

    def test_exactly_one_claimant_wins_a_slot(self, state):
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        won = [
            exp._claim_credit(str(path), "t", f"agent-{i}", credit=1, spent=0, owners=[])
            for i in range(5)
        ]
        assert won.count(True) == 1, "five racers, one unit of credit, one winner"

    def test_n_slots_admit_exactly_n_claimants(self, state):
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        won = [
            exp._claim_credit(str(path), "t", f"agent-{i}", credit=3, spent=0, owners=[])
            for i in range(10)
        ]
        assert won.count(True) == 3

    def test_the_slot_target_is_the_claiming_agent_id(self, state):
        """Claim and ownership stamp are ONE atomic operation. A
        mkdir-then-write-owner pair would leave a window where a claimed
        slot is anonymous, and readlink is what lets a re-entering agent
        recognise its own claim without consulting the ledger."""
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        exp._claim_credit(str(path), "t", "agent-me", credit=1, spent=0, owners=[])
        assert os.readlink(f"{path}.credit.t.1") == "agent-me"

    def test_re_entry_by_the_same_agent_does_not_consume_a_second_unit(self, state):
        """A crash between the claim and its CONSUMED row re-enters here.
        Still owes; must not spend twice."""
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        first = exp._claim_credit(str(path), "t", "agent-me", credit=2, spent=0, owners=[])
        again = exp._claim_credit(str(path), "t", "agent-me", credit=2, spent=0, owners=[])
        assert first is True and again is True
        assert not Path(f"{path}.credit.t.2").exists(), "the second unit stays unspent"

    def test_reconciliation_recreates_a_slot_per_recorded_row(self, state):
        """Slots are derived state; CONSUMED rows are durable. A ledger from
        a build predating slots has rows and no slots, and must not hand the
        type a fresh fully-unspent pool."""
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        exp._claim_credit(
            str(path), "t", "newcomer", credit=2, spent=1, owners=["older-agent"]
        )
        assert os.readlink(f"{path}.credit.t.1") == "older-agent"
        assert os.readlink(f"{path}.credit.t.2") == "newcomer"

    def test_the_ceiling_holds_with_the_lock_DISABLED(self, state, monkeypatch):
        """The permanent property, and the reason this file exists.

        Round 3's whole point is that correctness no longer depends on the
        lock. If this ever needs the lock to pass, the port has regressed
        into the shape whose unfixability the bash file documents at length.
        """
        # NOTE: _claim_credit reads NO lock variable at all — it is
        # lock-free by construction, which is round 3's entire point. So
        # this assertion holds whatever the environment says, and an
        # earlier version of it set a MISSPELLED variable
        # (NX_EXPECT_LOCK_DISABLED, with a trailing D) without that
        # changing anything, which is how the typo survived. The real
        # lock-disabled property is carried by
        # TestOwesReportSurvivesTheLockBeingDisabled, which drives the
        # function that actually consults the lock.
        monkeypatch.setenv("NX_EXPECT_LOCK_DISABLE", "1")
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        won = [
            exp._claim_credit(str(path), "t", f"a{i}", credit=2, spent=0, owners=[])
            for i in range(12)
        ]
        assert won.count(True) == 2


class TestTwoClaimantsCannotBothWin:
    """Deterministic interleaving — and an honest note on what it does NOT prove.

    The fault-injection seam constructs the losing interleaving instead of
    hoping to observe it: a second claimant runs to completion inside the
    window before our own create lands. That makes the one-winner outcome
    and the loser-moves-on behaviour deterministic rather than sampled,
    which the 16-process load test is not.

    **IT DOES NOT DISCRIMINATE ATOMIC FROM CHECK-THEN-ACT, and I measured
    that rather than assuming it.** Replacing ``os.symlink`` with a
    check-then-act non-exclusive create leaves every assertion in this
    class passing: ``interloper_won == [True]`` and ``mine_won is False``
    both hold, because the seam sits before the whole create operation, so
    for a check-then-act implementation it lands before the CHECK. The
    interloper finishes, the loser then checks, sees the slot taken, and
    behaves exactly as the atomic version does.

    Catching check-then-act behaviourally would need a seam BETWEEN its
    check and its act — a point that exists only in the broken
    implementation. You cannot place a seam in code you do not have. That
    is why ``TestTheClaimIsStructurallyAtomic`` below is kept rather than
    replaced: an AST assertion is the only instrument here that fails on
    the unsafe shape, and its cost (it pins the shape, so a refactor must
    update it) is the price of catching a property no behavioural test in
    this process can reach.

    The seam was suggested by nexus-c3 as a replacement for the AST test.
    It is a good test and it earns its place, but the replacement claim did
    not survive being checked — reported back to them rather than quietly
    kept.
    """

    def test_a_second_claimant_in_the_window_still_leaves_one_winner(self, state, monkeypatch):
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        interloper_won: list[bool] = []

        def _interleave():
            # Run a whole competing claim to completion, exactly once, in
            # the window before our own create lands.
            monkeypatch.setattr(exp, "_CLAIM_INTERLEAVE", None)
            interloper_won.append(
                exp._claim_credit(str(path), "t", "interloper", credit=1, spent=0, owners=[])
            )

        monkeypatch.setattr(exp, "_CLAIM_INTERLEAVE", _interleave)
        mine_won = exp._claim_credit(str(path), "t", "me", credit=1, spent=0, owners=[])

        assert interloper_won == [True], "the interloper must genuinely claim the unit"
        assert mine_won is False, (
            "one unit, two claimants, and the interloper took it inside our own "
            "window — a second winner here is the double-spend three rounds of "
            "this file failed to close"
        )
        assert os.readlink(f"{path}.credit.t.1") == "interloper"

    def test_the_loser_moves_on_to_a_free_slot_rather_than_failing(self, state, monkeypatch):
        """Losing a slot is not losing the claim: with credit to spare the
        claimant must take the next one, or contention would look like
        exhaustion."""
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        fired: list[int] = []

        def _interleave():
            if fired:
                return
            fired.append(1)
            monkeypatch.setattr(exp, "_CLAIM_INTERLEAVE", None)
            exp._claim_credit(str(path), "t", "interloper", credit=2, spent=0, owners=[])

        monkeypatch.setattr(exp, "_CLAIM_INTERLEAVE", _interleave)
        assert exp._claim_credit(str(path), "t", "me", credit=2, spent=0, owners=[]) is True
        assert os.readlink(f"{path}.credit.t.1") == "interloper"
        assert os.readlink(f"{path}.credit.t.2") == "me"

    def test_the_seam_is_None_in_production(self):
        """It is a one-line test hook; it must never be armed by default."""
        assert exp._CLAIM_INTERLEAVE is None


class TestTheClaimIsStructurallyAtomic:
    """The ONLY instrument here that fails on a non-atomic claim.

    Measured while writing this file: replacing ``os.symlink`` with a
    check-then-act using a NON-exclusive create left the 16-process load
    test's winners assertion GREEN. The race is real but rare, so 16 racers
    did not interleave in the window; the test only went red incidentally,
    on a later assertion, because the slots had stopped being symlinks.

    That is precisely the failure the bash file's own history describes —
    "a test which only re-asserts the ceiling under load will keep passing
    while the defect relocates", which is how rounds 1 and 2 were each
    declared fixed.

    The deterministic seam test above does not close it either, and that
    was measured: its assertions pass under a check-then-act claim, because
    a seam placed before the create lands before that implementation's
    check. Catching the unsafe shape behaviourally would need a seam inside
    code that only the broken version contains.

    So the property is asserted STRUCTURALLY: the claim must be a single
    create-or-fail syscall with no existence check before it. The cost is
    real — this pins the SHAPE, so a legitimate refactor has to update it —
    and it is accepted deliberately, because the alternative is no guard at
    all on the one property three rounds of this file failed to hold. Same
    technique as ``test_hook_runtime_thin.py``, which pins an import
    property no functional test can see.
    """

    def _claim_calls(self) -> set[str]:
        return {
            node.func.attr
            for node in ast.walk(ast.parse(inspect.getsource(exp._claim_credit).lstrip()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    def test_the_claim_uses_create_or_fail_and_nothing_else(self):
        calls = self._claim_calls()
        assert "symlink" in calls, "the claim must be os.symlink — atomic create-or-fail"
        forbidden = {"lexists", "exists", "isfile", "islink", "access", "stat"}
        assert not (calls & forbidden), (
            "an existence check before the create reintroduces check-then-act, "
            f"which POSIX cannot make safe on a path: found {sorted(calls & forbidden)}"
        )

    def test_the_claim_never_reclaims_or_removes_a_slot(self):
        """Deliberately NOT done, per the bash file: reclaiming an orphaned
        slot after a bounded age is the same check-then-act shape in a new
        costume, and it is what makes a stolen lock possible."""
        calls = self._claim_calls()
        assert not (calls & {"unlink", "remove", "rmdir"}), (
            "a slot must never be reclaimed inside the claim path"
        )

    def test_the_slot_target_is_an_identity_never_resolved_as_a_path(self):
        """The links are dangling BY DESIGN — the target is an agent id, not
        a path. Anything that resolves them breaks the mechanism."""
        calls = self._claim_calls()
        assert "readlink" in calls, "re-entry detection needs the raw target"
        assert not (calls & {"realpath", "resolve", "abspath"}), (
            "resolving a slot target treats an identity as a path"
        )


class TestCreditClaimUnderRealParallelLoad:
    """The bead's Verification item: N concurrent claims produce exactly N
    credits, no double-claim. Processes, not threads -- the guarantee is a
    kernel one about link(2), and threads in one interpreter would not
    exercise it.

    A SMOKE TEST, not the guarantee. See TestTheClaimIsStructurallyAtomic
    above: a non-atomic claim can pass the winners assertion here, because
    16 racers do not reliably hit the window. Kept because it exercises the
    real syscall under real contention, which the structural test cannot.
    """

    def test_concurrent_processes_never_exceed_the_ceiling(self, state, tmp_path):
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        claimant = tmp_path / "claim.py"
        claimant.write_text(
            "import sys\n"
            "from nexus.hooks import expectations as exp\n"
            "won = exp._claim_credit(sys.argv[1], 't', sys.argv[2],\n"
            "                        credit=4, spent=0, owners=[])\n"
            "sys.exit(0 if won else 1)\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, str(claimant), str(path), f"agent-{i}"],
                env={**os.environ, "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]},
            )
            for i in range(16)
        ]
        winners = sum(1 for p in procs if p.wait() == 0)
        assert winners == 4, f"16 racers, 4 units, {winners} winners"
        slots = sorted(Path(path.parent).glob(f"{path.name}.credit.t.*"))
        assert len(slots) == 4
        assert len({os.readlink(s) for s in slots}) == 4, "each slot a distinct owner"


# ── block-at-most-once, and the terminal-verb read ────────────────────────

class TestBlockedRows:
    def test_a_blocked_row_is_recorded_and_found(self, state):
        exp.expectations_mark_blocked("s", "a1")
        assert exp.expectations_already_blocked("s", "a1") is True

    def test_a_cause_is_carried_when_given(self, state):
        exp.expectations_mark_blocked("s", "a1", "lock-exhausted")
        assert _rows(Path(exp.expectations_file("s")))[0][3] == "lock-exhausted"

    def test_a_different_agent_is_not_blocked(self, state):
        exp.expectations_mark_blocked("s", "a1")
        assert exp.expectations_already_blocked("s", "a2") is False

    def test_a_missing_ledger_reads_as_not_blocked(self, state):
        """Fails OPEN. This composes with owes_report's own fail-open into
        'never block', which is the whole file's failure direction."""
        assert exp.expectations_already_blocked("never-seen", "a1") is False

    @pytest.mark.parametrize("bad", ["tab\there", "nl\nhere"])
    def test_a_tab_or_newline_is_refused(self, state, bad):
        """A misaligned BLOCKED row would never match already_blocked's
        exact-field compare, silently defeating block-at-most-once."""
        with pytest.raises(exp.ExpectationsUsageError):
            exp.expectations_mark_blocked("s", bad)
        with pytest.raises(exp.ExpectationsUsageError):
            exp.expectations_mark_blocked("s", "a1", bad)


class TestLastTerminal:
    def test_the_LAST_terminal_verb_wins(self, state):
        """An agent can be blocked and then report; the caller wants where
        it ended up, not where it started."""
        exp.expectations_mark_blocked("s", "a1")
        exp._append(exp.expectations_file("s"), f"{exp._ts()}\tREPORTED\ta1")
        assert exp.expectations_last_terminal("s", "a1") == "REPORTED"

    def test_a_non_terminal_row_is_not_reported(self, state):
        exp.expectations_start("s", "a1", "t")
        assert exp.expectations_last_terminal("s", "a1") == ""

    def test_a_missing_ledger_is_empty_not_an_error(self, state):
        assert exp.expectations_last_terminal("never-seen", "a1") == ""


# ── archive and sweep ─────────────────────────────────────────────────────

class TestArchiveAndSweep:
    def test_archive_copies_a_ledger(self, state):
        exp.expectations_expect("s", "t", "background")
        exp.expectations_archive()
        archived = exp._archive_dir() / "s.expectations"
        assert archived.is_file()
        assert archived.read_text() == Path(exp.expectations_file("s")).read_text()

    def test_archive_does_not_clobber_a_newer_copy(self, state):
        exp.expectations_expect("s", "t", "background")
        exp.expectations_archive()
        archived = exp._archive_dir() / "s.expectations"
        archived.write_text("NEWER\n")
        os.utime(archived, (time.time() + 60, time.time() + 60))
        exp.expectations_archive()
        assert archived.read_text() == "NEWER\n"

    def test_sweep_reaps_a_ledger_past_the_floor(self, state):
        exp.expectations_expect("s", "t", "background")
        path = Path(exp.expectations_file("s"))
        old = time.time() - (exp._REAP_DAYS + 1) * 86400
        os.utime(path, (old, old))
        exp.expectations_sweep()
        assert not path.exists()

    def test_sweep_spares_a_fresh_ledger(self, state):
        exp.expectations_expect("s", "t", "background")
        exp.expectations_sweep()
        assert Path(exp.expectations_file("s")).exists()

    def test_sweep_reaps_an_aged_credit_slot(self, state):
        """The slots live beside the ledger under a longer name, so the
        ledger's own glob never matched them. Safe because reconciliation
        re-creates any slot that still has a backing CONSUMED row."""
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        exp._claim_credit(str(path), "t", "a1", credit=1, spent=0, owners=[])
        slot = Path(f"{path}.credit.t.1")
        assert slot.is_symlink()
        old = time.time() - (exp._REAP_DAYS + 1) * 86400
        os.utime(slot, (old, old), follow_symlinks=False)
        exp.expectations_sweep()
        assert not slot.is_symlink()

    def test_sweep_uses_lstat_so_a_dangling_slot_is_still_aged(self, state):
        """The slot targets are agent identities, not paths, so every slot
        is a dangling symlink. A sweep that stat()ed through the link would
        raise or skip every one of them."""
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        exp._claim_credit(str(path), "t", "a1", credit=1, spent=0, owners=[])
        slot = Path(f"{path}.credit.t.1")
        assert not slot.exists(), "dangling by design — exists() follows the link"
        assert slot.is_symlink()
        exp.expectations_sweep()
        assert slot.is_symlink(), "fresh, so spared — but it was reachable to stat"


# ── undeclared: the retro audit and all four exit codes ───────────────────

def _seed(session: str, rows: list[tuple[str, ...]]) -> None:
    exp._state_dir()
    for row in rows:
        exp._append(exp.expectations_file(session), "\t".join(("2026-01-01T00:00:00Z", *row)))


class TestUndeclaredExitCodes:
    """All four codes, quoted verbatim in AGENTS.md as the caller-facing
    API: 0 clean, 1 BLINDSPOT, 2 undeclared>0, 3 no ledger."""

    def test_code_0_when_every_start_has_credit(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        r = exp.expectations_undeclared("s")
        assert r.code == 0
        assert not [ln for ln in r.lines if ln.startswith("UNDECLARED")]

    def test_code_2_when_a_start_has_no_credit(self, state):
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_undeclared("s")
        assert r.code == 2
        assert "UNDECLARED\ta1\tt" in r.lines

    def test_code_1_blindspot_when_expects_exist_but_no_start_does(self, state):
        """The one false-clean shape: the audit walked nothing, so
        undeclared=0 is not evidence of compliance."""
        _seed("s", [("EXPECT", "t", "background")])
        r = exp.expectations_undeclared("s")
        assert r.code == 1
        assert any(ln.startswith("BLINDSPOT\tledger holds 1 EXPECT row(s)") for ln in r.lines)

    def test_code_3_when_there_is_no_ledger_at_all(self, state):
        """3 is NOT a pass. Absence of a ledger is not evidence of
        cleanliness, and the note has to say so."""
        r = exp.expectations_undeclared("never-seen")
        assert r.code == 3
        assert r.lines == []
        assert "absence is not evidence of cleanliness" in r.note

    def test_an_empty_ledger_is_clean_not_a_blindspot(self, state):
        """checked==0 AND expect_total==0 is a session that dispatched
        nothing — genuinely clean, distinct from the blindspot shape."""
        Path(exp.expectations_file("s")).write_text("")
        r = exp.expectations_undeclared("s")
        assert r.code == 0


class TestUndeclaredCreditAccounting:
    def test_n_expects_cover_n_starts_of_that_type(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("EXPECT", "t", "background"),
                    ("START", "a1", "t"), ("START", "a2", "t")])
        assert exp.expectations_undeclared("s").code == 0

    def test_the_n_plus_first_start_is_undeclared(self, state):
        _seed("s", [("EXPECT", "t", "background"),
                    ("START", "a1", "t"), ("START", "a2", "t")])
        r = exp.expectations_undeclared("s")
        assert r.code == 2
        assert "UNDECLARED\ta2\tt" in r.lines
        assert "UNDECLARED\ta1\tt" not in r.lines, "credit is spent in START order"

    def test_a_sync_expect_also_supplies_credit(self, state):
        """An EXPECT row of EITHER mode counts: a deliberately-declared
        sync dispatch must stay audit-clean."""
        _seed("s", [("EXPECT", "t", "sync"), ("START", "a1", "t")])
        assert exp.expectations_undeclared("s").code == 0

    def test_a_duplicate_dispatch_id_does_not_inflate_the_pool(self, state):
        """A duplicate EXPECT is not the harmless nuisance a duplicate
        START is — it inflates credit and MASKS an undeclared start. The
        writing hook takes a BOUNDED lock, so this really happens."""
        _seed("s", [("EXPECT", "t", "background", "d1"),
                    ("EXPECT", "t", "background", "d1"),
                    ("START", "a1", "t"), ("START", "a2", "t")])
        r = exp.expectations_undeclared("s")
        assert r.code == 2, "the second START must NOT be covered by a duplicate row"
        assert "UNDECLARED\ta2\tt" in r.lines

    def test_two_distinct_dispatch_ids_both_count(self, state):
        _seed("s", [("EXPECT", "t", "background", "d1"),
                    ("EXPECT", "t", "background", "d2"),
                    ("START", "a1", "t"), ("START", "a2", "t")])
        assert exp.expectations_undeclared("s").code == 0

    def test_a_duplicate_start_id_is_counted_once(self, state):
        _seed("s", [("EXPECT", "t", "background"),
                    ("START", "a1", "t"), ("START", "a1", "t")])
        assert exp.expectations_undeclared("s").code == 0

    def test_credit_is_keyed_by_type_not_shared(self, state):
        _seed("s", [("EXPECT", "t1", "background"), ("START", "a1", "t2")])
        r = exp.expectations_undeclared("s")
        assert r.code == 2
        assert "UNDECLARED\ta1\tt2" in r.lines

    def test_the_summary_line_reports_the_tallies(self, state):
        _seed("s", [("EXPECT", "t", "background"),
                    ("START", "a1", "t"), ("START", "a2", "other")])
        summary = [ln for ln in exp.expectations_undeclared("s").lines
                   if ln.startswith("SUMMARY")][0]
        assert summary == "SUMMARY\tchecked=2 recognized=1 unrecognized=1 undeclared=1"


# ── nexus-silj0: 'workflow-subagent' gets its own bucket, undeclared side ──

class TestUndeclaredWorkflowSubagentBucket:
    """Sam's ruling 2026-09-23 (bead nexus-silj0, option 2 of the bead's own
    candidates): a START whose type is exactly ``workflow-subagent`` is
    counted and reported, but excluded from the ``undeclared`` deficit and
    can never spend another type's EXPECT credit. Reproduces the bead's own
    measured shape (session 2109cc46: 11 workflow STARTs + 2 clean
    Agent-tool dispatches -> real bash gave rc=2 with SUMMARY
    checked=13 ... undeclared=11)."""

    def test_the_beads_measured_shape_is_now_clean_and_reports_workflow(self, state):
        rows = [("EXPECT", "conexus:code-review-expert", "background"),
                ("EXPECT", "conexus:substantive-critic", "background"),
                ("START", "r1", "conexus:code-review-expert"),
                ("START", "r2", "conexus:substantive-critic")]
        rows += [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        r = exp.expectations_undeclared("s")
        assert r.code == 0
        assert not [ln for ln in r.lines if ln.startswith("UNDECLARED")]
        assert "WORKFLOW\tchecked=11" in r.lines
        assert "SUMMARY\tchecked=2 recognized=2 unrecognized=0 undeclared=0" in r.lines

    def test_a_genuinely_undeclared_agent_still_flags_only_itself(self, state):
        """Workflow STARTs must not swamp OR hide a real undeclared
        Agent-tool dispatch mixed into the same ledger."""
        rows = [("START", "rogue", "conexus:developer")]
        rows += [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        r = exp.expectations_undeclared("s")
        assert r.code == 2
        assert "UNDECLARED\trogue\tconexus:developer" in r.lines
        assert not [ln for ln in r.lines
                    if ln.startswith("UNDECLARED") and exp.WORKFLOW_SUBAGENT_TYPE in ln]
        assert "WORKFLOW\tchecked=11" in r.lines
        assert "SUMMARY\tchecked=1 recognized=0 unrecognized=1 undeclared=1" in r.lines

    def test_a_workflow_only_session_with_no_expect_rows_is_clean_not_blindspot(self, state):
        """Nothing Agent-tool-shaped happened, so 0 is correct -- and it is
        not the false-clean BLINDSPOT shape either, since that needs an
        EXPECT row with zero STARTs and this ledger has neither."""
        rows = [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        r = exp.expectations_undeclared("s")
        assert r.code == 0
        assert not [ln for ln in r.lines if ln.startswith("BLINDSPOT")]
        assert "WORKFLOW\tchecked=11" in r.lines
        assert "SUMMARY\tchecked=0 recognized=0 unrecognized=0 undeclared=0" in r.lines

    def test_a_stray_expect_with_no_start_still_trips_blindspot_despite_workflow_starts(self, state):
        """Workflow STARTs must not paper over a genuine BLINDSPOT: an
        Agent-tool EXPECT row with zero matching STARTs is still a walk
        that examined nothing about that type, whatever else ran."""
        rows = [("EXPECT", "conexus:developer", "background")]
        rows += [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        r = exp.expectations_undeclared("s")
        assert r.code == 1
        assert any(ln.startswith("BLINDSPOT\t") for ln in r.lines)
        assert "WORKFLOW\tchecked=11" in r.lines

    def test_a_workflow_start_never_spends_another_types_credit(self, state):
        _seed("s", [("EXPECT", "t", "background"),
                     ("START", "w1", exp.WORKFLOW_SUBAGENT_TYPE),
                     ("START", "a1", "t")])
        r = exp.expectations_undeclared("s")
        assert r.code == 0
        assert "WORKFLOW\tchecked=1" in r.lines

    def test_duplicate_workflow_agent_ids_are_counted_once(self, state):
        _seed("s", [("START", "w1", exp.WORKFLOW_SUBAGENT_TYPE),
                     ("START", "w1", exp.WORKFLOW_SUBAGENT_TYPE)])
        r = exp.expectations_undeclared("s")
        assert "WORKFLOW\tchecked=1" in r.lines

    def test_no_workflow_line_when_there_are_no_workflow_starts(self, state):
        """The line is conditional, matching BLINDSPOT's own convention --
        an all-Agent-tool ledger must not gain a spurious 'checked=0' line."""
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        r = exp.expectations_undeclared("s")
        assert not [ln for ln in r.lines if ln.startswith("WORKFLOW")]

    def test_the_workflow_line_precedes_summary_so_the_last_line_invariant_holds(self, state):
        """Pins TestTheEmptyShapeIsNotNarrowerThanThePopulatedOne's
        'a populated reader always ends with SUMMARY or BLINDSPOT' contract
        against a ledger that now also carries a WORKFLOW line."""
        rows = [("EXPECT", "t", "background"), ("START", "a1", "t")]
        rows += [("START", "w1", exp.WORKFLOW_SUBAGENT_TYPE)]
        _seed("s", rows)
        r = exp.expectations_undeclared("s")
        assert r.lines[-1].startswith("SUMMARY\t")
        assert r.lines.index("WORKFLOW\tchecked=1") < len(r.lines) - 1


# ── owes_report: the consult rule, and both disclosed causes ─────────────

#: Retained for :class:`TestTheDualImplementationParamDiesWithTheLibrary`
#: below, which checks whether the bash library still exists on disk. The
#: differential classes that used to run it as a live oracle (RDR-215 bead
#: nexus-q02nx.21: all twelve hooks.json entries, including this one, are
#: now re-declared to their Python mcp_tools, so the bash path they
#: compared against no longer runs in production) are gone; every scenario
#: they covered has a direct assertion above (e.g.
#: ``test_n_expects_cover_n_starts_of_that_type``,
#: ``test_a_sync_expect_also_supplies_credit``,
#: ``test_credit_is_keyed_by_type_not_shared``).
_BASH_LIB = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "expectations.sh"

class TestOwesReport:
    def test_a_background_expect_makes_the_first_stop_owe(self, state):
        _seed("s", [("EXPECT", "t", "background")])
        assert exp.expectations_owes_report("s", "a1", "t").owes is True

    def test_the_stop_writes_a_consumed_row(self, state):
        _seed("s", [("EXPECT", "t", "background")])
        exp.expectations_owes_report("s", "a1", "t")
        assert any(r[1] == "CONSUMED" and r[2] == "a1"
                   for r in _rows(Path(exp.expectations_file("s"))))

    def test_a_second_agent_of_the_same_type_does_not_owe(self, state):
        """One unit of credit, one debit. The second stop of that type is
        not covered, and must not be blocked."""
        _seed("s", [("EXPECT", "t", "background")])
        exp.expectations_owes_report("s", "a1", "t")
        assert exp.expectations_owes_report("s", "a2", "t").owes is False

    def test_two_credits_cover_two_agents(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("EXPECT", "t", "background")])
        assert exp.expectations_owes_report("s", "a1", "t").owes is True
        assert exp.expectations_owes_report("s", "a2", "t").owes is True

    def test_re_entry_by_the_same_agent_still_owes_and_spends_once(self, state):
        """A crash between the claim and the stop re-enters here: the self
        branch. Still owes; must not consume a second unit."""
        _seed("s", [("EXPECT", "t", "background"), ("EXPECT", "t", "background")])
        exp.expectations_owes_report("s", "a1", "t")
        again = exp.expectations_owes_report("s", "a1", "t")
        assert again.owes is True
        consumed = [r for r in _rows(Path(exp.expectations_file("s"))) if r[1] == "CONSUMED"]
        assert len(consumed) == 1, "the re-entry must not write a second debit"

    def test_a_mixed_pool_never_owes(self, state):
        """A type with ANY non-background EXPECT row is not a pure
        background pool, and guessing at one is how an ordinary sync
        dispatch gets blocked."""
        _seed("s", [("EXPECT", "t", "background"), ("EXPECT", "t", "sync")])
        assert exp.expectations_owes_report("s", "a1", "t").owes is False

    def test_credit_is_type_keyed(self, state):
        _seed("s", [("EXPECT", "t1", "background")])
        assert exp.expectations_owes_report("s", "a1", "t2").owes is False

    @pytest.mark.parametrize(
        "args",
        [("", "a", "t"), ("s", "", "t"), ("s", "a", ""), ("s", "a", "bad type"),
         ("s", "tab\tid", "t")],
    )
    def test_degraded_input_fails_open(self, state, args):
        """Fails OPEN on every degraded input. A missing ledger, a bad
        charset or an empty id must never block a stop."""
        assert exp.expectations_owes_report(*args).owes is False

    def test_a_missing_ledger_fails_open(self, state):
        assert exp.expectations_owes_report("never-seen", "a1", "t").owes is False

    def test_a_colon_qualified_type_encodes_its_sidecar_names(self, state):
        """':' is legal in a subagent type and would otherwise land in
        sidecar FILE names — the round-2 fix."""
        _seed("s", [("EXPECT", "conexus:critic", "background")])
        assert exp.expectations_owes_report("s", "a1", "conexus:critic").owes is True
        base = Path(exp.expectations_file("s"))
        assert Path(f"{base}.credit.conexus__critic.1").is_symlink()


class TestOwesReportDisclosedCauses:
    """Both values are asserted BY VALUE in test_subagent_stop_hook.py and
    are appended to the block reason an operator reads."""

    def test_lock_exhaustion_blocks_with_its_cause(self, state, monkeypatch):
        """Over-blocking is explicable; a silent miss is the failure this
        subsystem exists to prevent. So an exhausted budget consults no
        credit and takes a fixed default of owes."""
        _seed("s", [("EXPECT", "t", "background")])
        monkeypatch.setenv("NX_EXPECT_LOCK_TRIES", "1")
        os.mkdir(f"{exp.expectations_file('s')}.owes.t.lock")  # held by "someone"
        verdict = exp.expectations_owes_report("s", "a1", "t")
        assert verdict.owes is True
        assert verdict.cause == "lock-exhausted"

    def test_a_credit_slot_orphan_blocks_with_its_cause(self, state):
        """Every slot claimed while the ROWS still say unspent credit is
        provably inconsistent: a claimant was killed between its slot claim
        and its CONSUMED row. SubagentStop has a 10s timeout, so that kill
        is routine and load-correlated, not rare."""
        _seed("s", [("EXPECT", "t", "background")])
        base = exp.expectations_file("s")
        os.symlink("ghost-agent", f"{base}.credit.t.1")  # claimed, no row
        verdict = exp.expectations_owes_report("s", "a1", "t")
        assert verdict.owes is True
        assert verdict.cause == "credit-slot-orphan"

    def test_a_clean_verdict_carries_no_cause(self, state):
        _seed("s", [("EXPECT", "t", "background")])
        assert exp.expectations_owes_report("s", "a1", "t").cause == ""


class TestOwesReportSurvivesTheLockBeingDisabled:
    def test_the_ceiling_holds_with_no_mutual_exclusion(self, state, monkeypatch):
        """Round 3's whole point: correctness moved to the atomic slot, so
        the accounting must be exact even with the lock switched off."""
        monkeypatch.setenv("NX_EXPECT_LOCK_DISABLE", "1")
        _seed("s", [("EXPECT", "t", "background"), ("EXPECT", "t", "background")])
        owed = [exp.expectations_owes_report("s", f"a{i}", "t").owes for i in range(6)]
        assert owed.count(True) == 2, "two units of credit, two debits, no more"
        consumed = [r for r in _rows(Path(exp.expectations_file("s"))) if r[1] == "CONSUMED"]
        assert len(consumed) == 2


# ── census: the scripted retro count, and its 0/1-only vocabulary ────────

class TestCensus:
    def test_a_reported_agent_is_classified_and_declared(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t"),
                    ("REPORTED", "a1")])
        lines = exp.expectations_census("s").lines
        assert "AGENT\ta1\tt\tREPORTED\tdeclared" in lines

    def test_blocked_then_reported_is_the_success_path(self, state):
        """BLOCKED then REPORTED is the whole guard working: stopped, told
        why, came back. It is BLOCKED_RESOLVED, not two separate states."""
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t"),
                    ("BLOCKED", "a1"), ("REPORTED", "a1")])
        lines = exp.expectations_census("s").lines
        assert "AGENT\ta1\tt\tBLOCKED_RESOLVED\tdeclared" in lines
        assert any("blocked_resolved=1 (immediate=1 later=0)" in ln for ln in lines)

    def test_a_later_resolution_is_counted_separately(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t"),
                    ("BLOCKED", "a1"), ("REPORTED", "a1", "later")])
        assert any("(immediate=0 later=1)" in ln for ln in exp.expectations_census("s").lines)

    def test_an_agent_with_no_terminal_row_is_named(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        assert "AGENT\ta1\tt\tNO_TERMINAL\tdeclared" in exp.expectations_census("s").lines

    def test_a_terminal_without_a_start_is_no_start(self, state):
        _seed("s", [("REPORTED", "ghost")])
        assert "AGENT\tghost\t-\tREPORTED\tno-start" in exp.expectations_census("s").lines

    def test_an_expect_with_no_start_is_reported(self, state):
        """Two DISTINCT dispatches declared, one started.

        The rows carry different dispatch ids deliberately: two
        byte-identical EXPECT rows are deduped as a double-write
        (nexus-3h0u6), so they would count as one declaration and this
        would not fire. Confirmed against bash before fixing the test —
        the first version of this asserted the wrong thing."""
        _seed("s", [("EXPECT", "t", "background", "d1"), ("START", "a1", "t"),
                    ("EXPECT", "t", "background", "d2")])
        assert "EXPECTED_NO_START\tt" in exp.expectations_census("s").lines

    def test_two_identical_expect_rows_do_NOT_make_an_expected_no_start(self, state):
        """The inverse, and the reason the test above needs distinct ids:
        a byte-identical repeat is one declaration, so one START covers it."""
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t"),
                    ("EXPECT", "t", "background")])
        assert "EXPECTED_NO_START\tt" not in exp.expectations_census("s").lines

    def test_an_exact_duplicate_row_is_counted_once(self, state):
        """nexus-3h0u6. A byte-identical repeat is a double-write, not two
        events."""
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        exp._append(exp.expectations_file("s"), "2026-01-01T00:00:00Z\tSTART\ta1\tt")
        assert any("start=1" in ln for ln in exp.expectations_census("s").lines)

    def test_code_1_when_the_walk_examined_nothing(self, state):
        _seed("s", [("EXPECT", "t", "background")])
        assert exp.expectations_census("s").code == 1

    def test_code_0_on_a_populated_ledger(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        assert exp.expectations_census("s").code == 0

    def test_census_NEVER_returns_2(self, state):
        """0 and 1 only. The 2 vocabulary belongs to undeclared alone, and
        conflating them is how a census gets read as an audit."""
        for rows in ([], [("START", "a1", "t")], [("EXPECT", "t", "background")],
                     [("EXPECT", "t", "background"), ("START", "a1", "t"),
                      ("START", "a2", "t")]):
            session = f"s{len(rows)}{rows!r:.10}".replace(" ", "")[:40]
            session = "".join(c for c in session if c.isalnum() or c in "-_")
            _seed(session, rows)
            assert exp.expectations_census(session).code in (0, 1)

    def test_a_missing_ledger_is_code_0_and_silent(self, state):
        r = exp.expectations_census("never-seen")
        assert r.code == 0 and r.lines == []


# ── nexus-silj0: 'workflow-subagent' gets its own bucket, census side ─────

class TestCensusWorkflowSubagentBucket:
    """The same bucketing rule, on the census's own vocabulary: no AGENT
    line per workflow instance, no contribution to CLASSIFIED's undeclared
    or no_terminal tallies, and a WORKFLOW checked=<n> line reporting the
    count instead of letting it vanish."""

    def test_the_beads_measured_shape_reports_workflow_and_classifies_clean(self, state):
        rows = [("EXPECT", "conexus:code-review-expert", "background"),
                ("EXPECT", "conexus:substantive-critic", "background"),
                ("START", "r1", "conexus:code-review-expert"), ("REPORTED", "r1"),
                ("START", "r2", "conexus:substantive-critic"), ("REPORTED", "r2")]
        rows += [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        lines = exp.expectations_census("s").lines
        assert "WORKFLOW\tchecked=11" in lines
        assert not [ln for ln in lines if ln.startswith("AGENT") and "workflow-subagent" in ln]
        assert any(
            "undeclared=0" in ln and "no_terminal=0" in ln
            for ln in lines if ln.startswith("CLASSIFIED")
        )

    def test_workflow_starts_do_not_inflate_checked_or_blindspot(self, state):
        """checked (and BLINDSPOT's tally, and the walked-nothing code=1
        rule) must key off Agent-tool STARTs only."""
        rows = [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        r = exp.expectations_census("s")
        assert r.code == 0, "no EXPECT rows at all, so this is clean, not a blindspot"
        assert any(ln == "BLINDSPOT\tchecked=0 recognized=0 unrecognized=0" for ln in r.lines)
        assert "WORKFLOW\tchecked=11" in r.lines

    def test_a_stray_expect_with_no_start_still_trips_census_code_1(self, state):
        rows = [("EXPECT", "conexus:developer", "background")]
        rows += [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(11)]
        _seed("s", rows)
        r = exp.expectations_census("s")
        assert r.code == 1
        assert "WORKFLOW\tchecked=11" in r.lines

    def test_a_workflow_terminal_row_does_not_become_a_no_start_ghost(self, state):
        """A REPORTED for a workflow agent must not fall into the 'terminal
        with no matching START' AGENT\t...\t-\t...\tno-start branch."""
        _seed("s", [("START", "w1", exp.WORKFLOW_SUBAGENT_TYPE), ("REPORTED", "w1")])
        lines = exp.expectations_census("s").lines
        assert not [ln for ln in lines if ln.startswith("AGENT\tw1\t-")]
        assert "WORKFLOW\tchecked=1" in lines

    def test_no_workflow_line_when_there_are_no_workflow_starts(self, state):
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        lines = exp.expectations_census("s").lines
        assert not [ln for ln in lines if ln.startswith("WORKFLOW")]

    def test_the_workflow_line_precedes_the_rows_classified_blindspot_tail(self, state):
        """Pins TestTheEmptyShapeIsNotNarrowerThanThePopulatedOne's rule that
        only SPACE_*/VERIFY_* lines may follow the last of ROWS/CLASSIFIED/
        BLINDSPOT -- WORKFLOW must sit before that boundary, not after."""
        _seed("s", [("START", "w1", exp.WORKFLOW_SUBAGENT_TYPE)])
        lines = exp.expectations_census("s").lines
        boundary = max(
            i for i, ln in enumerate(lines)
            if ln.startswith(("ROWS\t", "CLASSIFIED\t", "BLINDSPOT\t"))
        )
        assert lines.index("WORKFLOW\tchecked=1") < boundary


# ── reconcile: the harness's own ground truth ────────────────────────────

def _payload(*tasks, transcript_path: str | None = None) -> str:
    data: dict = {"background_tasks": list(tasks)}
    if transcript_path is not None:
        data["transcript_path"] = transcript_path
    return json.dumps(data)


class TestReconcile:
    def test_a_started_agent_the_harness_still_tracks_is_clean(self, state):
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"agent_id": "a1"}))
        assert r.code == 0
        assert not [ln for ln in r.lines if ln.startswith("STRANDED")]

    def test_an_outstanding_start_the_harness_forgot_is_STRANDED(self, state):
        """The gap no other surface can see: a hook crash, an OOM kill or a
        SIGKILL between dispatch and stop leaves a START with no terminal
        row, indistinguishable from 'still running' from the ledger alone."""
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload())
        assert r.code == 4
        assert "STRANDED\ta1\tt" in r.lines

    def test_a_terminated_agent_is_not_outstanding(self, state):
        _seed("s", [("START", "a1", "t"), ("REPORTED", "a1")])
        r = exp.expectations_reconcile("s", _payload())
        assert r.code == 0

    def test_a_harness_task_with_no_start_is_an_undeclared_task(self, state):
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"agent_id": "a1"}, {"id": "ghost"}))
        assert r.code == 2
        assert "UNDECLARED_TASK\tghost" in r.lines

    def test_stranded_outranks_undeclared(self, state):
        """4 takes priority over 2: a silent death outranks a bookkeeping
        gap, and a caller branching on the code must see the worse one."""
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"id": "ghost"}))
        assert r.code == 4

    def test_an_ABSENT_background_tasks_key_reconciles_nothing(self, state):
        """ABSENT is not an empty list. The harness told us nothing, so
        there is no ground truth and an outstanding START is NOT stranded —
        treating absent as empty would strand every live agent."""
        _seed("s", [("START", "a1", "t")])
        assert exp.expectations_reconcile("s", json.dumps({})).code == 0
        assert exp.expectations_reconcile("s", json.dumps({"background_tasks": None})).code == 0

    def test_an_EMPTY_list_is_affirmative_and_does_strand(self, state):
        """The distinguishing case, and the reason absent must stay its own
        outcome: here the harness affirmatively reports nothing running."""
        _seed("s", [("START", "a1", "t")])
        assert exp.expectations_reconcile("s", json.dumps({"background_tasks": []})).code == 4

    def test_a_junk_payload_never_blocks(self, state):
        _seed("s", [("START", "a1", "t")])
        for junk in ("", "not json", "[]", "null", '{"background_tasks": "nope"}'):
            assert exp.expectations_reconcile("s", junk).code == 0

    def test_a_mixed_population_is_read_by_any_known_id_key(self, state):
        """Bead .6 measured the live shape: a shell task and a subagent
        task carry DIFFERENT key sets. The reader tries each candidate."""
        _seed("s", [("START", "a1", "t"), ("START", "b2", "t")])
        r = exp.expectations_reconcile(
            "s", _payload({"id": "a1", "type": "shell"},
                          {"agent_id": "b2", "type": "subagent"})
        )
        assert r.code == 0

    def test_an_unidentifiable_task_still_counts_toward_the_total(self, state):
        """Dropping it would make an unidentifiable task look like no task."""
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"agent_id": "a1"}, {"no": "id"}))
        summary = [ln for ln in r.lines if ln.startswith("SUMMARY")][0]
        assert "harness_tasks=2 unidentified=1" in summary


# ── nexus-silj0 follow-up: measured, 'workflow-subagent' on reconcile ─────

def _workflow_rows(n=11, reported=None):
    """n workflow-subagent STARTs; ``reported`` (default: all) get a
    REPORTED terminal row too."""
    reported = range(n) if reported is None else reported
    rows = [("START", f"w{i}", exp.WORKFLOW_SUBAGENT_TYPE) for i in range(n)]
    rows += [("REPORTED", f"w{i}") for i in reported]
    return rows


#: The harness-visible identity for THE bead's own measured Workflow run,
#: confirmed by directly reading session 2109cc46-2876-4409-b4f1-ac730d1cc5ed's
#: transcript and its persisted workflow state (nexus-silj0 follow-up round
#: 2; see the expectations_reconcile docstring for the full citation).
#: ``<project>/2109cc46-.../workflows/wf_baae5a4e-bfd.json`` carries BOTH
#: ``"runId": "wf_baae5a4e-bfd"`` (the tool's own ^wf_[a-z0-9-]{6,}$
#: resume-token) and ``"taskId": "w2bole9id"`` (a SEPARATE, opaque id, no
#: ``wf_`` prefix); the transcript's own delivered task-notification
#: (.jsonl lines 604/606) carries ``<task-id>w2bole9id</task-id>``, never
#: the runId, with one ``<summary>Dynamic workflow "..." completed</summary>``
#: line for the whole 11-agent run. THIS is the id a Workflow container task
#: would carry in a real ``background_tasks`` entry -- an earlier round of
#: this fix used the runId here, which was wrong (corrected).
_WORKFLOW_CONTAINER_TASK_ID = "w2bole9id"

#: The Workflow run's own runId (filename stem of its persisted state), as
#: opposed to its taskId above -- kept distinct in every fixture that uses
#: both, so a test can never accidentally collapse the two identities the
#: round-2 correction exists to keep apart.
_WORKFLOW_RUN_ID = "wf_baae5a4e-bfd"


def _measured_workflow_state(task_id: str = _WORKFLOW_CONTAINER_TASK_ID,
                              run_id: str = _WORKFLOW_RUN_ID) -> dict:
    """A trimmed copy of session 2109cc46's own real
    ``workflows/wf_baae5a4e-bfd.json`` -- every top-level key that file
    carries (runId, timestamp, taskId, script, scriptPath, args, result,
    agentCount, logs, durationMs, summary, workflowName, status, startTime,
    phases, defaultModel, workflowProgress, totalTokens, totalToolCalls),
    with the bulky script/logs/result/phases/workflowProgress payloads
    shortened to a marker per the nexus-silj0 round-4 measured-shape
    instruction. agentCount=11, durationMs=505313, totalTokens=1580374,
    totalToolCalls=90, status="completed" are the real measured values;
    only script/logs/result/phases/workflowProgress are trimmed.
    """
    return {
        "runId": run_id,
        "timestamp": "2026-09-21T21:31:10.402Z",
        "taskId": task_id,
        "script": "// dead-wire-census.js (trimmed for the test fixture)",
        "scriptPath": "/private/tmp/.../scratchpad/dead-wire-census.js",
        "args": {"surface": "every MCP tool ...", "scopeHints": None},
        "result": {"surface": "trimmed", "itemCount": 0, "rows": [],
                    "droppedIds": [], "unverifiedCandidateIds": [], "complete": True},
        "agentCount": 11,
        "logs": [],
        "durationMs": 505313,
        "summary": (
            'Enumerate a surface (MCP tools, CLI verbs, skills, or HTTP '
            'routes), trace each item to its consumers, adversarially '
            're-check every "dead" verdict, and return an evidence-backed '
            "census table."
        ),
        "workflowName": "dead-wire-census",
        "status": "completed",
        "startTime": 1790025765076,
        "phases": [],
        "defaultModel": "claude-opus-5",
        "workflowProgress": [],
        "totalTokens": 1580374,
        "totalToolCalls": 90,
    }


def _seed_workflow_container_file(tmp_path: Path, session: str,
                                   task_id: str = _WORKFLOW_CONTAINER_TASK_ID,
                                   run_id: str = _WORKFLOW_RUN_ID,
                                   state_json: dict | None = None) -> str:
    """Writes a Workflow run's persisted state to
    ``<tmp_path>/<session>/workflows/<run_id>.json`` -- the real, measured
    location (nexus-silj0 round 4) -- and returns the ``transcript_path``
    a Stop-hook payload for that session would carry:
    ``<tmp_path>/<session>.jsonl``, a SIBLING path the reader never opens.
    """
    workflows_dir = tmp_path / session / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    payload = state_json if state_json is not None else _measured_workflow_state(task_id, run_id)
    (workflows_dir / f"{run_id}.json").write_text(json.dumps(payload))
    return str(tmp_path / f"{session}.jsonl")


class TestReconcileWorkflowSubagentBucket:
    """MEASURED (not assumed) via constructed ledgers + harness payload
    shapes mirroring the existing TestReconcile fixtures, per the follow-up
    to Sam's nexus-silj0 ruling. Three rounds of findings:

    (1) A Workflow-tool run whose STARTs are still mid-flight (not yet
    REPORTED) when reconcile happens to run gets falsely STRANDED -- every
    one of them, at the module's WORST severity (code 4) -- because the
    check can never tell 'this agent died' from 'the harness only tracks
    the workflow at container granularity, so no per-agent id was ever
    going to appear'. FIXED (round 2): workflow-subagent STARTs are
    excluded from the STRANDED population, exactly as they are from
    undeclared/census, and counted on their own WORKFLOW line instead.

    (2)/(3) The Workflow tool's own container-level harness task (identity
    confirmed by direct transcript read, see :data:`_WORKFLOW_CONTAINER_TASK_ID`)
    still fails to match any START's agent_id and so would surface as
    UNDECLARED_TASK whenever the harness reports one. CLOSED (round 4) via
    session-directory discovery, NOT a guessed `type` value: the Stop-hook
    payload's own `transcript_path` (see :func:`nexus.hooks.expectations._payload_transcript_path`)
    names the session's sibling directory, whose `workflows/*.json` files
    carry a `taskId` field that is EXACTLY the identity a real
    `background_tasks` entry uses for that run --
    :func:`nexus.hooks.expectations._workflow_container_task_ids` reads
    them and the reconcile function excludes any match from UNDECLARED_TASK,
    counting it on the WORKFLOW line's `containers=<m>` field instead. The
    fail-safe fallback (no `transcript_path`, no session dir, no
    `workflows/` subdir, an unreadable/unparseable file) keeps TODAY'S
    behaviour -- the pre-round-4 tests below that never wire a
    `transcript_path` exercise exactly that fallback, deliberately.
    """

    def test_mid_flight_workflow_starts_are_no_longer_falsely_stranded(self, state):
        """The bug: a healthy, still-running workflow misread as several
        silent deaths purely because the harness tracks it at container
        granularity, not per agent."""
        _seed("s", _workflow_rows(11, reported=range(8)))  # w8, w9, w10 still running
        r = exp.expectations_reconcile(
            "s", _payload({"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"})
        )
        assert not [ln for ln in r.lines if ln.startswith("STRANDED")]
        assert "WORKFLOW\tchecked=11 containers=0" in r.lines
        assert "SUMMARY\toutstanding=0 harness_tasks=1 unidentified=0 stranded=0 undeclared_tasks=1" in r.lines

    def test_mid_flight_workflow_starts_are_not_stranded_even_with_zero_harness_tasks(self, state):
        """The harness reporting nothing at all (container gone too) must
        not turn into per-agent STRANDED lines either -- the signal was
        never reliable enough to act on for this class either way."""
        _seed("s", _workflow_rows(11, reported=range(10)))  # w10 never reported
        r = exp.expectations_reconcile("s", _payload())
        assert r.code == 0
        assert not [ln for ln in r.lines if ln.startswith("STRANDED")]
        assert "WORKFLOW\tchecked=11 containers=0" in r.lines

    def test_a_clean_workflow_run_without_discovery_still_trips_the_fallback(self, state):
        """No transcript_path wired: the fail-safe fallback applies, so the
        container's own UNDECLARED_TASK still fires -- this is the
        pre-round-4 (fallback) behaviour, pinned deliberately, not a
        regression."""
        _seed("s", _workflow_rows(11))
        r = exp.expectations_reconcile(
            "s", _payload({"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"})
        )
        assert not [ln for ln in r.lines if ln.startswith("STRANDED")]
        assert r.code == 2
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines
        assert "WORKFLOW\tchecked=11 containers=0" in r.lines

    def test_the_container_task_is_now_excluded_when_discovered_via_transcript_path(self, state, tmp_path):
        """THE round-4 fix, built from the measured shape
        (:func:`_measured_workflow_state`): with a real
        workflows/<runId>.json on disk and transcript_path wired through
        the payload, the SAME container id that round 3 could only pin as
        an open residual is now excluded from UNDECLARED_TASK entirely and
        counted on the WORKFLOW line instead."""
        transcript_path = _seed_workflow_container_file(tmp_path, "s")
        _seed("s", _workflow_rows(11))
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                transcript_path=transcript_path,
            ),
        )
        assert not [ln for ln in r.lines if ln.startswith("STRANDED")]
        assert not [ln for ln in r.lines if ln.startswith("UNDECLARED_TASK")]
        assert r.code == 0
        assert "WORKFLOW\tchecked=11 containers=1" in r.lines

    def test_discovery_does_not_mask_a_genuinely_undeclared_harness_task(self, state, tmp_path):
        """The discovered container id excludes ONLY itself -- a second,
        unrelated harness identity with no matching START must still trip
        UNDECLARED_TASK even with transcript_path wired."""
        transcript_path = _seed_workflow_container_file(tmp_path, "s")
        _seed("s", _workflow_rows(11))
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                {"id": "ghost-real"},
                transcript_path=transcript_path,
            ),
        )
        assert r.code == 2
        assert "UNDECLARED_TASK\tghost-real" in r.lines
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" not in r.lines

    def test_a_missing_transcript_path_excludes_nothing(self, state, tmp_path):
        """Fail-safe axis 1: transcript_path absent from the payload."""
        _seed_workflow_container_file(tmp_path, "s")  # file exists...
        _seed("s", _workflow_rows(11))
        r = exp.expectations_reconcile(
            "s", _payload({"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"})
        )  # ...but transcript_path is never passed
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines
        assert "WORKFLOW\tchecked=11 containers=0" in r.lines

    def test_a_transcript_path_with_no_session_dir_excludes_nothing(self, state, tmp_path):
        """Fail-safe axis 2: transcript_path points somewhere with no
        sibling session directory at all."""
        _seed("s", _workflow_rows(11))
        transcript_path = str(tmp_path / "no-such-session.jsonl")
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                transcript_path=transcript_path,
            ),
        )
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines

    def test_a_session_dir_with_no_workflows_subdir_excludes_nothing(self, state, tmp_path):
        """Fail-safe axis 3: the session dir exists (e.g. subagent
        transcripts live there) but has no workflows/ subdirectory."""
        (tmp_path / "s").mkdir()
        _seed("s", _workflow_rows(11))
        transcript_path = str(tmp_path / "s.jsonl")
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                transcript_path=transcript_path,
            ),
        )
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines

    def test_an_unreadable_workflow_file_excludes_nothing_and_does_not_raise(self, state, tmp_path):
        """Fail-safe axis 4: a workflows/*.json file that fails to parse
        must be skipped, not raise and not block the stop."""
        workflows_dir = tmp_path / "s" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / f"{_WORKFLOW_RUN_ID}.json").write_text("{not valid json")
        _seed("s", _workflow_rows(11))
        transcript_path = str(tmp_path / "s.jsonl")
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                transcript_path=transcript_path,
            ),
        )
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines

    def test_a_workflow_file_with_no_taskId_field_is_skipped_not_matched(self, state, tmp_path):
        """A malformed-but-parseable state file (e.g. an older format with
        no taskId) contributes no identity, so the container id it would
        have carried is still not excluded."""
        state_json = _measured_workflow_state()
        del state_json["taskId"]
        _seed_workflow_container_file(tmp_path, "s", state_json=state_json)
        _seed("s", _workflow_rows(11))
        transcript_path = str(tmp_path / "s.jsonl")
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                transcript_path=transcript_path,
            ),
        )
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines

    def test_multiple_workflow_runs_in_one_session_all_excluded(self, state, tmp_path):
        """More than one Workflow tool call in the same session persists
        more than one workflows/*.json -- every discovered taskId is
        honoured, not just the first."""
        session_dir = tmp_path / "s" / "workflows"
        session_dir.mkdir(parents=True)
        (session_dir / f"{_WORKFLOW_RUN_ID}.json").write_text(
            json.dumps(_measured_workflow_state())
        )
        (session_dir / "wf_second-run.json").write_text(
            json.dumps(_measured_workflow_state(task_id="second-task-id", run_id="wf_second-run"))
        )
        _seed("s", _workflow_rows(11))
        transcript_path = str(tmp_path / "s.jsonl")
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                {"id": "second-task-id", "type": "workflow"},
                transcript_path=transcript_path,
            ),
        )
        assert r.code == 0
        assert not [ln for ln in r.lines if ln.startswith("UNDECLARED_TASK")]
        assert "WORKFLOW\tchecked=11 containers=2" in r.lines

    def test_the_harness_own_per_agent_id_for_a_workflow_start_is_not_undeclared(self, state):
        """Guards the OTHER direction: if the harness ever DOES expose a
        workflow-subagent's own agent_id (a shape this module has never
        measured), it must be recognised, not misread as undeclared."""
        _seed("s", _workflow_rows(3, reported=range(2)))  # w2 still running
        r = exp.expectations_reconcile(
            "s",
            _payload(
                {"id": _WORKFLOW_CONTAINER_TASK_ID, "type": "workflow"},
                {"agent_id": "w2"},
            ),
        )
        assert "UNDECLARED_TASK\tw2" not in r.lines
        assert f"UNDECLARED_TASK\t{_WORKFLOW_CONTAINER_TASK_ID}" in r.lines

    def test_a_genuinely_undeclared_non_workflow_task_still_flags_amid_workflow_starts(self, state):
        """Workflow STARTs in the ledger must not suppress a real undeclared
        task belonging to something else entirely."""
        _seed("s", _workflow_rows(11) + [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"agent_id": "a1"}, {"id": "ghost-real"}))
        assert r.code == 2
        assert "UNDECLARED_TASK\tghost-real" in r.lines
        assert not [ln for ln in r.lines if ln.startswith("STRANDED")]

    def test_no_workflow_line_when_there_are_no_workflow_starts(self, state):
        _seed("s", [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"agent_id": "a1"}))
        assert not [ln for ln in r.lines if ln.startswith("WORKFLOW")]

    def test_the_workflow_line_precedes_summary_so_the_last_line_invariant_holds(self, state):
        _seed("s", _workflow_rows(1) + [("START", "a1", "t")])
        r = exp.expectations_reconcile("s", _payload({"agent_id": "a1"}))
        assert r.lines[-1].startswith("SUMMARY\t")
        assert r.lines.index("WORKFLOW\tchecked=1 containers=0") < len(r.lines) - 1

    def test_the_harness_container_id_is_NOT_the_runid_an_earlier_round_wrongly_assumed(self, state):
        """Pins the round-2 correction itself, so a future edit cannot
        silently regress the fixture back to the wrong identity. Confirmed
        by direct read of session 2109cc46's transcript (.jsonl lines
        604/606: <task-id>w2bole9id</task-id>) and its persisted workflow
        state (workflows/wf_baae5a4e-bfd.json: "taskId": "w2bole9id" vs
        "runId": "wf_baae5a4e-bfd") -- the two are DIFFERENT identities, and
        only the taskId is what a background_tasks entry would carry.

        Round 4 makes this more than a naming nit: _workflow_container_task_ids
        (nexus.hooks.expectations) reads taskId out of workflows/*.json and
        matches it against background_tasks identities to exclude a real
        container task from UNDECLARED_TASK. Had this constant stayed
        wrong (the runId), that matching would silently never fire --
        every discovery test above would still pass its own assertions in
        isolation, but the mechanism would be checking a string the harness
        never sends."""
        assert not _WORKFLOW_CONTAINER_TASK_ID.startswith("wf_")
        assert _WORKFLOW_CONTAINER_TASK_ID == "w2bole9id"
        assert _WORKFLOW_RUN_ID.startswith("wf_")


class TestTheEmptyShapeIsNotNarrowerThanThePopulatedOne:
    """A no-ledger result must not be structurally narrower than a
    populated one, or a consumer reading it unconditionally breaks only
    when a session happens to have no ledger — the narrowest possible
    reproduction window. Raised by nexus-c3's reviewers on la5pr, where
    query()'s empty dict was missing two keys the populated one carried.

    LedgerReport is a frozen dataclass, so the FIELD set cannot diverge.
    What can is the lines: every populated path appends a SUMMARY, and a
    caller indexing lines[-1] for it would IndexError on the empty shape.
    """

    def test_every_reader_returns_the_same_type_on_a_missing_ledger(self, state):
        for result in (
            exp.expectations_undeclared("gone"),
            exp.expectations_census("gone"),
            exp.expectations_reconcile("gone", _payload()),
        ):
            assert isinstance(result, exp.LedgerReport)
            assert result.lines == []
            assert isinstance(result.code, int)
            assert isinstance(result.note, str)

    def test_a_populated_reader_always_ends_with_a_summary_or_blindspot(self, state):
        """So a consumer CAN rely on the last line when lines is non-empty,
        which is the guarantee the empty shape deliberately does not make.

        CENSUS IS EXCLUDED, and the exclusion is a correction rather than a
        carve-out. Real bash has ALWAYS appended the space-backed
        SPACE_*/VERIFY_* tail after its summary, so "ends with SUMMARY or
        BLINDSPOT" was never true of the census; it only looked true while
        the bead .9 port had not reproduced those lines. Asserting it here
        was asserting a property of the incomplete port, not of the ledger.
        The other two readers genuinely do end that way.
        """
        _seed("s", [("EXPECT", "t", "background"), ("START", "a1", "t")])
        for result in (exp.expectations_undeclared("s"),
                       exp.expectations_reconcile("s", _payload())):
            assert result.lines, "a populated ledger always produces lines"
            assert result.lines[-1].startswith(("SUMMARY\t", "BLINDSPOT\t"))

        census = exp.expectations_census("s")
        assert census.lines, "a populated ledger always produces lines"
        summary = [
            i for i, ln in enumerate(census.lines)
            if ln.startswith(("ROWS\t", "CLASSIFIED\t", "BLINDSPOT\t"))
        ]
        assert summary, f"census produced no summary line at all: {census.lines}"
        assert all(
            ln.startswith(("SPACE_", "VERIFY_"))
            for ln in census.lines[max(summary) + 1:]
        ), f"only the space-backed tail may follow the summary: {census.lines}"


# ── the bead .14 obligation, made mechanical ─────────────────────────────

class TestTheDualImplementationParamDiesWithTheLibrary:
    """When the bash library stops being what production runs, the "bash"
    param must go too.

    tests/hooks/test_expectations_reconcile.py and test_expectations_archive.py
    ran every assertion against BOTH implementations while both were live.
    That was deliberate (a straight retarget would have deleted the only
    coverage of the running bash path), but it left an obligation: the
    param has to die with the library it drove.

    Left behind, it becomes the vacuous-gate shape this project has a name
    for — a parametrised test that still reports two passes while one of
    them exercises nothing, or worse, a skip that reports green forever. So
    the obligation is enforced here rather than written in a bead nobody
    re-reads.

    The live-ness check is against ``hooks.json`` (whether any wired entry
    still runs ``expectations.sh``), not against the raw file's presence
    on disk (RDR-215 bead nexus-q02nx.21): the physical deletion and the
    hooks.json re-point land in the same change, but the re-point is what
    actually retires the bash path — a script that still exists on disk
    with nothing left pointing at it is not "the live production path" by
    any definition that matters here, and gating on file presence alone
    would have kept demanding a bash param these two files correctly
    dropped once their hook was re-declared.
    """

    _DUAL_FILES = (
        "tests/hooks/test_expectations_reconcile.py",
        "tests/hooks/test_expectations_archive.py",
    )

    def _repo(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _library_still_wired(self) -> bool:
        hooks = json.loads(
            (self._repo() / "conexus" / "hooks" / "hooks.json").read_text()
        )
        return _BASH_LIB.name in json.dumps(hooks)

    def test_the_param_list_tracks_whether_the_library_is_still_wired(self):
        library_wired = self._library_still_wired()
        for relative in self._DUAL_FILES:
            source = (self._repo() / relative).read_text()
            drives_bash = 'params=["bash", "python"]' in source
            if library_wired:
                assert drives_bash, (
                    f"{relative} must drive BOTH implementations while "
                    f"{_BASH_LIB.name} is still wired in hooks.json"
                )
            else:
                assert not drives_bash, (
                    f"{_BASH_LIB.name} is no longer wired in hooks.json "
                    f"(RDR-215 bead nexus-q02nx.21), so {relative} still "
                    'carrying params=["bash", "python"] now runs a '
                    "parametrisation whose bash half exercises nothing. Drop "
                    'the param and the fixture, and call the module directly.'
                )

    def test_neither_dual_file_skips_or_xfails_its_way_to_green(self):
        """A skip left where the bash param used to be would report green
        forever while proving nothing — the same failure the param removal
        exists to prevent, one step later."""
        for relative in self._DUAL_FILES:
            source = (self._repo() / relative).read_text()
            for banned in ("pytest.mark.skip", "pytest.mark.xfail", "pytest.skip("):
                assert banned not in source, (
                    f"{relative} uses {banned}: a dual-implementation suite that "
                    "skips is a suite that stops comparing"
                )
