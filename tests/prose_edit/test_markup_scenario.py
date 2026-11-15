# SPDX-License-Identifier: AGPL-3.0-or-later
"""The whole path-run review without the app, scripted (nexus-scmcs.2.7, A1).

translate -> the app's propose_edits reply -> the app's list_suggestions reply -> decisions -> apply (dry run, then
real). The app is a stand-in that returns real-shaped replies. The point: the document changes only at the accepted
edits (everything else byte for byte, the wrapped paragraph included), rejections are stored once, and the session is
logged once, because `apply` is the only writer and the only logger.

Two tests, two tenants (nexus-h72g3: the tenant ceiling). The first is the journey itself. The second runs four
smaller runs in one tenant, each on a document of its own: the fixed clock (`PROSE_EDIT_NOW`) gives every apply on one
path the same log title, and the rejection record is kept for each path, so a path is how a run keeps its T2 records
apart from the others'.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_get, t2_json, t2_titles
from tests.prose_edit.test_brief import brief_ok, fenced, proposal
from tests.prose_edit.test_markup_verbs import (
    E1,
    E2,
    E3,
    E4,
    E5,
    TEXT,
    app_reply,
    listing,
    read,
    read_list,
    review_ok,
    run_review,
    translated,
    write_json,
)


def expected_text() -> str:
    """TEXT with edits 1, 2 and 3 applied (the accepted ones) and nothing else touched."""
    out = TEXT
    for e in (E1, E2, E3):
        assert out.count(e["old"]) == 1
        out = out.replace(e["old"], e["new"])
    return out


def logs(rel: str = "docs/s.md") -> list[str]:
    return [t for t in t2_titles(REPO_PROJECT) if t.startswith(f"log/{rel}/")]


def proposed_ids(prose: Prose, work: Path) -> list[dict]:
    """The app takes every edit translate sent: its suggestions, as `proposed` records them."""
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    return read_list(work / "suggestions.json")


def test_the_file_changes_only_at_accepted_edits_and_each_edit_is_logged_and_rejected_once(
    prose: Prose, repo: Path
) -> None:
    work, out = translated(prose, repo)
    assert out["sent"] == 4
    # the app refuses edit 2 (index 1 of the call), the others become suggestions s12, s13, s14
    reply = write_json(work / "proposed.json", app_reply(read(work / "propose.json"), refuse={1: "overlaps a pending suggestion"}))
    proposed = review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    assert proposed["suggested"] == 3 and [r["n"] for r in proposed["refused"]] == [2]
    sugg = read_list(work / "suggestions.json")
    ids = {s["n"]: s["id"] for s in sugg}
    # the author accepts 1 and 3 in the app, rejects 4, and the earlier document's s1 is still listed
    statuses = listing(sugg, {ids[1]: "accepted", ids[3]: "accepted", ids[4]: "rejected"})
    decided = json.loads(run_review(prose, "decisions", "--work", str(work), "--from-file",
                                    str(write_json(work / "statuses.json", statuses)),
                                    "--accept", "2", "--hold", "5").stdout)  # the chat takes edit 2, holds edit 5
    assert decided["apply_args"] == ["--accept", "1,2,3", "--reject", "4", "--hold", "5"]
    assert not any(t.startswith(("log/", "doc/")) for t in t2_titles(REPO_PROJECT)), "decisions stores nothing"
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == TEXT

    dry = review_ok(prose, "apply", "--work", str(work), *decided["apply_args"], "--dry-run", dry_first=False)
    assert dry["dry_run"] and [e["n"] for e in dry["would_apply"]] == [1, 2, 3] and dry["would_skip"] == []
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == TEXT and logs() == []

    done = review_ok(prose, "apply", "--work", str(work), *decided["apply_args"], dry_first=False)
    assert [e["n"] for e in done["applied"]] == [1, 2, 3] and done["rejected"] == [4] and done["held"] == [5]
    assert (repo / "docs" / "s.md").read_bytes() == expected_text().encode("utf-8")
    assert not work.exists()
    rec = t2_json(REPO_PROJECT, "doc/docs/s.md")
    assert [(r["old"], r["new"]) for r in rec["rejections"]] == [(E4["old"], E4["new"])]
    assert logs() == [done["log"]["title"]]
    row = t2_get(REPO_PROJECT, logs()[0])
    assert row is not None
    session = json.loads(row["content"])["session"]
    assert [e["n"] for e in session["edits"]] == [1, 2, 3, 4, 5]  # each edit exactly once
    assert (session["accepted"], session["applied"], session["rejected"], session["held"]) == (
        [1, 2, 3], [1, 2, 3], [4], [5])


def test_a_run_that_accepts_nothing_sends_nothing_or_meets_a_changed_file_logs_and_rejects_as_it_should(
    prose: Prose, repo: Path
) -> None:
    # --- a rejected edit is not proposed again on the next run (docs/d.md)
    rel = "docs/d.md"
    work, _ = translated(prose, repo, rel=rel)
    sugg = proposed_ids(prose, work)
    ids = {s["n"]: s["id"] for s in sugg}
    decided = json.loads(run_review(prose, "decisions", "--work", str(work), "--from-file", str(
        write_json(work / "statuses.json", listing(sugg, {ids[4]: "rejected", ids[3]: "accepted"})))).stdout)
    out = review_ok(prose, "apply", "--work", str(work), *decided["apply_args"])
    assert out["rejected"] == [4], f"{rel}: apply rejected {out['rejected']}"
    (repo / rel).write_text(TEXT, encoding="utf-8")
    again = json.loads(brief_ok(prose, "filter", rel, stdin=fenced(proposal([E3, E4]))))
    assert [e["n"] for e in again["edits"]] == [3], again["dropped"]
    assert [d["cause"] for d in again["dropped"]] == ["rejected"], again["dropped"]

    # --- nothing accepted changes nothing and still logs once (docs/a.md)
    rel = "docs/a.md"
    work, _ = translated(prose, repo, rel=rel)
    sugg = proposed_ids(prose, work)
    decided = json.loads(run_review(prose, "decisions", "--work", str(work), "--from-file",
                                    str(write_json(work / "statuses.json", listing(sugg, {})))).stdout)
    assert decided["accept"] == [] and decided["hold"] == [1, 2, 3, 4], decided
    out = review_ok(prose, "apply", "--work", str(work), *decided["apply_args"])
    assert out["applied"] == [] and (repo / rel).read_text(encoding="utf-8") == TEXT, f"{rel}: {out['applied']}"
    assert len(logs(rel)) == 1, f"{rel}: logged {logs(rel)}"
    assert t2_get(REPO_PROJECT, f"doc/{rel}") is None, f"{rel}: a rejection record was stored"

    # --- a run that sent nothing is decided in the chat and logged once by apply (docs/b.md)
    rel = "docs/b.md"
    work, out = translated(prose, repo, edits=[E5], rel=rel)
    assert out["sent"] == 0, out
    decided = json.loads(run_review(prose, "decisions", "--work", str(work), "--accept", "5").stdout)
    done = review_ok(prose, "apply", "--work", str(work), *decided["apply_args"])
    assert [e["n"] for e in done["applied"]] == [5], done["applied"]
    assert (repo / rel).read_text(encoding="utf-8") == TEXT.replace(E5["old"], E5["new"]), f"{rel}: wrong text"
    assert len(logs(rel)) == 1, f"{rel}: logged {logs(rel)}"

    # --- an accepted edit that no longer matches the file is skipped, not guessed (docs/c.md)
    rel = "docs/c.md"
    work, _ = translated(prose, repo, rel=rel)
    sugg = proposed_ids(prose, work)
    ids = {s["n"]: s["id"] for s in sugg}
    decided = json.loads(run_review(prose, "decisions", "--work", str(work), "--from-file", str(
        write_json(work / "statuses.json", listing(sugg, {ids[1]: "accepted"})))).stdout)
    # the author edited the file by hand in the meantime: the dry run sees it and apply says why
    (repo / rel).write_text(TEXT.replace("is basically ordered,", "is mostly ordered,"), encoding="utf-8")
    dry = review_ok(prose, "apply", "--work", str(work), *decided["apply_args"], "--dry-run", dry_first=False)
    assert [s["n"] for s in dry["would_skip"]] == [1], f"{rel}: {dry['would_skip']}"
    assert dry["would_skip"][0]["cause"] == "not-found", dry["would_skip"]
