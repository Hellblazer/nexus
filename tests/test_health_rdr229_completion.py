# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): ``nx doctor``'s rows for the engine's boot-time completion check, from ``/v1/status``.

The engine reports the check in its own top-level field, ``rdr229_completion`` (``CompletionStatusJson.java``), not in
``boot_work``: the three gates wait by design across releases, so a ``boot_work`` entry would read as work in progress to
``nx upgrade`` and ``nx daemon status`` for as long as a release has no P3.4 index builder. The field is absent until the
engine's first read, and a client reads that as "not known", never as "ok". It carries gate bookkeeping, table and constraint
names and operator text, and NO number that describes tenants (``/v1/status`` is unauthenticated; linda's ruling on review N2).
The tenant's own counts come from the authenticated ``GET /v1/catalog/rdr229_completion_counts``, which the doctor reads through
the catalog reader (injected here as ``counts_reader``). The rows never turn ``nx doctor`` red: every waiting state clears by
itself or by a step the row names.

Fixtures: ``tests/fixtures/rdr229_completion_status/*.json`` are bodies the engine's own renderer produced (developer A's
tests); the validate-wait body below is the shape of ``validate_waits`` with the engine's ``what`` text. The two queries
the dangling and self-id rows carry are run against a real store in ``tests/test_rdr229_completion_queries.py``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import nexus.health as health
import nexus.rdr229_completion_health as rdr229_completion_health
from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable
from nexus.health import HealthResult
from nexus.rdr229_completion_health import (
    DANGLING_LABEL,
    DANGLING_QUERY,
    ID_INDEX_LABEL,
    LATCH_POSTURE_LABEL,
    NOT_KNOWN,
    SELF_ID_LABEL,
    SELF_ID_QUERY,
    SUMMARY_LABEL,
    check_rdr229_completion,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rdr229_completion_status"

#: Every key the engine's field carries; the module reads each of them, and a key the engine adds that is not here fails the shape test.
FIELD_KEYS = {
    "gate1", "gate2", "gate3", "gates_holding", "gates_total", "summary", "gate3_why", "gate3_latched", "id_index_invalid",
    "gate3_latch_pci_off", "labels_total", "labels_incomplete", "checks_missing", "validate_waits", "rename_missing",
}

#: A number in front of what it counts: the shape of every tenant count the old status text carried.
TENANT_COUNT_TEXT = re.compile(r"\d+\s+(orphan|unstamped|registry|collection|tombstone|live|row)", re.I)

#: The authenticated route's body for ONE tenant (``CompletionTenantCounts``): that tenant's own counts, never a total.
COUNTS = {
    "registry_rows": 171, "live_collections": 160, "superseded_by_waiting": 1,
    "dangling_tombstones": {"total": 5, "by_content_type": {"docs": 3, "unknown": 2},
                            "self_id": {"total": 4, "by_content_type": {"docs": 3, "unknown": 1}}},
    "tables": [{"table": "catalog_documents", "orphans": 3, "unstamped": 1}],
}
NO_COUNTS = {
    "registry_rows": 4, "live_collections": 4, "superseded_by_waiting": 0,
    "dangling_tombstones": {"total": 0, "by_content_type": {}, "self_id": {"total": 0, "by_content_type": {}}}, "tables": [],
}


def _reader(body):
    """A counts reader that answers *body*, or raises it when it is an exception."""
    def read():
        if isinstance(body, Exception):
            raise body
        return body
    return read


def _check(status, counts=NO_COUNTS):
    return check_rdr229_completion(status, counts_reader=_reader(counts))

SUPERSEDED = "catalog_collections_superseded_by_id_check"
VALIDATE_WHAT = (
    "waits: catalog_documents_physical_collection_id_check on catalog_documents is NOT VALID while rows remain that it refuses: "
    "orphan rows (a collection name no registry row of their tenant carries) and rows the stamp has not given an id yet. An orphan row "
    "is write-frozen: any UPDATE of it fails with SQLSTATE 23514 until its collection is registered or the row is deleted. To finish: "
    "register each missing collection or delete the orphan rows, if there are any, then run  SELECT nexus.rdr229_restamp("
    "'catalog_documents');  as the migrating role. It re-registers this table's stamp move once no orphan remains; the move stamps "
    "the rows and validates the CHECK. With no orphan row, that restamp step alone finishes it."
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
        assert "dangling_tombstones" not in field and "superseded_by_waiting" not in field, name
        for w in field["validate_waits"]:
            assert set(w) == {"table", "constraints", "what"}, name
        for text in [field["summary"], field["gate3_why"], *(c["what"] for c in field["checks_missing"]),
                     *(w["what"] for w in field["validate_waits"]), *(l["why"] for l in field["labels_incomplete"])]:
            assert not TENANT_COUNT_TEXT.search(text), f"{name}: a tenant count in the unauthenticated text: {text}"


def test_gates_holding_and_the_summary_make_one_ok_row():
    rows = _check(_fixture("all_stamps_done_gate3_waiting"))

    row = _by_label(rows)[SUMMARY_LABEL]
    assert row.ok and not row.warn
    assert row.detail.startswith("2 of 3 gates hold: gate 1 holds; gate 2 holds; gate 3 waits")
    assert "P3.4" in row.detail
    _never_red(rows)


def test_an_invalid_id_index_is_its_own_row_a_warning_once_gate3_has_latched_and_information_before():
    base = _fixture("all_stamps_done_gate3_waiting")["rdr229_completion"]
    quiet = {"rdr229_completion": {**base, "id_index_invalid": False, "gate3_latched": True}}
    assert ID_INDEX_LABEL not in _by_label(_check(quiet)), "non-vacuity: no row while no id index is INVALID"

    latched = _by_label(_check({"rdr229_completion": {**base, "id_index_invalid": True, "gate3_latched": True}}))[ID_INDEX_LABEL]
    assert latched.warn and not latched.ok
    assert "latched" in latched.detail and "no index" in latched.detail

    early = _by_label(_check({"rdr229_completion": {**base, "id_index_invalid": True, "gate3_latched": False}}))[ID_INDEX_LABEL]
    assert early.ok and not early.warn
    assert "not latched" in early.detail


def test_a_latch_written_while_pci_was_off_is_an_information_row_that_says_the_id_indexes_are_built_first():
    base = _fixture("all_stamps_done_gate3_waiting")["rdr229_completion"]
    assert LATCH_POSTURE_LABEL not in _by_label(_check({"rdr229_completion": {**base, "gate3_latch_pci_off": False}})), "non-vacuity"

    rows = _check({"rdr229_completion": {**base, "gate3_latched": True, "gate3_latch_pci_off": True}})
    row = _by_label(rows)[LATCH_POSTURE_LABEL]
    assert row.ok and not row.warn
    assert "NX_SEARCH_PCI was off" in row.detail and "builds the id indexes first" in row.detail
    _never_red(rows)


def test_blocked_stamps_are_one_summary_row_and_no_warning():
    """Incomplete stamp labels are the Data moves rows' business (a blocked move says why there); the gate row counts them."""
    rows = _check(_fixture("stamps_blocked"))

    assert _by_label(rows)[SUMMARY_LABEL].detail.startswith("0 of 3 gates hold: gate 1 waits (15 of 16 stamp moves incomplete)")
    assert [r.label for r in rows if r.warn] == []
    _never_red(rows)


def test_the_superseded_by_check_waiting_on_d2s_data_is_a_warning_that_says_what_clears_it():
    rows = _check(_fixture("superseded_by_check_missing"), COUNTS)

    row = _by_label(rows)[f"RDR-229 CHECK {SUPERSEDED}"]
    assert row.warn and not row.ok
    assert "registry rows with a blank model_version and no superseded_by_id hold its changeset back" in row.detail
    assert "For this tenant, 1 registry row(s) hold it back." in row.detail, "the count is the caller's own, from the authenticated route"
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

    rows = _check(body)

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


def _validate_body() -> dict:
    body = _fixture("all_stamps_done_gate3_waiting")
    body["rdr229_completion"]["validate_waits"] = [{
        "table": "catalog_documents", "constraints": ["catalog_documents_physical_collection_id_check"], "what": VALIDATE_WHAT}]
    return body


def test_a_validate_with_orphan_rows_says_the_orphans_are_write_frozen_and_names_register_delete_and_the_restamp_step():
    """Shape one (review S1 residual (a)): this tenant has orphan rows."""
    rows = _check(_validate_body(), COUNTS)

    row = _by_label(rows)["RDR-229 VALIDATE catalog_documents"]
    assert row.warn and not row.ok
    assert "3 orphan row(s)" in row.detail and "1 unstamped row(s)" in row.detail, "the caller's own counts, from the authenticated route"
    assert "write-frozen" in row.detail and "SQLSTATE 23514" in row.detail
    assert "until its collection is registered or the row is deleted" in row.detail
    text = " ".join(row.fix_suggestions)
    assert "register each missing collection or delete the orphan rows" in text
    assert "SELECT nexus.rdr229_restamp('catalog_documents');" in text
    assert "nexus_admin" in text
    assert "registered:" in text and "not registered:" in text
    assert "Register that collection, or remove the rows, then validate" not in text, "registering alone stamps nothing"
    assert "VALIDATE CONSTRAINT" not in text, "the stamp move validates; the operator does not"
    _never_red(rows)


def test_a_validate_with_no_orphan_row_and_only_unstamped_rows_is_finished_by_the_restamp_alone():
    """Shape two (review S1 residual (b), critique N4): the orphan count is 0, so there is nothing to register or delete."""
    counts = {**NO_COUNTS, "tables": [{"table": "catalog_documents", "orphans": 0, "unstamped": 2}]}

    rows = _check(_validate_body(), counts)

    row = _by_label(rows)["RDR-229 VALIDATE catalog_documents"]
    assert row.warn and not row.ok
    assert "0 orphan row(s)" in row.detail and "2 unstamped row(s)" in row.detail
    everything = row.detail + " " + " ".join(row.fix_suggestions)
    assert "register each missing collection" not in everything
    assert "delete the orphan rows" not in everything
    assert "write-frozen" not in everything, "no orphan row here: nothing is frozen"
    text = " ".join(row.fix_suggestions)
    assert "SELECT nexus.rdr229_restamp('catalog_documents');" in text
    assert "alone" in text and "nexus_admin" in text
    _never_red(rows)


def test_a_validate_whose_counts_cannot_be_read_shows_the_engines_text_as_sent_with_the_conditional_advice():
    rows = _check(_validate_body(), EngineRouteUnavailable("/rdr229_completion_counts", "f", EngineRouteUnavailable.UNSUPPORTED))

    row = _by_label(rows)["RDR-229 VALIDATE catalog_documents"]
    assert row.detail.startswith(VALIDATE_WHAT), "the engine's text is shown as it sent it"
    assert "counts not read" in row.detail
    text = " ".join(row.fix_suggestions)
    assert "if there are any" in text or "With no orphan row" in text
    assert "SELECT nexus.rdr229_restamp('catalog_documents');" in text
    _never_red(rows)


def test_the_dangling_tombstone_preflight_and_the_self_id_rows_are_counts_by_content_type_with_a_query_that_lists_them():
    rows = _by_label(_check(_fixture("superseded_by_check_missing"), COUNTS))

    pre = rows[DANGLING_LABEL]
    assert pre.ok and not pre.warn
    assert "5 dangling tombstone(s) of 171 registry rows of this tenant" in pre.detail
    assert "docs: 3" in pre.detail and "unknown: 2" in pre.detail
    assert any(DANGLING_QUERY in f and "nexus_admin" in f for f in pre.fix_suggestions)
    own = rows[SELF_ID_LABEL]
    assert own.ok and not own.warn
    assert "4 tombstone(s)" in own.detail and "docs: 3" in own.detail and "unknown: 1" in own.detail
    assert any(SELF_ID_QUERY in f and "nexus_admin" in f for f in own.fix_suggestions)


def test_a_clean_preflight_says_none_and_carries_no_query_and_an_unread_one_says_why_it_was_not_read():
    clean = _by_label(_check(_fixture("all_stamps_done_gate3_waiting"), NO_COUNTS))
    assert "no dangling tombstone" in clean[DANGLING_LABEL].detail and not clean[DANGLING_LABEL].fix_suggestions
    assert "no self-id" in clean[SELF_ID_LABEL].detail and not clean[SELF_ID_LABEL].fix_suggestions

    unread = _by_label(_check(
        _fixture("all_stamps_done_gate3_waiting"),
        EngineRouteUnavailable("/rdr229_completion_counts", "catalog_rdr229_completion_counts_supported", EngineRouteUnavailable.UNSUPPORTED)))
    assert "not read" in unread[DANGLING_LABEL].detail and "GET /v1/catalog/rdr229_completion_counts" in unread[DANGLING_LABEL].detail
    assert unread[DANGLING_LABEL].ok and SELF_ID_LABEL not in unread


def test_the_unauthenticated_status_alone_never_gives_a_row_a_tenant_count():
    """With no counts read, no row's text carries a number in front of orphans, rows, registry rows or tombstones."""
    for name in ("stamps_blocked", "superseded_by_check_missing", "all_stamps_done_gate3_waiting"):
        rows = _check(_fixture(name), EngineRouteUnavailable("/r", "f", EngineRouteUnavailable.FAILED))
        for r in rows:
            assert not TENANT_COUNT_TEXT.search(r.detail), f"{name}: {r.label}: {r.detail}"


def test_the_two_queries_are_read_only_and_name_no_other_table():
    for query in (DANGLING_QUERY, SELF_ID_QUERY):
        low = query.lower()
        assert low.startswith("select ") and "nexus.catalog_collections" in low
        for word in ("insert", "update", "delete", "drop", "alter", "truncate"):
            assert word not in low.split(), word


def test_an_absent_field_is_not_known_never_ok_and_old_boot_work_entries_are_ignored():
    """Absent means the engine has not read yet, or predates the check: "not known", not "nothing waiting"."""
    for status in ({"boot_work": []}, {}, {"rdr229_completion": None}, {"rdr229_completion": "nope"}):
        rows = _check(status)
        assert len(rows) == 1 and rows[0].label == SUMMARY_LABEL and rows[0].ok and not rows[0].warn
        assert rows[0].detail == NOT_KNOWN
    old = {"boot_work": [{"id": "rdr229-completion", "what": "gate 1 waits", "unit": "gates"}]}
    assert _check(old)[0].detail == NOT_KNOWN


def test_not_applicable_when_the_engine_cannot_be_read():
    rows = _check(None)
    assert len(rows) == 1 and rows[0].ok and rows[0].detail.startswith("not applicable")


def test_a_field_with_a_malformed_part_still_gives_the_rows_it_can():
    body = _fixture("superseded_by_check_missing")
    body["rdr229_completion"]["checks_missing"] = ["nope", {"table": "t"}]
    body["rdr229_completion"]["validate_waits"] = [None]

    rows = _check(body, {"tables": "nope", "dangling_tombstones": []})

    assert SUMMARY_LABEL in _by_label(rows)
    _never_red(rows)


def test_the_sweep_has_the_row_and_hands_it_the_status_it_already_fetched(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rdr229_completion_health, "_default_counts_reader", lambda: _reader(NO_COUNTS))
    rows = health._check_rdr229_completion(_fixture("all_stamps_done_gate3_waiting"))

    assert SUMMARY_LABEL in [r.label for r in rows]
    assert "_check_rdr229_completion" in Path(health.__file__).read_text(encoding="utf-8")


def test_a_rename_the_engine_refuses_is_one_informational_row_naming_what_it_waits_for():
    """RDR-229 P3.7: ``rename_missing`` other than ``none`` is one ok row naming the wait; ``none`` and an older engine add none."""
    h = rdr229_completion_health
    base = _fixture("restart_needed")
    rows = h.check_rdr229_completion(base, counts_reader=lambda: {})
    rename = [r for r in rows if r.label == h.RENAME_LABEL]
    assert base["rdr229_completion"]["rename_missing"] == "restart", "non-vacuity: the fixture waits on the restart"
    assert len(rename) == 1 and rename[0].ok and "restart" in rename[0].detail
    for value in ("none", None):
        field = dict(base["rdr229_completion"])
        if value is None:
            field.pop("rename_missing")
        else:
            field["rename_missing"] = value
        assert not [r for r in h.check_rdr229_completion({**base, "rdr229_completion": field}, counts_reader=lambda: {})
                    if r.label == h.RENAME_LABEL]
