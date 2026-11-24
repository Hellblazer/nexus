# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.8 (nexus-p2m1x.28): ``nx doctor`` names a live collection tuple held by more than one registry row.

The engine's partial UNIQUE index on ``catalog_collections`` (tenant_id, content_type, owner_id, embedding_model,
model_version) WHERE superseded_by = '' AND lifecycle_state <> 'quarantine' is created by a changeset that WAITS for the
re-own move and for no duplicate to remain (``collections-001-2``). A duplicate holds it back, quietly, at every boot;
this row says which tuple and which collections, so an operator sees why. The rows come from the catalog's collection
listing (the caller's own tenant, authenticated): ``GET /v1/status`` is unauthenticated and carries no tenant text.

A warning while a duplicate exists, and no row at all when there is none (the index exists, or only the move is
pending, which the data-move rows already show).
"""
from __future__ import annotations

from unittest.mock import patch

import nexus.health as health
from nexus.collection_tuple_health import (
    INDEX_WAIT_LABEL,
    LABEL,
    REOWN_MOVE_ID,
    REOWN_MOVE_LABEL,
    check_collection_tuples,
    duplicate_tuples,
)

_MODEL = "bge-base-en-v15-768"


def _row(name: str, *, owner: str = "owner-1", content_type: str = "docs", model: str = _MODEL,
         version: str = "", lifecycle: str = "live", superseded_by: str = "") -> dict:
    return {
        "name": name, "content_type": content_type, "owner_id": owner, "embedding_model": model,
        "model_version": version, "lifecycle_state": lifecycle, "superseded_by": superseded_by,
    }


def test_two_live_rows_on_one_tuple_are_one_warning_naming_the_tuple_and_both_collections() -> None:
    rows = check_collection_tuples([_row("docs__a"), _row("docs__b"), _row("docs__other", owner="owner-2")])

    assert len(rows) == 1
    r = rows[0]
    assert r.label == LABEL
    assert r.ok is False and r.warn is True and r.fatal is False
    assert "docs__a" in r.detail and "docs__b" in r.detail
    assert "docs__other" not in r.detail
    assert "owner-1" in r.detail and _MODEL in r.detail
    assert any("collections-001-2" in s or "unique index" in s.lower() for s in r.fix_suggestions)


def test_no_duplicate_is_no_row() -> None:
    assert check_collection_tuples([_row("docs__a"), _row("docs__b", owner="owner-2")]) == []
    assert check_collection_tuples([]) == []


def test_what_the_index_predicate_leaves_out_is_not_a_duplicate() -> None:
    rows = [
        _row("docs__a"),
        _row("quarantine-docs__a", lifecycle="quarantine"),  # a quarantine sibling
        _row("docs__old", superseded_by="docs__a"),  # a superseded row
        _row("docs__v2", version="v2"),  # a different model_version (a same-model re-embed target)
    ]
    assert check_collection_tuples(rows) == []
    assert duplicate_tuples(rows) == []


def test_a_dormant_or_disputed_row_still_holds_the_tuple() -> None:
    (r,) = check_collection_tuples([_row("docs__a"), _row("docs__b", lifecycle="dormant"), _row("docs__c", lifecycle="disputed")])

    assert all(n in r.detail for n in ("docs__a", "docs__b", "docs__c"))


def test_a_row_without_lifecycle_or_supersession_keys_counts_as_live() -> None:
    bare = {"name": "docs__x", "content_type": "docs", "owner_id": "o", "embedding_model": _MODEL, "model_version": ""}
    twin = dict(bare, name="docs__y")

    assert [d.names for d in duplicate_tuples([bare, twin])] == [("docs__x", "docs__y")]


def test_each_duplicated_tuple_is_named_and_a_long_list_is_capped_with_a_count() -> None:
    rows = []
    for i in range(8):
        rows += [_row(f"docs__a{i}", owner=f"owner-{i}"), _row(f"docs__b{i}", owner=f"owner-{i}")]

    (r,) = check_collection_tuples(rows)

    assert "owner-0" in r.detail
    assert "3 more" in r.detail
    assert "8" in r.detail  # the total


def test_an_unreadable_registry_is_no_row_and_never_raises() -> None:
    with patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("no engine")):
        assert check_collection_tuples() == []

    class _Broken:
        def list_collections(self, *a, **k):
            raise RuntimeError("boom")

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_Broken()):
        assert check_collection_tuples() == []
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        assert check_collection_tuples() == []


def test_the_rows_come_from_the_reader_when_none_are_given() -> None:
    class _Reader:
        def list_collections(self, *a, **k):
            return [_row("docs__a"), _row("docs__b")]

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_Reader()):
        (r,) = check_collection_tuples()

    assert "docs__a" in r.detail and "docs__b" in r.detail


def test_the_sweep_has_the_row_under_a_check_name_so_a_stubbed_sweep_steps_over_it() -> None:
    assert callable(health._check_collection_tuples)
    status = {"collection_tuple_index_present": False, "boot_work": []}
    with patch("nexus.collection_tuple_health.check_collection_tuples", return_value=[]) as inner:
        assert health._check_collection_tuples(status) == []
    inner.assert_called_once_with(engine_status=status)


# ── the index waiting for no reason the data-move rows or the duplicate row give ──────────────────────────────────────
# GET /v1/status says collection_tuple_index_present (an [additive] boolean). false with no duplicate and no open or
# failed re-own move means the engine's guard is waiting for something nobody names: for example the counter erroring
# under onError=CONTINUE. An absent key is an older engine: no row.

_MOVE_ID = "data-move:" + REOWN_MOVE_LABEL


def _status(present: object = False, boot_work: object = ()) -> dict:
    body: dict = {"boot_work": list(boot_work) if isinstance(boot_work, (list, tuple)) else boot_work}
    if present is not None:
        body["collection_tuple_index_present"] = present
    return body


def _wait_rows(rows: list[dict], status: object) -> list:
    return [r for r in check_collection_tuples(rows, engine_status=status) if r.label == INDEX_WAIT_LABEL]


def test_index_absent_no_duplicate_no_open_move_is_a_warning_that_points_at_the_boot_log() -> None:
    (r,) = _wait_rows([_row("docs__a")], _status(False))

    assert r.ok is False and r.warn is True and r.fatal is False
    joined = " ".join(r.fix_suggestions)
    assert "boot log" in joined and "collections-001-2" in joined
    assert "unique index" in r.detail.lower()


def test_index_present_is_no_row() -> None:
    assert _wait_rows([_row("docs__a")], _status(True)) == []


def test_an_absent_key_is_an_older_engine_and_no_row() -> None:
    assert _wait_rows([_row("docs__a")], _status(None)) == []
    assert _wait_rows([_row("docs__a")], {"boot_work": []}) == []


def test_a_key_that_is_not_a_boolean_is_not_read_as_false() -> None:
    assert _wait_rows([_row("docs__a")], _status("false")) == []
    assert _wait_rows([_row("docs__a")], _status(0)) == []


def test_a_duplicate_is_the_known_reason_so_only_the_duplicate_row_shows() -> None:
    rows = check_collection_tuples([_row("docs__a"), _row("docs__b")], engine_status=_status(False))

    assert [r.label for r in rows] == [LABEL]


def test_an_open_or_failed_reown_move_is_the_known_reason_and_no_row() -> None:
    for what in ("working: 1 of 3 chunks", "failed: boom", "blocked: waiting", "queued: first"):
        move = {"id": _MOVE_ID, "what": what}
        assert _wait_rows([_row("docs__a")], _status(False, [move])) == [], what


def test_another_moves_entry_is_not_the_reown_move() -> None:
    other = {"id": "data-move:some-other-move", "what": "working: 1 of 3 chunks"}
    assert len(_wait_rows([_row("docs__a")], _status(False, [other]))) == 1


def test_a_status_with_no_readable_boot_work_cannot_tell_and_gives_no_row() -> None:
    assert _wait_rows([_row("docs__a")], _status(False, None)) == []
    assert _wait_rows([_row("docs__a")], {"collection_tuple_index_present": False}) == []


def test_an_unreadable_registry_cannot_rule_a_duplicate_out_so_it_gives_no_wait_row() -> None:
    assert check_collection_tuples(None, engine_status=_status(False)) == []


def test_no_status_is_no_wait_row_and_the_duplicate_row_is_unchanged() -> None:
    assert check_collection_tuples([_row("docs__a")], engine_status=None) == []
    assert [r.label for r in check_collection_tuples([_row("docs__a"), _row("docs__b")], engine_status=None)] == [LABEL]


# ── the prose says what the engine does today ─────────────────────────────────────────────────────────────────────────
# Since P3.8 the engine refuses to put a second live collection on a tuple in BOTH index states (a mint, a named
# registration, an import, a restore). Only a duplicate that already exists is left, and it is what holds the index back.


def _both_fix_texts() -> list[str]:
    dup = check_collection_tuples([_row("docs__a"), _row("docs__b")])
    wait = check_collection_tuples([_row("docs__a")], engine_status=_status(False))
    assert len(dup) == 1 and len(wait) == 1  # non-vacuity: both rows were produced
    return [" ".join(dup[0].fix_suggestions), " ".join(wait[0].fix_suggestions)]


def test_the_fix_text_never_says_a_second_collection_can_still_register_on_a_tuple() -> None:
    for text in _both_fix_texts():
        assert "can still register" not in text
        assert "still register on one tuple" not in text


def test_the_fix_text_says_the_engine_already_refuses_a_second_live_collection_on_a_tuple() -> None:
    for text in _both_fix_texts():
        assert "refuses" in text and "second live collection" in text


# ── the re-own move's label is one string in three places ─────────────────────────────────────────────────────────────
# The index changeset waits for the nexus.data_moves row with this label; the doctor names it; the Java test registers it.
# marlowe's registration of the move is not on this branch: when a changeset or move file that registers it lands, it is read
# here too, so a label that drifts in any place fails this test and not a boot that waits forever.

_REPO = __import__("pathlib").Path(__file__).resolve().parent.parent
_INDEX_XML = _REPO / "service/src/main/resources/db/changelog/collections-001-tuple-unique-index.xml"
_JAVA_TEST = _REPO / "service/src/test/java/dev/nexus/service/CollectionTupleUniqueIndexLiquibaseTest.java"


def test_the_reown_move_label_is_the_same_in_the_changeset_the_doctor_and_the_java_test() -> None:
    import re

    xml_labels = set(re.findall(r"label = '([^']+)'", _INDEX_XML.read_text()))
    java_labels = set(re.findall(r'MOVE = "([^"]+)"', _JAVA_TEST.read_text()))

    assert xml_labels == {REOWN_MOVE_LABEL}, f"changeset waits for {xml_labels}, the doctor names {REOWN_MOVE_LABEL}"
    assert java_labels == {REOWN_MOVE_LABEL}, f"the Java test registers {java_labels}, the doctor names {REOWN_MOVE_LABEL}"
    assert REOWN_MOVE_ID == "data-move:" + REOWN_MOVE_LABEL


def test_every_other_place_that_names_the_move_agrees_with_the_label() -> None:
    import re

    resources = _REPO / "service/src/main/resources"
    stem = re.compile(r"rdr229-reown-[a-z0-9-]+")
    seen: dict[str, set[str]] = {}
    for path in resources.rglob("*.xml"):
        found = set(stem.findall(path.read_text(errors="ignore")))
        if found:
            seen[str(path.relative_to(resources))] = found
    assert seen, "non-vacuity: the changeset that waits for the move names it"
    for path, labels in seen.items():
        assert labels == {REOWN_MOVE_LABEL}, f"{path} names {labels}, the doctor names {REOWN_MOVE_LABEL}"


# ── a fresh install's first boot (nexus-p2m1x.48) ─────────────────────────────────────────────────────────────────────
# collections-001-2 is skipped at boot 1 (its precondition needs the re-own move's completed_at, and the move worker starts
# after the Liquibase walk). On an empty store the re-own move then completes within seconds and leaves boot_work, while the
# index stays absent until the first restart after the move completed. In that window the status carries no entry for the
# re-own move, and the doctor must not call the wait "unexplained": the install is still working through RDR-229's moves
# (marginalia's cold-install gate: two stamp moves queued), or it has published the restart that applies the changeset too.
# Probed 2026-11-24 on a fresh engine (boot 1, 7 s after start): the re-own entry IS listed while the move is queued or
# blocked, which is the "queued: first" case above; the rows below are the windows where it has left the list.

_RESTART = {
    "id": "rdr229-restart",
    "what": "restart the engine once: it applies the P3.5b changesets that a collection rename waits for (15 of 15 not applied yet)",
}
_STAMP = {
    "id": "data-move:rdr229-stamp-catalog_collections",
    "what": "queued: rdr229-stamp-catalog_collections (the worker has not reached it yet)",
}


def test_a_pending_rdr229_restart_explains_the_wait_so_the_row_is_not_shown() -> None:
    # reown complete (no entry), stamps complete, the restart entry is published: that restart builds the index too
    assert _wait_rows([_row("docs__a")], _status(False, [_RESTART])) == []


def test_rdr229_moves_still_open_explain_the_wait_when_the_reown_move_has_left_the_list() -> None:
    # marginalia's boot 1: the re-own move completed, two stamp moves are still queued, no restart entry yet
    stamps = [_STAMP, {**_STAMP, "id": "data-move:rdr229-stamp-chash_remap"}]
    assert _wait_rows([_row("docs__a")], _status(False, stamps)) == []
    failed = {"id": "data-move:rdr229-stamp-topics", "what": "failed: boom"}
    assert _wait_rows([_row("docs__a")], _status(False, [failed])) == []


def test_the_unexplained_row_stays_when_no_reown_entry_no_restart_and_no_rdr229_move_is_listed() -> None:
    other = {"id": "data-move:some-other-move", "what": "working: 1 of 3 chunks"}
    unrelated = {"id": "index-build:docs__x", "what": "building"}
    assert len(_wait_rows([_row("docs__a")], _status(False, []))) == 1
    assert len(_wait_rows([_row("docs__a")], _status(False, [other, unrelated]))) == 1


def test_a_duplicate_still_wins_over_a_pending_restart() -> None:
    rows = check_collection_tuples([_row("docs__a"), _row("docs__b")], engine_status=_status(False, [_RESTART]))
    assert [r.label for r in rows] == [LABEL]
