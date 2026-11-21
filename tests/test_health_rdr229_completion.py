# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): ``nx doctor``'s rows for the engine's boot-time completion check, from ``/v1/status``.

The engine reports the check in its own top-level field, ``rdr229_completion`` (``CompletionStatusJson.java``), not in
``boot_work``: the three gates wait by design across releases, so a ``boot_work`` entry would read as work in progress to
``nx upgrade`` and ``nx daemon status`` for as long as a release has no P3.4 index builder. The field is absent until the
engine's first read, and a client reads that as "not known", never as "ok". It carries counts and table and constraint
names only (``/v1/status`` is unauthenticated). The rows never turn ``nx doctor`` red: every waiting state clears by
itself or by a step the row names.

Fixtures: ``tests/fixtures/rdr229_completion_status/*.json`` are bodies the engine's own renderer produced (developer A's
tests); the validate-wait body below is the shape of ``validate_waits`` with the engine's ``what`` text. The two queries
the dangling and self-id rows carry are run against a real store in ``tests/test_rdr229_completion_queries.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import nexus.health as health
from nexus.health import HealthResult
from nexus.rdr229_completion_health import (
    DANGLING_LABEL,
    DANGLING_QUERY,
    NOT_KNOWN,
    SELF_ID_LABEL,
    SELF_ID_QUERY,
    SUMMARY_LABEL,
    check_rdr229_completion,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rdr229_completion_status"

#: Every key the engine's field carries; the module reads each of them, and a key the engine adds that is not here fails the shape test.
FIELD_KEYS = {
    "gate1", "gate2", "gate3", "gates_holding", "gates_total", "summary", "gate3_why", "labels_total", "labels_incomplete",
    "checks_missing", "superseded_by_waiting", "validate_waits", "dangling_tombstones",
}

SUPERSEDED = "catalog_collections_superseded_by_id_check"
VALIDATE_WHAT = (
    "waits: 3 orphan row(s) (a name with no registry row in its tenant) and 1 unstamped row(s) keep "
    "catalog_documents_physical_collection_id_check on catalog_documents NOT VALID. To finish: register each missing "
    "collection or delete the orphan rows, then run  SELECT nexus.rdr229_restamp('catalog_documents');  as the migrating "
    "role. It re-registers this table's stamp move once no orphan remains; the move stamps the rows and validates the CHECK."
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _by_label(rows: list[HealthResult]) -> dict[str, HealthResult]:
    return {r.label: r for r in rows}


def _never_red(rows: list[HealthResult]) -> None:
    assert [r.label for r in rows if not r.ok and not r.warn] == []


def test_the_fixtures_carry_exactly_the_keys_this_module_reads():
    """The engine's field and the module cannot drift: the bodies are the engine's own output."""
    names = sorted(p.stem for p in FIXTURES.glob("*.json"))
    assert len(names) >= 3, "non-vacuity: the fixtures are present"
    for name in names:
        field = _fixture(name)["rdr229_completion"]
        assert set(field) == FIELD_KEYS, name
        assert set(field["dangling_tombstones"]) == {"read", "registry_rows", "total", "by_content_type", "self_id"}, name
        assert set(field["dangling_tombstones"]["self_id"]) == {"total", "by_content_type"}, name


def test_gates_holding_and_the_summary_make_one_ok_row():
    rows = check_rdr229_completion(_fixture("all_stamps_done_gate3_waiting"))

    row = _by_label(rows)[SUMMARY_LABEL]
    assert row.ok and not row.warn
    assert row.detail.startswith("2 of 3 gates hold: gate 1 holds; gate 2 holds; gate 3 waits")
    assert "P3.4" in row.detail
    _never_red(rows)


def test_blocked_stamps_are_one_summary_row_and_no_warning():
    """Incomplete stamp labels are the Data moves rows' business (a blocked move says why there); the gate row counts them."""
    rows = check_rdr229_completion(_fixture("stamps_blocked"))

    assert _by_label(rows)[SUMMARY_LABEL].detail.startswith("0 of 3 gates hold: gate 1 waits (15 of 16 stamp moves incomplete)")
    assert [r.label for r in rows if r.warn] == []
    _never_red(rows)


def test_the_superseded_by_check_waiting_on_d2s_data_is_a_warning_that_says_what_clears_it():
    rows = check_rdr229_completion(_fixture("superseded_by_check_missing"))

    row = _by_label(rows)[f"RDR-229 CHECK {SUPERSEDED}"]
    assert row.warn and not row.ok
    assert "1 registry row(s)" in row.detail
    text = " ".join(row.fix_suggestions)
    assert "repair-catalog-collection-model-version" in text and "next boot" in text
    assert "nothing to run" in text.lower()
    _never_red(rows)


def test_any_other_missing_check_is_a_warning_that_names_the_changeset_file_and_the_re_add():
    body = _fixture("superseded_by_check_missing")
    body["rdr229_completion"]["checks_missing"] = [{
        "table": "chunks", "constraint": "chunks_collection_id_check",
        "what": "missing: chunks_collection_id_check is not on chunks; the switch is refused until it is restored "
                "(its changeset text is in vectors-068-collection-id-expand.xml)"}]

    rows = check_rdr229_completion(body)

    row = _by_label(rows)["RDR-229 CHECK chunks_collection_id_check"]
    assert row.warn and not row.ok
    text = " ".join(row.fix_suggestions)
    assert "vectors-068-collection-id-expand.xml" in text
    assert "rdr-229-p31-readd.sql" in text, "the generated re-add is the way back after the down-step"
    # why: neither file ships in the installed package, so a bare repo path would point at nothing on the user's machine
    assert "https://github.com/Hellblazer/nexus/blob/main/docs/runbooks/rdr-229-p31-readd.sql" in text
    assert "https://github.com/Hellblazer/nexus/blob/main/docs/runbooks/rdr-229-p31-rollback.md" in text
    assert "psql -X -v ON_ERROR_STOP=1 --single-transaction -f rdr-229-p31-readd.sql" in text, "the command that runs it"
    assert " docs/runbooks/" not in text, "no repo-relative path"
    _never_red(rows)


def test_a_validate_that_waits_on_orphans_says_what_the_engine_says_and_names_the_restamp_step():
    body = _fixture("all_stamps_done_gate3_waiting")
    body["rdr229_completion"]["validate_waits"] = [{
        "table": "catalog_documents", "constraints": ["catalog_documents_physical_collection_id_check"],
        "orphans": 3, "unstamped": 1, "counted": True, "what": VALIDATE_WHAT}]

    rows = check_rdr229_completion(body)

    row = _by_label(rows)["RDR-229 VALIDATE catalog_documents"]
    assert row.warn and not row.ok
    assert row.detail == VALIDATE_WHAT, "the engine's text is shown as it sent it"
    text = " ".join(row.fix_suggestions)
    assert "SELECT nexus.rdr229_restamp('catalog_documents');" in text
    assert "nexus_admin" in text
    assert "registered:" in text and "not registered:" in text
    assert "Register that collection, or remove the rows, then validate" not in text, "registering alone stamps nothing"
    assert "VALIDATE CONSTRAINT" not in text, "the stamp move validates; the operator does not"
    _never_red(rows)


def test_the_dangling_tombstone_preflight_and_the_self_id_rows_are_counts_by_content_type_with_a_query_that_lists_them():
    body = _fixture("superseded_by_check_missing")
    body["rdr229_completion"]["dangling_tombstones"] = {
        "read": True, "registry_rows": 171, "total": 5, "by_content_type": {"docs": 3, "unknown": 2},
        "self_id": {"total": 4, "by_content_type": {"docs": 3, "unknown": 1}}}

    rows = _by_label(check_rdr229_completion(body))

    pre = rows[DANGLING_LABEL]
    assert pre.ok and not pre.warn
    assert "5 dangling tombstone(s) of 171 registry rows" in pre.detail
    assert "docs: 3" in pre.detail and "unknown: 2" in pre.detail
    assert any(DANGLING_QUERY in f and "nexus_admin" in f for f in pre.fix_suggestions)
    own = rows[SELF_ID_LABEL]
    assert own.ok and not own.warn
    assert "4 tombstone(s)" in own.detail and "docs: 3" in own.detail and "unknown: 1" in own.detail
    assert any(SELF_ID_QUERY in f and "nexus_admin" in f for f in own.fix_suggestions)


def test_a_clean_preflight_says_none_and_carries_no_query_and_an_unread_one_says_not_read():
    clean = _by_label(check_rdr229_completion(_fixture("all_stamps_done_gate3_waiting")))
    assert "no dangling tombstone" in clean[DANGLING_LABEL].detail and not clean[DANGLING_LABEL].fix_suggestions
    assert "no self-id" in clean[SELF_ID_LABEL].detail and not clean[SELF_ID_LABEL].fix_suggestions

    body = _fixture("all_stamps_done_gate3_waiting")
    body["rdr229_completion"]["dangling_tombstones"] = {
        "read": False, "registry_rows": 0, "total": 0, "by_content_type": {}, "self_id": {"total": 0, "by_content_type": {}}}
    unread = _by_label(check_rdr229_completion(body))
    assert "not read" in unread[DANGLING_LABEL].detail
    assert unread[DANGLING_LABEL].ok and SELF_ID_LABEL not in unread


def test_the_two_queries_are_read_only_and_name_no_other_table():
    for query in (DANGLING_QUERY, SELF_ID_QUERY):
        low = query.lower()
        assert low.startswith("select ") and "nexus.catalog_collections" in low
        for word in ("insert", "update", "delete", "drop", "alter", "truncate"):
            assert word not in low.split(), word


def test_an_absent_field_is_not_known_never_ok_and_old_boot_work_entries_are_ignored():
    """Absent means the engine has not read yet, or predates the check: "not known", not "nothing waiting"."""
    for status in ({"boot_work": []}, {}, {"rdr229_completion": None}, {"rdr229_completion": "nope"}):
        rows = check_rdr229_completion(status)
        assert len(rows) == 1 and rows[0].label == SUMMARY_LABEL and rows[0].ok and not rows[0].warn
        assert rows[0].detail == NOT_KNOWN
    old = {"boot_work": [{"id": "rdr229-completion", "what": "gate 1 waits", "unit": "gates"}]}
    assert check_rdr229_completion(old)[0].detail == NOT_KNOWN


def test_not_applicable_when_the_engine_cannot_be_read():
    rows = check_rdr229_completion(None)
    assert len(rows) == 1 and rows[0].ok and rows[0].detail.startswith("not applicable")


def test_a_field_with_a_malformed_part_still_gives_the_rows_it_can():
    body = _fixture("superseded_by_check_missing")
    body["rdr229_completion"]["checks_missing"] = ["nope", {"table": "t"}]
    body["rdr229_completion"]["validate_waits"] = [None]
    body["rdr229_completion"]["dangling_tombstones"] = []

    rows = check_rdr229_completion(body)

    assert SUMMARY_LABEL in _by_label(rows)
    _never_red(rows)


def test_the_sweep_has_the_row_and_hands_it_the_status_it_already_fetched():
    rows = health._check_rdr229_completion(_fixture("all_stamps_done_gate3_waiting"))

    assert SUMMARY_LABEL in [r.label for r in rows]
    assert "_check_rdr229_completion" in Path(health.__file__).read_text(encoding="utf-8")
