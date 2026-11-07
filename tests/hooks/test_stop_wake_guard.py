# SPDX-License-Identifier: AGPL-3.0-or-later
"""``stop_wake_guard.py``: continue a turn once when it ends promising work nothing will wake (nexus-4cgop).

The contract these tests pin:

- every allow branch (kill switch, ``stop_hook_active``, a running task, a cron, no text, malformed
  input) exits 0 with nothing on stdout; the block is ``{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": ...}}`` (no "Stop hook error" notice, unlike ``decision: block``);
- a Sam-actor clause, a peer or restart as the wake, and a past-tense report are dropped; a CI or run
  wait, a promise to do something later and a timed step are the three classes that block;
- a hand-labelled sample of real turn ends (``tests/fixtures/stop_wake_guard_sample.json``, 168 rows
  from the 2026-10-21..11-05 transcripts) replays with ZERO Sam-actor rows blocked, every true row
  blocked, and a small bound on blocked peer-wake and past-tense rows;
- the script imports only the standard library (it runs at the end of every turn), writes only to
  ``hook.log``, and is wired in ``hooks.json`` as a ``Stop`` command hook.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

from tests._platform import scrubbed_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "conexus" / "hooks" / "scripts" / "stop_wake_guard.py"
HOOKS_JSON = ROOT / "conexus" / "hooks" / "hooks.json"
SAMPLE = ROOT / "tests" / "fixtures" / "stop_wake_guard_sample.json"


def _load():
    spec = importlib.util.spec_from_file_location("stop_wake_guard_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


swg = _load()

BLOCKING = "The suite is still running. When it finishes, I'll read the result and close the bead."


def _payload(text: str = BLOCKING, **extra: object) -> dict:
    base: dict = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": text,
        "background_tasks": [],
        "session_crons": [],
    }
    base.update(extra)
    return base


def _run(payload: object, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    child_env = scrubbed_env(NEXUS_CONFIG_DIR=str(tmp_path), PATH=os.environ.get("PATH", ""), **env)
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT)], input=raw, capture_output=True, text=True, encoding="utf-8", env=child_env, timeout=60
    )


def _log(tmp_path: Path) -> str:
    log = tmp_path / "logs" / "hook.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


# --- the allow branches ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "env", "reason"),
    [
        (_payload(), {swg.KILL_SWITCH_ENV: "0"}, "kill-switch"),
        (_payload(stop_hook_active=True), {}, "stop-hook-active"),
        (_payload(background_tasks=[{"id": "t1", "type": "shell", "status": "running"}]), {}, "armed"),
        (_payload(session_crons=[{"id": "c1", "schedule": "0 9 * * *", "recurring": False, "prompt": "x"}]), {}, "armed"),
        (_payload(""), {}, "no-text"),
        (_payload(None), {}, "no-text"),
        ([], {}, "bad-input"),
        (_payload("Done. Everything is pushed and green."), {}, "no-match"),
    ],
    ids=["kill-switch", "stop-hook-active", "task-armed", "cron-armed", "empty-text", "null-text", "not-an-object", "nothing-open"],
)
def test_allow_branches(payload: object, env: dict[str, str], reason: str) -> None:
    decision = swg.decide(payload, env)
    assert (decision.block, decision.reason) == (False, reason)


@pytest.mark.parametrize(
    "fields",
    [
        {"background_tasks": None, "session_crons": None},
        {"background_tasks": "running", "session_crons": []},
        {"background_tasks": {"id": "t"}, "session_crons": []},
        {"background_tasks": [], "session_crons": 0},
        "absent-both",
        "absent-tasks",
        "absent-crons",
    ],
    ids=["null-both", "tasks-string", "tasks-dict", "crons-int", "absent-both", "absent-tasks", "absent-crons"],
)
def test_an_absent_or_non_list_armed_field_is_unknown_and_allows(fields: object) -> None:
    """Unknown is not 'nothing armed' (review r1, F4): a Claude Code build that omits the fields would
    otherwise have the guard fire while a subagent or Monitor runs. Both fields were present on 2.1.319."""
    payload = _payload()
    if fields == "absent-both":
        del payload["background_tasks"], payload["session_crons"]
    elif fields == "absent-tasks":
        del payload["background_tasks"]
    elif fields == "absent-crons":
        del payload["session_crons"]
    else:
        assert isinstance(fields, dict)
        payload.update(fields)
    decision = swg.decide(payload, {})
    assert (decision.block, decision.reason) == (False, "armed-state-unknown")


def test_kill_switch_only_for_a_literal_zero() -> None:
    """Any other value (including unset, empty, '1') leaves the guard on."""
    for value in ("", "1", "off", "false"):
        assert swg.decide(_payload(), {swg.KILL_SWITCH_ENV: value}).block is True


def test_block_decision_names_class_and_clause() -> None:
    decision = swg.decide(_payload(), {})
    assert decision.block is True
    assert decision.reason == swg.CLASS_CI_WAIT
    assert "still running" in decision.clause


# --- the three classes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cls"),
    [
        ("Develop CI is running on 1bd583657. I'll read the verdict once when it finishes.", swg.CLASS_CI_WAIT),
        ("The planner is still running.", swg.CLASS_CI_WAIT),
        ("Once both are green I commit the three files by explicit path.", swg.CLASS_CI_WAIT),
        ("The PyPI publish run is still in progress.", swg.CLASS_CI_WAIT),
        ("I'll check again later and close the bead if everything is green.", swg.CLASS_PROMISE),
        ("Next I claim the bead and read develop CI on the tip.", swg.CLASS_PROMISE),
        ("Moving on to the epic gate.", swg.CLASS_PROMISE),
        ("The listing flap is next unless you'd rather I take something else.", swg.CLASS_PROMISE),
        ("After it publishes (about 40 to 60 minutes), I'll run the battery.", swg.CLASS_CI_WAIT),
        ("Closing the bake after 2026-10-26 13:46Z.", swg.CLASS_TIMED),
        ("I will retry in 25 minutes.", swg.CLASS_PROMISE),
    ],
)
def test_each_class_blocks(text: str, cls: str) -> None:
    verdict = swg.evaluate_text(text)
    assert verdict.block is True, text
    assert verdict.cls == cls, (text, verdict)


def test_timed_class_has_its_own_match() -> None:
    """A timed step with no promise verb and no run noun still blocks (the class is not shadowed)."""
    verdict = swg.evaluate_text("The bake ends no earlier than 2026-10-26 13:46Z.")
    assert (verdict.block, verdict.cls) == (True, swg.CLASS_TIMED)


# --- what is dropped ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Say go and I'll start the runs.",
        "If you want, I'll start on 1 and 2 now.",
        "Waiting for your go on the tag.",
        "Should I go ahead with the restart?",
        "Once you give it, I'll push the tag.",
        "I'll rebase when you say go.",
        "Name a bead and I'll take it.",
        "! run nx upgrade, then I'll check it.",
        "The tag needs Sam. Once Sam merges, I'll rebase.",
        # Gates phrased without "you" (nexus-4cgop review r1, B1 and F1): an approval word is the gate.
        "I'll push the tag once the release go arrives.",
        "I'll merge the PR once approved.",
        "I'll cut the tag after approval.",
        "Pushing to develop is next, pending the ack.",
        "It is Sam's decision. I'll proceed once that comes through.",
        "This needs the operator's go, then I'll run the migration.",
        "Once Sam's go lands, I'll push the tag.",
        "It needs the go from Sam. I'll check the publish after.",
        "Sam will decide; I'll proceed after.",
        "Once it's approved I'll merge.",
        "The approval is pending. I'll push when it is granted.",
        "Tag push is gated on authorization; I'll push when it's recorded.",
        "Cutting the tag is Sam's call. I'll check CI after it publishes.",
        "Standing by for the green light; then I'll push.",
        "I'll pick this up when a decision comes.",
    ],
)
def test_sam_actor_is_never_blocked(text: str) -> None:
    verdict = swg.evaluate_text(text)
    assert verdict.block is False, verdict
    assert verdict.sam_dropped >= 1


def test_default_proceed_idiom_is_not_a_sam_actor_clause() -> None:
    """'unless you'd rather ...' names Sam only to offer an override; the session carries on."""
    assert swg.evaluate_text("The flap is next unless you'd rather I take something else.").block is True


def test_sam_gate_drops_the_chained_follow_ups() -> None:
    chained = "Once you give it, I'll push the tag. After it publishes (about 40 minutes), I'll run the battery."
    assert swg.evaluate_text(chained).block is False
    # No gate: the same second sentence on its own blocks.
    assert swg.evaluate_text("After it publishes (about 40 minutes), I'll run the battery.").block is True


def test_a_question_to_sam_gates_the_conditional_that_follows() -> None:
    assert swg.evaluate_text("Build it or not? Once the marks are in, I'll rebase on develop.").block is False


def test_sam_clause_without_a_gate_does_not_hide_an_independent_step() -> None:
    verdict = swg.evaluate_text("Your calls: the cut after the freeze. Next, I'll rebase both branches. Then CI runs the shards.")
    assert verdict.block is True and "rebase" in verdict.clause


def test_a_message_that_ends_on_sam_is_allowed_whole() -> None:
    """His reply is the wake, so an independent step earlier in the message does not block it."""
    text = "Your calls: the cut after the freeze. Next, I'll rebase both branches. Say if you want the job first."
    verdict = swg.evaluate_text(text)
    assert (verdict.block, verdict.final_sam, verdict.cls) == (False, True, swg.CLASS_PROMISE)
    decision = swg.decide(_payload(text), {})
    assert (decision.block, decision.reason) == (False, "final-to-sam")
    # The same step with the Sam sentence moved off the end blocks again: the rule reads the LAST clause.
    assert swg.evaluate_text("Your calls: the cut after the freeze. Next, I'll rebase both branches.").block is True


@pytest.mark.parametrize(
    "text",
    [
        "CI is still running on the branch. Say go and I'll tag.",
        "Shard 3 is still in progress. Sam, which option do you want?",
        "The suite is still running. I'll read it when it finishes. Waiting on Sam for the tag.",
        "Once the shards finish I'll read the result. The tag is Sam's call.",
    ],
)
def test_a_status_line_before_a_hand_off_to_sam_does_not_block(text: str) -> None:
    assert swg.evaluate_text(text).block is False, text


def test_only_the_tail_of_a_long_message_is_read() -> None:
    """An early status line cannot block when the message ends on a question to Sam (review r1, S1)."""
    head = "The planner is still running. " + "Background detail that is neither a wait nor a promise. " * 40
    assert swg.evaluate_text(head + "Which option do you want?").block is False
    # And the reverse: a promise inside the tail still blocks, whatever precedes it.
    assert swg.evaluate_text("Which option do you want? " + head + "I'll check the build later.").block is True
    # The cut starts at a sentence boundary, so the tail never opens on half a sentence.
    assert not swg.clauses("x" * 700 + ". " + "Next, I'll rebase.")[0].startswith("x")


def test_a_fifty_kilobyte_single_line_returns_quickly() -> None:
    """Review r1 S2/F2: unanchored lookaheads were quadratic, 8 KB took 1.3 to 2.2 s and 40 KB passed the hook's 10 s timeout."""
    for line in ("alpha beta " * 4600, "at " * 17000, "| a | b | c " * 4200, "ci run, " * 6300):
        assert len(line) >= 50_000
        start = time.perf_counter()
        swg.evaluate_text(line)
        assert time.perf_counter() - start < 1.0, line[:20]
    start = time.perf_counter()
    assert swg.decide(_payload("alpha beta " * 4600 + "I'll check the build later."), {}).block is True
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "text",
    [
        "I'll send the merged picture once the replies are in.",
        "I'll start on the code once kimi answers.",
        "If it asks for READY after this restart, I'll answer then.",
        "I'll rebase the fold once bob lands it.",
        "It will send me the develop sha when the push lands.",
        "Once nexus_981 sends the all-clear, I'll push.",
        "I'll be notified when the run finishes.",
        "When the mailbox note arrives I'll read it.",
        "Once the script logs its new run, I'll do the head count.",
        "After the next compaction, I'll read the checkpoint.",
    ],
)
def test_peer_or_event_wake_is_dropped(text: str) -> None:
    verdict = swg.evaluate_text(text)
    assert verdict.block is False, verdict
    assert verdict.peer_dropped >= 1


@pytest.mark.parametrize(
    "text",
    [
        "I told linda I'll check the shard tomorrow.",
        "I asked bob; he'll read the check runs.",
        "Next time I'll run the whole set once before any reviewer.",
        "I haven't checked CI, so nothing is running.",
        "I'll stay off engine and edge work until 20:27.",
        "I'll hold all pushes in 25 minutes.",
        "Nothing is in flight and no suite is still running.",
        "The nightly run was at 09:25Z.",
        "The checks passed at 04:58Z.",
    ],
)
def test_reports_restraints_and_negations_do_not_block(text: str) -> None:
    assert swg.evaluate_text(text).block is False, text


def test_text_preparation_ignores_code_and_fences() -> None:
    fenced = "Here is the command:\n```\nsleep 600 && gh run watch\n```\nDone."
    assert swg.evaluate_text(fenced).block is False
    assert swg.evaluate_text("Run `until ci is green; do sleep 5; done` yourself.").block is False


def test_bullets_and_newlines_split_sentences() -> None:
    text = "- **Waiting on you:** the tag.\n- I'll check CI on the tip once it finishes the shards."
    verdict = swg.evaluate_text(text)
    assert verdict.block is True and verdict.sam_dropped == 1


def test_curly_apostrophes_are_normalised() -> None:
    assert swg.evaluate_text("I’ll check the build later.").block is True


# --- the reason -----------------------------------------------------------------------------------


def test_reason_quotes_the_clause_and_offers_the_ways_out() -> None:
    reason = swg.block_reason(swg.CLASS_CI_WAIT, "The planner is still running.")
    for needle in ("The planner is still running.", "Monitor", "until-loop", "ScheduleWakeup", "CronCreate", "who it is"):
        assert needle in reason, needle
    assert "a CI or run wait" in reason


def test_reason_never_says_do_it_now_for_a_clause_that_names_an_approval() -> None:
    plain = swg.block_reason(swg.CLASS_PROMISE, "I'll check the build later.")
    assert "do it" in plain
    for clause in ("I'll push once the ruling arrives.", "I'll push after Sam's note."):
        gated = swg.block_reason(swg.CLASS_PROMISE, clause)
        assert "do it" not in gated, clause
        assert "who it is" in gated and "ScheduleWakeup" in gated


def test_reason_caps_a_long_clause() -> None:
    reason = swg.block_reason(swg.CLASS_PROMISE, "I'll " + "check " * 200)
    assert len(reason) < 1200 and "..." in reason


# --- the process ----------------------------------------------------------------------------------


def test_block_end_to_end(tmp_path: Path) -> None:
    proc = _run(_payload(), tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "The suite is still running." in out["hookSpecificOutput"]["additionalContext"]
    assert proc.stderr == ""
    line = _log(tmp_path).strip().splitlines()[-1]
    assert " INFO stop-wake-guard: block class=ci-wait session=11111111 " in line


@pytest.mark.parametrize(
    ("payload", "env", "reason"),
    [
        (_payload(stop_hook_active=True), {}, "stop-hook-active"),
        (_payload(), {"NX_STOP_WAKE_GUARD": "0"}, "kill-switch"),
        (_payload(background_tasks=[{"id": "t"}]), {}, "armed"),
        (_payload("All done."), {}, "no-match"),
    ],
)
def test_allow_end_to_end_prints_nothing_and_logs_the_reason(tmp_path: Path, payload: object, env: dict, reason: str) -> None:
    proc = _run(payload, tmp_path, **env)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert f" INFO stop-wake-guard: allow reason={reason} " in _log(tmp_path)


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null", '{"last_assistant_message": 5}', "\xff\xfe"])
def test_malformed_input_fails_open(tmp_path: Path, raw: str) -> None:
    proc = _run(raw, tmp_path)
    assert (proc.returncode, proc.stdout) == (0, "")


def test_unwritable_log_directory_fails_open(tmp_path: Path) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    proc = _run(_payload(), blocker)  # NEXUS_CONFIG_DIR is a file: the log cannot be made
    assert proc.returncode == 0
    assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"]  # the verdict still stands; only the log is lost


def test_internal_error_fails_open(tmp_path: Path) -> None:
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('m', {str(SCRIPT)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "def boom(*a, **k): raise RuntimeError('boom')\n"
        "m.decide = boom\n"
        "sys.exit(m.main())\n"
    )
    env = scrubbed_env(NEXUS_CONFIG_DIR=str(tmp_path), PATH=os.environ.get("PATH", ""))
    proc = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(_payload()), capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert "allow reason=error error=RuntimeError" in _log(tmp_path)


def test_unicode_closing_text_round_trips_on_a_cp1252_console(tmp_path: Path) -> None:
    """The block is ASCII-escaped JSON, so a console that cannot encode the quoted clause still gets it.

    PYTHONIOENCODING makes stdout cp1252 on any platform (``-I`` would ignore it, so the child runs
    without it); with ``ensure_ascii=False`` the check mark is unencodable, the write raises, the guard
    fails open and stdout is empty, so this test fails.
    """
    env = scrubbed_env(NEXUS_CONFIG_DIR=str(tmp_path), PATH=os.environ.get("PATH", ""), PYTHONIOENCODING="cp1252")
    text = "The suite is still running \u2713 \u2014 I\u2019ll read it when it finishes."
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(_payload(text)), capture_output=True, text=True, encoding="utf-8", env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "\u2713" in context


# --- structure ------------------------------------------------------------------------------------


def test_script_imports_only_the_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))
    assert not {"nexus", "structlog"} & imported


def test_config_dir_matches_the_shared_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import _endpoint_resolve  # noqa: PLC0415 -- the plugin's own sibling, loaded for parity only
    finally:
        sys.path.remove(str(SCRIPT.parent))
    for name, value in (("NEXUS_CONFIG_DIR", None), ("NX_CONFIG_DIR", str(tmp_path / "a")), ("NEXUS_CONFIG_DIR", str(tmp_path / "b"))):
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.delenv("NX_CONFIG_DIR", raising=False)
        if value:
            monkeypatch.setenv(name, value)
        assert Path(swg._config_dir()) == _endpoint_resolve.default_config_dir()


def test_hooks_json_wires_stop_as_a_command_hook() -> None:
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    entries = data["hooks"]["Stop"]
    handlers = [h for entry in entries for h in entry["hooks"]]
    ours = [h for h in handlers if any(str(a).endswith("hooks/scripts/stop_wake_guard.py") for a in h.get("args", []))]
    assert len(ours) == 1, "exactly one Stop entry runs the guard"
    hook = ours[0]
    assert hook["type"] == "command" and hook["command"] == "uv"
    assert hook["args"][:2] == ["tool", "run"] and "--no-config" in hook["args"]
    assert 5 <= hook["timeout"] <= 15, "a per-turn hook gets a short timeout"
    assert all("matcher" not in entry for entry in entries), "Stop takes no matcher"


# --- the labelled sample --------------------------------------------------------------------------


def _rows() -> list[dict]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))["rows"]


def _blocked(row: dict) -> bool:
    payload = _payload(row["text"], background_tasks=[{"id": "t"}] if row["armed"] else [])
    return swg.decide(payload, {}).block


def test_sample_is_not_vacuous() -> None:
    """A replay over a thin or lopsided sample proves nothing (the nexus-moht0 doctrine)."""
    rows = _rows()
    counts = Counter(r["label"] for r in rows)
    assert len(rows) >= 100
    assert counts["TRUE"] >= 15 and counts["SAM-ACTOR"] >= 50 and counts["PEER-WAKE"] >= 25 and counts["PAST-TENSE"] >= 15
    assert counts["ARMED"] >= 1
    assert sum(1 for r in rows if r.get("holdout")) >= 30
    assert len({r["id"] for r in rows}) == len(rows)
    assert {r["label"] for r in rows} <= {"TRUE", "SAM-ACTOR", "PEER-WAKE", "PAST-TENSE", "ARMED"}
    assert all(r["armed"] == (r["label"] == "ARMED") for r in rows)


def test_sample_carries_sentences_only() -> None:
    for row in _rows():
        text = row["text"]
        assert len(text) <= 400, row["id"]
        for forbidden in ("/Users/", "/private/", "http://", "https://", "Bearer ", "sk-"):
            assert forbidden not in text, (row["id"], forbidden)


def test_replay_blocks_no_sam_actor_row_and_no_armed_row() -> None:
    wrong = [(r["id"], r["text"][-80:]) for r in _rows() if r["label"] in ("SAM-ACTOR", "ARMED") and _blocked(r)]
    assert wrong == []


def test_replay_flags_every_true_row_but_the_known_misses() -> None:
    """TRUE recall. A row may carry ``known_miss``: a TRUE row the design lets through on purpose (the
    message ends on a question to Sam, so his reply is the wake). The set of misses must equal the set of
    marked rows, so neither a new miss nor a silently fixed one passes."""
    true_rows = [r for r in _rows() if r["label"] == "TRUE"]
    missed = sorted(r["id"] for r in true_rows if not _blocked(r))
    marked = sorted(r["id"] for r in true_rows if r.get("known_miss"))
    assert missed == marked, f"recall {len(true_rows) - len(missed)}/{len(true_rows)}"
    assert len(marked) <= 2


def test_the_known_miss_is_the_final_to_sam_rule() -> None:
    for row in (r for r in _rows() if r.get("known_miss")):
        decision = swg.decide(_payload(row["text"]), {})
        assert (decision.block, decision.reason) == (False, "final-to-sam"), row["id"]
        assert row["known_miss"] == "final-to-sam"


def test_replay_holdout_recall_and_false_blocks_stay_bounded() -> None:
    """The holdout is SPENT: its three TRUE rows scored the rules once and then drove the final
    "Moving on to" rule, so this pins the fit; a fresh transcript window is the next real holdout."""
    held = [r for r in _rows() if r.get("holdout")]
    hits = sum(1 for r in held if r["label"] == "TRUE" and _blocked(r))
    total = sum(1 for r in held if r["label"] == "TRUE")
    assert total >= 3 and hits == total
    other = [r for r in held if r["label"] in ("PEER-WAKE", "PAST-TENSE")]
    assert sum(1 for r in other if _blocked(r)) <= 1


def test_replay_false_block_rate_on_non_true_rows_is_small() -> None:
    non_true = [r for r in _rows() if r["label"] != "TRUE"]
    false_blocks = [r["id"] for r in non_true if _blocked(r)]
    assert len(false_blocks) <= 2, false_blocks
