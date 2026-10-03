# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 3 for the RDR-221 Phase 1 reviews (nexus-ger02.5 code review, nexus-ger02.6 critique), script side.

The brief id that lives only inside brief.md, the card anchor warning, document-layer treatments of the
site-page rules, and the small review items. The acceptance verdicts have their own tests in
test_acceptance_tools.py; the RDR and line-editor wording is pinned at the end.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

from tests.prose_edit.conftest import ROOT, Prose
from tests.prose_edit.test_brief import (
    AGENT,
    SKILL,
    brief_ok,
    edit,
    fenced,
    proposal,
    run_brief,
    seed,
)
from tests.prose_edit.test_fix_round1 import _age, _idle, _keyed_phrases, _step
from tests.prose_edit.test_review import DOC, E1, E2, REVIEW, run_review, start
from tests.prose_edit.test_review_apply_safety import hook, tracked

RDR = ROOT / "docs" / "rdr" / "rdr-221-prose-editor.md"
ID_LINE = re.compile(r"\nBrief id: ([0-9a-f]{12})\n\Z")


def _build(prose: Prose, *args: str) -> tuple[Path, str]:
    """A path-run build with --work: (the work directory, the brief as printed)."""
    out = brief_ok(prose, "build", *args, "--work")
    head, brief = out.split("\n\n", 1)
    assert head.startswith("WORK=") and "\n" not in head, head
    return Path(head[len("WORK="):]), brief


def _file_id(work: Path) -> str:
    m = ID_LINE.search((work / "brief.md").read_text(encoding="utf-8"))
    assert m, "brief.md does not end with a `Brief id: <12 hex>` line"
    return m.group(1)


# ---------------------------------------------------------------------------
# 2. The brief id is inside brief.md and nowhere else
# ---------------------------------------------------------------------------


def test_build_prints_no_id_and_brief_md_ends_with_the_id_of_the_text_above_it(prose: Prose) -> None:
    out = brief_ok(prose, "build", "docs/x.md", "--work")
    assert "Brief id" not in out and "BRIEF_SHA" not in out
    work, brief = _build(prose, "docs/x.md")
    try:
        data = (work / "brief.md").read_text(encoding="utf-8")
        m = ID_LINE.search(data)
        assert m and data[:m.start()] == brief  # the file is the printed brief, then a blank line, then the id
        assert m.group(1) == hashlib.sha256(brief.encode("utf-8")).hexdigest()[:12]
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_stdin_build_in_a_work_directory_prints_the_brief_alone_and_writes_the_id_inside_the_file(
    prose: Prose,
) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        (work / "input.txt").write_text("fix: a thing\n", encoding="utf-8")
        out = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(work / "input.txt"))
        assert out.startswith("# Editing brief") and "Brief id" not in out and "BRIEF_SHA" not in out
        data = (work / "brief.md").read_text(encoding="utf-8")
        m = ID_LINE.search(data)
        assert m and data[:m.start()] == out
        assert m.group(1) == hashlib.sha256(out.encode("utf-8")).hexdigest()[:12]
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_filter_compares_the_reply_with_the_id_the_file_ends_with_and_says_what_a_miss_shows(prose: Prose) -> None:
    work, _ = _build(prose, "docs/x.md")
    try:
        sha = _file_id(work)
        save = str(work / "filtered.json")

        def go(**extra: object) -> tuple[int, str, list[str]]:
            proc = run_brief(prose, "filter", "docs/x.md", "--save", save, stdin=fenced(proposal([], **extra)))
            return proc.returncode, proc.stderr, json.loads(proc.stdout)["warnings"]

        assert go(brief_sha=sha) == (0, "", [])
        missing = go()
        assert missing[0] == 0 and any("brief_sha" in w for w in missing[2])
        wrong = go(brief_sha="0" * 12)
        assert wrong[0] == 0 and any(sha in w for w in wrong[2])
        for _, err, warnings in (missing, wrong):
            text = err + " ".join(warnings)
            # a copied id is not proof of a read, so the warning must not say a right id proves one, and a
            # miss must say what it does show: the file's last line was not read
            assert "last line" in text and "brief.md" in text
            assert "may not have read" not in text
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_filter_warns_when_brief_md_was_changed_after_it_was_built(prose: Prose) -> None:
    work, _ = _build(prose, "docs/x.md")
    try:
        sha = _file_id(work)
        path = work / "brief.md"
        path.write_text(path.read_text(encoding="utf-8").replace("# Editing brief", "# Edited brief"), encoding="utf-8")
        proc = run_brief(prose, "filter", "docs/x.md", "--save", str(work / "filtered.json"),
                         stdin=fenced(proposal([], brief_sha=sha)))
        warnings = json.loads(proc.stdout)["warnings"]
        assert proc.returncode == 0 and any("changed" in w and "brief.md" in w for w in warnings), warnings
        path.write_text("# Editing brief\nno id line at all\n", encoding="utf-8")
        again = run_brief(prose, "filter", "docs/x.md", "--save", str(work / "filtered.json"),
                          stdin=fenced(proposal([], brief_sha=sha)))
        assert any("no id line" in w for w in json.loads(again.stdout)["warnings"])
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_the_dispatch_prompt_names_only_the_path_and_the_editor_reads_the_id_from_the_file() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    assert "BRIEF_SHA" not in skill  # step 4 prints no id and step 7 passes none
    step7 = _step(skill, 7)
    assert "WORK/brief.md" in step7 and "brief_sha" in step7 and "Brief id" in step7
    assert "last line" in step7
    step4 = _step(skill, 4)
    assert "BRIEF_SHA" not in step4 and "Brief id" in step4  # says the id is inside the file, not printed
    step9 = _step(skill, 9)
    assert "brief_sha" in step9 and "may not have read" not in step9 and "last line" in step9
    agent = AGENT.read_text(encoding="utf-8")
    assert "BRIEF_SHA" not in agent and "last line" in agent and "Brief id" in agent
    assert "brief_sha" in agent[agent.index("## Output"):]


# ---------------------------------------------------------------------------
# 7. The stored voice card is the anchor: a reply card that differs is a warning
# ---------------------------------------------------------------------------

CARD = "First person plural, plain register. Refrain: the closing line of each section."


def _store_card(prose: Prose, text: str = CARD, doc: str = "docs/x.md") -> None:
    prose.ok("voice-card", doc, "--from-stdin", stdin={"voice_card": text})


def test_filter_warns_when_the_brief_carried_a_stored_card_and_the_reply_card_differs(prose: Prose) -> None:
    _store_card(prose)
    work, brief = _build(prose, "docs/x.md")
    try:
        assert CARD in brief
        sha = _file_id(work)
        save = str(work / "filtered.json")
        same = run_brief(prose, "filter", "docs/x.md", "--save", save,
                         stdin=fenced(proposal([], brief_sha=sha, voice_card=f"  {CARD}\n")))
        assert same.returncode == 0 and json.loads(same.stdout)["warnings"] == [] and same.stderr == ""
        drift = run_brief(prose, "filter", "docs/x.md", "--save", save,
                          stdin=fenced(proposal([], brief_sha=sha, voice_card="Another card the editor wrote.")))
        out = json.loads(drift.stdout)
        assert drift.returncode == 0  # a warning, never fatal
        assert any("voice_card" in w and "author-approved" in w for w in out["warnings"]), out["warnings"]
        assert "voice_card" in drift.stderr
        assert out["voice_card"] == "Another card the editor wrote."  # the proposal itself is left as the editor sent it
        gone = run_brief(prose, "filter", "docs/x.md", "--save", save, stdin=fenced(proposal([], brief_sha=sha)))
        assert any("voice_card" in w for w in json.loads(gone.stdout)["warnings"])  # a reply card that is not the stored one, whatever it is, is the same warning
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_brief_with_no_stored_card_never_warns_about_the_reply_card(prose: Prose) -> None:
    work, brief = _build(prose, "docs/x.md")
    try:
        assert "<voice_card>" not in brief
        proc = run_brief(prose, "filter", "docs/x.md", "--save", str(work / "filtered.json"),
                         stdin=fenced(proposal([], brief_sha=_file_id(work), voice_card="whatever the editor wrote")))
        assert json.loads(proc.stdout)["warnings"] == [] and proc.stderr == ""
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_step_13_shows_the_stored_card_back_after_saving_it() -> None:
    text = SKILL.read_text(encoding="utf-8")
    step = text[text.index("\n13. "):]
    bullet = step[step.index("The voice card the editor returned"):step.index("An edit the author says is never a defect")]
    after = bullet[bullet.index("--from-stdin"):]  # round 5 made the show-back its own numbered sub-step after the save
    assert "show the stored card back" in after.lower() and "stored" in after and "voice_card" in after


# ---------------------------------------------------------------------------
# 5. A document entry can ignore or query a site-page section 3 rule
# ---------------------------------------------------------------------------

ESSAY = "docs/exploration/e.md"
SEMICOLON_RULE = "Plain technical English for non-native readers..."
STALE_OPEN = "A deleted rule..."


def _sheet_of(prose: Prose, path: str = ESSAY) -> str:
    text = brief_ok(prose, "build", path, "--genre", "exploration-essay")
    return text[text.index("## 3. Style sheet"):text.index("## 4. Not a defect")]


def _treat(kind: str, opening: str) -> dict:
    key = {"ignored": "site_page_section3_ignored", "query_only": "site_page_section3_query_only",
           "note_only": "site_page_section3_note_only"}[kind]
    return {key: [f'"{opening}" (the author\'s choice for this essay)']}


def test_a_document_entry_can_ignore_the_semicolon_rule_for_one_exploration_essay(prose: Prose) -> None:
    assert "Active voice, no semicolons" in _sheet_of(prose)  # the genre layer applies it by default
    seed(prose, "doc", lists=_treat("ignored", SEMICOLON_RULE), path=ESSAY)
    assert "no semicolons" not in _sheet_of(prose)
    assert "no semicolons" in _sheet_of(prose, "docs/exploration/other.md")  # another document keeps it


def test_a_document_entry_can_make_a_site_page_rule_query_only_or_note_only(prose: Prose) -> None:
    seed(prose, "doc", lists=_treat("query_only", SEMICOLON_RULE), path=ESSAY)
    sheet = _sheet_of(prose)
    assert "QUERY ONLY" in sheet and "no semicolons" in sheet
    rules = sheet[:sheet.index("QUERY ONLY")]
    assert "no semicolons" not in rules  # moved out of the rules the editor applies as edits
    seed(prose, "doc", lists=_treat("note_only", SEMICOLON_RULE), path="docs/exploration/n.md")
    note = _sheet_of(prose, "docs/exploration/n.md")
    assert "editor's note only" in note and "no semicolons" in note[note.index("editor's note only"):]


def test_the_narrower_layer_wins_when_repo_and_document_treat_the_same_rule_differently(prose: Prose) -> None:
    seed(prose, "repo", lists=_treat("ignored", SEMICOLON_RULE))
    assert "no semicolons" not in _sheet_of(prose)
    seed(prose, "doc", lists=_treat("query_only", SEMICOLON_RULE), path=ESSAY)
    sheet = _sheet_of(prose)  # the document says query: it beats the repo's ignore
    assert "QUERY ONLY" in sheet and "no semicolons" in sheet[sheet.index("QUERY ONLY"):]
    assert "no semicolons" not in _sheet_of(prose, "docs/exploration/other.md")  # the repo ignore still holds there


def test_a_stale_document_treatment_stops_the_build_and_names_the_document_layer(prose: Prose) -> None:
    seed(prose, "doc", lists=_treat("ignored", STALE_OPEN), path=ESSAY)
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1 and STALE_OPEN.rstrip(".") in proc.stderr
    assert run_brief(prose, "build", "docs/exploration/other.md", "--genre", "exploration-essay").returncode == 0


def test_the_section_3_rules_heading_no_longer_says_apply_as_written() -> None:
    text = (ROOT / ".claude" / "skills" / "prose-edit" / "scripts" / "brief.py").read_text(encoding="utf-8")
    assert "apply as written" not in text  # it contradicted "the narrower layer wins"


# ---------------------------------------------------------------------------
# 8. Small items
# ---------------------------------------------------------------------------


def test_each_refusal_for_a_missing_or_stale_dry_run_matches_exactly_one_key_of_skill_step_12(
    prose: Prose, repo: Path
) -> None:
    keys = _keyed_phrases(_step(SKILL.read_text(encoding="utf-8"), 12))
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])

    def matched(stderr: str) -> set[str]:
        return {k for k in keys if k in stderr}

    none = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert none.returncode == 1 and matched(none.stderr) == {"dry run"}, (matched(none.stderr), none.stderr)
    run_review(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    other = run_review(prose, "apply", "--work", str(work), "--accept", "2", dry_first=False)
    assert other.returncode == 1 and matched(other.stderr) == {"dry run"}, (matched(other.stderr), other.stderr)
    f.write_text(f.read_text(encoding="utf-8") + "AUTHOR SAVE\n", encoding="utf-8")
    saved = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert saved.returncode == 1 and matched(saved.stderr) == {"dry run"}, (matched(saved.stderr), saved.stderr)


def test_filter_save_renews_the_work_sentinel_and_a_swept_directory_is_named_expired(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "s.md").write_text("An editable sentence here.\n", encoding="utf-8")
    work = Path(brief_ok(prose, "tmpdir").strip())
    _age(work)
    proc = run_brief(prose, "filter", "docs/s.md", "--save", str(work / "filtered.json"),
                     stdin=fenced(proposal([edit(1, "An editable sentence here.", "")])))
    assert proc.returncode == 0 and _idle(work) < 600
    other = Path(brief_ok(prose, "tmpdir").strip())  # a second run's sweep must leave this directory
    try:
        assert work.is_dir()
    finally:
        brief_ok(prose, "rmtmp", str(other))
        brief_ok(prose, "rmtmp", str(work))
    gone = prose.tmp / "prose-edit-deadbeef"
    swept = run_brief(prose, "filter", "docs/s.md", "--save", str(gone / "filtered.json"),
                      stdin=fenced(proposal([])))
    assert swept.returncode == 1 and "expired" in swept.stderr and "swept" in swept.stderr
    assert "not inside a prose-edit work directory" not in swept.stderr
    bogus = run_brief(prose, "filter", "docs/s.md", "--save", str(prose.tmp / "notmine" / "filtered.json"),
                      stdin=fenced(proposal([])))
    assert bogus.returncode == 1 and "expired" not in bogus.stderr


def test_a_save_by_the_author_is_not_described_as_the_file_not_being_changed(prose: Prose, repo: Path, tmp_path: Path) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **hook(tmp_path, "open(target, 'a').write('AUTHOR SAVE\\n')")}
    stored = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", env=env)
    assert stored.returncode == 1 and "run apply again" in stored.stderr
    assert "file was not changed" not in stored.stderr  # the author's save changed it
    assert "apply did not change the file" in stored.stderr and "rejections already stored" in stored.stderr
    assert f.read_text(encoding="utf-8") == DOC + "AUTHOR SAVE\n"


def test_the_state_comes_before_the_retry_words_when_the_message_already_ends_with_them() -> None:
    spec = importlib.util.spec_from_file_location("prose_edit_review_r3", REVIEW)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    exc = mod._aborted(mod._user("the file is open elsewhere. Save and close it, then run apply again"), stored=False)
    text = str(exc)
    assert text.index("nothing was written") < text.index("run apply again") and text.count("run apply again") == 1


def test_reasons_file_keys_that_normalise_to_the_same_edit_are_refused(prose: Prose, repo: Path) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    rf = work / "reasons.json"
    rf.write_text('{"2": "first", "02": "second"}', encoding="utf-8")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2",
                      "--reasons-file", str(rf), "--dry-run")
    assert proc.returncode == 1 and "--reasons-file" in proc.stderr and "two reasons for edit 2" in proc.stderr
    assert "Traceback" not in proc.stderr and f.read_text(encoding="utf-8") == DOC
    rf.write_text('{"02": "only one"}', encoding="utf-8")  # a single zero-padded key is the same edit and is fine
    ok = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2",
                    "--reasons-file", str(rf), "--dry-run")
    assert ok.returncode == 0 and json.loads(ok.stdout)["reject"][0]["reason"] == "only one"


def test_a_promote_dry_run_sweeps_stale_tmp_files_as_well_as_stale_records(prose: Prose, repo: Path) -> None:
    doc = "docs/x.md"
    prose.ok("reject", doc, "--old", "just", "--new", "")
    folder = repo / ".git" / "prose-edit-promote"
    folder.mkdir(exist_ok=True)
    old_tmp, new_tmp, old_json = folder / ("a" * 64 + ".tmp"), folder / ("b" * 64 + ".tmp"), folder / ("c" * 64 + ".json")
    for p in (old_tmp, new_tmp, old_json):
        p.write_text("{}", encoding="utf-8")
    then = time.time() - 3 * 3600
    for p in (old_tmp, old_json):
        os.utime(p, (then, then))
    prose.ok("promote", doc, "1", "--level", "repo", "--dry-run")
    assert not old_tmp.exists() and not old_json.exists() and new_tmp.exists()


# ---------------------------------------------------------------------------
# 1. The qualifier ruling is one statement, and the RDR body keeps its accepted text
# ---------------------------------------------------------------------------


def test_the_rdr_body_keeps_its_accepted_qualifier_text_and_the_history_says_the_ruling_is_in_effect() -> None:
    text = RDR.read_text(encoding="utf-8")
    technical = text[text.index("is this qualifier justified by a"):text.index("A construction that matches a device")]
    assert "Unjustified qualifiers are proposed for cutting; justified\nones stay; unclear ones become queries." in technical
    assert "Filler qualifiers" not in text and "(Sam, 2026-09-30)" not in text
    assert ("- **Scenario**: A sentence with one justified and one unjustified qualifier. — **Verify**: only the "
            "unjustified one is proposed for cutting.") in text
    history = text[text.index("- 2026-10-03: Phase 1"):]
    items = [ln for ln in history.splitlines() if "qualifier" in ln.lower()]
    assert len(items) == 1, items
    line = items[0]
    assert "2026-09-30" in line and "in effect" in line and "post-mortem" in line
    assert "amended" not in line  # the body is not amended: the deviation is recorded at close
    assert "Phase 1 (Steps 1.2 to 1.5)" in history  # the entry itself is still there


def test_the_line_editor_states_the_qualifier_ruling_once_and_has_no_keep_it_class() -> None:
    agent = AGENT.read_text(encoding="utf-8")
    row = next(ln for ln in agent.splitlines() if ln.startswith("| Is this qualifier"))
    assert '"basically", "really", "quite" and "just" used as filler' in row
    assert "keep it" not in agent and "justified" not in agent.lower()
    assert "never cut" in row and "query" in row
    rule = next(ln for ln in agent.splitlines() if ln.startswith("- Cut only the filler words"))
    assert rule.endswith("is a query.")
