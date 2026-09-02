# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``scripts/enumerate_release_cells.py`` (RDR-201 P2.1, nexus-j9z30.11).

TDD per the bead: ``TestEnumerateChainSynthetic`` below is written FIRST, against
a small synthetic (toy) guard chain with a known reachable-cell count -- before
the enumerator is pointed at the real release-gate scripts. It proves three
properties the real-script chains below then rely on:

1. **Exhaustive**: ``enumerate_chain`` walks the FULL cartesian product of a
   chain's declared dimension domains, not a sample.
2. **Non-vacuous**: it actually finds more than zero reachable cells, and
   every leaf the chain's guard steps can name is either found reachable or
   explicitly reported as unreachable -- never silently dropped.
3. **Driven, not guessed**: a reachable cell's representative input tuple,
   fed back into the REAL decision function, reproduces that cell's exit
   code -- the same contract the real-script driver tests rely on below.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml``, so ``enumerate_release_cells`` and the two gate scripts
import directly with no ``sys.path`` hack.
"""
from __future__ import annotations

import json

import pytest

import enumerate_release_cells as erc


# ---------------------------------------------------------------------------
# Synthetic guard chain fixtures (a toy decision function, NOT a real script)
# ---------------------------------------------------------------------------

def _toy_decision(temp: str, door: str) -> tuple[int, str]:
    """A tiny 2-guard short-circuit decision function, structurally identical
    in shape to a real release-gate guard chain (e.g. ``check_pin_currency``
    or the ``check_source_ancestry`` existence-then-diff chain): an ordered
    sequence of checks, the first failing one wins, else a fixed success."""
    if temp == "unavailable":
        return 2, "temp_unavailable"
    if temp == "hot":
        return 1, "too_hot"
    if door == "open":
        return 1, "door_open"
    return 0, "all_clear"


def _toy_chain_clean() -> erc.GuardChain:
    """Model of ``_toy_decision`` with no dead branches: 3 x 2 = 6 combinations,
    4 distinct reachable leaves (short-circuit collapses the 2 door values for
    both the ``unavailable`` and ``hot`` temp branches)."""
    temp = erc.Dimension("temp", ("unavailable", "hot", "ok"))
    door = erc.Dimension("door", ("open", "closed"))
    steps = (
        erc.GuardStep("temp_gate", "temp", {
            "unavailable": erc.Leaf(2, "temp_unavailable"),
            "hot": erc.Leaf(1, "too_hot"),
            "ok": erc.CONTINUE,
        }),
        erc.GuardStep("door_gate", "door", {
            "open": erc.Leaf(1, "door_open"),
            "closed": erc.CONTINUE,
        }),
    )
    return erc.GuardChain(
        function="toy_decision",
        steps=steps,
        success=erc.Leaf(0, "all_clear"),
        dims={"temp": temp, "door": door},
    )


def _toy_chain_with_dead_branch() -> erc.GuardChain:
    """Same chain, PLUS a trailing guard whose single domain value ALWAYS
    returns a leaf -- so the chain's own ``success`` leaf ("all_clear") can
    never be reached (every combination that would have fallen through to it
    now terminates one step earlier, at the dead guard). Exercises the
    unreachable-declared-leaf detector: ``success`` is declared (it is the
    chain's own ``success`` field) but no input tuple ever selects it."""
    base = _toy_chain_clean()
    always_true = erc.Dimension("always_true", ("true",))
    dead_step = erc.GuardStep("dead_gate", "always_true", {
        "true": erc.Leaf(9, "never_happens"),
    })
    return erc.GuardChain(
        function="toy_decision_with_dead_branch",
        steps=base.steps + (dead_step,),
        success=base.success,
        dims={**base.dims, "always_true": always_true},
    )


class TestEnumerateChainSynthetic:
    """TDD anchor: written before any real-script chain exists."""

    def test_exhaustive_over_full_cartesian_product(self) -> None:
        chain = _toy_chain_clean()
        result = erc.enumerate_chain(chain)
        assert result.total_combinations == 3 * 2

    def test_non_vacuous_finds_every_expected_leaf(self) -> None:
        chain = _toy_chain_clean()
        result = erc.enumerate_chain(chain)
        found = {(c.exit_code, c.message_key) for c in result.reachable}
        assert found == {
            (2, "temp_unavailable"),
            (1, "too_hot"),
            (1, "door_open"),
            (0, "all_clear"),
        }
        assert len(result.unreachable_declared_leaves) == 0

    def test_reachable_cells_are_deduplicated_by_leaf_not_by_combo(self) -> None:
        """6 combinations collapse to 4 distinct leaves via short-circuit:
        the enumerator reports CELLS (distinct leaves), not raw combos, and
        each cell carries exactly one representative input tuple."""
        chain = _toy_chain_clean()
        result = erc.enumerate_chain(chain)
        assert result.total_combinations == 6
        assert len(result.reachable) == 4
        for cell in result.reachable:
            assert set(cell.inputs) == {"temp", "door"}

    def test_representative_inputs_actually_reproduce_the_leaf(self) -> None:
        """Cross-check: driving the REAL toy function with each reachable
        cell's representative input tuple reproduces that cell's exit code
        -- the same "drive, don't guess" contract the real-script chains
        below rely on via monkeypatched sensors."""
        chain = _toy_chain_clean()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = _toy_decision(cell.inputs["temp"], cell.inputs["door"])
            assert erc.verify_cell(cell, observed)

    def test_shadowed_success_leaf_is_recorded_as_unreachable_not_dropped(self) -> None:
        chain = _toy_chain_with_dead_branch()
        result = erc.enumerate_chain(chain)
        assert result.total_combinations == 3 * 2 * 1
        found = {(c.exit_code, c.message_key) for c in result.reachable}
        assert (0, "all_clear") not in found
        assert (9, "never_happens") in found
        assert result.unreachable_declared_leaves == ({"exit_code": 0, "message_key": "all_clear"},)

    def test_verify_cell_via_monkeypatched_sensor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact idiom the real-script drivers use below: a "sensor" is a
        monkeypatchable module attribute the decision function reads, not a
        parameter -- prove ``verify_cell`` works against that shape too."""

        class _SensorModule:
            temp_sensor = "ok"
            door_sensor = "closed"

            @classmethod
            def read(cls) -> tuple[int, str]:
                return _toy_decision(cls.temp_sensor, cls.door_sensor)

        chain = _toy_chain_clean()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            monkeypatch.setattr(_SensorModule, "temp_sensor", cell.inputs["temp"])
            monkeypatch.setattr(_SensorModule, "door_sensor", cell.inputs["door"])
            assert erc.verify_cell(cell, _SensorModule.read())


# ---------------------------------------------------------------------------
# Real-script chains: linear guard-chain functions (fully cross-produced)
# ---------------------------------------------------------------------------

class TestFloorLinearChains:
    """The floor script's pure guard-chain functions: single or double
    sensor, no delegation to another decision function. Each chain is
    enumerated exhaustively (full cartesian product) and every reachable
    cell is DRIVEN against the real function via monkeypatched sensors."""

    def test_pin_currency_chain_is_non_vacuous_and_exhaustive(self) -> None:
        chain = erc.pin_currency_chain()
        result = erc.enumerate_chain(chain)
        assert result.total_combinations == len(chain.dims["newest"].domain)
        assert len(result.reachable) == len(chain.dims["newest"].domain)
        assert result.unreachable_declared_leaves == ()

    def test_pin_currency_chain_cells_are_driven(self) -> None:
        chain = erc.pin_currency_chain()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = erc.drive_pin_currency(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_source_ancestry_chain_is_non_vacuous_and_exhaustive(self) -> None:
        chain = erc.source_ancestry_chain()
        result = erc.enumerate_chain(chain)
        assert result.total_combinations == (
            len(chain.dims["tag_exists"].domain) * len(chain.dims["diff_result"].domain)
        )
        assert len(result.reachable) == 6
        assert result.unreachable_declared_leaves == ()

    def test_source_ancestry_chain_cells_are_driven(self) -> None:
        chain = erc.source_ancestry_chain()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = erc.drive_source_ancestry(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_client_lag_ledger_chain_is_non_vacuous_and_exhaustive(self) -> None:
        chain = erc.client_lag_ledger_chain()
        result = erc.enumerate_chain(chain)
        assert len(result.reachable) == 4
        assert result.unreachable_declared_leaves == ()

    def test_client_lag_ledger_chain_cells_are_driven(self) -> None:
        chain = erc.client_lag_ledger_chain()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = erc.drive_client_lag_ledger(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_paired_preconditions_chain_is_non_vacuous_and_exhaustive(self) -> None:
        chain = erc.paired_preconditions_chain()
        result = erc.enumerate_chain(chain)
        expected_total = 1
        for dim in chain.dims.values():
            expected_total *= len(dim.domain)
        assert result.total_combinations == expected_total
        # 12 named fail leaves + 1 armed/pass leaf, per the guard order in
        # check_paired_preconditions (nexus-k1c08).
        assert len(result.reachable) == 13
        assert result.unreachable_declared_leaves == ()

    def test_paired_preconditions_chain_cells_are_driven(self) -> None:
        chain = erc.paired_preconditions_chain()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = erc.drive_paired_preconditions(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_tracker_outcome_chain_is_non_vacuous_and_exhaustive(self) -> None:
        chain = erc.tracker_outcome_chain()
        result = erc.enumerate_chain(chain)
        assert len(result.reachable) == 8
        assert result.unreachable_declared_leaves == ()

    def test_tracker_outcome_chain_cells_are_driven(self) -> None:
        chain = erc.tracker_outcome_chain()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = erc.drive_tracker_outcome(cell)
            assert erc.verify_cell(cell, observed), cell


class TestPreconditionLinearChains:
    def test_wire_contract_ledger_chain_is_non_vacuous_and_exhaustive(self) -> None:
        chain = erc.wire_contract_ledger_chain()
        result = erc.enumerate_chain(chain)
        assert len(result.reachable) == 4
        assert result.unreachable_declared_leaves == ()

    def test_wire_contract_ledger_chain_cells_are_driven(self) -> None:
        chain = erc.wire_contract_ledger_chain()
        result = erc.enumerate_chain(chain)
        for cell in result.reachable:
            observed = erc.drive_wire_contract_ledger(cell)
            assert erc.verify_cell(cell, observed), cell


# ---------------------------------------------------------------------------
# Real-script cells: tree-shaped orchestrator functions (hand-enumerated,
# each still driven against the real function -- see module docstring in
# enumerate_release_cells.py for why these are not cartesian-producted).
# ---------------------------------------------------------------------------

class TestFloorOrchestratorCells:
    def test_check_floor_bare_mode_cells_are_non_vacuous_and_driven(self) -> None:
        result = erc.check_floor_bare_cells()
        assert len(result.reachable) == 5
        for cell in result.reachable:
            observed = erc.drive_check_floor_bare(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_check_floor_paired_explicit_cells_are_non_vacuous_and_driven(self) -> None:
        result = erc.check_floor_paired_explicit_cells()
        assert len(result.reachable) == 7
        for cell in result.reachable:
            observed = erc.drive_check_floor_paired_explicit(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_check_floor_auto_paired_cells_are_non_vacuous_and_driven(self) -> None:
        result = erc.check_floor_auto_paired_cells()
        assert len(result.reachable) == 9
        for cell in result.reachable:
            observed = erc.drive_check_floor_auto_paired(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_main_dispatch_cells_are_non_vacuous_and_driven(self) -> None:
        """code-review CRITICAL, 2026-09-01: main()'s FULL post-argparse tail --
        mode dispatch (bare/--paired-deploy/--paired-deploy-auto/--ledger-only),
        check_floor/ancestry propagation, the tracker leg, and the non_bare
        early return -- not just the bare-mode tracker leg the prior
        tracker_leg_dispatch_cells() covered."""
        result = erc.main_dispatch_cells()
        assert len(result.reachable) == 15
        for cell in result.reachable:
            observed = erc.drive_main_dispatch(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_main_dispatch_cells_cover_all_four_modes(self) -> None:
        result = erc.main_dispatch_cells()
        modes = {cell.inputs["mode"] for cell in result.reachable}
        assert modes == {"bare", "paired_explicit", "paired_auto", "ledger_only"}

    def test_main_dispatch_ledger_only_covers_all_four_ledger_outcomes(self) -> None:
        result = erc.main_dispatch_cells()
        ledger_only_cells = [c for c in result.reachable if c.inputs["mode"] == "ledger_only"]
        assert {c.inputs["ledger"] for c in ledger_only_cells} == {
            "empty", "blocking", "additive", "acked_only",
        }


class TestPreconditionOrchestratorCells:
    def test_check_composite_cells_are_non_vacuous_and_driven(self) -> None:
        result = erc.check_composite_cells()
        assert len(result.reachable) == 11
        for cell in result.reachable:
            observed = erc.drive_check_composite(cell)
            assert erc.verify_cell(cell, observed), cell

    def test_precond_main_dispatch_cells_are_non_vacuous_and_driven(self) -> None:
        """critique SIGNIFICANT 1: check_client_release_precondition.main()'s
        own argparse wiring (--engine-tag, --ack-client-lag threading) and
        exit-code pass-through, driven through main() itself, not just
        check()."""
        result = erc.precond_main_dispatch_cells()
        assert len(result.reachable) == 2
        for cell in result.reachable:
            observed = erc.drive_precond_main_dispatch(cell)
            assert erc.verify_cell(cell, observed), cell


class TestCLIValidationRefusals:
    """critique CRITICAL: the 6 parser.error() mutual-exclusion refusals in
    check_engine_release_floor.py's main() are CLI-shape refusals, excluded
    from the decision-cell model on that ground -- but the exclusion must be
    NON-VACUOUS: a scan that finds nothing would silently pass forever."""

    def test_scan_finds_at_least_one_site(self) -> None:
        found = erc.scan_parser_error_call_sites()
        assert len(found) > 0

    def test_scan_matches_the_declared_exclusion_list_exactly(self) -> None:
        found = erc.scan_parser_error_call_sites()
        found_set = {(s["script"], s["message"]) for s in found}
        declared_set = {(s["script"], s["message"]) for s in erc.CLI_VALIDATION_REFUSAL_SITES}
        assert found_set == declared_set

    def test_precondition_script_has_zero_parser_error_sites(self) -> None:
        found = erc.scan_parser_error_call_sites()
        precond_sites = [s for s in found if s["script"] == "check_client_release_precondition.py"]
        assert precond_sites == []

    def test_fixture_declares_cli_validation_refusals_as_a_third_exclusion(self) -> None:
        fixture = erc.build_fixture()
        excluded = fixture["header"]["excluded_dimensions"]
        assert len(excluded) == 3
        row = next(r for r in excluded if r["name"] == "cli_argument_validation_refusals")
        assert len(row["sites"]) == len(erc.CLI_VALIDATION_REFUSAL_SITES) == 6
        assert row["fixed_representative_value"]


class TestRdrLeafTraceability:
    """critique SIGNIFICANT 2: each of the RDR's 7 previously-untested leaves
    (T2 nexus_rdr/201-research-4) must resolve to at least one cell id
    actually present in the fixture -- pinned, not merely claimed in prose."""

    def test_traceability_covers_exactly_seven_leaves(self) -> None:
        assert len(erc.RDR_UNCOVERED_LEAF_TRACEABILITY) == 7

    def test_every_traced_cell_id_exists_in_the_fixture(self) -> None:
        fixture = erc.build_fixture()
        cell_ids = {c["cell_id"] for c in fixture["cells"]}
        for entry in erc.RDR_UNCOVERED_LEAF_TRACEABILITY:
            assert entry["cell_ids"], entry
            for cid in entry["cell_ids"]:
                assert cid in cell_ids, (entry["rdr_leaf"], cid, sorted(cell_ids))

    def test_fixture_includes_the_traceability_table(self) -> None:
        fixture = erc.build_fixture()
        assert len(fixture["rdr_leaf_traceability"]) == 7


# ---------------------------------------------------------------------------
# EVENT dimension (pre-tag / tag-push / deploy / post-deploy-verify)
# ---------------------------------------------------------------------------

class TestEventDimension:
    def test_event_domain_has_all_four_named_events(self) -> None:
        assert erc.EVENT_DIMENSION.domain == (
            "pre-tag", "tag-push", "deploy", "post-deploy-verify",
        )

    def test_event_mode_matrix_covers_every_declared_mode_at_least_once(self) -> None:
        matrix = erc.event_mode_matrix()
        modes_with_an_event = {
            row["mode"] for row in matrix if row["reachable"]
        }
        assert modes_with_an_event == set(erc.FLOOR_MODES) | set(erc.PRECOND_MODES)

    def test_deploy_event_is_unreachable_for_every_mode(self) -> None:
        """The scripts never run AT deploy time -- the deploy relay is on
        conexus's side (AGENTS.md § Engine-service release). Every (mode,
        "deploy") combination is a recorded, cited unreachable cell, not a
        silently dropped one."""
        matrix = erc.event_mode_matrix()
        deploy_rows = [row for row in matrix if row["event"] == "deploy"]
        assert deploy_rows  # non-vacuous: the combination was considered
        assert all(not row["reachable"] for row in deploy_rows)
        assert all(row["citation"] for row in deploy_rows)

    def test_bare_mode_is_reachable_at_two_distinct_events(self) -> None:
        """Finding 2's incident correction: the same bare-mode guard is
        legitimately invoked at BOTH pre-tag and post-deploy-verify -- the
        7.1.0/v0.1.62 fix was rebinding which EVENT a guard answered for,
        not a guard defect. A table for this choreography needs the event
        column to distinguish them."""
        matrix = erc.event_mode_matrix()
        bare_events = {
            row["event"] for row in matrix
            if row["mode"] == "bare" and row["reachable"]
        }
        assert bare_events == {"pre-tag", "post-deploy-verify"}


# ---------------------------------------------------------------------------
# Fixture assembly: the JSON file this bead delivers
# ---------------------------------------------------------------------------

class TestBuildFixture:
    def test_build_fixture_is_non_vacuous_and_json_serializable(self) -> None:
        fixture = erc.build_fixture()
        assert fixture["header"]["reachable_cell_count"] > 0
        assert fixture["header"]["reachable_cell_count"] == len(fixture["cells"])
        # round-trips through json without error
        json.loads(json.dumps(fixture))

    def test_fixture_header_names_all_three_excluded_dimensions(self) -> None:
        fixture = erc.build_fixture()
        excluded = fixture["header"]["excluded_dimensions"]
        assert len(excluded) == 3
        names = {row["name"] for row in excluded}
        assert names == {
            "gate_report_directory_contents",
            "no_record_deploy_reason",
            "cli_argument_validation_refusals",
        }
        for row in excluded:
            assert row["fixed_representative_value"]

    def test_fixture_header_names_every_dimension_domain(self) -> None:
        fixture = erc.build_fixture()
        dims = fixture["header"]["dimensions"]
        assert len(dims) > 0
        for row in dims:
            assert row["domain"]

    def test_every_cell_has_function_inputs_exit_code_and_message_key(self) -> None:
        fixture = erc.build_fixture()
        seen_ids = set()
        for cell in fixture["cells"]:
            assert cell["cell_id"]
            assert cell["cell_id"] not in seen_ids, "cell_id must be unique"
            seen_ids.add(cell["cell_id"])
            assert cell["function"]
            assert isinstance(cell["inputs"], dict)
            assert isinstance(cell["exit_code"], int)
            assert cell["message_key"]

    def test_fixture_includes_the_event_mode_matrix(self) -> None:
        fixture = erc.build_fixture()
        assert len(fixture["event_mode_matrix"]) > 0
        assert any(row["reachable"] for row in fixture["event_mode_matrix"])
        assert any(not row["reachable"] for row in fixture["event_mode_matrix"])

    def test_fixture_written_to_disk_matches_build_fixture(self, tmp_path) -> None:
        out = tmp_path / "release_cells.json"
        erc.write_fixture(out)
        on_disk = json.loads(out.read_text(encoding="utf-8"))
        assert on_disk["header"]["reachable_cell_count"] > 0
        assert len(on_disk["cells"]) == on_disk["header"]["reachable_cell_count"]
