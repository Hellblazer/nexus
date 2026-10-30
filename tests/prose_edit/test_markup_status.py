# SPDX-License-Identifier: AGPL-3.0-or-later
"""suggest.py map_statuses: list_suggestions back to edit numbers (nexus-scmcs.2.3, A1).

Pure, no substrate. There is no saved-file check: the author never saves in the app.
"""
from __future__ import annotations

import json

from tests.prose_edit.test_markup_translate import FIXTURE, _suggest

LISTING = FIXTURE.parent / "list_suggestions.json"


def sug(sid: str, status: str, find: str = "x", replace: str = "", note: str | None = None) -> dict:
    """A listed suggestion; its note is what translate writes for the edit whose number is the id's (s3 is edit 3)."""
    note = f"Edit {int(sid[1:])}: why" if note is None else note
    return {"id": sid, "status": status, "block": 1, "find": find, "replace": replace, "note": note}


def sent(*items: tuple[int, str, str]) -> list[dict]:
    return [{"n": n, "id": i, "find": f, "replace": ""} for n, i, f in items]


def test_every_status_maps_to_a_decision() -> None:
    got = _suggest().map_statuses(
        sent((1, "s1", "a"), (2, "s2", "b"), (3, "s3", "c"), (4, "s4", "d")),
        [sug("s1", "accepted", "a"), sug("s2", "rejected", "b"), sug("s3", "pending", "c"), sug("s4", "stale", "d")])
    assert got == {"accept": [1], "reject": [2], "hold": [3, 4], "warnings": []}


def test_an_id_the_app_no_longer_lists_is_held_with_a_warning() -> None:
    got = _suggest().map_statuses(sent((1, "s1", "a"), (2, "s2", "b")), [sug("s1", "accepted", "a")])
    assert got["accept"] == [1] and got["hold"] == [2]
    assert len(got["warnings"]) == 1 and "edit 2" in got["warnings"][0] and "s2" in got["warnings"][0]


def test_a_suggestion_whose_find_differs_from_the_one_sent_is_treated_as_absent() -> None:
    # ids restart after the document is reopened: s5 may now name another suggestion
    got = _suggest().map_statuses(sent((1, "s5", "the one we sent")), [sug("s5", "accepted", "something else")])
    assert got["accept"] == [] and got["hold"] == [1]
    assert "edit 1" in got["warnings"][0]


def test_a_suggestion_with_the_same_id_and_find_but_another_replace_is_not_this_runs_edit() -> None:
    # F1: another run's suggestion can share an id and a find; the replacement tells them apart
    mine = [{"n": 1, "id": "s5", "find": "basically ", "replace": ""}]
    got = _suggest().map_statuses(mine, [sug("s5", "rejected", "basically ", replace="mostly ", note="Edit 1: other run")])
    assert got["reject"] == [] and got["hold"] == [1] and "edit 1" in got["warnings"][0]


def test_a_suggestion_whose_note_is_not_this_edits_is_not_this_runs_edit() -> None:
    mine = [{"n": 1, "id": "s5", "find": "basically ", "replace": ""}]
    for note in ("Edit 7: other run", "", "Edit 12: another", "edit 1: lower case", "see Edit 1"):
        got = _suggest().map_statuses(mine, [sug("s5", "rejected", "basically ", note=note)])
        assert got["reject"] == [] and got["hold"] == [1], note
        assert got["warnings"], note


def test_a_note_that_is_exactly_the_edit_number_or_continues_after_a_colon_matches() -> None:
    mine = [{"n": 1, "id": "s5", "find": "a", "replace": ""}, {"n": 12, "id": "s6", "find": "b", "replace": ""}]
    got = _suggest().map_statuses(mine, [sug("s5", "accepted", "a", note="Edit 1"), sug("s6", "rejected", "b", note="Edit 12: why")])
    assert got == {"accept": [1], "reject": [12], "hold": [], "warnings": []}


def test_the_apps_straightened_quotes_in_replace_still_match() -> None:
    # NOTES d: a curly quote in `replace` comes back straight
    mine = [{"n": 1, "id": "s5", "find": "x", "replace": "it is “plainly” simple, it’s fine"}]
    got = _suggest().map_statuses(mine, [sug("s5", "accepted", "x", replace='it is "plainly" simple, it\'s fine', note="Edit 1")])
    assert got["accept"] == [1] and got["warnings"] == []


def test_ids_this_run_never_sent_are_ignored() -> None:
    got = _suggest().map_statuses(sent((1, "s1", "a")), [sug("s1", "rejected", "a"), sug("s9", "accepted", "z")])
    assert got == {"accept": [], "reject": [1], "hold": [], "warnings": []}


def test_an_unknown_status_is_held_with_a_warning() -> None:
    got = _suggest().map_statuses(sent((1, "s1", "a")), [sug("s1", "applied", "a")])
    assert got["hold"] == [1] and "applied" in got["warnings"][0]


def test_numbers_come_back_sorted() -> None:
    got = _suggest().map_statuses(sent((3, "s3", "c"), (1, "s1", "a")), [sug("s3", "accepted", "c"), sug("s1", "accepted", "a")])
    assert got["accept"] == [1, 3]


def test_the_real_listing_maps_only_this_runs_ids() -> None:
    listing = json.loads(LISTING.read_text(encoding="utf-8"))["suggestions"]
    run = sent((1, "s12", "basically ordered"), (2, "s13", "really quite simply "), (3, "s14", "basically "),
               (6, "s15", "it is “really” simple"))
    run[0]["replace"] = "ordered"
    run[3]["replace"] = "it is “plainly” simple"
    got = _suggest().map_statuses(run, listing)
    assert got == {"accept": [1], "reject": [2], "hold": [3, 6], "warnings": []}
    # the earlier document's s1 (accepted) and s2 (rejected) are in the listing and ignored
    assert {s["id"] for s in listing} > {"s1", "s2"}


def test_change_core_has_one_home() -> None:
    mod = _suggest()
    assert mod.change_core("It really is quite simple.", "It is simple.")[1:3] == ("really is quite", "is")
