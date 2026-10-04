# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 5 for the RDR-221 Phase 1 reviews (nexus-ger02.5/.6/.7): the exit batch at 4e20c6414.

A device on the voice card is never edited, a null genre after parse is never a question, step 13 shows the stored
card and the promote entry in the author's own words, the acceptance runners cannot contaminate each other or the
editor's search, and the review verdicts tell a scenario that was not asked for from one that failed.

Every transcript here is planted and every runner is exercised with a fake `nx` and a missing `claude`: no model
runs and no live T2 record is written by this file.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.prose_edit.conftest import ROOT, Prose, git
from tests.prose_edit.test_acceptance_tools import (
    ACC,
    _check_a_runs,
    _review_verdicts,
    _script,
    _write_runs,
)
from tests.prose_edit.test_brief import AGENT, SKILL, brief_ok
from tests.prose_edit.test_fix_round1 import _step

AGENT_TEXT = AGENT.read_text(encoding="utf-8")
SKILL_TEXT = SKILL.read_text(encoding="utf-8")
GUARD = ACC / "runner_guard.bash"
FAKE_NX = f"{sys.executable} {ACC / 'fake_nx_unavailable.py'}"


def _section(text: str, head: str, nxt: str) -> str:
    return text[text.index(head):text.index(nxt, text.index(head))]


# ---------------------------------------------------------------------------
# 1. A device on the voice card is never edited, whatever a style-sheet rule says
# ---------------------------------------------------------------------------


def test_the_line_editor_never_edits_a_device_whatever_a_style_sheet_rule_says_and_queries_the_conflict() -> None:
    rules = _section(AGENT_TEXT, "## Rules", "## Output")
    rule = next(ln for ln in rules.splitlines() if ln.startswith("- A voice-card device is never edited"))
    assert "whatever a genre, repo or user style-sheet rule says" in rule
    assert "query" in rule and "never an edit" in rule
    # the voice-card section carries the same rule, with the case that went wrong in the exit batch
    card = _section(AGENT_TEXT, "## Voice card", "## Protected regions")
    assert "Never edit one either" in card and "whatever a genre, repo or user style-sheet rule says" in card
    assert "semicolon" in card  # the genre rule against semicolons does not license splitting a listed device
    assert "raise a query" in card.lower() or "ask a query" in card.lower()


def test_the_line_editor_says_a_device_is_not_a_style_sheet_layer_and_wins_over_every_layer() -> None:
    procedure = _section(AGENT_TEXT, "## Procedure", "## Voice card")
    step5 = next(ln for ln in procedure.splitlines() if ln.startswith("5. "))
    assert "document > genre > repo > user" in step5  # the layer order itself is unchanged
    assert "not a layer" in step5 and "device" in step5 and "beats" in step5


def test_the_brief_tells_the_editor_a_device_beats_every_style_sheet_entry(prose: Prose) -> None:
    brief = brief_ok(prose, "build", "docs/x.md", "--genre", "reference-doc")
    sheet = _section(brief, "## 3. Style sheet", "## 4. Not a defect")
    assert "never edited" in sheet and "device" in sheet and "query" in sheet
    assert "whatever such an entry below says" in sheet
    assert "document > genre > repo > user" in sheet  # the layer order is still the one stated


# ---------------------------------------------------------------------------
# 2. A null genre after parse is never a reason to ask
# ---------------------------------------------------------------------------


def test_step_1_says_a_null_genre_is_never_a_reason_to_ask_and_to_go_on_to_step_2() -> None:
    step1 = _step(SKILL_TEXT, 1)
    assert "A null `genre` in the output only means the flag was not given for a path run." in step1
    assert "Never ask the author for a genre because of it" in step1
    assert "Go on to step 2" in step1
    assert "only step 4" in step1.lower() or "only that report" in step1
    assert "no genre" in step1  # the one condition that is a reason, named


def test_the_only_place_the_skill_asks_for_a_genre_is_step_4_on_a_no_genre_failure() -> None:
    asks = [m.start() for m in re.finditer(r"ask the author which genre", SKILL_TEXT, re.IGNORECASE)]
    assert len(asks) == 1, asks
    assert SKILL_TEXT.index("\n4. ") < asks[0] < SKILL_TEXT.index("\n5. ")
    row = next(ln for ln in _step(SKILL_TEXT, 4).splitlines() if "stderr contains `no genre`" in ln)
    assert "ask the author which genre" in row.lower()


# ---------------------------------------------------------------------------
# 3 and 4. Step 13: the stored card and the promote entry are shown in the words of the script
# ---------------------------------------------------------------------------


def _bullet_block(step: str, head: str) -> str:
    """One top-level bullet of step 13 with its indented lines."""
    lines = step.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("    - ") and head in ln)
    block = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("    - ") or not ln.startswith("      "):
            break
        block.append(ln)
    return "\n".join(block)


def _numbered(block: str) -> list[str]:
    return [ln.strip() for ln in block.splitlines() if re.match(r"\s{6,}\d+\. ", ln)]


def test_the_voice_card_bullet_has_a_numbered_sub_step_that_shows_the_stored_card_back_verbatim() -> None:
    block = _bullet_block(_step(SKILL_TEXT, 13), "The voice card the editor returned")
    subs = _numbered(block)
    assert len(subs) >= 4, subs
    assert [int(s.split(".")[0]) for s in subs] == list(range(1, len(subs) + 1))
    save = next(i for i, s in enumerate(subs) if "--from-stdin" in s and "voice-card" in s)
    show = next(i for i, s in enumerate(subs) if "stored" in s and "verbatim" in s)
    assert show > save, subs  # the show-back comes after the save, as its own step
    sub = subs[show]
    assert "`voice_card`" in sub and "reply" in sub
    assert "Do not" in sub and ("Done" in sub or "paraphrase" in sub)  # a bare "Done" is named as the wrong thing
    assert "--from-stdin" not in sub  # one thing per sub-step: the save is the step before


def test_the_promote_bullet_has_a_numbered_sub_step_that_quotes_the_entry_exactly_from_the_dry_run() -> None:
    block = _bullet_block(_step(SKILL_TEXT, 13), "never a defect")
    subs = _numbered(block)
    assert len(subs) >= 3, subs
    assert [int(s.split(".")[0]) for s in subs] == list(range(1, len(subs) + 1))
    dry = next(i for i, s in enumerate(subs) if "--dry-run" in s and "promote" in s)
    quote = next(i for i, s in enumerate(subs) if "`<old> -> <new>`" in s)
    ask = next(i for i, s in enumerate(subs) if "confirm" in s and "end your turn" in s.lower())
    assert dry < quote <= ask, subs
    sub = subs[quote]
    assert "entry" in sub and "dry-run output" in sub
    assert "exactly" in sub and "word for word" in sub
    assert "paraphrase" in sub.lower()
    assert "`old`" in sub and "`new`" in sub


def test_the_no_dry_run_refusal_and_the_two_hour_window_are_still_in_step_13() -> None:
    block = _bullet_block(_step(SKILL_TEXT, 13), "never a defect")
    assert "refuses a real promote that no matching dry run came before" in block
    assert "never skip it" in block


# ---------------------------------------------------------------------------
# 5. The runners: one at a time, copies outside the editor's search, nothing left behind
# ---------------------------------------------------------------------------


def _bash(script: str, repo: Path, lock: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PROSE_EDIT_RUNNER_LOCK": str(lock), **extra}
    return subprocess.run(["bash", "-c", f"set -u\nWT={repo}\n. {GUARD}\n{script}"],
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.fixture
def box(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "docs").mkdir()
    (repo / "docs" / "kept.md").write_text("tracked\n")
    git(repo, "add", "docs/kept.md")
    git(repo, "commit", "-q", "-m", "init")
    return repo, tmp_path / "prose-edit-runner.lock"


def _plant_holder(lock: Path, pid: int, name: str = "run-other.sh") -> None:
    lock.mkdir()
    (lock / "holder").write_text(f"{pid} {name}\n", encoding="utf-8")


def test_the_guard_takes_the_lock_for_the_run_and_releases_it_and_removes_its_copies_at_exit(
        box: tuple[Path, Path]) -> None:
    repo, lock = box
    proc = _bash('runner_lock run-test.sh\n'
                 'mkdir -p "$WT/docs/zz-test"; echo x > "$WT/docs/zz-test/scenario.md"; runner_track docs/zz-test\n'
                 'test -d "$PROSE_EDIT_RUNNER_LOCK" && echo LOCKED\n', repo, lock)
    assert proc.returncode == 0, proc.stderr
    assert "LOCKED" in proc.stdout
    assert not lock.exists()  # released
    assert not (repo / "docs" / "zz-test").exists()  # the copy is gone


def test_the_guard_refuses_while_a_live_runner_holds_the_lock_and_touches_nothing(box: tuple[Path, Path]) -> None:
    repo, lock = box
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _plant_holder(lock, holder.pid, "run-review.sh")
        proc = _bash('runner_lock run-teach.sh\necho REACHED\n', repo, lock)
        assert proc.returncode == 75, (proc.returncode, proc.stderr)
        assert "REACHED" not in proc.stdout
        assert "run-review.sh" in proc.stderr and str(holder.pid) in proc.stderr
        assert "another" in proc.stderr and "runner" in proc.stderr
        assert (lock / "holder").read_text(encoding="utf-8").startswith(str(holder.pid))  # not reclaimed
    finally:
        holder.kill()
        holder.wait()


def test_the_guard_reclaims_a_lock_whose_holder_is_dead(box: tuple[Path, Path]) -> None:
    repo, lock = box
    done = subprocess.Popen(["true"])
    done.wait()
    _plant_holder(lock, done.pid)
    proc = _bash('runner_lock run-test.sh\ncat "$PROSE_EDIT_RUNNER_LOCK/holder"\n', repo, lock)
    assert proc.returncode == 0, proc.stderr
    assert "run-test.sh" in proc.stdout  # the new holder wrote its own record
    assert not lock.exists()


def test_a_lock_with_no_holder_record_is_reclaimed_and_a_live_gate_blocks_until_it_is_freed(
        box: tuple[Path, Path]) -> None:
    # The lock is made and its record written under the gate, so a lock with no record outside the gate means its
    # maker died between the two: it is reclaimed at once, whatever its age (no file time is read).
    repo, lock = box
    lock.mkdir()
    assert _bash('runner_lock run-test.sh\n', repo, lock).returncode == 0
    # A gate keeps everyone out until its owner releases it (removes it); then the waiter goes on.
    gate = Path(f"{lock}.gate")
    shutil.rmtree(lock, ignore_errors=True)
    gate.mkdir()
    (gate / "pid").write_text("1\n", encoding="utf-8")
    blocked = subprocess.Popen(
        ["bash", "-c", f'set -u\nWT={repo}\n. {GUARD}\nrunner_lock run-test.sh\n'],
        env={**os.environ, "PROSE_EDIT_RUNNER_LOCK": str(lock)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    assert blocked.poll() is None and not lock.exists()  # waiting behind the gate
    shutil.rmtree(gate)  # the owner releases it
    assert blocked.wait(timeout=30) == 0  # it took the lock (its exit trap has since released it)
    assert not gate.exists()


def test_a_gate_whose_owner_is_dead_is_never_reclaimed_the_runner_refuses_and_names_it(
        box: tuple[Path, Path]) -> None:
    # nexus-w2j8c: reclaiming a gate is check-then-remove. A waiter that read a pid, saw it dead (an owner that
    # had just released and exited, as every refused runner does) and then removed "the" gate removed a fresh
    # gate a third runner had made meanwhile, and two runners were inside. So no gate is reclaimed, by pid or by
    # a missing record: one that outlives the wait is a runner killed inside it, and a human removes it.
    repo, lock = box
    gate = Path(f"{lock}.gate")
    gate.mkdir()
    done = subprocess.Popen(["true"])
    done.wait()
    (gate / "pid").write_text(f"{done.pid}\n", encoding="utf-8")
    proc = _bash('runner_lock run-test.sh\n', repo, lock, PROSE_EDIT_RUNNER_GATE_WAIT="1")
    assert proc.returncode == 75, proc.stderr
    assert str(gate) in proc.stderr and "remove" in proc.stderr
    assert gate.is_dir() and (gate / "pid").read_text(encoding="utf-8") == f"{done.pid}\n"  # untouched
    assert not lock.exists()
    (gate / "pid").unlink()  # and a gate with no record is not taken for dead either
    # 7 s: longer than the 5 s record-less reclaim that 158e2cfe6 had, so bringing it back fails this.
    proc = _bash('runner_lock run-test.sh\n', repo, lock, PROSE_EDIT_RUNNER_GATE_WAIT="7")
    assert proc.returncode == 75 and gate.is_dir() and not lock.exists(), proc.stderr


@pytest.mark.parametrize("wait", ["0.5", "08x", "30s", "abc", "-3", " 5"])
def test_an_invalid_gate_wait_refuses_instead_of_running_unlocked(box: tuple[Path, Path], wait: str) -> None:
    # Review of 4f467c0da: a non-integer wait aborted runner_lock inside $(( )) and the caller went on with no lock.
    repo, lock = box
    proc = _bash('runner_lock run-test.sh\necho UNLOCKED-RUN\n', repo, lock, PROSE_EDIT_RUNNER_GATE_WAIT=wait)
    assert "UNLOCKED-RUN" not in proc.stdout, (wait, proc.stderr)
    assert proc.returncode == 75 and "PROSE_EDIT_RUNNER_GATE_WAIT" in proc.stderr, (wait, proc.stderr)


def test_a_missing_lock_directory_parent_fails_fast(box: tuple[Path, Path], tmp_path: Path) -> None:
    repo, _ = box
    lock = tmp_path / "no-such-dir" / "lock"
    started = time.monotonic()
    proc = _bash('runner_lock run-test.sh\n', repo, lock)
    assert proc.returncode == 75 and time.monotonic() - started < 5, proc.stderr


def test_two_runners_cannot_run_at_once_and_a_killed_runner_still_cleans_up(
        box: tuple[Path, Path], tmp_path: Path) -> None:
    repo, lock = box
    ready = tmp_path / "ready"
    env = {**os.environ, "PROSE_EDIT_RUNNER_LOCK": str(lock)}
    first = subprocess.Popen(
        ["bash", "-c", f'set -u\nWT={repo}\n. {GUARD}\nrunner_lock run-first.sh\n'
                       f'mkdir -p "$WT/docs/zz-first"; echo x > "$WT/docs/zz-first/s.md"; runner_track docs/zz-first\n'
                       f'touch {ready}\nwhile :; do sleep 0.2; done'],
        env=env, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        assert ready.exists(), "the first runner never took the lock"
        second = _bash('runner_lock run-second.sh\n', repo, lock)
        assert second.returncode == 75 and "run-first.sh" in second.stderr
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
    assert not (repo / "docs" / "zz-first").exists()  # the trap ran on the signal
    assert not lock.exists()
    assert _bash('runner_lock run-second.sh\n', repo, lock).returncode == 0  # and the next runner can start


def test_the_guard_sweeps_untracked_leftovers_of_an_older_runner_and_keeps_tracked_files(
        box: tuple[Path, Path]) -> None:
    repo, lock = box
    (repo / "docs" / "zz-review-scenario.md").write_text("left behind by the old runner\n")
    (repo / "docs" / "zz-memgate").mkdir()
    (repo / "docs" / "zz-memgate" / "sub-1.md").write_text("left behind\n")
    (repo / "docs" / "zz-tracked.md").write_text("a tracked file that happens to start with zz-\n")
    git(repo, "add", "docs/zz-tracked.md")
    git(repo, "commit", "-q", "-m", "tracked zz")
    proc = _bash('runner_lock run-test.sh\nrunner_sweep_stale\n', repo, lock)
    assert proc.returncode == 0, proc.stderr
    assert not (repo / "docs" / "zz-review-scenario.md").exists()
    assert not (repo / "docs" / "zz-memgate").exists()
    assert (repo / "docs" / "zz-tracked.md").exists() and (repo / "docs" / "kept.md").exists()
    assert "zz-review-scenario.md" in proc.stderr  # the sweep says what it removed


RUNNERS = {
    "run-review.sh": [],
    "run-teach.sh": [],
    "run-memory-gate.sh": ["a", str(ROOT / "tests" / "prose_edit" / "fixtures" / "review-scenario.md"), "1"],
}


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_each_runner_refuses_while_another_runner_holds_the_lock_before_it_writes_anything(
        name: str, box: tuple[Path, Path], tmp_path: Path) -> None:
    _, lock = box
    holder = subprocess.Popen(["sleep", "60"])
    out = tmp_path / "out"
    try:
        _plant_holder(lock, holder.pid, "run-other.sh")
        env = {**os.environ, "PROSE_EDIT_RUNNER_LOCK": str(lock), "OUT_DIR": str(out),
               "TMPDIR": str(tmp_path / "tmp"), "PROSE_EDIT_NX": FAKE_NX,
               "CLAUDE_BIN": "/nonexistent/claude-for-the-test"}
        proc = subprocess.run([str(ACC / name), *RUNNERS[name]], capture_output=True, text=True, env=env,
                              cwd=ROOT, timeout=60)
        assert proc.returncode == 75, (proc.returncode, proc.stderr)
        assert "run-other.sh" in proc.stderr and name in proc.stderr
        assert not out.exists()  # nothing written: no status file, no seeded viewer, no transcript
        assert not list((ROOT / "docs").glob("zz-*"))  # no copy made
    finally:
        holder.kill()
        holder.wait()


def _assigned(sh: str, var: str) -> str:
    value = re.search(rf'^{var}=(\S+)', sh, re.MULTILINE).group(1)  # type: ignore[union-attr]
    if "$DIR" in value:
        value = value.replace("$DIR", _assigned(sh, "DIR"))
    return value


def _default_genre(path: str) -> object:
    spec = importlib.util.spec_from_file_location(
        "prose_edit_memory_r5", ROOT / ".claude" / "skills" / "prose-edit" / "scripts" / "memory.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod._default_genre(path)


def test_the_runners_copy_the_scenario_where_no_built_in_genre_reaches_it_and_pass_the_genre() -> None:
    review = (ACC / "run-review.sh").read_text(encoding="utf-8")
    teach = (ACC / "run-teach.sh").read_text(encoding="utf-8")
    gate = (ACC / "run-memory-gate.sh").read_text(encoding="utf-8")
    review_doc, teach_doc, gate_dir = _assigned(review, "DOC"), _assigned(teach, "DOC"), _assigned(gate, "DIR")
    for path in (review_doc, teach_doc, f"{gate_dir}/a-1.md"):
        assert path.startswith("docs/zz-") and path.count("/") >= 2, path  # a nested directory of its own
        assert _default_genre(path) is None, path  # so it is not in any genre's "Genre paths:" list
    # a path no genre maps needs --genre on every edit run
    for sh in (review, teach):
        runs = [ln for ln in sh.splitlines() if '"/prose-edit $DOC' in ln]
        assert runs and all("--genre reference-doc" in ln for ln in runs), runs
    assert "docs/zz-review-scenario.md" not in review and "docs/zz-teach-scenario.md" not in teach


def test_a_brief_built_for_a_nested_copy_lists_the_real_documents_and_never_the_copies(prose: Prose) -> None:
    for d in ("zz-review", "zz-teach", "zz-memgate"):
        (prose.cwd / "docs" / d).mkdir()
        (prose.cwd / "docs" / d / "scenario.md").write_text("The scenario copy.\n", encoding="utf-8")
    brief = brief_ok(prose, "build", "docs/zz-review/scenario.md", "--genre", "reference-doc")
    paths = next(ln for ln in brief.splitlines() if ln.startswith("Genre paths:"))
    assert paths == "Genre paths: docs/*.md", paths
    assert "zz-" not in paths


def test_every_runner_uses_the_guard_and_none_installs_its_own_trap_or_copies_into_docs_directly() -> None:
    guard_head = GUARD.read_text(encoding="utf-8").splitlines()[:2]
    assert GUARD.exists() and any("SPDX-License-Identifier" in ln for ln in guard_head)
    for name in RUNNERS:
        sh = (ACC / name).read_text(encoding="utf-8")
        assert 'runner_guard.bash' in sh and f"runner_lock {name}" in sh, name
        assert "runner_sweep_stale" in sh and "runner_track" in sh, name
        assert not re.search(r"^trap ", sh, re.MULTILINE), name  # the guard owns the exit trap
        lock_at = sh.index(f"runner_lock {name}")
        for effect in ('mkdir -p "$OUT"', "memory.py viewer", "cp "):
            if effect in sh:
                assert lock_at < sh.index(effect), (name, effect)  # the lock comes before the first write
    readme = (ACC / "README.md").read_text(encoding="utf-8")
    assert "runner_guard.bash" in readme and "one runner at a time" in readme.lower()


# ---------------------------------------------------------------------------
# 6. review_verdicts: a scenario the runner was not asked for is NOT-RUN, not FAIL
# ---------------------------------------------------------------------------

C_OK_STDIN = {"mode": "stdin", "applied": [], "text": "x", "rejections_stored": False, "log": {"title": "log/stdin/1"}}


def _c_runs() -> dict[str, list[dict]]:
    return {"c1": _script("review", "render", "r", {"genre": "commit-message"}),
            "c2": _script("review", "apply", "d", {"dry_run": True, "accept": [{"n": 1}]}),
            "c2c": _script("review", "apply", "p", C_OK_STDIN)}


def _rows(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    return {ln.split()[0]: ln.split()[1] for ln in capsys.readouterr().out.splitlines() if ln.strip()}


def test_c1_and_c2_are_not_run_when_the_runner_was_not_asked_to_run_them(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv = _review_verdicts()
    _write_runs(tmp_path, _check_a_runs([2], {"edits": [{"n": 1, "old": "x", "new": ""}], "dropped": []}))
    (tmp_path / "scenarios.txt").write_text("a b\n", encoding="utf-8")
    rv.main([str(tmp_path)])
    rows = _rows(capsys)
    assert rows["c1"] == "NOT-RUN" and rows["c2"] == "NOT-RUN"
    assert "FAIL" not in (rows["c1"], rows["c2"])


def test_c1_and_c2_fail_when_they_were_asked_for_and_left_no_transcript(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv = _review_verdicts()
    _write_runs(tmp_path, _check_a_runs([2], {"edits": [], "dropped": []}))
    (tmp_path / "scenarios.txt").write_text("a b c\n", encoding="utf-8")
    rv.main([str(tmp_path)])
    rows = _rows(capsys)
    assert rows["c1"] == "FAIL" and rows["c2"] == "FAIL"
    # no record of what was asked: an absent scenario is not known to be unrequested, so it keeps failing
    (tmp_path / "scenarios.txt").unlink()
    rv.main([str(tmp_path)])
    rows = _rows(capsys)
    assert rows["c1"] == "FAIL" and rows["c2"] == "FAIL"


def test_c1_and_c2_still_fail_when_they_ran_and_failed_and_are_scored_when_they_ran_unasked(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv = _review_verdicts()
    bad = {**_c_runs(), "c2c": _script("review", "apply", "p", {**C_OK_STDIN, "applied": [{"n": 1}]})}
    _write_runs(tmp_path, bad)
    (tmp_path / "scenarios.txt").write_text("c\n", encoding="utf-8")
    rv.main([str(tmp_path)])
    rows = _rows(capsys)
    assert rows["c1"] == "PASS" and rows["c2"] == "FAIL"
    # asked for a and b only, but c ran anyway: it is scored, not hidden behind NOT-RUN
    (tmp_path / "scenarios.txt").write_text("a b\n", encoding="utf-8")
    rv.main([str(tmp_path)])
    rows = _rows(capsys)
    assert rows["c1"] == "PASS" and rows["c2"] == "FAIL"


def test_unrequested_scenarios_exit_3_when_everything_that_ran_passed_and_1_on_any_failure(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv = _review_verdicts()
    _write_runs(tmp_path, _c_runs())
    (tmp_path / "scenarios.txt").write_text("c\n", encoding="utf-8")
    assert rv.main([str(tmp_path)]) == 3
    rows = _rows(capsys)
    assert rows["c1"] == "PASS" and rows["c2"] == "PASS" and rows["a1"] == "NOT-RUN" and rows["b4"] == "NOT-RUN"
    _write_runs(tmp_path, {"c2c": _script("review", "apply", "p", {**C_OK_STDIN, "applied": [{"n": 1}]})})
    assert rv.main([str(tmp_path)]) == 1


def test_run_review_takes_a_scenarios_list_and_records_it_for_the_verdicts() -> None:
    sh = (ACC / "run-review.sh").read_text(encoding="utf-8")
    assert 'SCENARIOS="${SCENARIOS:-a b c}"' in sh
    assert 'scenarios.txt' in sh and "unknown scenario" in sh
    for group in ("a", "b", "c"):
        assert re.search(rf'\*" {group} "\*\)', sh), group  # each group runs only when asked for
    readme = (ACC / "README.md").read_text(encoding="utf-8")
    assert "SCENARIOS" in readme and "NOT-RUN" in readme and "scenarios.txt" in readme


def test_run_review_refuses_an_unknown_scenario_before_anything_runs(tmp_path: Path) -> None:
    env = {**os.environ, "OUT_DIR": str(tmp_path / "out"), "TMPDIR": str(tmp_path / "tmp"), "SCENARIOS": "a z",
           "PROSE_EDIT_RUNNER_LOCK": str(tmp_path / "lock"), "PROSE_EDIT_NX": FAKE_NX,
           "CLAUDE_BIN": "/nonexistent/claude-for-the-test"}
    proc = subprocess.run([str(ACC / "run-review.sh")], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert proc.returncode == 2 and "unknown scenario" in proc.stderr and "z" in proc.stderr
    assert not (tmp_path / "out").exists() and not (tmp_path / "lock").exists()
