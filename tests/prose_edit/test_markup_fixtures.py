# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real MarkupEditor replies the review code is written against (nexus-scmcs.2.1).

The fixtures were captured from the suggestions build; NOTES.md beside them records what
they settled. These tests pin the key names later beads read, so a reply shape that moves
upstream fails here first.
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "markupeditor"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_get_document_blocks_carry_the_keys_translation_reads() -> None:
    reply = _load("get_document.json")
    assert {"hasChanges", "url", "view", "firstEditable"} <= reply.keys()
    assert reply["blocks"], "fixture has no blocks"
    for block in reply["blocks"]:
        assert {"index", "kind", "hash", "markdown", "warnings"} <= block.keys()
    kinds = {block["kind"] for block in reply["blocks"]}
    assert {"paragraph", "code", "bullet-list"} <= kinds


def test_propose_edits_results_are_proposed_with_an_id_or_refused_with_a_reason() -> None:
    results = _load("propose_edits.json")["results"]
    statuses = {r["status"] for r in results}
    assert statuses == {"proposed", "refused"}
    for r in results:
        assert isinstance(r["index"], int)
        if r["status"] == "proposed":
            assert r["id"].startswith("s") and isinstance(r["block"], int)
        else:
            assert r["reason"] and "id" not in r


def test_list_suggestions_has_every_status_the_mapping_reads() -> None:
    suggestions = _load("list_suggestions.json")["suggestions"]
    for s in suggestions:
        assert {"id", "status", "block", "find", "replace", "note"} <= s.keys()
    assert {s["status"] for s in suggestions} == {"accepted", "rejected", "pending"}


def test_list_suggestions_holds_ids_this_run_did_not_propose() -> None:
    proposed = {r["id"] for r in _load("propose_edits.json")["results"] if r["status"] == "proposed"}
    listed = {s["id"] for s in _load("list_suggestions.json")["suggestions"]}
    assert proposed <= listed
    assert listed - proposed, "the earlier document's suggestions are the case NOTES.md e describes"


def test_notes_answer_each_question_the_plan_asked() -> None:
    notes = (FIXTURES / "NOTES.md").read_text(encoding="utf-8")
    for label in ("a. **Key names", "b. **Locating", "c. **Reply size", "d. **Saving", "e. **Suggestion ids", "f. **open_document"):
        assert label in notes
