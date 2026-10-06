# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 1 for the RDR-221 Phase 1 reviews (nexus-ger02.5 code review, nexus-ger02.6 critique).

The WORK lifecycle (a review in progress is never swept, an expired one says so, every apply failure
before the file write says what happened and to run apply again), the reasons file, the brief file, and
the small items. The acceptance verdicts have their own tests in test_acceptance_tools.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, ROOT, SPY, USER_PROJECT, Prose, t2_get, t2_json, t2_titles
from tests.prose_edit.test_brief import (
    AGENT,
    BRIEF,
    NAMES_A_REPAIR,
    SKILL,
    brief_ok,
    edit,
    fenced,
    proposal,
    run_brief,
)
from tests.prose_edit.test_review import DOC, E1, E2, REVIEW, review_ok, run_review, start
from tests.prose_edit.test_review_apply_safety import hook, tracked

SENTINEL = ".prose-edit-work"
SCRIPTS = ROOT / ".claude" / "skills" / "prose-edit" / "scripts"
THREE_HOURS = 3 * 3600


def _age(work: Path, seconds: float = THREE_HOURS) -> None:
    then = time.time() - seconds
    os.utime(work / SENTINEL, (then, then))


def _idle(work: Path) -> float:
    return time.time() - (work / SENTINEL).stat().st_mtime


def _nx(tmp_path: Path, *, fail_when: str, message: str, after_call: bool = False, once: bool = False) -> dict[str, str]:
    """PROSE_EDIT_NX that fails a call whose argv contains `fail_when` with `message` on stderr (exit 1).
    `after_call` runs the real call first and then reports the failure (the write landed, the reply was lost);
    `once` fails only the first such call."""
    fake = tmp_path / "failingnx.py"
    fake.write_text(
        "import os, subprocess, sys\n"
        f"marker = {str(tmp_path / 'failed-once')!r}\n"
        f"if {fail_when!r} in ' '.join(sys.argv[1:]) and not (os.path.exists(marker) and {once!r}):\n"
        "    open(marker, 'w').close()\n"
        f"    if {after_call!r}:\n"
        f"        subprocess.run([sys.executable, {str(SPY)!r}, *sys.argv[1:]], stdin=sys.stdin, capture_output=True)\n"
        f"    sys.stderr.write({message!r} + '\\n')\n"
        "    sys.exit(1)\n"
        f"os.execv(sys.executable, [sys.executable, {str(SPY)!r}, *sys.argv[1:]])\n")
    return {"PROSE_EDIT_NX": f"{sys.executable} {fake}"}


HTTP_500 = "Error: T2 storage service error: Server error '500 Internal Server Error' for url 'http://x/v1/y'"
DOWN = "Error: T2 storage service unavailable: connection refused"


# ---------------------------------------------------------------------------
# I1a / S6: a review in progress is not swept; an expired one says so
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["render", "dry-run", "refused-answer"])
def test_render_and_apply_refresh_the_work_sentinel_so_the_sweep_takes_only_abandoned_reviews(
    prose: Prose, repo: Path, step: str
) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    _age(work)
    if step == "render":
        review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    elif step == "dry-run":
        review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    else:  # an answer the grammar refuses: the author is still there
        assert run_review(prose, "apply", "--work", str(work), "--accept", "9", "--dry-run").returncode == 1
    assert _idle(work) < 600
    other = Path(brief_ok(prose, "tmpdir").strip())  # a second edit run starts: its sweep must leave this one
    try:
        assert work.is_dir()
    finally:
        brief_ok(prose, "rmtmp", str(other))


def test_log_retry_refreshes_the_work_sentinel(prose: Prose, repo: Path, tmp_path: Path) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_nx(tmp_path, fail_when="log/", message=DOWN)}
    assert run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env).returncode == 0
    _age(work)
    down = run_review(prose, "log-retry", "--work", str(work), env=env, dry_first=False)
    assert down.returncode == 3 and _idle(work) < 600
    other = Path(brief_ok(prose, "tmpdir").strip())
    try:
        assert work.is_dir()
    finally:
        brief_ok(prose, "rmtmp", str(other))


def test_an_idle_work_directory_is_swept_and_a_later_apply_says_it_expired(prose: Prose, repo: Path) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    _age(work)
    other = Path(brief_ok(prose, "tmpdir").strip())
    try:
        assert not work.exists()
        proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
        assert proc.returncode == 1
        assert "expired" in proc.stderr and "swept" in proc.stderr
        assert "not a prose-edit work directory" not in proc.stderr
        assert "run apply again" not in proc.stderr  # there is nothing to run again: the edit starts over
        render = run_review(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
        assert render.returncode == 1 and "expired" in render.stderr
        # a name that never was a work directory keeps the old refusal
        bogus = run_review(prose, "apply", "--work", str(prose.tmp / "prose-edit-notmine1x"), "--accept", "1")
        assert bogus.returncode == 1 and "expired" not in bogus.stderr
    finally:
        brief_ok(prose, "rmtmp", str(other))


# ---------------------------------------------------------------------------
# I1b / S1: every apply failure before the file write says so, and to run apply again
# ---------------------------------------------------------------------------


def test_a_store_failure_says_nothing_was_written_and_to_run_apply_again(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_nx(tmp_path, fail_when="put", message=HTTP_500)}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", env=env)
    assert proc.returncode == 1 and proc.stdout == "" and "HTTP 500" in proc.stderr
    assert "nothing was written" in proc.stderr and "run apply again" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir()
    assert not NAMES_A_REPAIR.search(proc.stderr)


def test_a_service_that_is_down_keeps_its_exit_code_and_says_to_run_apply_again(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_nx(tmp_path, fail_when="", message=DOWN)}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 3 and "nothing was written" in proc.stderr and "run apply again" in proc.stderr
    assert "Stop here and tell the author." in proc.stderr and not NAMES_A_REPAIR.search(proc.stderr)
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir()


def test_a_save_during_the_apply_says_nothing_was_written_when_no_rejection_was_stored(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **hook(tmp_path, "open(target, 'a').write('AUTHOR SAVE\\n')")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 1 and "nothing was written" in proc.stderr and "run apply again" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC + "AUTHOR SAVE\n" and work.is_dir()


def test_nothing_was_written_is_never_claimed_when_rejections_were_already_stored(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **hook(tmp_path, "open(target, 'a').write('AUTHOR SAVE\\n')")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", env=env)
    assert proc.returncode == 1 and "run apply again" in proc.stderr
    assert "nothing was written" not in proc.stderr  # the rejection IS stored: say what is true instead
    assert "rejections already stored" in proc.stderr and "apply did not change the file" in proc.stderr
    assert [r["old"] for r in t2_json(REPO_PROJECT, "doc/docs/s.md")["rejections"]] == [E2["old"]]
    assert f.read_text(encoding="utf-8") == DOC + "AUTHOR SAVE\n" and work.is_dir()


def test_a_log_failure_with_rejections_stored_and_no_file_change_says_so_and_keeps_work(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_nx(tmp_path, fail_when="log/", message=DOWN)}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "none", "--reject", "2", env=env)
    assert proc.returncode == 3 and "run apply again" in proc.stderr
    assert "nothing was written" not in proc.stderr and "rejections already stored" in proc.stderr
    assert work.is_dir() and [r["old"] for r in t2_json(REPO_PROJECT, "doc/docs/s.md")["rejections"]] == [E2["old"]]


def test_a_file_with_mixed_line_endings_says_nothing_was_written_and_to_run_apply_again(
    prose: Prose, repo: Path
) -> None:
    f = tracked(repo, text="One line here.\r\nTwo lines here.\nThree lines here.\n")
    work, _ = start(prose, repo, [edit(1, "Two lines here.", "Two.")], text=f.read_text(encoding="utf-8"))
    f.write_bytes(b"One line here.\r\nTwo lines here.\nThree lines here.\n")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert proc.returncode == 1 and "mixed line endings" in proc.stderr
    assert "nothing was written" in proc.stderr and "run apply again" in proc.stderr


def test_accept_is_optional_and_an_unnamed_edit_is_held(prose: Prose, repo: Path) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    dry = review_ok(prose, "apply", "--work", str(work), "--reject", "2", "--dry-run")
    assert dry["accept"] == [] and [e["n"] for e in dry["hold"]] == [1] and [e["n"] for e in dry["reject"]] == [2]
    done = review_ok(prose, "apply", "--work", str(work), "--reject", "2")
    assert done["applied"] == [] and done["held"] == [1] and done["rejected"] == [2]


SKILL_STDERR_PHRASES = {"--accept", "--hold", "--reject", "--reason", "--reasons-file", "run apply again", "dry run",
                        "expired"}


def _step(text: str, n: int) -> str:
    start_at = text.index(f"\n{n}. ")
    nxt = text.find(f"\n{n + 1}. ", start_at)
    return text[start_at:nxt if nxt != -1 else len(text)]


def _keyed_phrases(step: str) -> set[str]:
    """Every backticked phrase in a sentence of the step that keys on what stderr contains."""
    out: set[str] = set()
    for sentence in re.split(r"(?<=[.:])\s+", step):
        if "in stderr" in sentence:
            out |= set(re.findall(r"`([^`]+)`", sentence))
    return out


def test_every_phrase_the_skill_keys_on_in_stderr_is_emitted_by_the_scripts_with_the_same_case(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    skill = SKILL.read_text(encoding="utf-8")
    keyed = _keyed_phrases(_step(skill, 12))
    assert keyed == SKILL_STDERR_PHRASES, "the skill keys on a phrase this test does not exercise (or the reverse)"
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    (work / "reasons.json").write_text(json.dumps({"1": "x"}), encoding="utf-8")
    cases: dict[str, tuple[str, ...]] = {
        "--accept": ("--accept", "9"), "--hold": ("--accept", "none", "--hold", "9"),
        "--reject": ("--accept", "none", "--reject", "9"),
        "--reason": ("--accept", "none", "--reject", "2", "--reason", "9=no"),
        "--reasons-file": ("--accept", "none", "--reject", "2", "--reasons-file", str(work / "reasons.json")),
    }
    for phrase, argv in cases.items():
        proc = run_review(prose, "apply", "--work", str(work), *argv, "--dry-run")
        assert proc.returncode == 1 and phrase in proc.stderr, (phrase, proc.stderr)
    nodry = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert nodry.returncode == 1 and "dry run" in nodry.stderr
    gone = run_review(prose, "apply", "--work", str(prose.tmp / "prose-edit-deadbeef"), "--accept", "1")
    assert gone.returncode == 1 and "expired" in gone.stderr  # well-formed name, no directory
    env = {**prose.env, **hook(tmp_path, "open(target, 'a').write('AUTHOR SAVE\\n')")}
    changed = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert changed.returncode == 1 and "run apply again" in changed.stderr
    assert f.read_text(encoding="utf-8").endswith("AUTHOR SAVE\n")
    # the phrase is lower case in the script source too: a case change on either side breaks the key
    assert "run apply again" in REVIEW.read_text(encoding="utf-8")
    assert "Run apply again" not in REVIEW.read_text(encoding="utf-8")


def test_the_skill_keeps_work_on_the_failures_a_retry_can_fix() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    step12, step10, step9 = _step(skill, 12), _step(skill, 10), _step(skill, 9)
    assert "exit 2" in step12 and "keep WORK" in step12  # an argparse usage error: nothing happened
    assert "expired" in step12 and "swept" in step12  # a work directory that is gone: nothing to delete
    assert "exit 3" in step10 and "keep WORK" in step10  # a T2 blip after the editor ran must not cost the run
    assert "exit 3" in step9 and "keep WORK" in step9  # reply.txt is in WORK: filter can run again
    assert "may be left out" in step12 and "defaults to `none`" in step12  # --accept is optional now
    step5 = _step(skill, 5)
    assert "step 9 or step 10" in step5 and "exceptions" in step5  # the retries of filter and render keep WORK


# ---------------------------------------------------------------------------
# S9: the author's words never ride a shell string
# ---------------------------------------------------------------------------

AWKWARD = 'it\'s "mine" $(touch /tmp/x) `y` \\ done; & more'


def test_a_reasons_file_carries_the_authors_words_exactly_through_dry_run_and_apply(
    prose: Prose, repo: Path
) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    rf = work / "reasons.json"
    rf.write_text(json.dumps({"2": AWKWARD}), encoding="utf-8")
    dry = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2",
                    "--reasons-file", str(rf), "--dry-run")
    assert dry["reject"][0]["reason"] == AWKWARD
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2", "--reasons-file", str(rf))
    assert done["rejected"] == [2]
    assert t2_json(REPO_PROJECT, "doc/docs/s.md")["rejections"][0]["reason"] == AWKWARD


def test_the_dry_run_hash_covers_the_reasons_file(prose: Prose, repo: Path) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    rf = work / "reasons.json"
    rf.write_text(json.dumps({"2": "first words"}), encoding="utf-8")
    argv = ("apply", "--work", str(work), "--accept", "1", "--reject", "2", "--reasons-file", str(rf))
    review_ok(prose, *argv, "--dry-run")
    rf.write_text(json.dumps({"2": "other words"}), encoding="utf-8")
    proc = run_review(prose, *argv, dry_first=False)
    assert proc.returncode == 1 and "dry run" in proc.stderr and "different answer" in proc.stderr
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    review_ok(prose, *argv, "--dry-run")
    assert run_review(prose, *argv, dry_first=False).returncode == 0


@pytest.mark.parametrize("body,fragment", [
    ("not json", "--reasons-file"),
    ('["a"]', "--reasons-file"),
    ('{"x": "no"}', "--reasons-file"),
    ('{"2": ""}', "--reasons-file"),
    ('{"2": 5}', "--reasons-file"),
    ('{"1": "edit 1 is not rejected"}', "not rejected"),
], ids=["syntax", "list", "key", "empty", "non-string", "not-rejected"])
def test_a_bad_reasons_file_is_refused_before_anything_is_written(
    prose: Prose, repo: Path, body: str, fragment: str
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    rf = work / "reasons.json"
    rf.write_text(body, encoding="utf-8")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2",
                      "--reasons-file", str(rf), "--dry-run")
    assert proc.returncode == 1 and fragment in proc.stderr and "Traceback" not in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir()


def test_a_reasons_file_must_sit_directly_inside_the_work_directory_and_not_clash_with_reason(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    outside = tmp_path / "r.json"
    outside.write_text(json.dumps({"2": "x"}), encoding="utf-8")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2",
                      "--reasons-file", str(outside), "--dry-run")
    assert proc.returncode == 1 and "work directory" in proc.stderr
    rf = work / "reasons.json"
    rf.write_text(json.dumps({"2": "from the file"}), encoding="utf-8")
    both = run_review(prose, "apply", "--work", str(work), "--accept", "1", "--reject", "2",
                      "--reasons-file", str(rf), "--reason", "2=from the flag", "--dry-run")
    assert both.returncode == 1 and "two reasons for edit 2" in both.stderr


def test_the_skill_passes_reasons_in_a_file_and_never_in_a_shell_string() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    step12 = _step(skill, 12)
    assert "--reasons-file '<reasons>'" in step12 and "`REASONS=` path" in step12 and "Write tool" in step12
    assert '--reason "' not in skill and "--reason <n>" not in skill
    assert "Never pass the author's text in a shell string." in skill[skill.index("## Rules"):]


# ---------------------------------------------------------------------------
# S5 / S2: the brief travels as a file, with a checksum the reply echoes
# ---------------------------------------------------------------------------


def _split_build(out: str) -> tuple[dict[str, str], str]:
    head, brief = out.split("\n\n", 1)
    fields = dict(line.split("=", 1) for line in head.splitlines())
    return fields, brief


def _brief_id(work: Path) -> str:
    """The id on the last line of WORK/brief.md (fix round 3: the id is in the file and nowhere else)."""
    m = re.search(r"\nBrief id: ([0-9a-f]{12})\n\Z", (work / "brief.md").read_text(encoding="utf-8"))
    assert m
    return m.group(1)


def test_build_work_writes_brief_md_and_prints_the_work_directory_and_the_dispatch_prompt(prose: Prose, repo: Path) -> None:
    out = brief_ok(prose, "build", "docs/x.md", "--work")
    fields, brief = _split_build(out)
    work = Path(fields["WORK"])
    try:
        assert list(fields) == ["WORK", "DISPATCH", "REPLY", "FILTERED", "REASONS", "ANSWERS"]
        data = (work / "brief.md").read_bytes()
        assert data.decode("utf-8").startswith(brief)
        assert re.fullmatch(r"[0-9a-f]{12}", _brief_id(work))
        assert hashlib.sha256(brief.encode("utf-8")).hexdigest().startswith(_brief_id(work))
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_stdin_build_inside_a_work_directory_writes_brief_md_too_and_one_outside_does_not(
    prose: Prose, tmp_path: Path
) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        (work / "input.txt").write_text("fix: a thing\n", encoding="utf-8")
        out = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(work / "input.txt"))
        fields, brief = _split_build(out)
        assert list(fields) == ["DISPATCH"] and brief.startswith("# Editing brief") and "Brief id" not in brief
        assert (work / "brief.md").read_text(encoding="utf-8").startswith(brief) and "fix: a thing" in brief
        assert hashlib.sha256(brief.encode("utf-8")).hexdigest().startswith(_brief_id(work))
    finally:
        brief_ok(prose, "rmtmp", str(work))
    loose = tmp_path / "m.txt"
    loose.write_text("fix: a thing\n", encoding="utf-8")
    plain = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(loose))
    assert plain.startswith("# Editing brief") and not (tmp_path / "brief.md").exists()


def _built(prose: Prose) -> tuple[Path, str]:
    fields, _ = _split_build(brief_ok(prose, "build", "docs/x.md", "--work"))
    return Path(fields["WORK"]), _brief_id(Path(fields["WORK"]))


def test_filter_warns_on_a_missing_or_mismatched_brief_sha_and_stays_quiet_on_a_match(prose: Prose) -> None:
    work, sha = _built(prose)
    try:
        save = str(work / "filtered.json")
        ok = run_brief(prose, "filter", "docs/x.md", "--save", save, stdin=fenced(proposal([], brief_sha=sha)))
        assert ok.returncode == 0 and "brief_sha" not in ok.stderr and json.loads(ok.stdout)["warnings"] == []
        body = (work / "brief.md").read_text(encoding="utf-8").rsplit("\nBrief id: ", 1)[0]
        full = hashlib.sha256(body.encode("utf-8")).hexdigest()
        longer = run_brief(prose, "filter", "docs/x.md", "--save", save, stdin=fenced(proposal([], brief_sha=full)))
        assert longer.returncode == 0 and "brief_sha" not in longer.stderr
        missing = run_brief(prose, "filter", "docs/x.md", "--save", save, stdin=fenced(proposal([])))
        assert missing.returncode == 0 and "brief_sha" in missing.stderr
        assert any("brief_sha" in w for w in json.loads(missing.stdout)["warnings"])
        wrong = run_brief(prose, "filter", "docs/x.md", "--save", save,
                          stdin=fenced(proposal([], brief_sha="000000000000")))
        assert wrong.returncode == 0 and "000000000000" in wrong.stderr and sha in wrong.stderr
        assert any(sha in w for w in json.loads(wrong.stdout)["warnings"])
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_filter_checks_nothing_when_the_work_directory_holds_no_brief_file(prose: Prose) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        proc = run_brief(prose, "filter", "docs/x.md", "--save", str(work / "filtered.json"),
                         stdin=fenced(proposal([])))
        assert proc.returncode == 0 and proc.stderr == "" and json.loads(proc.stdout)["warnings"] == []
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_the_skill_and_the_agent_pass_the_brief_by_file_and_the_id_is_read_from_it() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    assert "The prompt is the brief, unchanged." not in skill
    step7 = _step(skill, 7)
    assert "DISPATCH=" in step7
    agent = AGENT.read_text(encoding="utf-8")
    assert "Read the brief in the prompt in full" not in agent
    assert "brief file" in agent and "brief_sha" in agent
    out = agent[agent.index("## Output"):]
    assert out.count("brief_sha") >= 2  # described and in the example
    assert not NAMES_A_REPAIR.search(skill) and not NAMES_A_REPAIR.search(agent)


# ---------------------------------------------------------------------------
# Small items
# ---------------------------------------------------------------------------


def test_the_stdin_text_row_says_it_is_the_text_with_the_accepted_edits_applied() -> None:
    row = next(ln for ln in SKILL.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("| `text` |"))
    assert "accepted edits applied" in row and "unchanged" not in row


def test_promote_dedupes_the_not_a_defect_list_by_the_change_not_by_the_old_string(prose: Prose) -> None:
    doc = "docs/x.md"
    prose.ok("reject", doc, "--old", "just", "--new", "")
    prose.ok("reject", doc, "--old", "just", "--new", "simply")
    prose.ok("reject", doc, "--old", "very  good", "--new", "good")
    prose.promote(doc, 1, "repo")
    prose.promote(doc, 2, "repo")
    entries = t2_json(REPO_PROJECT, "not-a-defect")["entries"]
    assert [(e["old"], e["new"]) for e in entries] == [("just", ""), ("just", "simply")]
    prose.promote(doc, 1, "repo")  # the same change again: still one entry for it
    assert len(t2_json(REPO_PROJECT, "not-a-defect")["entries"]) == 2
    prose.promote(doc, 3, "user")
    prose.ok("reject", doc, "--old", "very good", "--new", "good")  # whitespace variant of the same change
    assert [e["old"] for e in t2_json(USER_PROJECT, "not-a-defect")["entries"]] == ["very  good"]


def test_the_agent_discards_the_target_document_from_the_genre_path_grep() -> None:
    agent = AGENT.read_text(encoding="utf-8")
    assert "files_with_matches" in agent
    assert "Exclude from the search" in agent and "discard" in agent
    assert "Use Grep for nothing else." in agent


def test_the_skill_quotes_paths_and_tokens_on_the_bash_line() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    assert "single quotes" in skill and "author input" in skill
    assert "< '<reply>'" in skill


def test_the_skill_asks_for_pasted_text_before_it_makes_a_work_directory() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    row = next(ln for ln in _step(skill, 3).splitlines() if ln.strip().startswith("| true |"))
    assert row.index("ask the author to paste") < row.index("BRIEF tmpdir")
    assert "no WORK exists yet" in row
    assert skill.count("BRIEF tmpdir") == 1


def test_log_retry_reuses_the_stamp_so_a_lost_reply_does_not_log_the_session_twice(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    tracked(repo)
    env_clock = {k: v for k, v in prose.env.items() if k != "PROSE_EDIT_NOW"}  # a real clock: stamps differ per call
    work, _ = start(prose, repo, [E1, E2])
    # the first log put LANDS and its reply is lost: the script reports a failure for a record that exists
    env = {**env_clock, **_nx(tmp_path, fail_when="log/", message=DOWN, after_call=True, once=True)}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 0 and json.loads(proc.stdout)["log_error"]
    first = [t for t in t2_titles(REPO_PROJECT) if t.startswith("log/")]
    assert len(first) == 1
    time.sleep(0.01)
    retry = run_review(prose, "log-retry", "--work", str(work), env=env_clock, dry_first=False)
    assert retry.returncode == 0, retry.stderr
    done = json.loads(retry.stdout)
    assert done["log"]["title"] == first[0]
    assert [t for t in t2_titles(REPO_PROJECT) if t.startswith("log/")] == first
    assert not work.exists()


def test_memory_log_takes_a_stamp_that_fixes_the_title_and_refuses_a_malformed_one(prose: Prose) -> None:
    out = prose.ok("log", "docs/x.md", "--genre", "reference-doc", "--stamp", "20261003T101500.123456Z", stdin={"a": 1})
    assert out["title"] == "log/docs/x.md/20261003T101500.123456Z"
    assert t2_json(REPO_PROJECT, out["title"])["at"] == "2026-10-03T10:15:00Z"
    again = prose.ok("log", "docs/x.md", "--genre", "reference-doc", "--stamp", "20261003T101500.123456Z", stdin={"a": 2})
    assert again["title"] == out["title"]
    assert len([t for t in t2_titles(REPO_PROJECT) if t.startswith("log/")]) == 1
    bad = prose.run("log", "docs/x.md", "--stamp", "yesterday", stdin={"a": 1})
    assert bad.returncode == 1 and "--stamp" in bad.stderr


SPDX = "# SPDX-License-Identifier: AGPL-3.0-or-later"


def test_the_prose_edit_scripts_and_the_acceptance_helpers_carry_the_spdx_header() -> None:
    files = [*SCRIPTS.glob("*.py"), ROOT / "tests" / "prose_edit" / "nxspy.py",
             *(ROOT / "tests" / "prose_edit" / "acceptance").glob("*.py"),
             *(ROOT / "tests" / "prose_edit" / "acceptance").glob("*.sh")]
    assert len(files) >= 11
    missing = [str(p.relative_to(ROOT)) for p in files if SPDX not in p.read_text(encoding="utf-8").splitlines()[:3]]
    assert missing == []


def test_brief_py_names_its_own_filter_flags_in_the_module_docstring() -> None:
    # the docstring is the contract the skill reads: the build output and the sha check are in it
    text = BRIEF.read_text(encoding="utf-8")
    assert "Brief id" in text and "brief.md" in text
