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

Deviation from the literal MVV phrasing, recorded here rather than left
silent: leg 1 does not assert a "closed-by-escape" advisory. Measured
directly (``check_table(load_packaged_table("rdr-lifecycle.toml"))``): the
real packaged table currently produces ZERO findings of any kind, blocking
or advisory. This is correct given the checker's own design — the table is
``kind = "state-machine"``, and every one of its bare ``escape = true``
rows lands in a zero-guard-dimension group; ``check.py``'s
``_check_group`` deliberately treats a zero-dimension group as "legitimate
and silent ON COVERAGE" for a state machine (only a ``decision-table``
gets the ``no-participating-dimension`` advisory there), and
``closed-by-escape`` itself only ever fires from within ``_check_coverage``,
which a zero-dimension group never reaches. So today's table has no group
where an escape row closes a gap left by ordinary rows — asserting one
would be a false claim about the real table, not a stricter test. Leg 1
instead asserts the two things RDR-201's MVV text actually states ("lints
clean") plus a non-vacuity floor on the table's own structure, so a
checker that trivially no-ops on an empty/near-empty table cannot pass this
leg by accident.

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
from nexus.tables.check import BLOCKING_CODES, OVERLAP, check_table, exit_code, groups_of
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

    assert findings == [], f"packaged rdr-lifecycle table is not clean: {[f.to_json() for f in findings]}"
    assert exit_code(findings) == 0
    assert not (BLOCKING_CODES & {f.code for f in findings})


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
