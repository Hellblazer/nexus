# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 7 for the RDR-221 Phase 1 exit (nexus-ger02.5/.6/.7): the final review and critique of rounds 4-6.

A voice-card device beats genre, repo and user rules (Sam, 2026-10-03) but not an entry the author wrote for the
document; the runner lock is reclaimed atomically, stops its background jobs and treats an EPERM holder as live;
the messages that name a remedy name it with the allowlisted prefix; a batch records the model and the tree state;
the remaining work-directory sites print their paths; the README says what Phase 1 exit does not claim.

Every transcript here is planted; no model runs in this file.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.prose_edit.conftest import ROOT, Prose
from tests.prose_edit.test_acceptance_tools import ACC, _verdicts
from tests.prose_edit.test_brief import AGENT, SKILL, brief_ok, run_brief, seed
from tests.prose_edit.test_fix_round1 import _step
from tests.prose_edit.test_fix_round3 import ESSAY, SEMICOLON_RULE, STALE_OPEN, _treat
from tests.prose_edit.test_fix_round4 import _allowlist
from tests.prose_edit.test_fix_round5 import GUARD, _bash, _plant_holder, box  # noqa: F401  (box is a fixture)

AGENT_TEXT = AGENT.read_text(encoding="utf-8")
SKILL_TEXT = SKILL.read_text(encoding="utf-8")
README = (ACC / "README.md").read_text(encoding="utf-8")


def _section(text: str, head: str, nxt: str) -> str:
    return text[text.index(head):text.index(nxt, text.index(head))]


# ---------------------------------------------------------------------------
# 1. A device beats genre, repo and user rules; an entry the author wrote for the document beats a device
# ---------------------------------------------------------------------------


def test_the_line_editor_puts_a_device_above_genre_repo_and_user_rules_and_below_a_document_entry() -> None:
    procedure = _section(AGENT_TEXT, "## Procedure", "## Voice card")
    step5 = next(ln for ln in procedure.splitlines() if ln.startswith("5. "))
    assert "document > genre > repo > user" in step5  # the layer order itself is unchanged
    assert "device" in step5 and "genre, repo and user" in step5
    assert "document" in step5.split("device", 1)[1]  # the document layer is named as what still wins
    assert "wins over every layer" not in AGENT_TEXT and "no entry from any layer overrides it" not in AGENT_TEXT


def test_the_line_editor_rules_say_a_conflicting_genre_repo_or_user_rule_becomes_a_query_and_a_document_entry_wins() -> None:
    rules = _section(AGENT_TEXT, "## Rules", "## Output")
    rule = next(ln for ln in rules.splitlines() if ln.startswith("- A voice-card device is never edited"))
    assert "genre, repo or user" in rule
    assert "query" in rule and "never an edit" in rule
    assert "document" in rule and "entry" in rule and "author" in rule  # the exception is stated in the same rule
    card = _section(AGENT_TEXT, "## Voice card", "## Protected regions")
    assert "genre, repo or user" in card and "semicolon" in card
    assert "document-layer entry" in card or "document entry" in card or "entry the author wrote for this document" in card


def test_the_brief_states_the_same_device_rule_in_section_3(prose: Prose) -> None:
    brief = brief_ok(prose, "build", "docs/x.md", "--genre", "reference-doc")
    sheet = _section(brief, "## 3. Style sheet", "## 4. Not a defect")
    assert "genre, repo and user" in sheet and "query" in sheet and "never an edit" in sheet
    assert "document" in sheet.split("device", 1)[1]
    assert "wins over every layer" not in sheet
    assert "document > genre > repo > user" in sheet


# ---------------------------------------------------------------------------
# 2. runner_guard.bash: atomic reclaim, background jobs, EPERM
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    done = subprocess.Popen(["true"])
    done.wait()
    return done.pid


def test_simultaneous_starts_over_a_dead_holder_never_both_hold_the_lock(tmp_path: Path) -> None:
    # A winner holds the lock until every other runner of its trial has given up (exit 75), so what is counted is
    # runners holding at the same moment. Counting exit 0 instead counted a later runner that legitimately took
    # the lock after the first one released it, which a loaded box (CI's -n 8) makes likely: qwen-linux at
    # e7a10089f saw {2: 3} that way.
    trials, procs = 12, 4
    go, release = tmp_path / "go", tmp_path / "release"
    dead = _dead_pid()
    running = []
    for t in range(trials):
        lock = tmp_path / f"lock-{t}"
        lock.mkdir()
        (lock / "holder").write_text(f"{dead} run-dead.sh\n", encoding="utf-8")
        for i in range(procs):
            held = tmp_path / f"held-{t}-{i}"
            script = (f'set -u\nWT={ROOT}\n. {GUARD}\nwhile [ ! -e {go} ]; do :; done\n'
                      f'runner_lock run-{t}-{i}.sh\n: > {held}\n'
                      f'while [ ! -e {release} ]; do sleep 0.05; done\n')
            with (tmp_path / f"err-{t}-{i}").open("w", encoding="utf-8") as err:
                running.append((t, i, subprocess.Popen(
                    ["bash", "-c", script], env={**os.environ, "PROSE_EDIT_RUNNER_LOCK": str(lock)},
                    stdout=subprocess.DEVNULL, stderr=err)))
    time.sleep(0.5)
    go.touch()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        settled = sum(1 for t, i, p in running if p.poll() is not None or (tmp_path / f"held-{t}-{i}").exists())
        if settled == len(running):
            break
        time.sleep(0.05)
    holders = {t: sum((tmp_path / f"held-{t}-{i}").exists() for i in range(procs)) for t in range(trials)}
    refused = [p.returncode for _, _, p in running if p.poll() is not None]
    release.touch()
    for _, _, p in running:
        p.wait(timeout=60)
    # On a failure, show what each runner of a bad trial said: three CI reds once left nothing to diagnose.
    bad = [t for t, n in holders.items() if n != 1]
    said = {f"{t}-{i}": (tmp_path / f"err-{t}-{i}").read_text(encoding="utf-8")[-400:]
            for t in bad[:2] for i in range(procs)}
    assert not bad, (holders, said)  # exactly one runner holds each lock at once
    assert refused and set(refused) == {75}, refused  # every other runner gave up, none crashed


def test_a_runner_that_is_killed_leaves_no_live_background_job_or_its_child(
        box: tuple[Path, Path], tmp_path: Path) -> None:  # noqa: F811
    repo, lock = box
    ready, sub, child = tmp_path / "ready", tmp_path / "sub.pid", tmp_path / "child.pid"
    env = {**os.environ, "PROSE_EDIT_RUNNER_LOCK": str(lock)}
    runner = subprocess.Popen(
        ["bash", "-c", f'set -u\nWT={repo}\n. {GUARD}\nrunner_lock run-jobs.sh\n'
                       f'( sleep 300 & echo $! > {child}; wait ) &\necho $! > {sub}\n'
                       f'touch {ready}\nwhile :; do sleep 0.2; done'],
        env=env, stderr=subprocess.PIPE, text=True)
    pids: list[int] = []
    try:
        for _ in range(100):
            if ready.exists() and child.exists():
                break
            time.sleep(0.1)
        assert ready.exists() and child.exists(), "the runner never started its job"
        pids = [int(sub.read_text()), int(child.read_text())]
        for pid in pids:
            os.kill(pid, 0)  # both alive before the kill
        runner.send_signal(signal.SIGTERM)
        runner.wait(timeout=30)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    assert not alive, f"live after the runner was killed: {alive}"
    assert not lock.exists()


def test_a_holder_this_user_cannot_signal_is_a_live_holder_not_a_dead_one(box: tuple[Path, Path]) -> None:  # noqa: F811
    probe = subprocess.run(["bash", "-c", "kill -0 1"], capture_output=True, text=True)
    if probe.returncode == 0:
        pytest.skip("pid 1 is signalable here (running as root): no EPERM holder to test with")
    repo, lock = box
    _plant_holder(lock, 1, "run-other-user.sh")
    proc = _bash('runner_lock run-test.sh\necho REACHED\n', repo, lock)
    assert proc.returncode == 75, (proc.returncode, proc.stderr)
    assert "REACHED" not in proc.stdout and "run-other-user.sh" in proc.stderr
    assert (lock / "holder").read_text(encoding="utf-8").startswith("1 ")  # not reclaimed


# ---------------------------------------------------------------------------
# 3. Messages that name a remedy name it with the allowlisted prefix
# ---------------------------------------------------------------------------


def _remedy(stderr: str) -> str:
    return stderr.split(" with: ", 1)[1].strip().splitlines()[0]


def _on_the_allowlist(command: str) -> bool:
    return any(command.startswith(prefix + " ") for prefix in _allowlist())


def test_the_stale_entry_message_prints_the_full_allowlisted_command(prose: Prose) -> None:
    seed(prose, "doc", lists=_treat("ignored", STALE_OPEN), path=ESSAY)
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1
    command = _remedy(proc.stderr)
    assert command.startswith("python3 .claude/skills/prose-edit/scripts/memory.py entries ")
    assert _on_the_allowlist(command), command
    assert "--level doc" in command and "--remove-item" in command


def test_the_malformed_and_doubled_entry_messages_name_a_remedy_with_the_allowlisted_prefix(prose: Prose) -> None:
    # an entry that does not start with quoted opening words
    seed(prose, "repo", lists={"site_page_section3_ignored": ["no quotes here"]})
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1 and "malformed" in proc.stderr
    assert _on_the_allowlist(_remedy(proc.stderr)), proc.stderr
    prose.ok("entries", "--level", "repo", "--remove-item", "site_page_section3_ignored=no quotes here")
    # opening words two bullets share
    seed(prose, "repo", lists={"site_page_section3_ignored": ['"No..." (too short)']})
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1 and "more than one section 3 bullet" in proc.stderr
    assert _on_the_allowlist(_remedy(proc.stderr)), proc.stderr
    prose.ok("entries", "--level", "repo", "--remove-item", 'site_page_section3_ignored="No..." (too short)')
    # the same rule given two treatments
    seed(prose, "repo", lists={**_treat("ignored", SEMICOLON_RULE), **_treat("query_only", SEMICOLON_RULE)})
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 1 and "more than one treatment" in proc.stderr
    assert _on_the_allowlist(_remedy(proc.stderr)), proc.stderr


# ---------------------------------------------------------------------------
# 4. record-run.sh records the session's model and the tree state
# ---------------------------------------------------------------------------


def _record(out: Path, name: str, events: list[dict]) -> None:
    (out / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    proc = subprocess.run([str(ACC / "record-run.sh"), str(out), name], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr


def test_record_run_writes_the_session_model_from_the_init_event(tmp_path: Path) -> None:
    _record(tmp_path, "a-1", [{"type": "system", "subtype": "init", "model": "claude-haiku-9-9", "tools": ["Bash"]},
                              {"type": "result"}])
    assert (tmp_path / "a-1.model").read_text(encoding="utf-8").strip() == "claude-haiku-9-9"
    _record(tmp_path, "a-2", [{"type": "result"}])
    assert (tmp_path / "a-2.model").read_text(encoding="utf-8").strip() == "unknown"  # no init event: said, not blank


def test_record_run_writes_git_status_porcelain_of_the_checkout(tmp_path: Path) -> None:
    _record(tmp_path, "a-1", [{"type": "result"}])
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT).stdout
    assert (tmp_path / "a-1.status").read_text(encoding="utf-8") == status


def test_verdicts_print_the_models_of_the_batch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    v = _verdicts()
    (tmp_path / "canary-nx.rc").write_text("rc=0\n", encoding="utf-8")
    (tmp_path / "canary-nx.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "canary-nx.model").write_text("claude-haiku-9-9\n", encoding="utf-8")
    (tmp_path / "other.model").write_text("claude-opus-9-9\n", encoding="utf-8")
    v.main([str(tmp_path)])
    assert "MODELS claude-haiku-9-9 claude-opus-9-9" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. The remaining work-directory sites print their paths
# ---------------------------------------------------------------------------


def _fields(head: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in head.splitlines())


def test_a_path_run_build_prints_ready_reply_filtered_and_reasons_paths(prose: Prose) -> None:
    out = brief_ok(prose, "build", "docs/x.md", "--work")
    head = out.split("\n\n", 1)[0]
    fields = _fields(head)
    work = Path(fields["WORK"])
    try:
        assert list(fields) == ["WORK", "DISPATCH", "REPLY", "FILTERED", "REASONS"], list(fields)
        assert Path(fields["REPLY"]) == work / "reply.txt"
        assert Path(fields["FILTERED"]) == work / "filtered.json"
        assert Path(fields["REASONS"]) == work / "reasons.json"
        assert all(Path(fields[k]).is_absolute() for k in ("REPLY", "FILTERED", "REASONS"))
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_tmpdir_ready_prints_the_directory_and_every_file_path_the_skill_writes(prose: Prose) -> None:
    fields = _fields(brief_ok(prose, "tmpdir", "--ready").strip())
    work = Path(fields["WORK"])
    try:
        assert list(fields) == ["WORK", "INPUT", "REPLY", "FILTERED", "REASONS", "ENTRY", "CARD"], list(fields)
        assert work.is_dir() and (work / ".prose-edit-work").is_file()
        for key, name in (("INPUT", "input.txt"), ("REPLY", "reply.txt"), ("FILTERED", "filtered.json"),
                          ("REASONS", "reasons.json"), ("ENTRY", "entry.json"), ("CARD", "card.json")):
            assert Path(fields[key]) == work / name and Path(fields[key]).is_absolute(), key
    finally:
        brief_ok(prose, "rmtmp", str(work))
    # the bare form is the directory alone, as before
    bare = Path(brief_ok(prose, "tmpdir").strip())
    try:
        assert bare.is_dir() and "=" not in str(bare.name)
    finally:
        brief_ok(prose, "rmtmp", str(bare))


def _command_spans() -> list[str]:
    return re.findall(r"`((?:BRIEF|MEMORY|REVIEW) [^`]+)`", SKILL_TEXT)


def test_no_command_form_of_the_skill_has_the_bare_word_work_as_a_path() -> None:
    for span in _command_spans():
        assert not re.search(r"(?<![A-Za-z_<=])WORK(?![A-Za-z_>=])", span), span
    assert not re.search(r"WORK/", SKILL_TEXT), "a literal WORK/ path is what the model typed"


def test_the_skill_uses_the_printed_paths_and_forbids_typing_work_or_creating_a_work_directory() -> None:
    assert "`BRIEF tmpdir --ready`" in _step(SKILL_TEXT, 3)
    step4 = _step(SKILL_TEXT, 4)
    for label in ("WORK", "DISPATCH", "REPLY", "FILTERED", "REASONS"):
        assert f"`{label}=" in step4, label
    step5 = _step(SKILL_TEXT, 5)
    assert "exactly as printed" in step5
    assert "Never type the word WORK as a path" in step5
    assert "Never create a directory named WORK" in step5 and "repository" in step5
    for n, key in ((8, "`REPLY=`"), (9, "`FILTERED=`"), (12, "`REASONS=`")):
        assert key in _step(SKILL_TEXT, n), (n, key)
    step13 = _step(SKILL_TEXT, 13)
    assert "`ENTRY=`" in step13 and "`CARD=`" in step13
    assert SKILL_TEXT.count("BRIEF tmpdir") == 1  # step 13 reuses step 3's command, it does not name a second one


# ---------------------------------------------------------------------------
# 6. README and runners
# ---------------------------------------------------------------------------


def test_the_readme_has_a_section_on_what_phase_1_exit_does_not_claim() -> None:
    assert "## What Phase 1 exit does not claim" in README
    body = _section(README + "\n## end", "## What Phase 1 exit does not claim", "\n## ")
    low = body.lower()
    for needle in (
        "pass rate", "failure rate", "n=1 to 4", "one rerun", "instructions were edited",
        "haiku", "opus", "dontask", "--add-dir",
        "interactive", "read size limit",
        "with and without", "correction flow", "section 3", "not performed",
        "edit quality", "protected spans", "qualifiers",
        "7, 8, 12 and 14", "unit", "sam's decision",
        "mechanism check", "not a rate",
        "model-written", "device coverage",
    ):
        assert needle in low, needle


def test_what_stays_by_hand_says_not_performed() -> None:
    body = _section(README, "### What stays by hand", "\n## ")
    assert "not performed" in body.lower()


def test_the_readme_says_scenario_b_needs_a_and_the_runner_refuses_it_alone(tmp_path: Path) -> None:
    assert "SCENARIOS=\"b\"" in README and "stored rejections" in README
    env = {**os.environ, "OUT_DIR": str(tmp_path / "out"), "TMPDIR": str(tmp_path / "tmp"), "SCENARIOS": "b",
           "PROSE_EDIT_RUNNER_LOCK": str(tmp_path / "lock"), "CLAUDE_BIN": "/nonexistent/claude-for-the-test"}
    proc = subprocess.run([str(ACC / "run-review.sh")], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert proc.returncode == 2 and "needs" in proc.stderr and "a" in proc.stderr
    assert not (tmp_path / "out").exists() and not (tmp_path / "lock").exists()


def test_the_memory_gate_runs_one_copy_at_a_time_by_default() -> None:
    sh = (ACC / "run-memory-gate.sh").read_text(encoding="utf-8")
    assert 'JOBS="${GATE_JOBS:-1}"' in sh
    assert "default 1" in sh and "default 4" not in sh
    assert "GATE_JOBS" in README and "default 1" in README
