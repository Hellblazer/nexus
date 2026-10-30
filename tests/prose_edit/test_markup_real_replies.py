# SPDX-License-Identifier: AGPL-3.0-or-later
"""The verbs over the app's real replies (Gate 1c F, nexus-scmcs.2.10).

get_document_serializer.json is what get_document returned for serializer-source.md, and
propose_edits_serializer.json holds the edits the app was sent for it and its reply. The other verb tests build
stand-in replies; these two feed the captured ones through `translate` and `proposed`.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_brief import edit
from tests.prose_edit.test_markup_serializer import PAIRS, SENT, SOURCE
from tests.prose_edit.test_markup_translate import FIXTURE
from tests.prose_edit.test_markup_verbs import read, read_list, review_ok, start, write_json

DOC = json.loads((FIXTURE.parent / "get_document_serializer.json").read_text(encoding="utf-8"))
REL = "docs/serializer-source.md"
HARD_BREAK = 8  # the pair the app refused: its find spans a hard break
# The pipeline in front of `translate` drops some of the eleven (the filter protects the quote and the table, render
# finds 3 and 4 overlapping on "use"), so the real call is replayed for these: indexes into the captured request.
RUN = [0, 1, 2, 3, 5, 8, 9, 10]
SENT_BY_TRANSLATE = [i for i in RUN if i != HARD_BREAK]


def edits() -> list[dict]:
    return [edit(i + 1, PAIRS[i][0], PAIRS[i][1], "r") for i in RUN]


def translated_real(prose: Prose, repo: Path) -> tuple[Path, dict]:
    work, _ = start(prose, repo, edits(), text=SOURCE, rel=REL)
    # the capture's url is a path on the capture machine: the session's target stands in for it
    blocks = write_json(work / "blocks.json", {**DOC, "url": str(repo / REL)})
    return work, review_ok(prose, "translate", "--work", str(work), "--blocks-file", str(blocks))


def test_the_real_get_document_reply_goes_through_translate(prose: Prose, repo: Path) -> None:
    work, out = translated_real(prose, repo)
    assert out["sent"] == len(SENT_BY_TRANSLATE) == 7  # non-vacuity: seven edits reach the app
    assert [(r["n"], r["cause"]) for r in out["refused"]] == [(HARD_BREAK + 1, "spans-lines")]
    saved = read(work / "propose.json")
    assert saved["order"] == [i + 1 for i in SENT_BY_TRANSLATE]
    for n, got in zip(saved["order"], saved["edits"]):
        request = SENT["request"][n - 1]
        assert (got["block"], got["expected"]) == (request["block"], request["expected"])
        assert got["note"].startswith(f"Edit {n}")


def test_the_real_propose_edits_reply_goes_through_proposed(prose: Prose, repo: Path) -> None:
    work, _ = translated_real(prose, repo)
    # the captured reply, one result for each edit translate sent, in the call's own order
    results = [{**SENT["reply"]["results"][i], "index": k} for k, i in enumerate(SENT_BY_TRANSLATE)]
    assert all(r["status"] == "proposed" for r in results)
    out = review_ok(prose, "proposed", "--work", str(work), "--from-file",
                    str(write_json(work / "proposed.json", {**SENT["reply"], "results": results})))
    assert out["suggested"] == 7 and out["refused"] == []
    sugg = read_list(work / "suggestions.json")
    assert [(s["n"], s["id"]) for s in sugg] == [(i + 1, SENT["reply"]["results"][i]["id"]) for i in SENT_BY_TRANSLATE]
