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
import os
import subprocess
import sys
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
        monkeypatch.setenv("NX_EXPECT_LOCK_DISABLED", "1")
        path = Path(exp.expectations_file("s"))
        path.parent.mkdir(parents=True, exist_ok=True)
        won = [
            exp._claim_credit(str(path), "t", f"a{i}", credit=2, spent=0, owners=[])
            for i in range(12)
        ]
        assert won.count(True) == 2


class TestTheClaimIsStructurallyAtomic:
    """The actual guard on atomicity, because the load test below is NOT one.

    Measured while writing this file: replacing ``os.symlink`` with a
    check-then-act using a NON-exclusive create left the 16-process load
    test's winners assertion GREEN. The race is real but rare, so 16 racers
    did not interleave in the window; the test only went red incidentally,
    on a later assertion, because the slots had stopped being symlinks.

    That is precisely the failure the bash file's own history describes —
    "a test which only re-asserts the ceiling under load will keep passing
    while the defect relocates", which is how rounds 1 and 2 were each
    declared fixed. So the property is asserted STRUCTURALLY here: the claim
    must be a single create-or-fail syscall with no existence check before
    it. Same technique as ``test_hook_runtime_thin.py``, which pins an
    import property no functional test can see.
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
