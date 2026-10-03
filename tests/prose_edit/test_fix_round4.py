# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 4 for the RDR-221 Phase 1 reviews (nexus-ger02.5/.6/.7): the exit batch's command-form blocker,
the protected and range verdicts on a non-unique old string, the author's path to a document-layer section 3
treatment, the teach runner's second run and starting state, and the batch records.

Every transcript here is planted: no model runs in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from tests.prose_edit.conftest import ROOT, Prose
from tests.prose_edit.test_acceptance_tools import (
    ACC,
    LINDA,
    PROTECTED,
    QA,
    QA_FILLER,
    QA_MAY_QUERY,
    XANADU,
    _both,
    _ed,
    _qual,
    _res,
    _run,
    _use,
    _verdict,
    _verdicts,
)
from tests.prose_edit.test_brief import NAMES_A_REPAIR, SKILL, brief_ok, run_brief, seed
from tests.prose_edit.test_fix_round1 import _step
from tests.prose_edit.test_fix_round3 import ESSAY, SEMICOLON_RULE, STALE_OPEN, _sheet_of, _treat
from tests.prose_edit.test_teach_acceptance import (
    AFTER,
    CARD,
    DRY,
    REFUSED,
    T,
    WORKER,
    _all_runs,
    _s6,
    _t1,
    _t2c,
    _t4,
    _t4c,
    _t5,
    _write,
)

SKILL_TEXT = SKILL.read_text(encoding="utf-8")
MEMORY_PY = ROOT / ".claude" / "skills" / "prose-edit" / "scripts" / "memory.py"
PREFIXES = {
    "BRIEF": "python3 .claude/skills/prose-edit/scripts/brief.py",
    "MEMORY": "python3 .claude/skills/prose-edit/scripts/memory.py",
    "REVIEW": "python3 .claude/skills/prose-edit/scripts/review.py",
}


# ---------------------------------------------------------------------------
# 1. The command form the skill teaches is the one the permission rule allows
# ---------------------------------------------------------------------------


def _allowlist() -> set[str]:
    """The command prefixes run-scenario.sh pre-approves: the text inside each Bash(<prefix>:*) rule."""
    sh = (ACC / "run-scenario.sh").read_text(encoding="utf-8")
    return {m for m in re.findall(r'"Bash\(([^)]*?):\*\)"', sh) if "prose-edit" in m}


def _command_spans(text: str) -> list[str]:
    """Every inline code span of the skill that is a command: it starts with BRIEF, MEMORY or REVIEW."""
    return re.findall(r"`((?:BRIEF|MEMORY|REVIEW) [^`]+)`", text)


def _expand(span: str, work: str = "/private/var/folders/ab/T/prose-edit-ab12cd34") -> str:
    """What the model types for a command form of the skill: the row's prefix text, then the arguments."""
    name, _, rest = span.partition(" ")
    return f"{PREFIXES[name]} {rest.replace('WORK', work)}"


def test_the_skill_table_prefixes_are_the_exact_text_the_runner_allows_and_the_verdicts_count_as_on_list() -> None:
    v = _verdicts()
    rows = dict(re.findall(r"^\| (BRIEF|MEMORY|REVIEW) \| `([^`]+)` \|$", SKILL_TEXT, re.MULTILINE))
    assert rows == PREFIXES
    assert _allowlist() == set(PREFIXES.values()) == set(v.ALLOWED_BASH)
    for prefix in PREFIXES.values():
        assert v.is_allowed_bash(f"{prefix} parse 'x.md'")


def test_every_command_form_the_skill_tells_the_model_to_run_matches_the_allowlist_and_the_verdicts() -> None:
    v = _verdicts()
    spans = _command_spans(SKILL_TEXT)
    assert len(spans) >= 12, spans  # the extraction saw the skill's commands, not a handful
    assert {s.split(" ")[0] for s in spans} == {"BRIEF", "MEMORY", "REVIEW"}
    allow = _allowlist()
    for span in spans:
        command = _expand(span)
        assert any(command.startswith(prefix + " ") for prefix in allow), (span, command)
        assert v.is_allowed_bash(command), (span, command)
        assert not command.startswith(("cd ", "python3 '", "python3 ./")), command


def test_the_shapes_the_exit_batch_saw_are_outside_the_allowlist_and_off_list_in_the_verdicts() -> None:
    v = _verdicts()
    allow = _allowlist()
    ok = "python3 .claude/skills/prose-edit/scripts/brief.py parse 'x.md'"
    assert v.is_allowed_bash(ok) and any(ok.startswith(p + " ") for p in allow)
    for seen in ("python3 '.claude/skills/prose-edit/scripts/brief.py' parse 'x.md'",
                 "python3 './.claude/skills/prose-edit/scripts/brief.py' parse 'x.md'",
                 "cd '/Users/x/wt' && python3 .claude/skills/prose-edit/scripts/brief.py parse 'x.md'"):
        assert not v.is_allowed_bash(seen), seen
        assert not any(seen.startswith(p + " ") for p in allow), seen
    events = [_use("a", "Bash", {"command": seen}) for seen in ("cd '/x' && " + ok,)]
    assert v.compliance(events, None)["off_list_bash"] == ["cd '/x' && " + ok]


def test_the_skill_writes_the_prefix_unquoted_relative_and_forbids_cd_and_a_leading_dot_slash() -> None:
    head = SKILL_TEXT[:SKILL_TEXT.index("## Invocation")]
    assert "put each token and each path in single quotes" not in SKILL_TEXT  # the rule that broke the batch
    assert "Never run `cd`" in head
    assert "unquoted" in head and "`./`" in head
    assert "exactly as it is shown" in head or "exactly as shown" in head
    assert "only the arguments" in head.lower()
    assert "the directory this session started in" in head
    assert re.search(r"\bBRIEF\b.*\bMEMORY\b.*\bREVIEW\b.*prefix", head, re.DOTALL)
    assert not NAMES_A_REPAIR.search(SKILL_TEXT)
    for span in re.findall(r"`([^`]+)`", SKILL_TEXT):
        assert not re.match(r"cd\s", span) or "&&" in span, span  # a cd appears only as the forbidden shape


def test_every_runner_goes_through_run_scenario_so_one_allowlist_covers_them_all() -> None:
    scripts = sorted(ACC.glob("*.sh"))
    names = {p.name for p in scripts}
    assert {"run-scenario.sh", "run-canaries.sh", "run-review.sh", "run-memory-gate.sh", "run-teach.sh"} <= names
    for p in scripts:
        text = p.read_text(encoding="utf-8")
        if p.name == "run-scenario.sh":
            assert text.count("--allowedTools") == 1
            continue
        assert "--allowedTools" not in text and "--permission-mode" not in text, p.name
        assert not re.search(r"\bclaude\b\s+-p|CLAUDE_BIN", text), p.name
        assert "run-scenario.sh" in text, p.name


# ---------------------------------------------------------------------------
# 2. protected and range: a non-unique old is its own problem, scored nowhere else
# ---------------------------------------------------------------------------


def test_a_non_unique_old_is_reported_as_such_and_never_as_a_protected_region_hit() -> None:
    # "really quite" is on lines 3, 9, 11, 14 and 21 of protected.md: the filter keeps it (line 9 is editable)
    # and apply would refuse it. The cause is the editor's own once-only rule, not the frontmatter.
    assert PROTECTED.count("really quite") == 5 and PROTECTED.count("very basically") == 3
    for phrase, times in (("really quite", 5), ("very basically", 3)):
        verdict, note = _verdict("protected", _both([_ed(1, phrase, "")]), PROTECTED)
        assert verdict == "FAIL", phrase
        assert f"occurs {times} times" in note and "apply refuses" in note and "edit 1" in note, note
        assert "protected" not in note.split("; dropped by the filter")[0], note  # not blamed on a region
    # a unique old that really is on a protected line is still reported as protected
    unique = _verdict("protected", _both([_ed(1, "A very basically important expiry column", "x")]), PROTECTED)
    assert unique[0] == "FAIL" and "protected table" in unique[1] and "occurs" not in unique[1]
    # and a unique editable one still passes
    assert _verdict("protected", _both([_ed(1, "The scheduler basically hands each job", "x")]), PROTECTED)[0] == "PASS"


def test_the_non_unique_old_is_reported_for_every_kind_and_beside_the_other_problems() -> None:
    twice = _both([_ed(1, "really quite", "")])
    assert "occurs 5 times" in _verdict("budget", twice, PROTECTED)[1]
    both = _both([_ed(1, "really quite", ""), _ed(2, "A very basically important expiry column", "x")])
    note = _verdict("protected", both, PROTECTED)[1]
    assert "occurs 5 times" in note and "protected table" in note  # each is its own problem


TWICE_DOC = "".join(f"Line {i} has plain words in it.\n" for i in range(1, 4)) + \
    "Line 4 says twice here and twice here again.\nLine 5 says twice here.\n" + \
    "".join(f"Line {i} has plain words in it.\n" for i in range(6, 11))


def test_range_reports_an_old_that_occurs_twice_and_an_anchor_split_across_the_range_edge() -> None:
    target = "CHANGELOG.md:3-5"
    # an old that occurs on lines 4 and 5, both inside the range: apply refuses it, so it is a problem
    repeated = [_ed(1, "twice here", "x")]
    verdict, note = _verdict("range", _run({"edits": repeated}, {"edits": repeated}, target=target), TWICE_DOC)
    assert verdict == "FAIL" and "occurs 3 times" in note and "outside" not in note
    # an old whose only occurrence is outside the range is still outside, and one inside still passes
    inside = [_ed(1, "Line 4 says twice here and twice here again.", "x")]
    assert _verdict("range", _run({"edits": inside}, {"edits": inside}, target=target), TWICE_DOC)[0] == "PASS"
    out = [_ed(1, "Line 8 has plain words", "x")]
    assert "outside lines 3-5" in _verdict("range", _run({"edits": out}, {"edits": out}, target=target), TWICE_DOC)[1]
    # a query anchor that repeats: all occurrences inside the range is fine, some outside is ambiguous
    kept = [_ed(1, "Line 3 has plain words", "x")]
    inside_q = {"n": 1, "anchor": "twice here", "text": "?"}
    ok = _run({"edits": kept}, {"edits": kept, "queries": [inside_q]}, target=target)
    assert _verdict("range", ok, TWICE_DOC)[0] == "PASS"
    split = {"n": 1, "anchor": "has plain words in it", "text": "?"}  # lines 1-3 and 6-10
    bad = _run({"edits": kept}, {"edits": kept, "queries": [split]}, target=target)
    verdict, note = _verdict("range", bad, TWICE_DOC)
    assert verdict == "FAIL" and "query 1" in note and "some outside" in note
    # the old behaviour passed any occurrence inside; the real fixture's repeated phrase shows it
    fixture = (ROOT / "tests/prose_edit/fixtures/range-notes.md").read_text(encoding="utf-8")
    again = [_ed(1, "really quite", "")]  # lines 7, 14 and 26: only 14 is inside 10-20
    target2 = "tests/prose_edit/fixtures/range-notes.md:10-20"
    verdict, note = _verdict("range", _run({"edits": again}, {"edits": again}, target=target2), fixture)
    assert verdict == "FAIL" and "occurs 3 times" in note


# ---------------------------------------------------------------------------
# 3. The author's path to a document-layer section 3 treatment
# ---------------------------------------------------------------------------

KEYS = ("site_page_section3_ignored", "site_page_section3_query_only", "site_page_section3_note_only")


def test_skill_step_13_says_how_a_correction_of_a_section_3_rule_is_stored() -> None:
    step = _step(SKILL_TEXT, 13)
    for key in KEYS:
        assert key in step, key
    assert "opening words" in step and '"<opening words>..."' in step
    assert "MEMORY add-entry --level" in step and "this document" in step
    assert "ask which" in step.lower() or "which treatment" in step.lower()
    assert not NAMES_A_REPAIR.search(step)


def test_memory_py_usage_names_the_section_3_treatment_keys() -> None:
    doc = MEMORY_PY.read_text(encoding="utf-8").split('"""')[1]
    for key in KEYS:
        assert key in doc, key
    out = subprocess.run([sys.executable, str(MEMORY_PY), "add-entry", "--help"], capture_output=True, text=True,
                         env={**os.environ, "COLUMNS": "200"}, timeout=60)
    assert out.returncode == 0
    for key in KEYS:
        assert key in out.stdout, key
    assert "opening words" in out.stdout


def test_a_stale_treatment_names_the_remove_command_for_its_layer_and_the_command_works(prose: Prose) -> None:
    seed(prose, "doc", lists=_treat("ignored", STALE_OPEN), path=ESSAY)
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1 and not NAMES_A_REPAIR.search(proc.stderr)
    assert f"memory.py entries --level doc --path {ESSAY} --remove-item" in proc.stderr
    assert "site_page_section3_ignored=" in proc.stderr
    cmd = proc.stderr[proc.stderr.index("memory.py entries"):].splitlines()[0]
    argv = shlex.split(cmd)[1:]
    assert prose.run(*argv).returncode == 0  # the command the message names removes the entry
    assert run_brief(prose, "build", ESSAY, "--genre", "exploration-essay").returncode == 0
    # the same for a repo-level entry: no --path
    seed(prose, "repo", lists=_treat("query_only", STALE_OPEN))
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1 and "memory.py entries --level repo --remove-item" in proc.stderr
    assert "--path" not in proc.stderr.split("--remove-item")[0].split("memory.py entries")[-1]
    argv = shlex.split(proc.stderr[proc.stderr.index("memory.py entries"):].splitlines()[0])[1:]
    assert prose.run(*argv).returncode == 0
    assert run_brief(prose, "build", ESSAY, "--genre", "exploration-essay").returncode == 0


def test_the_entry_the_skill_teaches_reaches_the_brief_through_add_entry(prose: Prose) -> None:
    """The author's flow of step 13, end to end: a document-layer entry written as the skill shows it."""
    body = {"scalars": {}, "lists": {"site_page_section3_ignored": [f'"{SEMICOLON_RULE}" (semicolons are this essay\'s voice)']}}
    assert "no semicolons" in _sheet_of(prose)
    prose.ok("add-entry", "--level", "doc", "--path", ESSAY, "--from-stdin", stdin=body)
    assert "no semicolons" not in _sheet_of(prose)
    assert brief_ok(prose, "build", ESSAY, "--genre", "exploration-essay")


# ---------------------------------------------------------------------------
# 4. Scenario 6 needs a tie to the entry; a qualifier is queried only when the anchor holds the word
# ---------------------------------------------------------------------------


def test_scenario_6_fails_on_a_worker_anchored_query_that_does_not_ask_about_the_entry() -> None:
    unrelated = {"n": 1, "anchor": "The worker takes one job", "text": "How long does it hold the job?"}
    verdict, note = T.check_s6(_s6(queries=[unrelated]))
    assert verdict == "FAIL" and "consumer" in note
    in_text = {"n": 1, "anchor": "The worker takes one job", "text": "The entry says consumer: rename it here?"}
    assert T.check_s6(_s6(queries=[in_text]))[0] == "PASS"
    # an edit still has to be worker -> consumer
    assert T.check_s6(_s6(edits=[WORKER]))[0] == "PASS"
    cut = {"n": 1, "old": "A worker that dies mid-job releases the lease", "new": "A lease is released", "reason": "r"}
    assert T.check_s6(_s6(edits=[cut], queries=[unrelated]))[0] == "FAIL"


def test_a_qualifier_counts_as_queried_only_when_its_word_is_in_the_query_anchor() -> None:
    elsewhere = {"n": 1, "anchor": "stale entries", "text": "Does 'may' mean sometimes or always?"}
    verdict, note = _verdict("qa", _qual([QA_FILLER], [elsewhere]), QA)
    assert verdict == "FAIL" and "'may'" in note and "queried" in note
    assert _verdict("qa", _qual([QA_FILLER], [QA_MAY_QUERY]), QA)[0] == "PASS"
    # both of a document's qualifiers need their own anchor: one query's text naming the other does not count
    qc = (ROOT / "tests/prose_edit/fixtures/qualifiers-c.md").read_text(encoding="utf-8")
    likely = {"n": 1, "anchor": "will likely double", "text": "Is that a forecast? The word truly is used again below."}
    verdict, note = _verdict("qc", _qual([], [likely]), qc)
    assert verdict == "FAIL" and "'truly'" in note


# ---------------------------------------------------------------------------
# 5. run-teach.sh: a second edit run after the card is stored
# ---------------------------------------------------------------------------


def test_the_card_reuse_check_passes_when_the_brief_holds_the_stored_card_and_the_reply_returns_it() -> None:
    verdict, note = T.check_card_reuse(_t5(), AFTER)
    assert verdict == "PASS", note
    # a two-line card: the Read result is line-numbered, the comparison is on the card's words
    card = "First person, plain register.\nRefrain: the closing sentence of each paragraph."
    after = {**AFTER, "voice_card": {"text": card, "at": "x"}}
    assert T.check_card_reuse(_t5(in_brief=card, reply_card=card.replace("\n", " ")), after)[0] == "PASS"


def test_the_card_reuse_check_fails_on_each_way_the_stored_card_can_miss_the_editor() -> None:
    cases = {
        "the brief holds no card": T.check_card_reuse(_t5(in_brief=None), AFTER),
        "the brief holds another card": T.check_card_reuse(_t5(in_brief="A card the author never approved."), AFTER),
        "the reply returns another card": T.check_card_reuse(_t5(reply_card="First person, plain register."), AFTER),
        "the reply returns no card": T.check_card_reuse(_t5(reply_card=None), AFTER),
        "the filter warned about the card": T.check_card_reuse(
            _t5(warnings=("the editor's voice_card is not the author-approved card the brief gave it (section 2)",)), AFTER),
        "the editor never read the brief": T.check_card_reuse(_t5(read=[]), AFTER),
        "no editor reply": T.check_card_reuse([], AFTER),
    }
    for why, (verdict, note) in cases.items():
        assert verdict == "FAIL", (why, note)
    # an unrelated filter warning is not a card warning
    assert T.check_card_reuse(_t5(warnings=("edit 2 may cover more than one sentence",)), AFTER)[0] == "PASS"


def test_the_card_reuse_check_is_not_measurable_without_a_stored_card_or_a_filter_output() -> None:
    assert T.check_card_reuse(_t5(), None)[0] == "NOT-MEASURABLE"
    assert T.check_card_reuse(_t5(), {**AFTER, "voice_card": None})[0] == "NOT-MEASURABLE"
    no_filter = _t5()[:-2]  # the transcript ends before the skill's filter ran
    assert T.check_card_reuse(no_filter, AFTER)[0] == "NOT-MEASURABLE"


def test_run_teach_runs_a_fresh_edit_after_the_promote_and_the_readme_says_what_stays_by_hand() -> None:
    sh = (ACC / "run-teach.sh").read_text(encoding="utf-8")
    t5 = next(ln for ln in sh.splitlines() if re.match(r"\s*run t5 ", ln))
    assert "RESUME_FROM" not in t5 and f"/prose-edit $DOC" in t5 and "--genre reference-doc" in t5  # a fresh session
    assert sh.index("run t4c ") < sh.index("run t5 ")
    assert "t5" in sh.split("# Between")[0]  # the header lists it
    readme = (ACC / "README.md").read_text(encoding="utf-8")
    assert "`t5`" in readme
    lowered = readme.lower()
    assert "by hand" in lowered and "with and without the card" in lowered
    assert "correction" in lowered and "add-entry" in readme


def test_main_scores_the_card_reuse_row_and_a_missing_t5_is_not_a_pass(tmp_path: Path, capsys: object) -> None:
    _all_runs(tmp_path)
    assert T.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "reuse" in out
    _write(tmp_path, "t5", _t5(reply_card="Another card."))
    assert T.main([str(tmp_path)]) == 1
    (tmp_path / "t5.jsonl").unlink()
    assert T.main([str(tmp_path)]) == 3


# ---------------------------------------------------------------------------
# 6. run-teach.sh: a clean start, and a first run that can show a rejection
# ---------------------------------------------------------------------------


def test_unclean_start_names_what_is_left_over_from_an_earlier_run() -> None:
    assert T.unclean_start({"voice_card": None}, {"rejections": []}) is None
    assert "voice card" in (T.unclean_start({"voice_card": {"text": "x", "at": "y"}}, {"rejections": []}) or "")
    assert "rejection" in (T.unclean_start({"voice_card": None}, {"rejections": [{"n": 1}]}) or "")
    assert T.unclean_start({}, {"rejections": []}) is not None  # a shape it does not know is not clean
    assert T.unclean_start({"voice_card": None}, None) is not None


def test_the_clean_start_command_aborts_with_exit_one_and_run_teach_calls_it_before_any_session(tmp_path: Path) -> None:
    clean_v, clean_r = tmp_path / "v.json", tmp_path / "r.json"
    clean_v.write_text(json.dumps({"voice_card": None}), encoding="utf-8")
    clean_r.write_text(json.dumps({"rejections": []}), encoding="utf-8")
    run = [sys.executable, str(ACC / "teach_verdicts.py"), "clean-start", str(clean_v), str(clean_r)]
    assert subprocess.run(run, capture_output=True, text=True).returncode == 0
    clean_r.write_text(json.dumps({"rejections": [{"n": 1}]}), encoding="utf-8")
    proc = subprocess.run(run, capture_output=True, text=True)
    assert proc.returncode == 1 and "rejection" in proc.stderr
    missing = subprocess.run([*run[:-2], str(tmp_path / "nope.json"), str(clean_r)], capture_output=True, text=True)
    assert missing.returncode == 1
    sh = (ACC / "run-teach.sh").read_text(encoding="utf-8")
    first_run = min(sh.index("\nrun s6-1"), sh.index("\nrun t1 "))
    assert "teach_verdicts.py clean-start" in sh and sh.index("teach_verdicts.py clean-start") < first_run
    assert "prose-edit-promote" in sh and sh.index("prose-edit-promote") < first_run  # a stale dry-run record aborts
    assert re.search(r"-mmin -120", sh)
    assert "exit 1" in sh[sh.index("teach_verdicts.py clean-start"):first_run]


def test_the_promote_check_is_not_measurable_when_the_first_run_could_not_store_a_rejection() -> None:
    ok = T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(), t1=_t1(), t2c=_t2c())
    assert ok[0] == "PASS", ok
    one = T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(), t1=_t1(edits=1), t2c=_t2c())
    assert one[0] == "NOT-MEASURABLE" and "fewer than two edits" in one[1]
    none = T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(), t1=_t1(), t2c=_t2c(rejects=False))
    assert none[0] == "NOT-MEASURABLE" and "no rejection" in none[1]
    no_apply = T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(), t1=_t1(), t2c=_t2c()[2:])
    assert no_apply[0] == "NOT-MEASURABLE" and "no rejection" in no_apply[1]
    # without the first run's transcripts the gate part still scores as before
    assert T.check_promote("rc=1\n", REFUSED, _t4(), _t4c())[0] == "PASS"


def test_the_promote_check_needs_the_entrys_old_text_in_the_reply_that_asks_the_author_to_confirm() -> None:
    hidden = _t4()[:-1] + [{"type": "result", "result": "Here is the entry. Confirm?"}]
    verdict, note = T.check_promote("rc=1\n", REFUSED, hidden, _t4c())
    assert verdict == "FAIL" and "just" in note and "author" in note
    shown = T.check_promote("rc=1\n", REFUSED, _t4(), _t4c())
    assert shown[0] == "PASS"
    # whitespace in the retyped entry does not matter
    spaced = _t4()[:-1] + [{"type": "result", "result": f"The entry:\n  {DRY['entry']['old']}  ->  cut. Confirm?"}]
    assert T.check_promote("rc=1\n", REFUSED, spaced, _t4c())[0] == "PASS"


def test_main_passes_the_first_run_transcripts_to_the_promote_check(tmp_path: Path) -> None:
    _all_runs(tmp_path)
    _write(tmp_path, "t1", _t1(edits=1))
    assert T.main([str(tmp_path)]) == 3  # the first run had one edit: promote is not measurable, nothing failed


# ---------------------------------------------------------------------------
# 7. Batch hygiene: the head sha, each transcript's hash, semicolon queries
# ---------------------------------------------------------------------------


def test_record_run_writes_the_head_sha_and_the_sha256_of_the_transcript(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "xanadu-1.jsonl").write_text('{"type": "result"}\n', encoding="utf-8")
    proc = subprocess.run([str(ACC / "record-run.sh"), str(out), "xanadu-1"], capture_output=True, text=True,
                          cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    assert (out / "xanadu-1.head").read_text(encoding="utf-8").strip() == head
    digest = hashlib.sha256((out / "xanadu-1.jsonl").read_bytes()).hexdigest()
    assert (out / "xanadu-1.sha256").read_text(encoding="utf-8").split() == [digest, "xanadu-1.jsonl"]
    missing = subprocess.run([str(ACC / "record-run.sh"), str(out), "nope"], capture_output=True, text=True, cwd=ROOT)
    assert missing.returncode != 0  # a run with no transcript records nothing


def test_every_runner_records_through_run_scenario() -> None:
    scenario = (ACC / "run-scenario.sh").read_text(encoding="utf-8")
    assert os.access(ACC / "record-run.sh", os.X_OK)
    assert 'record-run.sh" "$OUT" "$NAME"' in scenario
    assert scenario.index('"$HERE/record-run.sh"') > scenario.index('echo "rc=$?"')  # after the session ended
    for name in ("run-canaries.sh", "run-review.sh", "run-memory-gate.sh", "run-teach.sh"):
        assert "run-scenario.sh" in (ACC / name).read_text(encoding="utf-8"), name
    readme = (ACC / "README.md").read_text(encoding="utf-8")
    assert "record-run.sh" in readme and ".sha256" in readme and ".head" in readme


def test_verdicts_print_the_head_shas_of_the_batch(tmp_path: Path, capsys: object) -> None:
    v = _verdicts()
    (tmp_path / "canary-nx.rc").write_text("rc=0\n", encoding="utf-8")
    events = [_use("t1", "Bash", {"command": "nx --version"}),
              _res("t1", "Permission to use Bash with command nx --version has been denied.")]
    (tmp_path / "canary-nx.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (tmp_path / "canary-nx.head").write_text("a" * 40 + "\n", encoding="utf-8")
    (tmp_path / "other.head").write_text("b" * 40 + "\n", encoding="utf-8")
    v.main([str(tmp_path)])
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert f"HEADS {'a' * 40} {'b' * 40}" in out


def _semi(n: int, anchor: str, text: str) -> dict:
    return {"n": n, "anchor": anchor, "text": text}


def test_the_xanadu_and_linda_notes_count_the_semicolon_queries() -> None:
    plain = _ed(1, "To be clear: ", "")
    queries = [_semi(1, "a; b", "Two clauses joined by a semicolon: split?"),
               _semi(2, "no mark here", "The rule says no semicolons; keep this one?"),
               _semi(3, "x; y", "Rewrite?"), _semi(4, "plain anchor", "Is this measured?")]
    for kind, doc in (("linda", LINDA), ("xanadu", XANADU)):
        events = _run({"edits": [plain], "queries": []}, {"edits": [plain], "queries": queries})
        verdict, note = _verdict(kind, events, doc)
        assert verdict == "PASS" and "semicolon queries: 3" in note, note
        none = _run({"edits": [plain], "queries": []}, {"edits": [plain], "queries": [queries[3]]})
        assert "semicolon queries: 0" in _verdict(kind, none, doc)[1]
    # only the two exploration documents are counted
    other = _both([_ed(1, "The scheduler basically hands each job", "x")])
    assert "semicolon" not in _verdict("protected", other, PROTECTED)[1]


# ---------------------------------------------------------------------------
# 8. SKILL wording
# ---------------------------------------------------------------------------


def test_skill_step_4_says_both_runs_print_a_header_with_the_dispatch_line_and_then_the_brief() -> None:
    step = _step(SKILL_TEXT, 4)
    assert "a stdin run prints the brief alone" not in step
    assert "Both runs print a header, a blank line, and then the brief" in step
    assert "`WORK=<dir>` and `DISPATCH=<prompt>`" in step and "one line, `DISPATCH=<prompt>`" in step


def test_skill_step_12_says_what_apply_did_not_change_in_the_words_of_the_script() -> None:
    step = _step(SKILL_TEXT, 12)
    assert "the file was not changed" not in step
    assert "apply did not change the file" in step
    assert "the author's own save" in step or "the author saved" in step
