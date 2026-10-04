# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 2 for the RDR-221 Phase 1 reviews (nexus-ger02.5 I2, nexus-ger02.6 S3, S4, S8): Sam's three decisions.

1. Precedence: every bullet of the brief's style sheet carries its layer, and the brief says how a conflict is
   settled (the narrower layer wins).
2. The voice card: an author-approved card is stored in the document's record and anchors later runs.
3. Promote: the filter drops an edit that makes a promoted not-a-defect change, and a real promote needs a
   matching dry run, as apply does.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, ROOT, USER_PROJECT, Prose, t2_json, t2_put, t2_titles
from tests.prose_edit.test_brief import (
    AGENT,
    NAMES_A_REPAIR,
    ORDER,
    SKILL,
    TREATMENTS,
    _module,
    brief_ok,
    fenced,
    proposal,
    run_brief,
    seed,
)

DOC = "docs/x.md"
RDR = ROOT / "docs" / "rdr" / "rdr-221-prose-editor.md"
RULE = "document > genre > repo > user"


def _sheet(text: str) -> str:
    return text[text.index(ORDER[2]):text.index(ORDER[3])]


def _voice(text: str) -> str:
    return text[text.index(ORDER[1]):text.index(ORDER[2])]


def _step13() -> str:
    text = SKILL.read_text(encoding="utf-8")
    return text[text.index("\n13. "):text.index("\n## Rules")]


# ---------------------------------------------------------------------------
# 1. Precedence (S4): every bullet carries its layer; the rule is operational
# ---------------------------------------------------------------------------


def test_a_document_entry_that_contradicts_a_repo_diagnostic_appears_with_both_labels_and_the_rule(
    prose: Prose,
) -> None:
    seed(prose, "repo", lists={"diagnostics": ["Cut every semicolon: write two sentences."]})
    seed(prose, "doc", lists={"diagnostics": ["Semicolons are this essay's voice: leave them."]}, path=DOC)
    seed(prose, "user", lists={"diagnostics": ["Prefer the active voice."]})
    sheet = _sheet(brief_ok(prose, "build", DOC))
    lines = sheet.splitlines()
    assert "- Cut every semicolon: write two sentences. (layer: repo)" in lines
    assert "- Semicolons are this essay's voice: leave them. (layer: document)" in lines
    assert "- Prefer the active voice. (layer: user)" in lines
    assert RULE in sheet
    rule = next(ln for ln in sheet.splitlines() if RULE in ln)
    assert "contradict" in rule and "wins" in rule  # the sentence that states it, not just the order


def test_every_style_sheet_bullet_names_its_layer_scalars_by_the_layer_that_wins(prose: Prose) -> None:
    seed(prose, "user", {"tone": "user-tone", "only_user": "u"}, {"banned": ["user-banned"]})
    seed(prose, "repo", {"tone": "repo-tone"}, {"diagnostics": ["Q?"]})
    seed(prose, "doc", {"tone": "doc-tone"}, {"banned": ["doc-banned"]}, path=DOC)
    sheet = _sheet(brief_ok(prose, "build", DOC))
    assert "- tone: doc-tone (layer: document)" in sheet
    assert "- only_user: u (layer: user)" in sheet
    assert "- user-banned (layer: user)" in sheet and "- doc-banned (layer: document)" in sheet
    bullets = [ln for ln in sheet.splitlines() if ln.startswith("- ")]
    assert bullets and all(re.search(r" \(layer: (document|genre|repo|user)\)$", ln) for ln in bullets), bullets


def test_an_entry_held_by_two_layers_is_labelled_with_the_narrower_one_and_listed_once(prose: Prose) -> None:
    seed(prose, "user", lists={"diagnostics": ["Same words."]})
    seed(prose, "repo", lists={"diagnostics": ["Same words."]})
    sheet = _sheet(brief_ok(prose, "build", DOC))
    assert sheet.count("Same words.") == 1 and "- Same words. (layer: repo)" in sheet


def test_site_page_rules_are_the_genre_layer(prose: Prose, repo: Path) -> None:
    seed(prose, "repo", lists={**TREATMENTS, "diagnostics": ["Q?"]})
    (repo / "web").mkdir()
    (repo / "web" / "page.html").write_text("<p>hi</p>\n")
    sheet = _sheet(brief_ok(prose, "build", "web/page.html"))
    rule = next(ln for ln in sheet.splitlines() if "Active voice, no semicolons" in ln)
    assert rule.endswith(" (layer: genre)")
    query = next(ln for ln in sheet.splitlines() if "No corpus counts" in ln)
    note = next(ln for ln in sheet.splitlines() if "Motivate a new thing" in ln)
    assert query.endswith(" (layer: genre)") and note.endswith(" (layer: genre)")
    assert "- Q? (layer: repo)" in sheet


def test_a_read_with_no_layers_still_renders_without_labels() -> None:
    mod = _module()
    read = {"genre": "reference-doc", "path": DOC, "range": None, "genre_record": None,
            "merged": {"scalars": {"k": "v"}, "lists": {"diagnostics": ["d"]}}}
    sheet = _sheet(mod.render_brief(read, 10, None))
    assert "- k: v" in sheet and "- d" in sheet and "(layer:" not in sheet


def test_the_editor_applies_the_same_rule() -> None:
    agent = AGENT.read_text(encoding="utf-8")
    assert RULE in agent and "contradict" in agent and "wins" in agent
    assert "author-approved voice card" in agent and "return it unchanged" in agent  # the anchor, kept as given
    assert not NAMES_A_REPAIR.search(agent)


# ---------------------------------------------------------------------------
# 2. The voice card (I2, S8): stored per document, an anchor for later runs
# ---------------------------------------------------------------------------

CARD = "First person, plain register. Refrain: the closing line of each section. Density is deliberate."
TITLE = f"doc/{DOC}"


def _store_card(prose: Prose, text: str = CARD, path: str = DOC) -> dict:
    return prose.ok("voice-card", path, "--from-stdin", stdin={"voice_card": text})


def test_there_is_no_card_until_the_author_approves_one(prose: Prose) -> None:
    assert prose.ok("voice-card", DOC) == {"path": DOC, "range": None, "voice_card": None}
    assert t2_titles(REPO_PROJECT) == []  # reading stores nothing


def test_an_approved_card_is_stored_in_the_document_record_with_its_time(prose: Prose) -> None:
    out = _store_card(prose)
    assert out["voice_card"] == {"text": CARD, "at": "2026-09-29T17:15:03Z"}
    assert t2_json(REPO_PROJECT, TITLE) == {
        "scalars": {}, "lists": {}, "rejections": [],
        "voice_card": {"text": CARD, "at": "2026-09-29T17:15:03Z"},
    }
    assert prose.ok("voice-card", DOC)["voice_card"]["text"] == CARD
    # the merged read shows it with the document layer, so the brief builder needs no second call
    assert prose.ok("read", DOC)["layers"]["document"]["voice_card"]["text"] == CARD


def test_a_second_card_replaces_the_first_and_a_range_target_keys_on_the_bare_path(prose: Prose) -> None:
    _store_card(prose, "old card")
    out = prose.ok("voice-card", f"{DOC}:2-3", "--from-stdin", stdin={"voice_card": "new card"})
    assert out["range"] == {"start": 2, "end": 3} and out["voice_card"]["text"] == "new card"
    assert t2_json(REPO_PROJECT, TITLE)["voice_card"]["text"] == "new card"
    assert t2_titles(REPO_PROJECT) == [TITLE]


def test_a_card_sits_beside_entries_and_rejections_and_none_disturbs_the_others(prose: Prose) -> None:
    seed(prose, "doc", {"tone": "t"}, {"banned": ["b"]}, path=DOC)
    prose.ok("reject", DOC, "--old", "just", "--new", "")
    _store_card(prose)
    rec = t2_json(REPO_PROJECT, TITLE)
    assert rec["scalars"] == {"tone": "t"} and rec["lists"] == {"banned": ["b"]}
    assert [r["old"] for r in rec["rejections"]] == ["just"] and rec["voice_card"]["text"] == CARD
    prose.ok("reject", DOC, "--old", "very", "--new", "")  # a later write keeps the card
    seed(prose, "doc", {"more": "m"}, path=DOC)
    assert t2_json(REPO_PROJECT, TITLE)["voice_card"]["text"] == CARD


def test_a_record_that_holds_only_a_card_is_not_taken_for_empty(prose: Prose) -> None:
    seed(prose, "doc", {"tone": "t"}, path=DOC)
    prose.ok("reject", DOC, "--old", "just", "--new", "")
    _store_card(prose)
    prose.ok("rejections", DOC, "--remove", "1")
    prose.ok("entries", "--level", "doc", "--path", DOC, "--remove", "tone")
    assert t2_json(REPO_PROJECT, TITLE)["voice_card"]["text"] == CARD  # not deleted with its last neighbour


def test_removing_the_card_deletes_a_record_that_held_nothing_else(prose: Prose) -> None:
    _store_card(prose)
    out = prose.ok("voice-card", DOC, "--remove")
    assert out["voice_card"] is None and TITLE not in t2_titles(REPO_PROJECT)
    seed(prose, "doc", {"tone": "t"}, path=DOC)
    _store_card(prose)
    prose.ok("voice-card", DOC, "--remove")
    rec = t2_json(REPO_PROJECT, TITLE)
    assert "voice_card" not in rec and rec["scalars"] == {"tone": "t"}
    gone = prose.run("voice-card", "docs/never.md", "--remove")
    assert gone.returncode == 1 and "no voice card" in gone.stderr


def test_a_stdin_run_has_no_document_and_stores_no_card(prose: Prose) -> None:
    for args in (("voice-card", "-"), ("voice-card", "-", "--from-stdin")):
        proc = prose.run(*args, stdin={"voice_card": CARD})
        assert proc.returncode == 1 and proc.stdout == "" and "stdin" in proc.stderr
    assert t2_titles(REPO_PROJECT) == []


@pytest.mark.parametrize(
    "body",
    [
        {"voice_card": ""}, {"voice_card": "   "}, {"voice_card": 7}, {"voice_card": ["a"]}, {},
        {"voice_card": "x" * 4001}, {"voice_card": "ok", "extra": 1}, ["not", "an", "object"],
    ],
    ids=lambda b: json.dumps(b)[:40],
)
def test_a_card_that_is_not_one_non_empty_string_is_refused_and_nothing_is_stored(
    prose: Prose, body: object
) -> None:
    proc = prose.run("voice-card", DOC, "--from-stdin", stdin=body)  # type: ignore[arg-type]
    assert proc.returncode == 1 and proc.stdout == "" and "voice-card" in proc.stderr
    assert t2_titles(REPO_PROJECT) == []


def test_the_set_flag_needs_stdin_and_the_two_flags_do_not_mix(prose: Prose) -> None:
    assert prose.run("voice-card", DOC, "--from-stdin", "--remove", stdin={"voice_card": "c"}).returncode == 2
    empty = prose.run("voice-card", DOC, "--from-stdin", stdin="")
    assert empty.returncode == 1 and t2_titles(REPO_PROJECT) == []


@pytest.mark.parametrize("card", ["a string", {"text": ""}, {"text": 3, "at": "x"}, {"text": "t"}, ["x"]],
                         ids=["string", "empty", "text-int", "no-at", "list"])
def test_a_stored_card_of_the_wrong_shape_stops_the_read_naming_the_record(prose: Prose, card: object) -> None:
    t2_put(REPO_PROJECT, TITLE, json.dumps({"scalars": {}, "lists": {}, "rejections": [], "voice_card": card}))
    for cmd in (("voice-card", DOC), ("read", DOC)):
        proc = prose.run(*cmd)
        assert proc.returncode == 1 and proc.stdout == ""
        assert "is malformed" in proc.stderr and "voice_card" in proc.stderr and TITLE in proc.stderr


def test_a_later_run_gets_the_saved_card_as_the_anchor_not_a_request_to_rebuild_it(prose: Prose) -> None:
    before = _voice(brief_ok(prose, "build", DOC))
    assert "write the voice card for this document from the whole document" in before  # today's behaviour
    _store_card(prose)
    after = _voice(brief_ok(prose, "build", DOC))
    assert CARD in after and "author-approved" in after
    assert "write the voice card for this document from the whole document" not in after
    assert "do not rebuild" in after.lower() and "edited" in after  # why: the text has been edited since
    assert '"voice_card"' in after  # the reply field is still named
    assert not NAMES_A_REPAIR.search(after)
    # the card stays out of every other section of the brief
    text = brief_ok(prose, "build", DOC)
    assert text.count(CARD) == 1


def test_a_range_run_uses_the_documents_card_and_a_run_without_one_is_unchanged(prose: Prose) -> None:
    _store_card(prose)
    assert CARD in _voice(brief_ok(prose, "build", f"{DOC}:2-3", "--genre", "reference-doc"))
    other = _voice(brief_ok(prose, "build", "docs/y.md", "--genre", "reference-doc"))
    assert CARD not in other and "write the voice card for this document" in other


def test_a_stdin_run_has_no_card_to_anchor_on(prose: Prose, tmp_path: Path) -> None:
    _store_card(prose)
    src = tmp_path / "msg.txt"
    src.write_text("fix: make the queue drain\n")
    got = _voice(brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(src)))
    assert CARD not in got and "write the voice card" in got


def test_the_skill_offers_to_save_the_card_only_after_the_text_is_read_and_the_author_says_yes() -> None:
    step = _step13()
    card = step[step.index("voice card"):step.index("An edit the author says is never a defect")]
    assert "MEMORY voice-card <path>" in card and "voice-card <path> --from-stdin" in card
    assert "none is stored" in card  # offered only when the document has no saved card
    assert "has read" in step and "says yes" in card
    assert "author-approved" in card
    assert "stdin run" in card and "stores no card" in card
    assert "Write tool" in card  # the text goes in as a file, never on the Bash line


# ---------------------------------------------------------------------------
# 3a. The filter drops an edit that makes a promoted not-a-defect change
# ---------------------------------------------------------------------------

GONE = "The worker really quite simply retries"  # the minimal change: cut "really quite "
KEPT = "The worker simply retries"


def _promoted(prose: Prose, level: str = "repo") -> None:
    prose.ok("reject", DOC, "--old", GONE, "--new", KEPT)
    prose.promote(DOC, 1, level)


def _edits(*pairs: tuple[str, str]) -> dict:
    return {"voice_card": "vc", "note": "n", "paragraphs": [], "queries": [],
            "edits": [{"n": i + 1, "old": o, "new": n, "reason": "r"} for i, (o, n) in enumerate(pairs)]}


@pytest.mark.parametrize("level", ["repo", "user"])
def test_the_filter_drops_an_edit_whose_change_is_a_promoted_entry_by_change_key(prose: Prose, level: str) -> None:
    _promoted(prose, level)
    out = prose.ok("filter", "docs/y.md", stdin=_edits(
        ("It says the worker really quite simply retries it.", "It says the worker simply retries it."),  # same change, other span
        ("really quite simply", "very simply"),  # same words, another replacement: overlap alone is no match
        ("an unrelated sentence", "a related one"),
    ))
    assert [d["n"] for d in out["dropped"]] == [1] and out["dropped"][0]["cause"] == "not-a-defect"
    assert out["dropped"][0]["old"].startswith("It says the worker really")
    assert [e["n"] for e in out["edits"]] == [2, 3]  # the numbers keep their gaps


def test_the_filter_applies_promoted_entries_to_a_stdin_run_too(prose: Prose) -> None:
    _promoted(prose)
    out = prose.ok("filter", "-", stdin=_edits((GONE, KEPT)))
    assert out["edits"] == [] and [(d["n"], d["cause"]) for d in out["dropped"]] == [(1, "not-a-defect")]


def test_a_document_rejection_keeps_its_own_cause_when_the_change_is_also_promoted(prose: Prose) -> None:
    _promoted(prose)
    out = prose.ok("filter", DOC, stdin=_edits((GONE, KEPT)))  # the document that stored it
    assert [d["cause"] for d in out["dropped"]] == ["rejected"]


def test_a_promoted_entry_no_longer_drops_anything_once_it_is_removed(prose: Prose) -> None:
    _promoted(prose)
    prose.ok("not-a-defect", "--level", "repo", "--remove", "1")
    out = prose.ok("filter", "docs/y.md", stdin=_edits((GONE, KEPT)))
    assert [e["n"] for e in out["edits"]] == [1] and out["dropped"] == []


def test_the_brief_filter_reports_the_promoted_drop_with_the_new_text_and_its_cause(
    prose: Prose, repo: Path
) -> None:
    _promoted(prose)
    (repo / "docs" / "y.md").write_text("Intro.\n\nThe worker really quite simply retries.\n")
    proc = run_brief(prose, "filter", "docs/y.md", stdin=fenced(proposal([
        {"n": 1, "old": GONE, "new": KEPT, "reason": "r"}])))
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["edits"] == [] and got["dropped"] == [
        {"n": 1, "old": GONE, "new": KEPT, "cause": "not-a-defect"}]


# ---------------------------------------------------------------------------
# 3b. A real promote needs a matching dry run, as apply does
# ---------------------------------------------------------------------------


def _promote_dir(repo: Path) -> Path:
    return repo / ".git" / "prose-edit-promote"


def _reject_two(prose: Prose) -> None:
    prose.ok("reject", DOC, "--old", "just", "--new", "")
    prose.ok("reject", DOC, "--old", "quite", "--new", "")


def _nad_titles() -> list[str]:
    return [t for t in t2_titles(REPO_PROJECT) + t2_titles(USER_PROJECT) if t == "not-a-defect"]


def test_a_real_promote_without_a_dry_run_is_refused_and_stores_nothing(prose: Prose) -> None:
    _reject_two(prose)
    proc = prose.run("promote", DOC, "1", "--level", "repo")
    assert proc.returncode == 1 and proc.stdout == ""
    assert "dry run" in proc.stderr and "--dry-run" in proc.stderr and "author" in proc.stderr
    assert not NAMES_A_REPAIR.search(proc.stderr)
    assert _nad_titles() == []


def test_a_matching_dry_run_lets_the_promote_through_once(prose: Prose) -> None:
    _reject_two(prose)
    dry = prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")
    assert dry["dry_run"] is True and _nad_titles() == []  # the dry run itself stores nothing in T2
    prose.ok("promote", DOC, "1", "--level", "repo")
    assert [e["old"] for e in t2_json(REPO_PROJECT, "not-a-defect")["entries"]] == ["just"]
    again = prose.run("promote", DOC, "1", "--level", "repo")  # one dry run vouches for one promote
    assert again.returncode == 1 and "dry run" in again.stderr


def test_a_dry_run_for_another_edit_or_level_does_not_count(prose: Prose) -> None:
    _reject_two(prose)
    prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")
    for other in (("2", "repo"), ("1", "user")):
        proc = prose.run("promote", DOC, other[0], "--level", other[1])
        assert proc.returncode == 1 and "dry run" in proc.stderr, other
    assert _nad_titles() == []
    prose.ok("promote", DOC, "1", "--level", "repo")  # the matching one still stands


def test_a_dry_run_is_not_spent_by_a_refused_promote_and_one_per_entry_can_wait(prose: Prose) -> None:
    _reject_two(prose)
    prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")
    prose.ok("promote", DOC, "2", "--level", "repo", "--dry-run")
    assert prose.run("promote", DOC, "1", "--level", "user").returncode == 1
    prose.ok("promote", DOC, "2", "--level", "repo")
    prose.ok("promote", DOC, "1", "--level", "repo")
    assert [e["old"] for e in t2_json(REPO_PROJECT, "not-a-defect")["entries"]] == ["quite", "just"]


def test_a_rejection_that_is_not_the_one_the_author_saw_is_refused(prose: Prose) -> None:
    _reject_two(prose)
    prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")  # the author saw "just"
    prose.ok("rejections", DOC, "--remove", "1")  # number 1 is now "quite"
    proc = prose.run("promote", DOC, "1", "--level", "repo")
    assert proc.returncode == 1 and "dry run" in proc.stderr and _nad_titles() == []


def test_the_dry_run_is_about_the_entry_not_the_clock(prose: Prose) -> None:
    _reject_two(prose)
    prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")
    later = {**prose.env, "PROSE_EDIT_NOW": "2026-09-30T08:00:00.000000Z"}
    out = prose.ok("promote", DOC, "1", "--level", "repo", env=later)
    assert out["entry"]["at"] == "2026-09-30T08:00:00Z"  # the entry takes the real time; the gate ignored it


def test_a_dry_run_older_than_two_hours_is_refused(prose: Prose, repo: Path) -> None:
    _reject_two(prose)
    prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")
    for record in _promote_dir(repo).iterdir():
        then = time.time() - 3 * 3600
        os.utime(record, (then, then))
    proc = prose.run("promote", DOC, "1", "--level", "repo")
    assert proc.returncode == 1 and "dry run" in proc.stderr and "two hours" in proc.stderr
    assert _nad_titles() == []


def test_the_dry_run_record_holds_hashes_only_and_sits_in_the_git_directory(prose: Prose, repo: Path) -> None:
    _reject_two(prose)
    prose.ok("promote", DOC, "1", "--level", "repo", "--dry-run")
    files = list(_promote_dir(repo).iterdir())
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert re.fullmatch(r'\{"entry": "[0-9a-f]{64}"\}', body.strip()), body
    assert not (repo / "prose-edit-promote").exists()  # nothing in the work tree


def test_the_skill_promotes_by_dry_run_show_confirm_then_the_real_command() -> None:
    step = _step13()
    promote = step[step.index("An edit the author says is never a defect"):]
    order = [promote.index("--dry-run"), promote.index("Show the entry"), promote.index("confirm"),
             promote.index("without `--dry-run`")]
    assert order == sorted(order), order
    assert "refuses" in promote and "dry run" in promote
    assert "filter drops" in promote.lower() or "drops" in promote  # what the entry does once stored
    assert "has read" in step


# ---------------------------------------------------------------------------
# 4. The design of record says so
# ---------------------------------------------------------------------------


def test_the_rdr_revision_history_names_the_three_decisions_and_the_record_table_the_card_shape() -> None:
    text = RDR.read_text(encoding="utf-8")
    history = text[text.index("## Revision History"):]
    entry = history[history.index("2026-10-03"):]
    for phrase in ("precedence labels", "stored voice card", "promote filter", "dry-run gate"):
        assert phrase in entry, phrase
    assert '"voice_card"' in text and "author-approved" in text
    assert re.search(r"status:\s*accepted", text[:600]), "the RDR status is not changed by this round"
