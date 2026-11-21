# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): ``nx doctor``'s rows for the engine's boot-time completion check, from canned
``/v1/status`` bodies.

The engine reports three kinds of ``boot_work`` entry (``CompletionBootWork.java``): ``rdr229-completion`` (where the
three gates stand), ``rdr229-check:<constraint>`` (a listed CHECK that is missing; the superseded_by one waits on D2's
data and says how many registry rows hold it back) and ``rdr229-validate:<table>`` (a table whose stamp is done and whose
CHECK is still NOT VALID because orphan rows violate it). The rows never turn ``nx doctor`` red: the gates wait by
design across releases, and every waiting state clears by itself or by an operator step the row names.
"""
from __future__ import annotations

import re
from pathlib import Path

import nexus.health as health
from nexus.health import HealthResult
from nexus.rdr229_completion_health import (
    CHECK_PREFIX,
    NOT_APPLICABLE_NOT_REPORTED,
    SUMMARY_ID,
    SUMMARY_LABEL,
    VALIDATE_PREFIX,
    check_rdr229_completion,
)

JAVA = (
    Path(__file__).resolve().parents[1]
    / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "datamove" / "CompletionBootWork.java"
)

SUMMARY_WHAT = (
    "gate 1 waits (15 of 16 stamp moves incomplete); gate 2 waits on gate 1; gate 3 waits: gate 2 does not hold"
)
SUPERSEDED = "catalog_collections_superseded_by_id_check"
SUPERSEDED_WHAT = (
    f"waits: {SUPERSEDED} is not on catalog_collections yet; 2 registry row(s) with a blank model_version and no "
    "superseded_by_id hold its changeset back (D2's repair move or the superseded_by stamp clears them, then the next "
    "boot adds it); the switch is refused until then"
)
VALIDATE_WHAT = (
    "waits: 3 orphan row(s) (a name with no registry row in its tenant) and 1 unstamped row(s) keep "
    "catalog_documents_physical_collection_id_check on catalog_documents NOT VALID; its VALIDATE waits instead of failing a boot"
)


def _entry(entry_id: str, what: str, done: int | None = None, total: int | None = None) -> dict:
    return {
        "id": entry_id, "what": what, "started_at": "2026-11-21T01:00:00Z", "done": done, "total": total,
        "unit": "gates", "safe_to_stop": True, "on_stop": "the check is read again on the next boot; nothing is lost",
        "step": None,
    }


def _summary(what: str = SUMMARY_WHAT, done: int = 0) -> dict:
    return _entry(SUMMARY_ID, what, done, 3)


def _never_red(rows: list[HealthResult]) -> None:
    assert [r.label for r in rows if not r.ok and not r.warn] == []


def test_the_summary_entry_is_one_ok_row_with_the_gates_in_its_detail():
    rows = check_rdr229_completion({"boot_work": [_summary()]})

    assert [r.label for r in rows] == [SUMMARY_LABEL]
    assert rows[0].ok and not rows[0].warn
    assert rows[0].detail.startswith("0 of 3 gates hold: gate 1 waits (15 of 16 stamp moves incomplete)")
    assert "gate 3 waits" in rows[0].detail


def test_the_summary_counts_the_gates_that_hold():
    rows = check_rdr229_completion({"boot_work": [_summary("gate 1 holds; gate 2 holds; gate 3 waits: P3.4", done=2)]})

    assert rows[0].detail.startswith("2 of 3 gates hold: gate 1 holds; gate 2 holds")


def test_the_superseded_by_check_waiting_on_d2s_data_is_a_warning_that_says_what_clears_it():
    status = {"boot_work": [_summary(), _entry(CHECK_PREFIX + SUPERSEDED, SUPERSEDED_WHAT)]}

    rows = check_rdr229_completion(status)

    by_label = {r.label: r for r in rows}
    row = by_label[f"RDR-229 CHECK {SUPERSEDED}"]
    assert row.warn and not row.ok
    assert "2 registry row(s)" in row.detail
    text = " ".join(row.fix_suggestions)
    assert "repair-catalog-collection-model-version" in text
    assert "next boot" in text
    assert "nothing to run" in text.lower()
    _never_red(rows)


def test_any_other_missing_check_is_a_warning_that_names_the_changeset_file():
    status = {"boot_work": [_entry(CHECK_PREFIX + "chunks_collection_id_check", (
        "missing: chunks_collection_id_check is not on chunks; the switch is refused until it is restored "
        "(its changeset text is in vectors-068-collection-id-expand.xml)"))]}

    rows = check_rdr229_completion(status)

    row = next(r for r in rows if r.label == "RDR-229 CHECK chunks_collection_id_check")
    assert row.warn and not row.ok
    assert "vectors-068-collection-id-expand.xml" in " ".join(row.fix_suggestions)
    _never_red(rows)


def test_a_validate_that_waits_on_orphans_names_the_table_the_counts_and_the_operator_step():
    status = {"boot_work": [_summary(), _entry(VALIDATE_PREFIX + "catalog_documents", VALIDATE_WHAT)]}

    rows = check_rdr229_completion(status)

    row = next(r for r in rows if r.label == "RDR-229 VALIDATE catalog_documents")
    assert row.warn and not row.ok
    assert "3 orphan row(s)" in row.detail and "1 unstamped" in row.detail
    text = " ".join(row.fix_suggestions)
    assert "Register that collection, or remove the rows" in text
    assert (
        "ALTER TABLE nexus.catalog_documents VALIDATE CONSTRAINT catalog_documents_physical_collection_id_check" in text
    )
    assert "nexus_admin" in text
    _never_red(rows)


def test_no_row_names_a_collection_or_a_tenant_the_engine_never_sent_one():
    status = {"boot_work": [_summary(), _entry(VALIDATE_PREFIX + "catalog_documents", VALIDATE_WHAT)]}

    for row in check_rdr229_completion(status):
        assert "ghost" not in row.detail


def test_entries_of_other_work_are_ignored():
    status = {"boot_work": [
        {"id": "data-move:rdr229-stamp-topics", "what": "blocked: x", "unit": "chunks"},
        {"id": "pci-reconcile", "what": "building search indexes", "unit": "indexes"},
    ]}

    rows = check_rdr229_completion(status)

    assert [r.label for r in rows] == [SUMMARY_LABEL]
    assert rows[0].ok and NOT_APPLICABLE_NOT_REPORTED in rows[0].detail


def test_not_applicable_when_the_engine_cannot_be_read_or_predates_boot_work():
    for status in (None, {}, {"boot_work": "nope"}):
        rows = check_rdr229_completion(status)
        assert len(rows) == 1 and rows[0].ok and not rows[0].warn
        assert rows[0].detail.startswith("not applicable")


def test_the_sweep_has_the_row_and_hands_it_the_status_it_already_fetched():
    status = {"boot_work": [_summary()]}

    assert [r.label for r in health._check_rdr229_completion(status)] == [SUMMARY_LABEL]
    assert "_check_rdr229_completion" in Path(health.__file__).read_text(encoding="utf-8")


def test_the_entry_ids_are_the_ones_the_engine_sends():
    """The Java constants and this module's cannot drift: both are read from the source."""
    text = JAVA.read_text(encoding="utf-8")

    def constant(name: str) -> str:
        m = re.search(rf'String {name} = "([^"]+)"', text)
        assert m, f"{name} not found in CompletionBootWork.java"
        return m.group(1)

    assert constant("SUMMARY_ID") == SUMMARY_ID
    assert constant("CHECK_PREFIX") == CHECK_PREFIX
    assert constant("VALIDATE_PREFIX") == VALIDATE_PREFIX
