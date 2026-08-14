# SPDX-License-Identifier: AGPL-3.0-or-later
"""check_client_release_precondition.py — the 9ssih deploy-order gate.

The mirror of test_check_engine_release_floor.py: this gate refuses an
engine tag whose required client commits are not in a RELEASED conexus
version. Inert gates are the recurring failure class (nexus-qc4p1), so
beyond the logic tests there is a WIRING test pinning that the
engine-release skill actually invokes the script.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import check_client_release_precondition as gate

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLogic:
    def test_no_preconditions_registered_is_ok(self, capsys):
        assert gate.check("engine-service-v0.0.0-nonexistent") == 0
        out = capsys.readouterr().out
        # nexus-f9z84: exit 0 stays correct for an empty registry, but the
        # message must be unmistakable that NOTHING was verified -- not
        # readable as "checked and found safe".
        assert "VACUOUS" in out
        assert "0 preconditions" in out
        assert "verified NOTHING" in out

    def test_registered_tag_blocks_when_commit_unreleased(self, monkeypatch, capsys):
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")
        monkeypatch.setattr(gate, "is_ancestor", lambda commit, tag: False)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        assert gate.check("engine-service-vTEST") == 1
        assert "BLOCKED" in capsys.readouterr().err

    def test_registered_tag_passes_when_commit_released(self, monkeypatch, capsys):
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")
        monkeypatch.setattr(gate, "is_ancestor", lambda commit, tag: True)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        assert gate.check("engine-service-vTEST") == 0

    def test_unverifiable_git_state_is_exit_2_not_pass(self, monkeypatch, capsys):
        """'Could not verify' is never 'must be fine'."""
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")

        def boom(commit, tag):
            raise RuntimeError("git exploded")

        monkeypatch.setattr(gate, "is_ancestor", boom)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        assert gate.check("engine-service-vTEST") == 2

    @pytest.fixture()
    def hermetic_repo(self, tmp_path, monkeypatch):
        """A scratch git repo with both tag shapes — CI checkouts are
        SHALLOW AND TAGLESS (found on the 7.0.0 release PR: the two
        against-the-real-repo tests 128'd), so these tests must carry
        their own git state."""
        repo = tmp_path / "repo"
        repo.mkdir()
        env = ["-c", "user.email=t@t.invalid", "-c", "user.name=t"]
        def g(*args):
            subprocess.run(["git", "-C", str(repo), *env, *args],
                           capture_output=True, text=True, check=True)
        g("init", "-q")
        g("commit", "--allow-empty", "-q", "-m", "one")
        g("commit", "--allow-empty", "-q", "-m", "two")
        g("tag", "v1.2.3")
        g("tag", "engine-service-v9.9.9")
        # The gate's git helpers run in CWD by design (the script runs from
        # the repo root) — chdir scopes every git call to the scratch repo.
        monkeypatch.chdir(repo)
        return repo

    def test_latest_release_tag_ignores_engine_tags(self, hermetic_repo):
        """The v[0-9]* glob must never match engine-service-v* tags."""
        tag = gate.latest_release_tag()
        assert tag == "v1.2.3"
        assert re.fullmatch(r"v\d+\.\d+\.\d+", tag), tag
        assert not tag.startswith("engine-service")

    def test_default_engine_tag_derives_from_the_pinned_floor(self):
        """The CLI default is DERIVED from REQUIRED_ENGINE_VERSION, never a
        hand-typed literal (the v0.1.61 literal sat stale after the floor
        moved — same drift class as nexus-b6qlf)."""
        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        expected = "engine-service-v" + ".".join(
            str(n) for n in REQUIRED_ENGINE_VERSION
        )
        assert gate._pinned_engine_tag() == expected

    def test_is_ancestor_real_git_smoke(self, hermetic_repo):
        """A root-ward commit is an ancestor of HEAD (hermetic repo — the
        real CI checkout is single-commit, HEAD~1 does not resolve)."""
        proc = subprocess.run(
            ["git", "-C", str(hermetic_repo), "rev-parse", "HEAD~1"],
            capture_output=True, text=True,
        )
        assert gate.is_ancestor(proc.stdout.strip(), "HEAD")


class TestStalePreconditionRowsDoNotOutliveTheFloor:
    """The script's own contract: 'Delete rows once the floor moves past the
    engine version that carried the requirement.' Mechanized, because the
    prose version was violated the first time it mattered (the v0.1.61 row
    survived the 2026-08-02 floor bump, pinned in place by its own test) —
    the same stale-interim-exception shape
    TestMvvAllowlistDoesNotOutliveItsTrigger and
    TestSmokeLegDiscriminatorDoesNotOutliveItsPower exist to kill.

    nexus-f9z84: the ORIGINAL ``test_every_row_names_an_engine_ahead_of_the_floor``
    looped ``gate.ENGINE_CLIENT_PRECONDITIONS`` directly — an almost-always-empty
    dict, so the loop body ran zero times and the test passed unconditionally
    regardless of whether the comparison logic worked at all ("the mechanized
    version is now in the state the prose version was in"). It is kept below
    (still a real, valuable regression pin on the LIVE table), but is now
    joined by tests against :func:`gate.stale_precondition_rows` with an
    INJECTED table — proving the detector can actually produce a non-empty,
    correct result, not just an unconditional pass.
    """

    def test_every_row_names_an_engine_ahead_of_the_floor(self):
        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        stale = gate.stale_precondition_rows()
        assert stale == [], (
            f"ENGINE_CLIENT_PRECONDITIONS row(s) {stale!r} are at or behind "
            f"the pinned floor {REQUIRED_ENGINE_VERSION} — the deploy they "
            "gated has already happened and every subsequent release "
            "supersets their required commits. Delete the row(s) (record "
            "in the docstring's pruned-rows list) per the module contract."
        )

    def test_detector_actually_fires_on_a_planted_stale_row(self):
        """The non-vacuity proof: an INJECTED table with a row at/behind an
        injected floor must come back as stale — demonstrating the
        comparison logic runs and works, independent of whatever the real
        (usually empty) table currently holds."""
        floor = (0, 1, 71)
        planted = {
            "engine-service-v0.1.70": {"deadbeef": "at the floor minus one"},
            "engine-service-v0.1.71": {"beadfeed": "exactly at the floor"},
            "engine-service-v0.1.99": {"c0ffee00": "still ahead of the floor"},
            "next": {"f00dface": "the always-ahead sentinel"},
        }
        stale = gate.stale_precondition_rows(table=planted, floor=floor)
        assert set(stale) == {"engine-service-v0.1.70", "engine-service-v0.1.71"}

    def test_detector_reports_nothing_stale_when_all_rows_are_ahead(self):
        floor = (0, 1, 71)
        planted = {"engine-service-v0.1.72": {"deadbeef": "ahead"}}
        assert gate.stale_precondition_rows(table=planted, floor=floor) == []

    def test_detector_defaults_to_the_live_table_and_pinned_floor(self):
        """Wiring check: the no-argument call path (what the pytest test
        above and any future caller actually uses) must reach the SAME
        module-level table and floor as explicit args would — a signature
        that silently ignored its defaults would make the two tests above
        prove nothing about the code path that runs in CI."""
        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        assert gate.stale_precondition_rows() == gate.stale_precondition_rows(
            table=gate.ENGINE_CLIENT_PRECONDITIONS, floor=REQUIRED_ENGINE_VERSION,
        )


class TestWireContractLedger:
    """Gap 1 fix (protocol-audit [22511], 2026-08-14): the mechanized
    both-halves wire-contract ledger is consulted from the UNPAIRED deploy
    path too, not only from check_engine_release_floor.py's --paired-deploy
    branch. Mirrors tests/scripts/test_check_engine_release_floor.py's
    ledger test shape (same ledger format, same DEFAULT_LEDGER_PATH patch
    idiom) but exercised through check_client_release_precondition's own
    entry points.
    """

    _FAKE_ENTRY = (
        "- `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef` -- bead nexus-fake -- "
        "engine tag `engine-service-v9.9.9` -- test fixture\n"
    )

    @staticmethod
    def _write_ledger(tmp_path, entry: str | None = None):
        ledger = tmp_path / "wire-contract-pending.md"
        body = entry or "(none)\n"
        ledger.write_text(f"## Unshipped\n\n{body}\n## Shipped\n")
        return ledger

    def test_refuses_on_synthetic_unshipped_entry(self, tmp_path, capsys):
        ledger = self._write_ledger(tmp_path, self._FAKE_ENTRY)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
            rc = gate.check("engine-service-v0.0.0-nonexistent")
        assert rc == 1
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "nexus-fake" in err
        assert "deadbeef" in err

    def test_ack_path_passes(self, tmp_path):
        ledger = self._write_ledger(tmp_path, self._FAKE_ENTRY)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
            rc = gate.check(
                "engine-service-v0.0.0-nonexistent", ack_client_lag=["nexus-fake"]
            )
        assert rc == 0

    def test_wrong_ack_still_blocks(self, tmp_path):
        ledger = self._write_ledger(tmp_path, self._FAKE_ENTRY)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
            rc = gate.check(
                "engine-service-v0.0.0-nonexistent", ack_client_lag=["nexus-other"]
            )
        assert rc == 1

    def test_both_sources_empty_prints_distinct_combined_message(
        self, tmp_path, capsys
    ):
        """The core Gap 1 non-vacuity requirement: when BOTH
        ENGINE_CLIENT_PRECONDITIONS and the ledger are empty, the message
        must name BOTH sources -- not just repeat the old hand-table-only
        VACUOUS wording, which would now be a false claim (the ledger WAS
        checked)."""
        ledger = self._write_ledger(tmp_path)  # empty ## Unshipped
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
            rc = gate.check("engine-service-v0.0.0-nonexistent")
        assert rc == 0
        out = capsys.readouterr().out
        assert "VACUOUS" in out
        assert "0 preconditions registered" in out
        assert "0 entries" in out
        assert str(ledger) in out
        assert "verified NOTHING from EITHER source" in out

    def test_hand_table_non_empty_ledger_empty_no_combined_vacuous_message(
        self, monkeypatch, tmp_path, capsys
    ):
        """When the hand table DID verify something, the combined
        both-empty VACUOUS banner must not fire -- it would misrepresent a
        real check as having verified nothing."""
        ledger = self._write_ledger(tmp_path)  # empty ## Unshipped
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")
        monkeypatch.setattr(gate, "is_ancestor", lambda commit, tag: True)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        monkeypatch.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
        rc = gate.check("engine-service-vTEST")
        assert rc == 0
        out = capsys.readouterr().out
        assert "BOTH sources are empty" not in out
        assert "verified NOTHING from EITHER source" not in out

    def test_check_wire_contract_ledger_reports_vacuous_flag(self, tmp_path):
        ledger = self._write_ledger(tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
            rc, vacuous = gate.check_wire_contract_ledger()
        assert (rc, vacuous) == (0, True)

    def test_check_wire_contract_ledger_non_vacuous_when_populated_and_clean(
        self, tmp_path
    ):
        ledger = self._write_ledger(tmp_path, self._FAKE_ENTRY)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
            rc, vacuous = gate.check_wire_contract_ledger(["nexus-fake"])
        assert (rc, vacuous) == (0, False)


class TestWireContractLedgerNonVacuitySelfTest:
    """Proves the ``import check_wire_contract_pairing as _wire_ledger``
    wiring actually reaches the REAL checked-in ledger, not a stub or a
    dead path -- a test that only ever monkeypatches DEFAULT_LEDGER_PATH
    could pass even if production code imported the wrong module."""

    def test_default_ledger_path_is_the_real_checked_in_file(self):
        assert gate._wire_ledger.DEFAULT_LEDGER_PATH.is_file()
        assert gate._wire_ledger.DEFAULT_LEDGER_PATH.name == "wire-contract-pending.md"

    def test_unpatched_ledger_check_parses_real_file_content(self):
        """Without any monkeypatch, check_wire_contract_ledger() must reach
        the exact same Ledger the sibling module's own parser produces for
        the real file -- proving the import is live, not vacuous."""
        direct = gate._wire_ledger.parse_ledger(gate._wire_ledger.DEFAULT_LEDGER_PATH)
        rc, vacuous = gate.check_wire_contract_ledger()
        assert vacuous == (not direct.unshipped)
        # rc can only be 1 (blocked) if direct.unshipped is non-empty and
        # unacknowledged -- exercise the real relationship, not a mock.
        if not direct.unshipped:
            assert rc == 0


class TestMainAckClientLagFlag:
    def test_main_accepts_and_threads_ack_client_lag(self):
        with patch.object(gate, "check", return_value=0) as mock_check:
            rc = gate.main(
                [
                    "--engine-tag", "engine-service-vTEST",
                    "--ack-client-lag", "nexus-fake",
                    "--ack-client-lag", "nexus-other",
                ]
            )
        assert rc == 0
        mock_check.assert_called_once_with(
            "engine-service-vTEST", ["nexus-fake", "nexus-other"]
        )

    def test_main_defaults_ack_client_lag_to_none(self):
        with patch.object(gate, "check", return_value=0) as mock_check:
            gate.main(["--engine-tag", "engine-service-vTEST"])
        mock_check.assert_called_once_with("engine-service-vTEST", None)


class TestWiring:
    """An unwired gate is a prose gate with extra steps (nexus-qc4p1 class)."""

    def test_engine_release_skill_invokes_the_gate(self):
        skill = (REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text()
        assert "check_client_release_precondition.py" in skill, (
            "the engine-release skill must surface the client-precondition "
            "check — without the invocation step this script is exactly as "
            "skippable as the prose gate it replaced"
        )
        # The check surfaces early (pre-tag) so a red is KNOWN while planning
        # the paired release — but it gates the DEPLOY, never the tag cut
        # (Hal directive 2026-08-02: the pre-tag-blocking wiring forced
        # conexus 7.1.0 to ship pinned to a pre-fence engine).
        gate_pos = skill.index("check_client_release_precondition.py")
        push_pos = skill.index("git push origin engine-service-v")
        assert gate_pos < push_pos, (
            "the check must surface before the tag-push step (advisory at "
            "tag time, blocking at deploy time)"
        )
        assert "never the tag cut" in skill, (
            "the skill must state the deploy-not-tag scoping explicitly — "
            "the 7.1.0/v0.1.62 inversion came from this wording"
        )
