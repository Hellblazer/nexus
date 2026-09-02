# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-201 § Minimum Viable Validation (nexus-j9z30.6) — the RDR's own
acceptance criterion, all four assertions in ONE module:

1. The packaged ``rdr-lifecycle`` table lints clean through the checker.
2. ``nx rdr set-status <id> closed`` on a ``draft`` record refuses with
   ``illegal-transition``.
3. The same command on an ``accepted`` record succeeds.
4. A planted second ``accept`` row (in a FIXTURE copy of the table, never
   the real packaged one) makes the checker report ``overlap``.

This is not a duplicate of tests/tables/test_check.py (P1.3, the checker's
own unit suite) or tests/test_rdr_set_status.py (P1.4, ``set-status``'s
full transition-table coverage) — it re-asserts exactly the four sentences
RDR-201's Minimum Viable Validation section names, end to end, so the RDR's
own acceptance bar has one place that proves it, independent of either
component suite drifting.

Leg 1's non-vacuity floor (code review, T2 nexus/code-review-
nexus-j9z30-6-2026-09-02 [24038]): "lints clean" means zero BLOCKING
findings, not zero findings — the checker also emits advisories, and
``check_table``'s own no-bare-green principle (RDR-201 Sec Technical
Design) means every one of the packaged table's ``*-otherwise`` escape
rows, each alone in its own zero-guard-dimension group after list-valued
match expansion, earns a ``closed-by-escape`` advisory: a group closed
only by its catch-all still gets flagged, exactly as a guarded group does.
Leg 1 asserts the packaged table's exact advisory count (24, the number of
escape-only groups: 6 events' explicit-status complements — see
``rdr-lifecycle.toml``'s own header comment for the per-event enumeration)
rather than merely "at least one", so a regression that silently drops or
duplicates an ``-otherwise`` row's expansion is caught, not just a
regression that drops the advisory path entirely.

Leg 5 (below) is the module's own non-vacuity guard: it re-runs all four
legs' assertion bodies directly, in one test, in one process. ``-n auto``
(the mandated run mode — see ``tests/AGENTS.md`` § Scenario journey layer)
distributes individual test items across xdist workers, so a module-level
runtime counter shared across separate test *functions* cannot be trusted
to observe all four run before it is itself collected. Composing the four
legs' own assertion functions directly sidesteps that entirely: a
``skip``/``xfail`` decorator on an individual leg's test wrapper does not
touch the underlying function the composite calls, so a silently skipped
leg still cannot make this module pass.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from click.testing import CliRunner

from nexus.commands.rdr import rdr
from nexus.tables.check import BLOCKING_CODES, CLOSED_BY_ESCAPE, OVERLAP, check_table, exit_code, groups_of
from nexus.tables.load import load_packaged_table, load_table

_RDR_BODY = """## Problem Statement

Some prose.

## Decision

A decision.
"""


def _write_rdr(rdr_dir: Path, num: int, status: str, extra_fm: str = "") -> Path:
    """Same shape as tests/test_rdr_set_status.py's ``_write_rdr`` — kept
    local rather than imported so this module stands alone as the RDR's
    acceptance-criterion record, not a re-export of another suite's
    fixtures."""
    fm = (
        "---\n"
        f'title: "RDR-{num:03d} MVV Example"\n'
        f"id: RDR-{num:03d}\n"
        "type: Architecture\n"
        f"status: {status}\n"
        "priority: high\n"
        "created: 2026-06-22\n"
        f"{extra_fm}"
        "---\n\n"
    )
    rdr_dir.mkdir(parents=True, exist_ok=True)
    p = rdr_dir / f"rdr-{num:03d}-mvv-example.md"
    p.write_text(fm + _RDR_BODY, encoding="utf-8")
    return p


def _invoke_set_status(repo_root: Path, *args: str):
    return CliRunner().invoke(rdr, ["set-status", *args, "--root", str(repo_root)])


# ---------------------------------------------------------------------------
# Leg 1: the packaged lifecycle table lints clean.
# ---------------------------------------------------------------------------


def _leg1_lifecycle_table_lints_clean() -> None:
    table = load_packaged_table("rdr-lifecycle.toml")

    # Non-vacuity floor: prove check_table walked a real, structurally rich
    # table (six events, each with at least its own row plus an
    # "-otherwise" escape row) rather than trivially no-oping on an empty
    # or near-empty one.
    assert len(table.dimensions["event"].domain) == 6
    assert len(table.rows) >= 12
    assert len(groups_of(table)) >= 6

    findings = check_table(table)

    # "Lints clean" == zero BLOCKING findings, never zero findings: the
    # checker's no-bare-green principle means every escape-only group
    # earns a closed-by-escape advisory (non-blocking) rather than passing
    # silently. Assert the exact count, not merely "at least one" -- a
    # regression that silently drops or duplicates a "*-otherwise" row's
    # per-status expansion changes this number.
    assert exit_code(findings) == 0, f"packaged rdr-lifecycle table has blocking findings: {[f.to_json() for f in findings]}"
    assert not (BLOCKING_CODES & {f.code for f in findings})

    closed_by_escape = [f for f in findings if f.code == CLOSED_BY_ESCAPE]
    assert len(closed_by_escape) == 24, (
        f"expected exactly 24 closed-by-escape advisories (one per escape-only "
        f"group), got {len(closed_by_escape)}: {[f.to_json() for f in closed_by_escape]}"
    )
    assert {f.code for f in findings} == {CLOSED_BY_ESCAPE}, (
        f"unexpected finding code(s) on the packaged table: {[f.to_json() for f in findings]}"
    )


def test_leg1_lifecycle_table_lints_clean() -> None:
    _leg1_lifecycle_table_lints_clean()


# ---------------------------------------------------------------------------
# Leg 2: `set-status <id> closed` on a draft record refuses illegal-transition.
# ---------------------------------------------------------------------------


def _leg2_draft_to_closed_refuses_illegal_transition(root: Path) -> None:
    rdr_dir = root / "docs" / "rdr"
    f = _write_rdr(rdr_dir, 601, "draft")
    before = f.read_text()

    res = _invoke_set_status(root, "601", "closed")

    assert res.exit_code != 0, res.output
    assert "illegal-transition" in res.output
    assert f.read_text() == before  # untouched


def test_leg2_draft_to_closed_refuses_illegal_transition(tmp_path: Path) -> None:
    _leg2_draft_to_closed_refuses_illegal_transition(tmp_path)


# ---------------------------------------------------------------------------
# Leg 3: `set-status <id> closed` on an accepted record succeeds.
# ---------------------------------------------------------------------------


def _leg3_accepted_to_closed_succeeds(root: Path) -> None:
    rdr_dir = root / "docs" / "rdr"
    f = _write_rdr(rdr_dir, 602, "accepted", extra_fm="accepted_date: 2026-06-22\n")

    res = _invoke_set_status(root, "602", "closed", "--date", "2026-06-24")

    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: closed" in text
    assert "closed_date: 2026-06-24" in text


def test_leg3_accepted_to_closed_succeeds(tmp_path: Path) -> None:
    _leg3_accepted_to_closed_succeeds(tmp_path)


# ---------------------------------------------------------------------------
# Leg 4: a planted second `accept` row makes the checker report overlap.
# ---------------------------------------------------------------------------

_REAL_ACCEPT_ROW = (
    '[[row]]\nid = "accept"\nmatch = { status = "draft", event = "accept" }\n'
    'guard = { gate = "passed" }\nto = { status = "accepted" }\n'
)

_PLANTED_DUPLICATE_ACCEPT_ROW = (
    '\n[[row]]\nid = "accept-dup"\nmatch = { status = "draft", event = "accept" }\n'
    'guard = { gate = "passed" }\nto = { status = "accepted" }\n'
)


def _leg4_planted_duplicate_accept_row_overlap(root: Path) -> None:
    real_text = (
        importlib.resources.files("nexus.tables").joinpath("rdr-lifecycle.toml").read_bytes().decode("utf-8")
    )
    # Fail loud rather than silently planting into the wrong spot if the
    # real table's "accept" row is ever reshaped — this fixture is a copy
    # of the real table, never the real file itself (per RDR-201 MVV text).
    assert _REAL_ACCEPT_ROW in real_text, (
        "the real rdr-lifecycle.toml's 'accept' row no longer matches the "
        "expected text — update _REAL_ACCEPT_ROW/_PLANTED_DUPLICATE_ACCEPT_ROW"
    )
    planted_text = real_text.replace(
        _REAL_ACCEPT_ROW, _REAL_ACCEPT_ROW + _PLANTED_DUPLICATE_ACCEPT_ROW, 1
    )

    fixture_path = root / "rdr-lifecycle-planted-overlap.toml"
    fixture_path.write_text(planted_text, encoding="utf-8")

    table = load_table(fixture_path)
    findings = check_table(table)

    overlap_findings = [f for f in findings if f.code == OVERLAP]
    assert overlap_findings, f"expected an overlap finding, got: {[f.to_json() for f in findings]}"
    named_rows = {overlap_findings[0].detail["row_a"], overlap_findings[0].detail["row_b"]}
    assert named_rows == {"accept", "accept-dup"}
    assert exit_code(findings) == 1


def test_leg4_planted_duplicate_accept_row_overlap(tmp_path: Path) -> None:
    _leg4_planted_duplicate_accept_row_overlap(tmp_path)


# ---------------------------------------------------------------------------
# Leg 5: non-vacuity — all four legs ran, in one process, immune to xdist
# scheduling and to an individual leg's test wrapper being skip/xfail-marked.
# ---------------------------------------------------------------------------


def test_all_four_mvv_legs_ran_together(tmp_path_factory) -> None:
    _leg1_lifecycle_table_lints_clean()
    _leg2_draft_to_closed_refuses_illegal_transition(tmp_path_factory.mktemp("leg2"))
    _leg3_accepted_to_closed_succeeds(tmp_path_factory.mktemp("leg3"))
    _leg4_planted_duplicate_accept_row_overlap(tmp_path_factory.mktemp("leg4"))
