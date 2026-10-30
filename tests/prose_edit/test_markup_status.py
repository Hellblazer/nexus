# SPDX-License-Identifier: AGPL-3.0-or-later
"""suggest.py map_statuses: list_suggestions back to edit numbers (nexus-scmcs.2.3, A1).

Pure, no substrate. There is no saved-file check: the author never saves in the app.
"""
from __future__ import annotations

import json

from tests.prose_edit.test_markup_translate import FIXTURE, _suggest

LISTING = FIXTURE.parent / "list_suggestions.json"


def sug(sid: str, status: str, find: str = "x") -> dict:
    return {"id": sid, "status": status, "block": 1, "find": find, "replace": "", "note": ""}


def sent(*items: tuple[int, str, str]) -> list[dict]:
    return [{"n": n, "id": i, "find": f} for n, i, f in items]


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
    got = _suggest().map_statuses(run, listing)
    assert got == {"accept": [1], "reject": [2], "hold": [3, 6], "warnings": []}
    # the earlier document's s1 (accepted) and s2 (rejected) are in the listing and ignored
    assert {s["id"] for s in listing} > {"s1", "s2"}


def test_change_core_has_one_home() -> None:
    mod = _suggest()
    assert mod.change_core("It really is quite simple.", "It is simple.")[1:3] == ("really is quite", "is")
