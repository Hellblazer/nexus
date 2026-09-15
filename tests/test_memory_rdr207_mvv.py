# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-207 Minimum Viable Validation (bead nexus-l3yuc.11): quarantine,
rollup-mark-reap, and the forced-failure leg, in one module.

DEFAULT SUITE, on purpose: no integration marker. The ``db`` fixture runs on
``t2_service_env``, the engine substrate ``_pin_t2_substrate`` pulls into
every test, with one freshly minted tenant per test. On GitHub Actions that
fixture skips unless NX_T2_SUBSTRATE_EXPECTED=1 and the jar is present; a
skip is not a pass. The summarizer is a stub, never a live LLM call.
"""
from __future__ import annotations

from nexus.db.t2 import T2Database
from nexus.memory_rollup import RollupGroup, rollup
from tests._t2_fixture_ops import backdate_memory

PROJECT = "mvv-207"
#: A content token no other row carries, so a search hit can only be this row.
TOKEN = "quarantinemvvtoken"


def _quarantine(db: T2Database, title: str) -> int:
    """Put a row with ttl=1, backdate it past its TTL, run expire."""
    row_id = db.put(project=PROJECT, title=title, content=f"{TOKEN} {title}", ttl=1)
    backdate_memory(db, PROJECT, title, days=2)
    swept = db.memory.expire()
    assert row_id in swept.quarantined_ids, "expire must quarantine the row"
    assert row_id not in swept.deleted_ids, "and must not delete it"
    return row_id


def _naming(group: RollupGroup) -> str:
    return "Summary naming " + ", ".join(group.titles)


def _omitting(group: RollupGroup) -> str:
    return "Summary naming nothing"


def test_leg_1_quarantine_hides_the_row_and_restore_brings_it_back_permanent(db: T2Database) -> None:
    row_id = _quarantine(db, "leg1.md")

    assert db.get(id=row_id) is None, "absent from get by id"
    assert db.get(project=PROJECT, title="leg1.md") is None, "absent from get by title"
    assert db.search(TOKEN, project=PROJECT) == [], "absent from search"
    assert all(e["id"] != row_id for e in db.list_entries(project=PROJECT)), "absent from list"
    [quarantined] = db.memory.list_quarantined(project=PROJECT)
    assert quarantined["id"] == row_id and quarantined["quarantined_at"], "present in the quarantined list"

    assert db.memory.restore(row_id) is True
    back = db.get(id=row_id)
    assert back is not None and back["ttl"] is None, "restored as permanent"
    assert db.memory.list_quarantined(project=PROJECT) == [], "neither stamp remains"


def test_leg_2_a_summary_naming_the_title_marks_the_row_and_reap_deletes_it(db: T2Database) -> None:
    row_id = _quarantine(db, "leg2.md")

    [outcome] = rollup(db.memory, PROJECT, _naming, model="stub")

    assert outcome.status == "marked"
    assert db.memory.reap() == [row_id], "reap deletes the marked row"
    assert db.memory.list_quarantined(project=PROJECT) == []
    [summary] = db.memory.list_summaries(project=PROJECT)
    assert summary["source_ids"] == [row_id], "the summaries list carries its id"


def test_leg_3_a_summary_that_omits_the_title_marks_nothing_and_reap_deletes_nothing(
    db: T2Database,
) -> None:
    row_id = _quarantine(db, "leg3.md")

    [outcome] = rollup(db.memory, PROJECT, _omitting, model="stub")

    assert outcome.status == "check_failed" and outcome.missing_titles == ("leg3.md",)
    assert db.memory.list_summaries(project=PROJECT) == []
    assert db.memory.reap() == [], "reap deletes nothing"
    [still] = db.memory.list_quarantined(project=PROJECT)
    assert still["id"] == row_id and still["rolled_up_at"] is None
