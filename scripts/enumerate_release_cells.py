#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Mechanically enumerate the reachable release-gate decision cells (RDR-201
P2.1, nexus-j9z30.11).

FIRST bead of RDR-201 Phase 2. Nothing in Phase 2 is rewired before this and
P2.2 (the side-by-side parity harness) exist -- this script is the stated
mitigation for "the release rewrite touches the scripts that gate releases".

Enumerates the reachable decision cells of

  scripts/check_engine_release_floor.py
  scripts/check_client_release_precondition.py

over the state space T2 nexus_rdr/201-research-4 measured: five genuine
enums, ten open dimensions that each reduce to a finite comparison outcome
(below / equal / above / unreachable and the like), plus the EVENT dimension
Finding 2's incident correction says the state space was missing entirely
(pre-tag / tag-push / deploy / post-deploy-verify -- the 7.1.0/v0.1.62
inversion was an UN-ENCODED EVENT, not a guard defect: commit 79fff05a9
changed only a remedy string and prose, no decision logic).

THREE things stay OUTSIDE this model entirely (recorded in the fixture
header, never silently dropped) -- two non-reducing dimensions fixed to one
representative value, plus one CLI-shape refusal class excluded on a
different ground (critique CRITICAL, T2 nexus/critique-nexus-j9z30-11-2026-09-01):

- the gate-report DIRECTORY CONTENTS deploy_tracker.py's discovery logic
  reads (arbitrary file listings / timestamps / JSON bodies) -- this module
  drives :func:`record_deploy_from_gate_report_leg`'s 8-way CLASSIFIED
  outcome (:func:`tracker_outcome_chain`) by monkeypatching
  ``nexus.deploy_tracker.record_deploy_from_gate_report`` directly to
  raise/return each named outcome, never by writing real report files and
  exercising deploy_tracker's own (separately tested) discovery/selection
  logic over them.
- the free-text ``--no-record-deploy`` reason: modeled only as
  "present" vs "absent" (argparse already enforces non-blank), never as its
  literal content.
- argparse's own ``parser.error()`` mutual-exclusion refusals (6 sites in
  ``check_engine_release_floor.py``'s ``main()``, exit code 2; zero in the
  precondition script's) are CLI-SHAPE refusals, not release decisions --
  Phase 2's table replaces the gated scripts' decision logic, not argparse's
  flag-combination validation. Declared as :data:`CLI_VALIDATION_REFUSAL_SITES`
  and made non-vacuous by :func:`scan_parser_error_call_sites`, an AST scan
  asserted (test suite) to find EXACTLY that set -- a new or reworded
  validation branch reds the test rather than silently vanishing.

Two modeling shapes, by function structure
-------------------------------------------

**Linear guard-chain functions** (single ordered AND-chain of independent
sensors: ``check_pin_currency``, ``check_source_ancestry``,
``check_client_lag_ledger`` / ``check_wire_contract_ledger``,
``check_paired_preconditions``, the tracker-outcome classification) are
modeled with :class:`GuardChain` and enumerated EXHAUSTIVELY: the full
cartesian product of their declared dimension domains is walked
(:func:`enumerate_chain`), each combination resolved via short-circuit
semantics to a leaf, and the DISTINCT leaves reported as reachable cells
(many combinations collapse onto the same leaf -- that collapse is exactly
what "cell" means here, distinct from "combination"). A leaf named in the
chain's own guard steps that no combination ever selects is reported in
``unreachable_declared_leaves``, never dropped silently.

**Orchestrator functions** (``check_floor``, ``_check_floor_auto_paired``,
BOTH scripts' full ``main()`` post-argparse-validation dispatch (mode
selection, propagation, the tracker leg, and the precondition script's own
``--engine-tag``/``--ack-client-lag`` threading), and the precondition
script's ``check()``) are TREE-shaped, not a single linear AND-chain: which sensor is
even consulted next depends on which branch a PRIOR sensor took (e.g.
``_check_floor_auto_paired`` probes the cloud FIRST and only then decides
whether to consult the ledger+battery at all). Forcing that shape through
the same linear cartesian-product machinery would either under-model it
(losing branches) or explode combinatorially by crossing dimensions that are
never jointly consulted. These are instead HAND-ENUMERATED directly from the
source (each cell traced to its file:line) into a fixed list of
:class:`Cell` objects -- still MECHANICALLY VERIFIED, exactly like the
linear chains: every declared cell is driven against the real function via
monkeypatched sensors (never guessed), and a mismatch fails the test loudly.
The difference from the linear chains is only in how the CANDIDATE list is
generated (hand-traced vs. cartesian-product-and-dedupe), not in whether it
is checked against real code.

Never invoked as a subprocess by anything: this module imports
``check_engine_release_floor`` and ``check_client_release_precondition``
directly and monkeypatches their sensors in-process. It never shells out to
either script, never touches the network, and never reads real repo state
(git/gh) -- every driven cell is fully synthetic.

Usage::

    uv run python scripts/enumerate_release_cells.py
    uv run python scripts/enumerate_release_cells.py --out tests/scripts/fixtures/release_cells.json

Test: uv run pytest -n auto tests/scripts/test_enumerate_release_cells.py
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime
import io
import itertools
import json
import os
import pathlib
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import check_client_release_precondition as precond
import check_engine_release_floor as floor
import check_wire_contract_pairing as wire_ledger
from nexus import deploy_tracker
from nexus.db.managed_endpoint import (
    ManagedCapabilities,
    ManagedServiceIncompatible,
    ManagedServiceUnreachable,
)
from nexus.engine_version import REQUIRED_ENGINE_VERSION

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _REPO_ROOT / "tests" / "scripts" / "fixtures" / "release_cells.json"


# ---------------------------------------------------------------------------
# Generic linear guard-chain model
# ---------------------------------------------------------------------------

class _Continue:
    """Sentinel: this guard step's value does not terminate the chain."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "CONTINUE"


#: Use as a :class:`GuardStep` outcome to mean "fall through to the next step".
CONTINUE = _Continue()


@dataclasses.dataclass(frozen=True)
class Dimension:
    """One axis of the state space, with its finite (already-reduced) domain."""

    name: str
    domain: tuple[str, ...]
    description: str = ""


@dataclasses.dataclass(frozen=True)
class Leaf:
    """A terminal decision-cell outcome: an exit code and a stable symbolic
    message key. ``message_key`` is a SEMANTIC label this module assigns to
    the branch (matching how the file:line research already names each
    dimension's outcomes) -- it is NOT parsed from the real function's
    stdout/stderr. Two branches can legitimately print IDENTICAL text (a
    documented defect: ``check_pin_currency`` prints "== newest published
    tag" on both the ``at_floor`` and ``below_floor`` branches) while still
    being distinct decision cells by the code's own branch structure; using
    printed text as the identity key would silently MERGE them and lose
    exactly the finding the research doc flagged."""

    exit_code: int
    message_key: str


@dataclasses.dataclass(frozen=True)
class GuardStep:
    """One guard in an ordered chain: reads ``dimension``'s value and either
    terminates at a :class:`Leaf` or falls through (:data:`CONTINUE`)."""

    name: str
    dimension: str
    outcomes: dict[str, Any]  # domain value -> Leaf | CONTINUE


@dataclasses.dataclass(frozen=True)
class GuardChain:
    """A linear, short-circuit AND-chain of :class:`GuardStep`, over a
    fixed set of :class:`Dimension`. ``success`` is the leaf reached when
    every step falls through; ``None`` when the steps' domains are fully
    covered by explicit leaves (no implicit fallthrough case exists)."""

    function: str
    steps: tuple[GuardStep, ...]
    dims: dict[str, Dimension]
    success: Leaf | None = None
    description: str = ""


@dataclasses.dataclass(frozen=True)
class Cell:
    """One reachable decision cell: a representative input tuple and the
    verdict it produces."""

    function: str
    inputs: dict[str, str]
    exit_code: int
    message_key: str
    note: str = ""


@dataclasses.dataclass(frozen=True)
class EnumerationResult:
    function: str
    total_combinations: int
    reachable: tuple[Cell, ...]
    unreachable_declared_leaves: tuple[dict[str, Any], ...]


def resolve_leaf(chain: GuardChain, tup: dict[str, str]) -> Leaf:
    """Walk ``chain``'s steps in order for input tuple ``tup``, short-circuit
    at the first terminating step, else return the chain's success leaf."""
    for step in chain.steps:
        outcome = step.outcomes[tup[step.dimension]]
        if outcome is not CONTINUE:
            return outcome
    if chain.success is None:
        raise ValueError(
            f"{chain.function}: guard chain exhausted for {tup!r} without a "
            "terminating leaf and no success leaf declared -- the steps' "
            "domains do not fully cover their dimensions"
        )
    return chain.success


def enumerate_chain(chain: GuardChain) -> EnumerationResult:
    """Exhaustively walk the full cartesian product of ``chain.dims``'
    domains, resolve each combination to a leaf via short-circuit semantics,
    and report the DISTINCT reachable leaves plus any declared-but-unreached
    leaf (never dropped silently)."""
    dim_names = list(chain.dims.keys())
    domains = [chain.dims[n].domain for n in dim_names]
    total = 1
    for d in domains:
        total *= len(d)

    seen: dict[tuple[int, str], dict[str, str]] = {}
    for values in itertools.product(*domains):
        tup = dict(zip(dim_names, values))
        leaf = resolve_leaf(chain, tup)
        key = (leaf.exit_code, leaf.message_key)
        if key not in seen:
            seen[key] = tup

    declared: set[tuple[int, str]] = set()
    if chain.success is not None:
        declared.add((chain.success.exit_code, chain.success.message_key))
    for step in chain.steps:
        for outcome in step.outcomes.values():
            if outcome is not CONTINUE:
                declared.add((outcome.exit_code, outcome.message_key))

    reachable = tuple(
        Cell(chain.function, seen[key], key[0], key[1]) for key in sorted(seen)
    )
    unreachable = tuple(
        {"exit_code": ec, "message_key": mk}
        for ec, mk in sorted(declared - set(seen))
    )
    return EnumerationResult(chain.function, total, reachable, unreachable)


def verify_cell(cell: Cell, observed: tuple[int, str] | int) -> bool:
    """Does ``observed`` (an ``(exit_code, message_key)`` pair, or a bare
    ``exit_code`` when only the code is being checked) match ``cell``?"""
    if isinstance(observed, tuple):
        return (cell.exit_code, cell.message_key) == observed
    return cell.exit_code == observed


def make_result(function: str, cells: list[Cell]) -> EnumerationResult:
    """Wrap a HAND-enumerated (already deduplicated) cell list from an
    orchestrator function in the same :class:`EnumerationResult` shape a
    linear chain produces, so the fixture writer treats both uniformly.
    ``total_combinations`` is the cell count itself (there is no separate
    "combination" concept for a tree-shaped function traced directly from
    source), and ``unreachable_declared_leaves`` is empty by construction:
    every hand-declared cell here has already been confirmed reachable by
    tracing its file:line in the real source."""
    return EnumerationResult(function, len(cells), tuple(cells), ())


def _capture(fn, *args: Any, **kwargs: Any) -> tuple[int, str, str]:
    """Run ``fn`` with stdout/stderr captured; return (exit_code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# FLOOR script: linear guard-chain functions
# ---------------------------------------------------------------------------

def _a_lesser_version(v: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, patch = v
    if patch > 0:
        return (major, minor, patch - 1)
    if minor > 0:
        return (major, minor - 1, 999)
    return (major - 1, 999, 999) if major > 0 else (0, 0, 0)


def _a_greater_version(v: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, patch = v
    return (major, minor, patch + 1)


_ABOVE_FLOOR = _a_greater_version(REQUIRED_ENGINE_VERSION)
_BELOW_FLOOR = _a_lesser_version(REQUIRED_ENGINE_VERSION)
_FRESH_AGE_HOURS = 1.0
_STALE_AGE_HOURS = floor._DEFAULT_PAIRED_TAG_MAX_AGE_HOURS + 100.0
_PINNED_TAG = floor._pinned_engine_tag()


def pin_currency_chain() -> GuardChain:
    newest = Dimension(
        "newest", ("unavailable", "none", "above_floor", "at_floor", "below_floor"),
        "newest_published_engine()'s outcome (F3): git-unavailable, an empty "
        "tag namespace, or a comparison against REQUIRED_ENGINE_VERSION.",
    )
    steps = (
        GuardStep("newest_gate", "newest", {
            "unavailable": Leaf(2, "pin_currency_tags_unavailable"),
            "none": Leaf(2, "pin_currency_zero_tags"),
            "above_floor": Leaf(1, "pin_currency_stale_pin"),
            "at_floor": Leaf(0, "pin_currency_current_at_floor"),
            "below_floor": Leaf(0, "pin_currency_current_below_floor"),
        }),
    )
    return GuardChain("check_pin_currency", steps, {"newest": newest})


def _newest_value(label: str) -> Any:
    return {
        "unavailable": floor._TAGS_UNAVAILABLE,
        "none": None,
        "above_floor": _ABOVE_FLOOR,
        "at_floor": REQUIRED_ENGINE_VERSION,
        "below_floor": _BELOW_FLOOR,
    }[label]


def drive_pin_currency(cell: Cell) -> tuple[int, str]:
    newest = _newest_value(cell.inputs["newest"])
    rc, out, err = _capture(floor.check_pin_currency, newest)
    classified = _classify_pin_currency(rc, out + err)
    if classified == "pin_currency_current":
        # at_floor and below_floor print IDENTICAL text (the documented
        # check_pin_currency:307 defect) -- text alone cannot disambiguate
        # them, so trust the INPUT that produced this rc/text rather than
        # re-deriving it from an ambiguous message.
        return rc, cell.message_key
    return rc, classified


def _classify_pin_currency(rc: int, out: str) -> str:
    if "could not read engine-service tags" in out:
        return "pin_currency_tags_unavailable"
    if "zero engine-service-v* tags visible" in out:
        return "pin_currency_zero_tags"
    if "is published but this release pins" in out:
        return "pin_currency_stale_pin"
    if "engine pin is current" in out:
        # NOTE (matches T2 nexus_rdr/201-research-4 / research doc
        # check_pin_currency:307): the print statement is IDENTICAL text
        # for at_floor and below_floor -- this classifier cannot and does
        # not try to tell them apart from output alone. The driver below
        # instead trusts the INPUT that produced this rc/text and returns
        # the cell's own message_key for either at_floor or below_floor,
        # since both are confirmed-reachable, exit-0, textually-identical
        # branches (a real finding, not a modeling gap).
        return "pin_currency_current"
    raise AssertionError(f"unclassified check_pin_currency output: rc={rc} out={out!r}")


def source_ancestry_chain() -> GuardChain:
    tag_exists = Dimension("tag_exists", ("unavailable", "false", "true"))
    diff_result = Dimension("diff_result", ("exception", "nonzero", "drift", "clean"))
    steps = (
        GuardStep("exists_gate", "tag_exists", {
            "unavailable": Leaf(2, "ancestry_tag_unavailable"),
            "false": Leaf(2, "ancestry_tag_missing"),
            "true": CONTINUE,
        }),
        GuardStep("diff_gate", "diff_result", {
            "exception": Leaf(2, "ancestry_diff_exception"),
            "nonzero": Leaf(2, "ancestry_diff_nonzero"),
            "drift": Leaf(1, "ancestry_drift"),
            "clean": Leaf(0, "ancestry_clean"),
        }),
    )
    return GuardChain(
        "check_source_ancestry", steps,
        {"tag_exists": tag_exists, "diff_result": diff_result},
    )


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def drive_source_ancestry(cell: Cell) -> tuple[int, str]:
    exists_label = cell.inputs["tag_exists"]
    diff_label = cell.inputs["diff_result"]
    exists_value = {
        "unavailable": floor._TAGS_UNAVAILABLE,
        "false": False,
        "true": True,
    }[exists_label]

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        if diff_label == "exception":
            raise OSError("simulated git diff failure")
        if diff_label == "nonzero":
            return _FakeCompletedProcess(1, stderr="simulated git diff error")
        if diff_label == "drift":
            return _FakeCompletedProcess(0, stdout=" service/src/main/Foo.java | 2 +-\n")
        return _FakeCompletedProcess(0, stdout="")

    with patch.object(floor, "_tag_exists_in_git", return_value=exists_value):
        if exists_value is not True:
            rc, out, err = _capture(floor.check_source_ancestry, _PINNED_TAG)
            return rc, _classify_source_ancestry(rc, out + err)
        with patch.object(floor.subprocess, "run", side_effect=fake_run):
            rc, out, err = _capture(floor.check_source_ancestry, _PINNED_TAG)
            return rc, _classify_source_ancestry(rc, out + err)


def _classify_source_ancestry(rc: int, out: str) -> str:
    if "could not confirm" in out and "exists" in out:
        return "ancestry_tag_unavailable"
    if "does not exist in this checkout" in out:
        return "ancestry_tag_missing"
    if "git diff failed" in out:
        return "ancestry_diff_exception"
    if "exited" in out and "ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE" in out:
        return "ancestry_diff_nonzero"
    if "ships" in out and "source that its pinned engine tag" in out:
        return "ancestry_drift"
    if "engine source is current" in out:
        return "ancestry_clean"
    raise AssertionError(f"unclassified check_source_ancestry output: rc={rc} out={out!r}")


_LEDGER_LABELS = ("empty", "blocking", "additive", "acked_only")


def _ledger_fixture(label: str) -> tuple[wire_ledger.Ledger, list[str] | None]:
    """A synthetic :class:`Ledger` for one of the 4 F2/P4 ledger states,
    plus the ``ack_beads`` argument to pair with it."""
    if label == "empty":
        return wire_ledger.Ledger(unshipped={}), None
    if label == "blocking":
        entry = wire_ledger.LedgerEntry(
            sha="deadbeef1", bead="nexus-fake1", note="blocking, no token",
            engine_tag="engine-service-v9.9.9", additive=None,
        )
        return wire_ledger.Ledger(unshipped={entry.sha: entry}), None
    if label == "additive":
        entry = wire_ledger.LedgerEntry(
            sha="deadbeef2", bead="nexus-fake2", note="[additive] safe",
            engine_tag="engine-service-v9.9.9", additive=True,
        )
        return wire_ledger.Ledger(unshipped={entry.sha: entry}), None
    # acked_only
    entry = wire_ledger.LedgerEntry(
        sha="deadbeef3", bead="nexus-fake3", note="blocking, but acked",
        engine_tag="engine-service-v9.9.9", additive=None,
    )
    return wire_ledger.Ledger(unshipped={entry.sha: entry}), ["nexus-fake3"]


def client_lag_ledger_chain() -> GuardChain:
    ledger = Dimension("ledger", _LEDGER_LABELS)
    steps = (
        GuardStep("ledger_gate", "ledger", {
            "empty": Leaf(0, "ledger_clean"),
            "blocking": Leaf(1, "ledger_blocked"),
            "additive": Leaf(0, "ledger_additive_authorized"),
            "acked_only": Leaf(0, "ledger_acked"),
        }),
    )
    return GuardChain("check_client_lag_ledger", steps, {"ledger": ledger})


def drive_client_lag_ledger(cell: Cell) -> tuple[int, str]:
    ledger, ack = _ledger_fixture(cell.inputs["ledger"])
    with patch.object(wire_ledger, "parse_ledger", return_value=ledger):
        rc, out, err = _capture(floor.check_client_lag_ledger, ack)
    return rc, _classify_client_lag_ledger(rc, out + err)


def _classify_client_lag_ledger(rc: int, text: str) -> str:
    if "client-lag ledger clean" in text:
        return "ledger_clean"
    if "PAIRED DEPLOY BLOCKED" in text:
        return "ledger_blocked"
    if "all marked [additive]" in text:
        return "ledger_additive_authorized"
    if "all explicitly acknowledged" in text:
        return "ledger_acked"
    raise AssertionError(f"unclassified check_client_lag_ledger output: rc={rc} text={text!r}")


def wire_contract_ledger_chain() -> GuardChain:
    ledger = Dimension("ledger", _LEDGER_LABELS)
    steps = (
        GuardStep("ledger_gate", "ledger", {
            "empty": Leaf(0, "ledger_clean"),
            "blocking": Leaf(1, "ledger_blocked"),
            "additive": Leaf(0, "ledger_additive_authorized"),
            "acked_only": Leaf(0, "ledger_acked"),
        }),
    )
    return GuardChain("check_wire_contract_ledger", steps, {"ledger": ledger})


def drive_wire_contract_ledger(cell: Cell) -> tuple[int, str]:
    ledger, ack = _ledger_fixture(cell.inputs["ledger"])
    with patch.object(wire_ledger, "parse_ledger", return_value=ledger):
        (rc, _is_vacuous), out, err = _capture(precond.check_wire_contract_ledger, ack)
    return rc, _classify_wire_contract_ledger(rc, out + err)


def _classify_wire_contract_ledger(rc: int, text: str) -> str:
    """``check_wire_contract_ledger`` (precondition script) prints slightly
    different wording than the floor script's ``check_client_lag_ledger``
    for the same 4 ledger states (both call ``classify_unshipped`` — the
    ONE shared interpreter, nexus-hcdk3 — so the VERDICTS agree; only the
    surrounding prose differs)."""
    if "wire-contract ledger: 0 unshipped entries" in text:
        return "ledger_clean"
    if "BLOCKED:" in text:
        return "ledger_blocked"
    if "all marked [additive]" in text:
        return "ledger_additive_authorized"
    if "all explicitly acknowledged" in text:
        return "ledger_acked"
    raise AssertionError(f"unclassified check_wire_contract_ledger output: rc={rc} text={text!r}")


#: check_paired_preconditions' guard order (nexus-k1c08 docstring): shape,
#: parse, existence, publication, version-match, newest-tag-match, freshness.
_BATTERY_TAG_SHAPE = Dimension("tag_shape", ("invalid_prefix", "valid_prefix"))
_BATTERY_TAG_PARSE = Dimension("tag_parse", ("unparseable", "parseable"))
_BATTERY_TAG_EXISTS = Dimension("tag_exists", ("unavailable", "false", "true"))
_BATTERY_TAG_PUBLISHED = Dimension("tag_published", ("unavailable", "false", "true"))
_BATTERY_VERSION_MATCH = Dimension("version_match", ("mismatch", "match"))
_BATTERY_NEWEST_STATE = Dimension(
    "newest_state", ("unavailable", "none", "mismatch", "match"),
)
_BATTERY_AGE_STATE = Dimension("age_state", ("unavailable", "too_old", "fresh"))


def paired_preconditions_chain() -> GuardChain:
    steps = (
        GuardStep("shape_gate", "tag_shape", {
            "invalid_prefix": Leaf(1, "battery_bad_prefix"),
            "valid_prefix": CONTINUE,
        }),
        GuardStep("parse_gate", "tag_parse", {
            "unparseable": Leaf(1, "battery_unparseable_tag"),
            "parseable": CONTINUE,
        }),
        GuardStep("exists_gate", "tag_exists", {
            "unavailable": Leaf(2, "battery_exists_unavailable"),
            "false": Leaf(1, "battery_tag_not_exists"),
            "true": CONTINUE,
        }),
        GuardStep("published_gate", "tag_published", {
            "unavailable": Leaf(2, "battery_published_unavailable"),
            "false": Leaf(1, "battery_not_published"),
            "true": CONTINUE,
        }),
        GuardStep("version_gate", "version_match", {
            "mismatch": Leaf(1, "battery_version_mismatch"),
            "match": CONTINUE,
        }),
        GuardStep("newest_gate", "newest_state", {
            "unavailable": Leaf(2, "battery_newest_unavailable"),
            "none": Leaf(1, "battery_newest_none"),
            "mismatch": Leaf(1, "battery_newest_mismatch"),
            "match": CONTINUE,
        }),
        GuardStep("age_gate", "age_state", {
            "unavailable": Leaf(2, "battery_age_unavailable"),
            "too_old": Leaf(1, "battery_too_old"),
            "fresh": CONTINUE,
        }),
    )
    dims = {
        "tag_shape": _BATTERY_TAG_SHAPE,
        "tag_parse": _BATTERY_TAG_PARSE,
        "tag_exists": _BATTERY_TAG_EXISTS,
        "tag_published": _BATTERY_TAG_PUBLISHED,
        "version_match": _BATTERY_VERSION_MATCH,
        "newest_state": _BATTERY_NEWEST_STATE,
        "age_state": _BATTERY_AGE_STATE,
    }
    return GuardChain(
        "check_paired_preconditions", steps, dims, success=Leaf(0, "battery_armed"),
    )


def _battery_tag(inputs: dict[str, str]) -> str:
    if inputs["tag_shape"] == "invalid_prefix":
        return "not-an-engine-tag"
    if inputs["tag_parse"] == "unparseable":
        return "engine-service-vBAD"
    if inputs["version_match"] == "mismatch":
        return "engine-service-v9.9.9"
    return "engine-service-v" + ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)


def _battery_newest(inputs: dict[str, str]) -> Any:
    return {
        "unavailable": floor._TAGS_UNAVAILABLE,
        "none": None,
        "mismatch": _ABOVE_FLOOR,
        "match": REQUIRED_ENGINE_VERSION,
    }[inputs["newest_state"]]


def drive_paired_preconditions(cell: Cell) -> tuple[int, str]:
    inputs = cell.inputs
    tag = _battery_tag(inputs)
    exists_value = {
        "unavailable": floor._TAGS_UNAVAILABLE, "false": False, "true": True,
    }[inputs["tag_exists"]]
    published_value = {
        "unavailable": (floor._TAGS_UNAVAILABLE, "gh unavailable"),
        "false": (False, "not published"),
        "true": (True, ""),
    }[inputs["tag_published"]]
    age_value = {
        "unavailable": floor._TAGS_UNAVAILABLE,
        "too_old": _STALE_AGE_HOURS,
        "fresh": _FRESH_AGE_HOURS,
    }[inputs["age_state"]]
    newest = _battery_newest(inputs)

    with patch.object(floor, "_tag_exists_in_git", return_value=exists_value), \
         patch.object(floor, "_paired_tag_published", return_value=published_value), \
         patch.object(floor, "_tag_age_hours", return_value=age_value):
        rc, out, err = _capture(floor.check_paired_preconditions, tag, newest)
    return rc, _classify_paired_preconditions(rc, out + err)


def _classify_paired_preconditions(rc: int, text: str) -> str:
    # COUPLING (RDR-201 P2.4 fix round, critique T2 nexus/critique-nexus
    # -j9z30-14-2026-09-02 [24073] finding (c)): the "could not be
    # consulted" / "verify publication" markers (battery_published_
    # unavailable) and "not published" (battery_not_published) are NOT
    # literal prose from release_messages.py's catalog -- those two catalog
    # entries carry only a bare "[reason]" placeholder. The actual text
    # this greps comes from check_engine_release_floor._paired_tag_published
    # ()'s own reason strings, substituted into that placeholder at the
    # check_paired_preconditions call site. Rewording either
    # _paired_tag_published's reason strings OR this markers dict without
    # the other reclassifies (or breaks) those two cells; see
    # release_messages.py's own COUPLING comment on those entries, and
    # tests/scripts/test_release_table_parity.py's test_battery_*_catalog_
    # reason_placeholder_drives_the_classifier.
    markers = {
        "is not an engine-service-v* tag": "battery_bad_prefix",
        "does not parse as a version": "battery_unparseable_tag",
        "could not read git tags to confirm": "battery_exists_unavailable",
        "does not exist in git": "battery_tag_not_exists",
        "could not be consulted": "battery_published_unavailable",
        "verify publication": "battery_published_unavailable",
        "is still a DRAFT": "battery_not_published",
        "has no `nexus-service-linux-amd64` asset": "battery_not_published",
        "not published": "battery_not_published",
        "wrong pairing": "battery_version_mismatch",
        "could not read engine-service tags from git to confirm no newer tag": "battery_newest_unavailable",
        "newest published engine tag is vnone": "battery_newest_none",
        "unaccounted engine work": "battery_newest_mismatch",
        "could not determine": "battery_age_unavailable",
        "past the": "battery_too_old",
        "paired mode ARMED": "battery_armed",
    }
    for marker, key in markers.items():
        if marker in text:
            return key
    raise AssertionError(f"unclassified check_paired_preconditions output: rc={rc} text={text!r}")


_TRACKER_OUTCOME_LABELS = (
    "directory_error", "schema_error", "no_report_for_version", "gate_red",
    "version_mismatch", "live_version_mismatch", "managed_service_error", "ok",
)


def tracker_outcome_chain() -> GuardChain:
    outcome = Dimension("outcome", _TRACKER_OUTCOME_LABELS)
    steps = (
        GuardStep("tracker_gate", "outcome", {
            "directory_error": Leaf(3, "tracker_directory_error"),
            "schema_error": Leaf(3, "tracker_schema_error"),
            "no_report_for_version": Leaf(3, "tracker_no_report_for_version"),
            "gate_red": Leaf(3, "tracker_gate_red"),
            "version_mismatch": Leaf(3, "tracker_version_mismatch"),
            "live_version_mismatch": Leaf(3, "tracker_live_version_mismatch"),
            "managed_service_error": Leaf(3, "tracker_managed_service_error"),
            "ok": Leaf(0, "tracker_recorded"),
        }),
    )
    return GuardChain("record_deploy_from_gate_report_leg", steps, {"outcome": outcome})


def _fake_tracker_write() -> deploy_tracker.TrackerWrite:
    report = deploy_tracker.GateReport(
        path=pathlib.Path("/fake/report.json"), schema_version=3,
        run_timestamp=datetime.datetime.now(datetime.timezone.utc),
        release_version=".".join(str(p) for p in REQUIRED_ENGINE_VERSION),
        passed=True, failures=(), advisories=(),
    )
    return deploy_tracker.TrackerWrite(
        content="deployed-engine-version: fake", live_version=report.release_version,
        base_url="https://example.test", report=report,
    )


def drive_tracker_outcome(cell: Cell) -> tuple[int, str]:
    label = cell.inputs["outcome"]
    error_cls = {
        "directory_error": deploy_tracker.GateReportDirectoryError,
        "schema_error": deploy_tracker.GateReportSchemaError,
        "no_report_for_version": deploy_tracker.NoGateReportForVersion,
        "gate_red": deploy_tracker.GateReportRed,
        "version_mismatch": deploy_tracker.GateReportVersionMismatch,
        "live_version_mismatch": deploy_tracker.LiveVersionMismatch,
    }.get(label)

    def fake_record(**kwargs: Any) -> deploy_tracker.TrackerWrite:
        if error_cls is not None:
            raise error_cls("simulated: " + label)
        if label == "managed_service_error":
            raise ManagedServiceIncompatible("simulated live re-read failure")
        return _fake_tracker_write()

    with patch.object(deploy_tracker, "record_deploy_from_gate_report", side_effect=fake_record):
        rc, out, err = _capture(
            floor.record_deploy_from_gate_report_leg, pathlib.Path("/fake/dir"), url=None,
        )
    return rc, _classify_tracker_outcome(rc, out + err, label)


def _classify_tracker_outcome(rc: int, text: str, label: str) -> str:
    if rc == 0:
        assert "deployed-engine-version recorded" in text
        return "tracker_recorded"
    assert "TRACKER NOT RECORDED" in text
    return {
        "directory_error": "tracker_directory_error",
        "schema_error": "tracker_schema_error",
        "no_report_for_version": "tracker_no_report_for_version",
        "gate_red": "tracker_gate_red",
        "version_mismatch": "tracker_version_mismatch",
        "live_version_mismatch": "tracker_live_version_mismatch",
        "managed_service_error": "tracker_managed_service_error",
    }[label]


# ---------------------------------------------------------------------------
# FLOOR script: tree-shaped orchestrator cells (hand-enumerated, driven)
# ---------------------------------------------------------------------------

def _caps(release_version: str) -> ManagedCapabilities:
    return ManagedCapabilities(
        base_url="https://example.test", app_version="1.0-SNAPSHOT",
        release_version=release_version, embedding_mode="voyage",
        embedding_models=[], schema_latest_id=None, schema_changeset_count=None,
    )


def check_floor_bare_cells() -> EnumerationResult:
    """``check_floor(paired_deploy=None, paired_deploy_auto=False)``: delegates
    pin-currency (already fully enumerated by :func:`pin_currency_chain`,
    represented here by ONE collapsed "delegate blocks" cell) then runs its
    OWN cloud-probe try/except (4 own branches)."""
    cells = [
        Cell("check_floor_bare", {"pin_currency": "blocks", "probe": "n/a"}, 2, "bare_pin_blocks",
             note="delegate to check_pin_currency; its own 5 leaves are enumerated separately. "
                  "exit_code=2 is the representative chosen (newest tags unavailable) -- "
                  "check_pin_currency's OTHER blocking leaf (stale pin) is exit_code=1, "
                  "already covered by pin_currency_chain()'s own enumeration."),
        Cell("check_floor_bare", {"pin_currency": "passes", "probe": "unreachable"}, 2, "bare_probe_unreachable"),
        Cell("check_floor_bare", {"pin_currency": "passes", "probe": "ms_error"}, 1, "bare_probe_stale_via_exception"),
        Cell("check_floor_bare", {"pin_currency": "passes", "probe": "success_stale"}, 1, "bare_probe_stale_via_success",
             note="defensive-only per check_floor's own docstring: probe_managed_service "
                  "fails closed on a below-floor release_version before ever returning a "
                  "successful caps, so this branch is unreachable via the REAL sensor"),
        Cell("check_floor_bare", {"pin_currency": "passes", "probe": "success_current"}, 0, "bare_probe_current"),
    ]
    return make_result("check_floor_bare", cells)


def drive_check_floor_bare(cell: Cell) -> tuple[int, str]:
    pin = cell.inputs["pin_currency"]
    probe = cell.inputs["probe"]
    newest = REQUIRED_ENGINE_VERSION if pin == "passes" else floor._TAGS_UNAVAILABLE

    def probe_side_effect(*args: Any, **kwargs: Any) -> Any:
        if probe == "unreachable":
            raise ManagedServiceUnreachable("simulated: unreachable")
        if probe == "ms_error":
            raise ManagedServiceIncompatible("simulated: stale", deployed_version=None)
        if probe == "success_stale":
            return _caps(".".join(str(p) for p in _BELOW_FLOOR))
        return _caps(".".join(str(p) for p in _ABOVE_FLOOR))

    with patch.object(floor, "newest_published_engine", return_value=newest), \
         patch.object(floor, "probe_managed_service", side_effect=probe_side_effect):
        rc, out, err = _capture(floor.check_floor, url="https://example.test")
    return rc, _classify_check_floor_bare(rc, out + err, pin)


def _classify_check_floor_bare(rc: int, text: str, pin: str) -> str:
    if pin == "blocks":
        return "bare_pin_blocks"
    if "unreachable" in text:
        return "bare_probe_unreachable"
    if "ENGINE FLOOR CHECK FAILED" in text and "required v" in text:
        return "bare_probe_stale_via_exception"
    if "ENGINE FLOOR CHECK FAILED: deployed engine at" in text:
        return "bare_probe_stale_via_success"
    if "cloud engine is current" in text:
        return "bare_probe_current"
    raise AssertionError(f"unclassified check_floor bare output: rc={rc} text={text!r}")


def check_floor_paired_explicit_cells() -> EnumerationResult:
    cells = [
        Cell("check_floor_paired", {"battery": "blocks", "probe": "n/a"}, 1, "paired_battery_blocks",
             note="delegate to _run_paired_precondition_battery (ledger + check_paired_preconditions); "
                  "their own leaves are enumerated separately"),
        Cell("check_floor_paired", {"battery": "passes", "probe": "unreachable"}, 2, "paired_probe_unreachable"),
        Cell("check_floor_paired", {"battery": "passes", "probe": "ms_error_not_below_floor"}, 2, "paired_probe_unverifiable_exception"),
        Cell("check_floor_paired", {"battery": "passes", "probe": "ms_error_below_floor"}, 0, "paired_probe_ack_via_exception"),
        Cell("check_floor_paired", {"battery": "passes", "probe": "success_unparseable"}, 2, "paired_probe_unverifiable_success",
             note="defensive-only: probe_managed_service fails closed before returning an "
                  "unparseable release_version on the real path"),
        Cell("check_floor_paired", {"battery": "passes", "probe": "success_below_floor"}, 0, "paired_probe_ack_via_success"),
        Cell("check_floor_paired", {"battery": "passes", "probe": "success_at_or_above_floor"}, 0, "paired_probe_current"),
    ]
    return make_result("check_floor_paired", cells)


def drive_check_floor_paired_explicit(cell: Cell) -> tuple[int, str]:
    battery = cell.inputs["battery"]
    probe = cell.inputs["probe"]
    ledger = wire_ledger.Ledger(unshipped={}) if battery == "passes" else _ledger_fixture("blocking")[0]
    exists_value = True if battery == "passes" else floor._TAGS_UNAVAILABLE

    def probe_side_effect(*args: Any, **kwargs: Any) -> Any:
        if probe == "unreachable":
            raise ManagedServiceUnreachable("simulated: unreachable")
        if probe == "ms_error_not_below_floor":
            raise ManagedServiceIncompatible("simulated: endpoint error", deployed_version=None)
        if probe == "ms_error_below_floor":
            raise ManagedServiceIncompatible("simulated: below floor", deployed_version=".".join(str(p) for p in _BELOW_FLOOR))
        if probe == "success_unparseable":
            return _caps("not-a-version")
        if probe == "success_below_floor":
            return _caps(".".join(str(p) for p in _BELOW_FLOOR))
        return _caps(".".join(str(p) for p in _ABOVE_FLOOR))

    with patch.object(wire_ledger, "parse_ledger", return_value=ledger), \
         patch.object(floor, "_tag_exists_in_git", return_value=exists_value), \
         patch.object(floor, "_paired_tag_published", return_value=(True, "")), \
         patch.object(floor, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(floor, "probe_managed_service", side_effect=probe_side_effect):
        rc, out, err = _capture(
            floor.check_floor, url="https://example.test", newest=REQUIRED_ENGINE_VERSION,
            paired_deploy=_PINNED_TAG,
        )
    return rc, _classify_check_floor_paired(rc, out + err, battery, probe)


#: (probe label -> (expected exit code, message key, a text marker to sanity
#: check the classification against, so a real drift in the source's printed
#: messages still fails the driver loudly). "ack_via_exception" and
#: "ack_via_success" print IDENTICAL text (the paired-ack catalog entries do not
#: distinguish which code path reached it) -- classified from the INPUT
#: that produced them, not from that shared text, matching
#: check_pin_currency's at_floor/below_floor collapse above.
_PAIRED_PROBE_EXPECTATIONS = {
    "unreachable": (2, "paired_probe_unreachable", "is unreachable"),
    "ms_error_not_below_floor": (2, "paired_probe_unverifiable_exception", "without a genuine below-floor"),
    "ms_error_below_floor": (0, "paired_probe_ack_via_exception", "PAIRED MODE: cloud reports"),
    "success_unparseable": (2, "paired_probe_unverifiable_success", "unparseable release_version"),
    "success_below_floor": (0, "paired_probe_ack_via_success", "PAIRED MODE: cloud reports"),
    "success_at_or_above_floor": (0, "paired_probe_current", "cloud engine is current"),
}


def _classify_check_floor_paired(rc: int, text: str, battery: str, probe: str) -> str:
    if battery == "blocks":
        assert rc == 1, f"expected battery-blocks to short-circuit with rc=1, got {rc}"
        return "paired_battery_blocks"
    expected_rc, key, marker = _PAIRED_PROBE_EXPECTATIONS[probe]
    assert rc == expected_rc, f"probe={probe!r}: expected rc={expected_rc}, got {rc} (text={text!r})"
    assert marker in text, f"probe={probe!r}: expected marker {marker!r} not found in {text!r}"
    return key


def check_floor_auto_paired_cells() -> EnumerationResult:
    cells = [
        Cell("check_floor_auto_paired", {"probe": "unreachable"}, 2, "auto_probe_unreachable"),
        Cell("check_floor_auto_paired", {"probe": "ms_error_not_below_floor"}, 2, "auto_probe_unverifiable_exception"),
        Cell("check_floor_auto_paired", {"probe": "ms_error_below_floor", "battery": "blocks"}, 1, "auto_below_via_exception_battery_blocks",
             note="delegate to _paired_below_floor_path -> _run_paired_precondition_battery"),
        Cell("check_floor_auto_paired", {"probe": "ms_error_below_floor", "battery": "passes"}, 0, "auto_below_via_exception_ack"),
        Cell("check_floor_auto_paired", {"probe": "success_at_or_above_floor", "pin_currency": "blocks"}, 2, "auto_current_pin_blocks",
             note="delegate to check_pin_currency; exit_code=2 representative, see "
                  "check_floor_bare_cells' matching note"),
        Cell("check_floor_auto_paired", {"probe": "success_at_or_above_floor", "pin_currency": "passes"}, 0, "auto_current"),
        Cell("check_floor_auto_paired", {"probe": "success_unparseable"}, 2, "auto_probe_unverifiable_success",
             note="defensive-only, see check_floor_paired_explicit_cells' matching note"),
        Cell("check_floor_auto_paired", {"probe": "success_below_floor", "battery": "blocks"}, 1, "auto_below_via_success_battery_blocks"),
        Cell("check_floor_auto_paired", {"probe": "success_below_floor", "battery": "passes"}, 0, "auto_below_via_success_ack"),
    ]
    return make_result("check_floor_auto_paired", cells)


def drive_check_floor_auto_paired(cell: Cell) -> tuple[int, str]:
    inputs = cell.inputs
    probe = inputs["probe"]
    battery = inputs.get("battery")
    pin_currency = inputs.get("pin_currency")
    ledger = wire_ledger.Ledger(unshipped={})
    exists_value: Any = True
    newest = REQUIRED_ENGINE_VERSION
    if battery == "blocks":
        ledger = _ledger_fixture("blocking")[0]
    if pin_currency == "blocks":
        newest = floor._TAGS_UNAVAILABLE

    def probe_side_effect(*args: Any, **kwargs: Any) -> Any:
        if probe == "unreachable":
            raise ManagedServiceUnreachable("simulated: unreachable")
        if probe == "ms_error_not_below_floor":
            raise ManagedServiceIncompatible("simulated: endpoint error", deployed_version=None)
        if probe == "ms_error_below_floor":
            raise ManagedServiceIncompatible("simulated: below floor", deployed_version=".".join(str(p) for p in _BELOW_FLOOR))
        if probe == "success_unparseable":
            return _caps("not-a-version")
        if probe == "success_below_floor":
            return _caps(".".join(str(p) for p in _BELOW_FLOOR))
        return _caps(".".join(str(p) for p in _ABOVE_FLOOR))

    with patch.object(wire_ledger, "parse_ledger", return_value=ledger), \
         patch.object(floor, "_tag_exists_in_git", return_value=exists_value), \
         patch.object(floor, "_paired_tag_published", return_value=(True, "")), \
         patch.object(floor, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(floor, "probe_managed_service", side_effect=probe_side_effect):
        rc, out, err = _capture(
            floor._check_floor_auto_paired, url="https://example.test", newest=newest,
            paired_tag_max_age_hours=floor._DEFAULT_PAIRED_TAG_MAX_AGE_HOURS, ack_client_lag=None,
        )
    return rc, _classify_check_floor_auto_paired(rc, out + err, probe, battery, pin_currency)


def _classify_check_floor_auto_paired(rc: int, text: str, probe: str, battery: str | None, pin_currency: str | None) -> str:
    if probe == "unreachable":
        return "auto_probe_unreachable"
    if probe == "ms_error_not_below_floor":
        return "auto_probe_unverifiable_exception"
    if probe == "success_unparseable":
        return "auto_probe_unverifiable_success"
    if probe == "success_at_or_above_floor":
        return "auto_current_pin_blocks" if pin_currency == "blocks" else "auto_current"
    if probe == "ms_error_below_floor":
        return "auto_below_via_exception_battery_blocks" if battery == "blocks" else "auto_below_via_exception_ack"
    if probe == "success_below_floor":
        return "auto_below_via_success_battery_blocks" if battery == "blocks" else "auto_below_via_success_ack"
    raise AssertionError(f"unclassified _check_floor_auto_paired output: rc={rc} text={text!r} probe={probe}")


def main_dispatch_cells() -> EnumerationResult:
    """``main()``'s FULL post-argparse-validation tail, over its own ``mode``
    dimension (code-review CRITICAL, 2026-09-01: the prior
    ``tracker_leg_dispatch_cells`` drove ONLY the bare-mode tracker leg with
    check_floor/ancestry hardcoded to pass, so ``--ledger-only``'s dispatch
    (:func:`floor.main`, the ``args.ledger_only`` branch) and the ``non_bare``
    early return (both paired modes, reached whenever check_floor AND
    check_source_ancestry both pass) had zero cells -- real release
    decisions, not CLI-shape refusals, unlike the parser.error() sites
    :data:`CLI_VALIDATION_REFUSAL_SITES` excludes).

    ``check_floor`` and ``check_source_ancestry`` are each collapsed to a
    representative "blocks" (rc=2) / "passes" (rc=0) pair here -- their OWN
    leaves are already fully enumerated by :func:`check_floor_bare_cells` /
    :func:`check_floor_paired_explicit_cells` / :func:`check_floor_auto_paired_cells`
    / :func:`source_ancestry_chain`; this function's job is main()'s
    SEQUENCING and MODE DISPATCH around them, not their own internals.

    mode=bare: check_floor blocks (1) + [passes: ancestry blocks (1) +
    passes: 3 tracker-leg cells] = 5.
    mode=paired_explicit / paired_auto (--paired-deploy / --paired-deploy-auto):
    check_floor blocks (1) + [passes: ancestry blocks (1) + passes: the
    ``non_bare`` early return, ONE cell, rc=0]) = 3 each.
    mode=ledger_only (--ledger-only): bypasses check_floor/ancestry entirely
    (the early explicit return in main()) -- driven over the same 4 ledger
    outcomes :func:`client_lag_ledger_chain` already enumerates for the
    function directly, this time through the REAL ``main(["--ledger-only"])``
    dispatch route. 4 cells.

    Total: 5 + 3 + 3 + 4 = 15.
    """
    cells = [
        # mode=bare
        Cell("main_dispatch", {"mode": "bare", "check_floor": "blocks"},
             2, "main_bare_check_floor_blocks"),
        Cell("main_dispatch", {"mode": "bare", "check_floor": "passes", "ancestry": "blocks"},
             2, "main_bare_ancestry_blocks"),
        Cell("main_dispatch",
             {"mode": "bare", "check_floor": "passes", "ancestry": "passes", "tracker": "resolved"},
             0, "main_bare_tracker_delegates",
             note="delegate to record_deploy_from_gate_report_leg; its own 8 leaves "
                  "are enumerated separately by tracker_outcome_chain"),
        Cell("main_dispatch",
             {"mode": "bare", "check_floor": "passes", "ancestry": "passes", "tracker": "opt_out"},
             0, "main_bare_tracker_opt_out"),
        Cell("main_dispatch",
             {"mode": "bare", "check_floor": "passes", "ancestry": "passes", "tracker": "refusal"},
             3, "main_bare_tracker_refusal"),
        # mode=paired_explicit (--paired-deploy)
        Cell("main_dispatch", {"mode": "paired_explicit", "check_floor": "blocks"},
             2, "main_paired_explicit_check_floor_blocks"),
        Cell("main_dispatch",
             {"mode": "paired_explicit", "check_floor": "passes", "ancestry": "blocks"},
             2, "main_paired_explicit_ancestry_blocks"),
        Cell("main_dispatch",
             {"mode": "paired_explicit", "check_floor": "passes", "ancestry": "passes"},
             0, "main_paired_explicit_non_bare_return",
             note="the `if non_bare: return 0` early return (pre-deploy modes verify "
                  "preconditions; there is no post-deploy report to record from yet)"),
        # mode=paired_auto (--paired-deploy-auto)
        Cell("main_dispatch", {"mode": "paired_auto", "check_floor": "blocks"},
             2, "main_paired_auto_check_floor_blocks"),
        Cell("main_dispatch",
             {"mode": "paired_auto", "check_floor": "passes", "ancestry": "blocks"},
             2, "main_paired_auto_ancestry_blocks"),
        Cell("main_dispatch",
             {"mode": "paired_auto", "check_floor": "passes", "ancestry": "passes"},
             0, "main_paired_auto_non_bare_return"),
        # mode=ledger_only (--ledger-only)
        Cell("main_dispatch", {"mode": "ledger_only", "ledger": "empty"},
             0, "main_ledger_only_clean"),
        Cell("main_dispatch", {"mode": "ledger_only", "ledger": "blocking"},
             1, "main_ledger_only_blocked"),
        Cell("main_dispatch", {"mode": "ledger_only", "ledger": "additive"},
             0, "main_ledger_only_additive"),
        Cell("main_dispatch", {"mode": "ledger_only", "ledger": "acked_only"},
             0, "main_ledger_only_acked"),
    ]
    return make_result("main_dispatch", cells)


def drive_main_dispatch(cell: Cell) -> tuple[int, str]:
    inputs = cell.inputs
    mode = inputs["mode"]

    if mode == "ledger_only":
        ledger, ack = _ledger_fixture(inputs["ledger"])
        argv = ["--ledger-only"]
        for bead in ack or []:
            argv += ["--ack-client-lag", bead]
        with patch.object(wire_ledger, "parse_ledger", return_value=ledger):
            rc, out, err = _capture(floor.main, argv)
        return rc, _classify_main_dispatch(rc, out + err, inputs)

    check_floor_rc = 2 if inputs["check_floor"] == "blocks" else 0
    ancestry_rc = 2 if inputs.get("ancestry") == "blocks" else 0

    argv = ["--url", "https://example.test"]
    if mode == "paired_explicit":
        argv += ["--paired-deploy", _PINNED_TAG]
    elif mode == "paired_auto":
        argv += ["--paired-deploy-auto"]

    patches = [
        patch.object(floor, "check_floor", return_value=check_floor_rc),
        patch.object(floor, "check_source_ancestry", return_value=ancestry_rc),
    ]
    tracker = inputs.get("tracker")
    if mode == "bare" and inputs["check_floor"] == "passes" and inputs.get("ancestry") == "passes":
        if tracker == "resolved":
            argv += ["--record-deploy-from-gate-report", "/fake/dir"]
            patches.append(patch.object(floor, "record_deploy_from_gate_report_leg", return_value=0))
        elif tracker == "opt_out":
            argv += ["--no-record-deploy", "a representative reason"]
        # "refusal": bare argv as-is (no tracker flag, no env var)

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        rc, out, err = _capture(floor.main, argv)
    return rc, _classify_main_dispatch(rc, out + err, inputs)


def _classify_main_dispatch(rc: int, text: str, inputs: dict[str, str]) -> str:
    mode = inputs["mode"]
    if mode == "ledger_only":
        generic_keys = {
            "empty": "ledger_clean",
            "blocking": "ledger_blocked",
            "additive": "ledger_additive_authorized",
            "acked_only": "ledger_acked",
        }
        main_keys = {
            "empty": "main_ledger_only_clean",
            "blocking": "main_ledger_only_blocked",
            "additive": "main_ledger_only_additive",
            "acked_only": "main_ledger_only_acked",
        }
        # sanity: --ledger-only's dispatch prints nothing beyond the real
        # check_client_lag_ledger text -- confirm it made it through unchanged.
        assert _classify_client_lag_ledger(rc, text) == generic_keys[inputs["ledger"]], (rc, text, inputs)
        return main_keys[inputs["ledger"]]
    if inputs["check_floor"] == "blocks":
        return f"main_{mode}_check_floor_blocks"
    if inputs.get("ancestry") == "blocks":
        return f"main_{mode}_ancestry_blocks"
    if mode == "bare":
        key = {
            "resolved": "main_bare_tracker_delegates",
            "opt_out": "main_bare_tracker_opt_out",
            "refusal": "main_bare_tracker_refusal",
        }[inputs["tracker"]]
        if key == "main_bare_tracker_opt_out":
            assert "NOTE (--no-record-deploy)" in text
        elif key == "main_bare_tracker_refusal":
            assert "TRACKER NOT RECORDED" in text
        return key
    return f"main_{mode}_non_bare_return"


# ---------------------------------------------------------------------------
# PRECONDITION script: tree-shaped orchestrator cells (hand-enumerated, driven)
# ---------------------------------------------------------------------------

def check_composite_cells() -> EnumerationResult:
    """``check()``: the hand table (:data:`ENGINE_CLIENT_PRECONDITIONS`, P1)
    THEN the wire-contract ledger (P4, already fully enumerated by
    :func:`wire_contract_ledger_chain`, collapsed to 2 representative values
    here since the hand-table branches that error/block never reach the
    ledger at all)."""
    cells = [
        Cell("check_composite", {"hand_table": "latest_release_tag_error"}, 2, "composite_latest_release_tag_error"),
        Cell("check_composite", {"hand_table": "is_ancestor_error"}, 2, "composite_is_ancestor_error"),
        Cell("check_composite", {"hand_table": "missing_commit"}, 1, "composite_missing_commit"),
        Cell("check_composite", {"hand_table": "vacuous", "ledger": "empty"}, 0, "composite_both_vacuous"),
        Cell("check_composite", {"hand_table": "vacuous", "ledger": "blocking"}, 1, "composite_vacuous_table_ledger_blocks"),
        Cell("check_composite", {"hand_table": "vacuous", "ledger": "additive"}, 0, "composite_vacuous_table_ledger_additive"),
        Cell("check_composite", {"hand_table": "vacuous", "ledger": "acked_only"}, 0, "composite_vacuous_table_ledger_acked"),
        Cell("check_composite", {"hand_table": "satisfied", "ledger": "empty"}, 0, "composite_table_satisfied_ledger_empty"),
        Cell("check_composite", {"hand_table": "satisfied", "ledger": "blocking"}, 1, "composite_table_satisfied_ledger_blocks"),
        Cell("check_composite", {"hand_table": "satisfied", "ledger": "additive"}, 0, "composite_table_satisfied_ledger_additive"),
        Cell("check_composite", {"hand_table": "satisfied", "ledger": "acked_only"}, 0, "composite_table_satisfied_ledger_acked"),
    ]
    return make_result("check_composite", cells)


def drive_check_composite(cell: Cell) -> tuple[int, str]:
    hand_table = cell.inputs["hand_table"]
    ledger_label = cell.inputs.get("ledger")
    engine_tag = "engine-service-vTEST"
    table: dict[str, dict[str, str]] = {}
    patches = []

    if hand_table == "latest_release_tag_error":
        patches.append(patch.object(precond, "latest_release_tag", side_effect=RuntimeError("simulated")))
        table = {engine_tag: {"deadbeef": "test"}}
    elif hand_table == "is_ancestor_error":
        patches.append(patch.object(precond, "latest_release_tag", return_value="v0.0.1"))
        patches.append(patch.object(precond, "is_ancestor", side_effect=RuntimeError("simulated")))
        table = {engine_tag: {"deadbeef": "test"}}
    elif hand_table == "missing_commit":
        patches.append(patch.object(precond, "latest_release_tag", return_value="v0.0.1"))
        patches.append(patch.object(precond, "is_ancestor", return_value=False))
        table = {engine_tag: {"deadbeef": "test"}}
    elif hand_table == "satisfied":
        patches.append(patch.object(precond, "latest_release_tag", return_value="v0.0.1"))
        patches.append(patch.object(precond, "is_ancestor", return_value=True))
        table = {engine_tag: {"deadbeef": "test"}}
    # "vacuous": table stays {}

    ledger, ack = _ledger_fixture(ledger_label) if ledger_label else (wire_ledger.Ledger(unshipped={}), None)
    patches.append(patch.object(wire_ledger, "parse_ledger", return_value=ledger))
    patches.append(patch.dict(precond.ENGINE_CLIENT_PRECONDITIONS, table, clear=True))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        rc, out, err = _capture(precond.check, engine_tag, ack)
    return rc, _classify_check_composite(rc, out + err, hand_table, ledger_label)


def _classify_check_composite(rc: int, text: str, hand_table: str, ledger_label: str | None) -> str:
    if "CANNOT VERIFY:" in text:
        return "composite_latest_release_tag_error"
    if "CANNOT VERIFY " in text and "CANNOT VERIFY:" not in text:
        return "composite_is_ancestor_error"
    if "BLOCKED:" in text and "must not deploy" in text:
        return "composite_missing_commit"
    if "BLOCKED:" in text and "both-halves commit" in text:
        return {
            "vacuous": "composite_vacuous_table_ledger_blocks",
            "satisfied": "composite_table_satisfied_ledger_blocks",
        }[hand_table]
    if hand_table == "vacuous":
        if "OK (VACUOUS" in text:
            return "composite_both_vacuous"
        if "all marked [additive]" in text:
            return "composite_vacuous_table_ledger_additive"
        return "composite_vacuous_table_ledger_acked"
    # satisfied
    if "all marked [additive]" in text:
        return "composite_table_satisfied_ledger_additive"
    if ledger_label == "empty":
        return "composite_table_satisfied_ledger_empty"
    return "composite_table_satisfied_ledger_acked"


# ---------------------------------------------------------------------------
# PRECONDITION script: main()'s own argparse wiring (critique SIGNIFICANT 1)
# ---------------------------------------------------------------------------

def precond_main_dispatch_cells() -> EnumerationResult:
    """``check_client_release_precondition.main()``'s own decision surface:
    which ``(engine_tag, ack_client_lag)`` tuple it threads into
    :func:`check` (already fully enumerated by :func:`check_composite_cells`
    under the name ``check_composite`` -- these cells verify the WIRING
    (argv -> engine_tag/ack_client_lag) AND that ``main()`` returns a
    GENUINELY DERIVED exit code from a real ``check()`` execution, not a
    re-derivation of ``check()``'s own 11 leaves).

    code-review Important (T2 nexus/code-review-nexus-j9z30-12-2026-09-01):
    the prior driver mocked ``check`` to return ``cell.exit_code`` verbatim,
    so ``rc == cell.exit_code`` by construction -- a comparison of the
    fixture to itself. Fixed by driving a real ``check()`` call (hand table
    forced vacuous via an empty ``ENGINE_CLIENT_PRECONDITIONS``, exactly as
    :func:`drive_check_composite`'s "vacuous" scenarios do) over two REAL,
    independently distinguishable ledger states: an acknowledged blocking
    entry (0) and an unacknowledged one (1). ``main()`` has zero
    ``.error()`` call sites (confirmed by :func:`scan_parser_error_call_sites`)
    -- no CLI-shape refusal exists here to exclude."""
    cells = [
        Cell("precond_main_dispatch", {"engine_tag": "explicit", "ack": "given"}, 0,
             "precond_main_explicit_tag_with_ack"),
        Cell("precond_main_dispatch", {"engine_tag": "default", "ack": "absent"}, 1,
             "precond_main_default_tag_no_ack"),
    ]
    return make_result("precond_main_dispatch", cells)


def drive_precond_main_dispatch(cell: Cell) -> tuple[int, str]:
    """Drive the REAL ``check()`` through ``main()``'s real argv parsing.

    ``precond.check`` is spied on, not stubbed: ``spying_check`` captures
    the arguments ``main()`` passed it (proving the wiring) and then
    DELEGATES to the original, unpatched ``check`` (proving the exit code
    is genuinely derived, not injected) -- "capture then delegate", unlike
    the prior driver's stub, which never let real ``check()`` logic run.
    """
    inputs = cell.inputs
    argv: list[str] = []
    if inputs["engine_tag"] == "explicit":
        argv += ["--engine-tag", "engine-service-vTEST"]
        expected_tag = "engine-service-vTEST"
    else:
        expected_tag = precond._pinned_engine_tag()

    if inputs["ack"] == "given":
        # a real blocking ledger entry, explicitly acknowledged by its own
        # bead -- check_wire_contract_ledger's real "acked" branch, rc=0.
        ledger, expected_ack = _ledger_fixture("acked_only")
    else:
        # a real blocking ledger entry, no acknowledgment -- the real
        # "blocked" branch, rc=1.
        ledger, expected_ack = _ledger_fixture("blocking")
    for bead in expected_ack or []:
        argv += ["--ack-client-lag", bead]

    captured: dict[str, Any] = {}
    real_check = precond.check

    def spying_check(engine_tag: str, ack_client_lag: list[str] | None = None) -> int:
        captured["engine_tag"] = engine_tag
        captured["ack_client_lag"] = ack_client_lag
        return real_check(engine_tag, ack_client_lag)

    with patch.object(precond, "check", side_effect=spying_check), \
         patch.dict(precond.ENGINE_CLIENT_PRECONDITIONS, {}, clear=True), \
         patch.object(wire_ledger, "parse_ledger", return_value=ledger):
        rc, _out, _err = _capture(precond.main, argv)
    assert captured["engine_tag"] == expected_tag, (cell, captured)
    assert captured["ack_client_lag"] == expected_ack, (cell, captured)
    return rc, cell.message_key


# ---------------------------------------------------------------------------
# CLI argument-validation refusals (critique CRITICAL): parser.error() sites
# ---------------------------------------------------------------------------
#
# Both scripts' main() validate FLAG COMBINATIONS before ever reaching a
# release decision (argparse's own ``parser.error()``, which prints usage +
# message to stderr and exits 2). These are CLI-SHAPE refusals, not release
# decisions -- Phase 2's table replaces the release-gate decision logic
# (check_floor / check_paired_preconditions / check() / ...), not argparse's
# flag-combination validation. So, per the critique's ruling, they are
# declared as a THIRD explicit exclusion (alongside gate-report directory
# contents and the --no-record-deploy free-text reason) rather than modeled
# as decision cells -- and the exclusion is made NON-VACUOUS by
# :func:`scan_parser_error_call_sites`, an AST-based scan asserted (in the
# test suite) to find EXACTLY this declared set: a new validation branch
# (or a changed one) reds that test instead of silently vanishing from the
# model.

CLI_VALIDATION_REFUSAL_SITES: tuple[dict[str, str], ...] = (
    {
        "script": "check_engine_release_floor.py",
        "message": "--paired-deploy and --paired-deploy-auto are mutually exclusive",
    },
    {
        "script": "check_engine_release_floor.py",
        "message": "--record-deploy-from-gate-report is the post-tag VERIFY's flag; it is "
                    "mutually exclusive with --paired-deploy, --paired-deploy-auto, and --ledger-only",
    },
    {
        "script": "check_engine_release_floor.py",
        "message": "--no-record-deploy needs a REASON (why this box is not recording the tracker)",
    },
    {
        "script": "check_engine_release_floor.py",
        "message": "--no-record-deploy and --record-deploy-from-gate-report are mutually exclusive",
    },
    {
        "script": "check_engine_release_floor.py",
        "message": "--no-record-deploy applies to the bare post-tag VERIFY only; the pre-deploy "
                    "modes never record",
    },
    {
        "script": "check_engine_release_floor.py",
        "message": "--ledger-only is mutually exclusive with --url, --paired-deploy, and --paired-deploy-auto",
    },
)

#: The two gated scripts, by their module's own ``__file__`` -- resolved via
#: the already-imported module objects rather than a hardcoded path, so this
#: scan can never silently point at a stale copy.
_SCANNED_SCRIPTS: dict[str, pathlib.Path] = {
    "check_engine_release_floor.py": pathlib.Path(floor.__file__),
    "check_client_release_precondition.py": pathlib.Path(precond.__file__),
}


def scan_parser_error_call_sites() -> list[dict[str, Any]]:
    """AST-scan both gated scripts for every ``<name>.error(...)`` call site
    (argparse's ``ArgumentParser.error`` convention -- neither script calls
    any OTHER ``.error(`` method, confirmed by grep at authoring time) and
    return each site's script, line, and literal message text.

    Deliberately keyed by MESSAGE, not line number, for the comparison this
    feeds (:data:`CLI_VALIDATION_REFUSAL_SITES`): line numbers are known to
    drift with unrelated edits elsewhere in a 1300+ line file (the bead's
    own reminder: "re-locate by symbol, not by line"), while the message
    text is what a human actually reads and is what changes when a
    validation branch is genuinely added, removed, or reworded.
    """
    sites: list[dict[str, Any]] = []
    for script_name, path in _SCANNED_SCRIPTS.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "error" and isinstance(node.func.value, ast.Name)):
                continue
            message = ""
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                message = node.args[0].value
            sites.append({"script": script_name, "line": node.lineno, "message": message})
    return sites


# ---------------------------------------------------------------------------
# RDR-201 research traceability (critique SIGNIFICANT 2): the 7 leaves T2
# nexus_rdr/201-research-4 flagged as having "no test at any layer" prior
# to this bead, each pinned to the cell id(s) that now cover it.
# ---------------------------------------------------------------------------

def cell_id(function: str, message_key: str) -> str:
    return f"{function}::{message_key}"


RDR_UNCOVERED_LEAF_TRACEABILITY: tuple[dict[str, Any], ...] = (
    {
        "rdr_leaf": "FLOOR ancestry git OSError (research doc ~line 523)",
        "cell_ids": [cell_id("check_source_ancestry", "ancestry_diff_exception")],
    },
    {
        "rdr_leaf": "FLOOR ancestry git-nonzero (research doc ~line 530)",
        "cell_ids": [cell_id("check_source_ancestry", "ancestry_diff_nonzero")],
    },
    {
        "rdr_leaf": "FLOOR bare-mode unparseable release_version via parsed is None "
                    "(research doc ~line 1099)",
        "cell_ids": [cell_id("check_floor_bare", "bare_probe_stale_via_success")],
    },
    {
        "rdr_leaf": "FLOOR tracker-leg ManagedServiceError re-read (research doc ~line 1172)",
        "cell_ids": [cell_id("record_deploy_from_gate_report_leg", "tracker_managed_service_error")],
    },
    {
        "rdr_leaf": "FLOOR paired battery newest-UNAVAILABLE (research doc ~line 701)",
        "cell_ids": [cell_id("check_paired_preconditions", "battery_newest_unavailable")],
    },
    {
        "rdr_leaf": "FLOOR paired battery newest-None (research doc ~line 708)",
        "cell_ids": [cell_id("check_paired_preconditions", "battery_newest_none")],
    },
    {
        "rdr_leaf": "PRECOND latest_release_tag RuntimeError (research doc ~line 236)",
        "cell_ids": [cell_id("check_composite", "composite_latest_release_tag_error")],
    },
)


# ---------------------------------------------------------------------------
# EVENT dimension (pre-tag / tag-push / deploy / post-deploy-verify)
# ---------------------------------------------------------------------------

EVENT_DIMENSION = Dimension(
    "event", ("pre-tag", "tag-push", "deploy", "post-deploy-verify"),
    "Finding 2's incident correction (T2 nexus_rdr/201-research-4): the "
    "7.1.0 / v0.1.62 failure (commit 79fff05a9) was an UN-ENCODED event "
    "dimension, not a guard defect -- the fix changed a remedy string and "
    "prose, no decision logic. A table for this choreography needs an "
    "event column.",
)

FLOOR_MODES = ("bare", "--paired-deploy", "--paired-deploy-auto", "--ledger-only")
PRECOND_MODES = ("default",)

#: Static (mode -> event(s)) mapping, derived from the scripts' own
#: docstrings and their actual call sites (ci.yml, release.yml, skill
#: markdown) -- NOT sensor-driven, since "which event is this invocation
#: happening at" is a fact about WHO calls the script, not about anything
#: the script's own guard chains observe. Every citation names the exact
#: source this module read to establish it.
_EVENT_CITATIONS: dict[tuple[str, str], str] = {
    ("check_engine_release_floor", "bare", "pre-tag"):
        "AGENTS.md § Cutting a release step 0: human bare invocation before "
        "the tag is cut.",
    ("check_engine_release_floor", "bare", "post-deploy-verify"):
        "release_messages.py's paired-ack entries: 'POST-TAG VERIFY REQUIRED: re-run "
        "this script WITHOUT --paired-deploy once the deploy lands'.",
    ("check_engine_release_floor", "--paired-deploy", "pre-tag"):
        "module docstring: the human names the tag BEFORE cutting the "
        "client tag; _DEFAULT_PAIRED_TAG_MAX_AGE_HOURS's docstring: "
        "'deploy fires AT client-tag push -- a pairing older than this is "
        "not THIS release's partner'.",
    ("check_engine_release_floor", "--paired-deploy-auto", "tag-push"):
        "nexus-gc9ir module docstring: 'release.yml's own copy of this "
        "gate ... runs --paired-deploy-auto instead of bare' -- fired at "
        "tag push, unattended.",
    ("check_engine_release_floor", "--ledger-only", "pre-tag"):
        "T2 nexus_rdr/201-research-4: 'ci.yml:153 runs --ledger-only on "
        "every pull_request with base_ref main' -- before any tag exists.",
    ("check_client_release_precondition", "default", "pre-tag"):
        "T2 nexus_rdr/201-research-4: 'only callers are skill markdown "
        "(.claude/skills/engine-release/SKILL.md:193,212,427)', used to "
        "decide deploy-before-tag ordering per nexus-1emxn choreography "
        "(a)/(b), i.e. before the client tag exists.",
}

#: Every (mode, event) NOT listed above is unreachable. The "deploy" event
#: in particular has no script invocation at all for either script -- the
#: deploy relay runs on conexus's side of the AGENTS.md bus, a different
#: system this repo's scripts never execute on.
_DEPLOY_EVENT_CITATION = (
    "AGENTS.md § Engine-service release: the deploy relay 'runs on a "
    "different system, on Hal's side of the AGENTS.md bus' -- neither "
    "script is invoked there."
)


def event_mode_matrix() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for script, modes in (
        ("check_engine_release_floor", FLOOR_MODES),
        ("check_client_release_precondition", PRECOND_MODES),
    ):
        for mode in modes:
            for event in EVENT_DIMENSION.domain:
                citation = _EVENT_CITATIONS.get((script, mode, event))
                if citation is None and event == "deploy":
                    citation = _DEPLOY_EVENT_CITATION
                rows.append({
                    "script": script,
                    "mode": mode,
                    "event": event,
                    "reachable": citation is not None and event != "deploy",
                    "citation": citation or "no invocation site found for this (mode, event) pairing",
                })
    return rows


# ---------------------------------------------------------------------------
# Fixture assembly
# ---------------------------------------------------------------------------

def _all_chains() -> list[GuardChain]:
    return [
        pin_currency_chain(),
        source_ancestry_chain(),
        client_lag_ledger_chain(),
        paired_preconditions_chain(),
        tracker_outcome_chain(),
        wire_contract_ledger_chain(),
    ]


def _all_orchestrator_results() -> list[EnumerationResult]:
    return [
        check_floor_bare_cells(),
        check_floor_paired_explicit_cells(),
        check_floor_auto_paired_cells(),
        main_dispatch_cells(),
        check_composite_cells(),
        precond_main_dispatch_cells(),
    ]


def _all_results() -> list[EnumerationResult]:
    return [enumerate_chain(c) for c in _all_chains()] + _all_orchestrator_results()


#: ``Cell.function`` -> the driver that runs the REAL gated function for one
#: cell of that function. Every key is one of the 12 functions
#: ``build_fixture`` enumerates; the parity harness dispatches through this
#: map and a 13th function appearing in the fixture with no driver here
#: fails loudly (``KeyError``), never silently.
DRIVERS: dict[str, Callable[[Cell], tuple[int, str]]] = {
    "check_pin_currency": drive_pin_currency,
    "check_source_ancestry": drive_source_ancestry,
    "check_client_lag_ledger": drive_client_lag_ledger,
    "check_wire_contract_ledger": drive_wire_contract_ledger,
    "check_paired_preconditions": drive_paired_preconditions,
    "record_deploy_from_gate_report_leg": drive_tracker_outcome,
    "check_floor_bare": drive_check_floor_bare,
    "check_floor_paired": drive_check_floor_paired_explicit,
    "check_floor_auto_paired": drive_check_floor_auto_paired,
    "main_dispatch": drive_main_dispatch,
    "check_composite": drive_check_composite,
    "precond_main_dispatch": drive_precond_main_dispatch,
}


def cell_from_dict(cell_dict: dict[str, Any]) -> Cell:
    return Cell(
        function=cell_dict["function"], inputs=cell_dict["inputs"],
        exit_code=cell_dict["exit_code"], message_key=cell_dict["message_key"],
        note=cell_dict.get("note", ""),
    )


def drive_cell_streams(cell: Cell) -> tuple[tuple[int, str], str, str]:
    """Drive ``cell``'s real gated function and return ``(verdict, stdout,
    stderr)`` -- the two streams captured SEPARATELY (a stream move is
    invisible to a concatenated comparison; RDR-201 P2.6 found one). Spies
    on :func:`_capture`, the single choke point every driver funnels its
    real call through, so no driver's sensor-patching is reimplemented."""
    original_capture = _capture
    outs: list[str] = []
    errs: list[str] = []

    def _spy(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str, str]:
        rc, out, err = original_capture(fn, *args, **kwargs)
        outs.append(out)
        errs.append(err)
        return rc, out, err

    with patch(f"{__name__}._capture", side_effect=_spy):
        verdict = DRIVERS[cell.function](cell)
    return verdict, "".join(outs), "".join(errs)


def text_normalizers() -> list[tuple[str, str]]:
    """The run-dependent substrings a captured message can carry, and the
    token each is replaced with so the frozen text oracle
    (``tests/scripts/fixtures/release_cell_texts.json``) survives a floor
    bump and a relocated checkout: the floor version, the one-above /
    one-below versions the drivers derive from it, and the wire-contract
    ledger path. Longest first, so a version that is a substring of
    another is never clipped."""
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    above = ".".join(str(p) for p in _a_greater_version(REQUIRED_ENGINE_VERSION))
    below = ".".join(str(p) for p in _BELOW_FLOOR)
    pairs = [
        (str(wire_ledger.DEFAULT_LEDGER_PATH), "<LEDGER_PATH>"),
        (above, "<FLOOR+1>"), (below, "<FLOOR-1>"), (floor, "<FLOOR>"),
    ]
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def normalize_text(text: str) -> str:
    for literal, token in text_normalizers():
        text = text.replace(literal, token)
    return text


def build_text_fixture(cells: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """``cell_id -> {"out", "err"}``: every cell's real printed text, per
    stream, normalized. The frozen oracle RDR-201 P2.6's cutover left in
    place of the deleted old-path comparison: the words the operator reads,
    pinned. Regenerate ONLY for a deliberate message change, in the same
    commit, with ``uv run python scripts/enumerate_release_cells.py
    --texts-out tests/scripts/fixtures/release_cell_texts.json`` -- the
    diff of the fixture is the review surface for the change."""
    texts: dict[str, dict[str, str]] = {}
    with patch.dict(os.environ):
        # nexus-nx3l5: an operator box sets NX_GATE_REPORT_DIR globally; the
        # test suite scrubs it (tests/scripts/conftest.py) and so must this.
        os.environ.pop("NX_GATE_REPORT_DIR", None)
        for cell_dict in cells:
            _verdict, out, err = drive_cell_streams(cell_from_dict(cell_dict))
            texts[cell_dict["cell_id"]] = {"out": normalize_text(out), "err": normalize_text(err)}
    return texts


def write_text_fixture(out: pathlib.Path, cells: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    texts = build_text_fixture(cells)
    out.write_text(json.dumps(texts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return texts


def build_fixture() -> dict[str, Any]:
    results = _all_results()
    all_dims: dict[str, Dimension] = {EVENT_DIMENSION.name: EVENT_DIMENSION}
    for chain in _all_chains():
        for name, dim in chain.dims.items():
            all_dims.setdefault(f"{chain.function}.{name}", dim)

    cells = []
    for result in results:
        for cell in result.reachable:
            cells.append({
                "cell_id": cell_id(cell.function, cell.message_key),
                "function": cell.function,
                "inputs": cell.inputs,
                "exit_code": cell.exit_code,
                "message_key": cell.message_key,
                "note": cell.note,
            })

    unreachable_declared = []
    for result in results:
        for leaf in result.unreachable_declared_leaves:
            unreachable_declared.append({"function": result.function, **leaf})

    header = {
        "bead": "nexus-j9z30.11",
        "scripts": [
            "scripts/check_engine_release_floor.py",
            "scripts/check_client_release_precondition.py",
        ],
        "source_research": "T2 nexus_rdr/201-research-4; T3 knowledge/"
                            "analysis-deep-paired-release-state-space-rdr201-2026-09-01",
        "dimensions": [
            {"name": name, "domain": list(dim.domain), "description": dim.description}
            for name, dim in sorted(all_dims.items())
        ],
        "excluded_dimensions": [
            {
                "name": "gate_report_directory_contents",
                "reason": "open-ended file listing / JSON body deploy_tracker.py's "
                          "discovery logic reads; not modeled beyond the 8 CLASSIFIED "
                          "outcomes deploy_tracker.py itself already reduces it to "
                          "(tracker_outcome_chain), which are driven by monkeypatching "
                          "nexus.deploy_tracker.record_deploy_from_gate_report directly.",
                "fixed_representative_value": "a directory holding exactly one green "
                                               "GateReport matching the live version "
                                               "(the 'ok' leaf's driven fixture)",
            },
            {
                "name": "no_record_deploy_reason",
                "reason": "free-text CLI argument; argparse already enforces non-blank, "
                          "and no downstream branch reads its content, only its presence.",
                "fixed_representative_value": "\"a representative reason\"",
            },
            {
                "name": "cli_argument_validation_refusals",
                "reason": "critique CRITICAL ruling (T2 nexus/critique-nexus-j9z30-11-2026-09-01): "
                          "argparse mutual-exclusion / validation refusals (parser.error(), exit "
                          "code 2) are CLI-shape refusals, not release decisions -- Phase 2's table "
                          "replaces release-gate DECISION logic, not argparse's own flag-combination "
                          "validation. Made non-vacuous by scan_parser_error_call_sites(), asserted "
                          "(test suite) to find EXACTLY this site list: a new/changed validation "
                          "branch reds that test instead of silently vanishing from the model.",
                "fixed_representative_value": "not applicable -- excluded from the decision-cell "
                                               "model entirely, not fixed to one representative input "
                                               "the way the other two exclusions above are",
                "sites": list(CLI_VALIDATION_REFUSAL_SITES),
            },
        ],
        "event_dimension_note": "P2.3 will join this static (mode -> event) matrix onto the "
                                 "per-cell verdicts as a genuine cross-referenced column; this "
                                 "measurement-phase side-table is not yet joined to individual "
                                 "cells (critique SIGNIFICANT 3, deferred).",
        "functions_enumerated_exhaustively": [c.function for c in _all_chains()],
        "functions_hand_enumerated_and_driven": [r.function for r in _all_orchestrator_results()],
        "reachable_cell_count": len(cells),
        "unreachable_declared_leaf_count": len(unreachable_declared),
        "total_combinations_considered": sum(r.total_combinations for r in results),
    }
    return {
        "header": header,
        "cells": cells,
        "unreachable_declared_leaves": unreachable_declared,
        "event_mode_matrix": event_mode_matrix(),
        "rdr_leaf_traceability": list(RDR_UNCOVERED_LEAF_TRACEABILITY),
    }


def write_fixture(out: pathlib.Path = _DEFAULT_OUT) -> dict[str, Any]:
    fixture = build_fixture()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=_DEFAULT_OUT)
    parser.add_argument(
        "--texts-out", type=pathlib.Path, default=None,
        help="also (re)write the per-cell stdout/stderr text oracle here -- "
             "only for a deliberate message change, in the same commit",
    )
    args = parser.parse_args(argv)
    fixture = write_fixture(args.out)
    if args.texts_out is not None:
        texts = write_text_fixture(args.texts_out, fixture["cells"])
        print(f"enumerate_release_cells: {len(texts)} cell texts written to {args.texts_out}")  # noqa: T201
    header = fixture["header"]
    # CLI summary line -- sanctioned stdout output for a script entrypoint,
    # the same convention scripts/check_engine_release_floor.py's own `main`
    # uses throughout (that file predates T201 enforcement on scripts/ and
    # is not itself clean under an explicit `ruff check`; this one is kept
    # clean deliberately since the bead's own verify step lints it).
    print(  # noqa: T201
        f"enumerate_release_cells: {header['reachable_cell_count']} reachable cells "
        f"({header['total_combinations_considered']} input combinations considered, "
        f"{header['unreachable_declared_leaf_count']} declared-but-unreachable leaves), "
        f"written to {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
