# SPDX-License-Identifier: AGPL-3.0-or-later
"""``stop_wake_guard.py``: continue a turn once when it ends promising work nothing will wake (nexus-4cgop).

The contract these tests pin:

- every allow branch (kill switch, ``stop_hook_active``, a running task, a cron, no text, malformed
  input) exits 0 with nothing on stdout; the block is ``{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": ...}}`` (no "Stop hook error" notice, unlike ``decision: block``);
- a Sam-actor clause, a peer or restart as the wake, and a past-tense report are dropped; a CI or run
  wait, a promise to do something later and a timed step are the three classes that block;
- the Sam gate is structural (a gate word governing an approval predicate, or Sam himself), pinned by the
  table-driven ``_CORPUS`` below: SAM-ACTOR rows stay at 0 blocked, TRUE rows block, and approval words used
  as test subjects, courtesies to Sam and unrelated CI waits after an approval word are not dropped (nexus-gyo4g);
- a hand-labelled sample of real turn ends (``tests/fixtures/stop_wake_guard_sample.json``, 185 rows
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


_SAM_ACTOR_TEXTS = [
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
]


@pytest.mark.parametrize("text", _SAM_ACTOR_TEXTS)
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


# --- the measurement corpus (nexus-gyo4g) ----------------------------------------------------------
#
# Every row is (group, text, blocks). The groups are the bead's own: the missed gate phrases (N1), an
# approval word used as a test subject (N2), a trailing courtesy to Sam (N3), the chain hole (N4), and the
# live block seen in hook.log. SAM-ACTOR rows must stay at 0 blocked; TRUE rows must block. The older
# parametrised cases above are folded in by ``_CORPUS`` so one table holds the whole measurement.

#: N1: the gate is structural (a gate word governing an approval predicate), not a closed word list.
_N1_GATED = [
    "I'll merge once it is cleared.",
    "Release is on hold until it is cleared; I'll cut it afterwards.",
    "I'll push once someone gives the word.",
    "I'll proceed once the request is granted.",
    "The question is open. I'll continue once it is settled.",
    "I'll push after the sign off lands.",
    "Hold for the nod. I'll push after.",
    "I'll run the migration when it is cleared for release.",
    "I'll push the tag once it's been signed off.",
    "Waiting on the sign off. I'll push afterwards.",
    "Whenever the operator greenlights, I'll push the tag.",
    "I'll push the tag when the release is greenlit.",
    "Once cleared by the operator, I'll push.",
    "I'll proceed as soon as the operator says so.",
    "I'll push once permitted.",
    "I'll push the tag when told to.",
    "I'll push the tag once we have the nod.",
    "Release cut awaits the OK. I'll run it then.",
    "The tag goes out when the human okays it, and I'll verify the publish after.",
    "I'll merge once it is signed-off.",
    "I'll push when Sam okays it.",
    "I'll cut the release when the user gives the go-ahead.",
]
#: N2: an approval word that is only the subject of a test, a log or a workflow is not a gate.
_N2_TEST_SUBJECTS = [
    "I'll rerun the approval-flow tests once the build finishes.",
    "I'll check the permission tests once the shards finish.",
    "I'll check whether the ack handling test passes once the shards finish.",
    "I'll look at the decision-log test result when the suite completes.",
    "I'll check the authorization tests when the run finishes.",
    "I'll check the run once it finishes, then look at the consent banner.",
    "I'll rebuild the go build once the shards finish.",
    "I'll verify the decision log later.",
    "I'll check the approval workflow run once it finishes.",
]
#: N3: a courtesy to Sam closes the message without asking him anything; the promise before it stands.
_N3_COURTESY = [
    "I'll check the CI result once the shards finish. I'll keep you posted.",
    "I'll check the CI result once the shards finish, and update you.",
    "I'll check the CI result once the shards finish. Will report back to Sam.",
    "I'll check the CI result once the shards finish. Thanks for your patience.",
    "I'll check the CI result once the shards finish. See you then.",
    "I'll check the CI result once the shards finish. If anything fails, you will hear from me.",
]
#: N4: a clause that merely names an approval, or Sam, does not chain to an unrelated CI wait.
_N4_UNCHAINED = [
    "Fixed the decision table bug. Once the shards finish, I'll check CI.",
    "Pushed the fix; Sam can review. After the shards finish, I'll check CI.",
    "I changed the ack handling. Then I'll check the CI result once the shards finish.",
    "Approved by the reviewer. Then I'll check the CI result once the shards finish.",
    "The permission tests pass. Next, I'll check CI once the shards finish.",
    "I'll merge the PR after the backlog is cleared.",
]
#: Observed in ~/.config/nexus/logs/hook.log on 2026-11-07 (a live block).
_LIVE_BLOCKS = ["The nine lane beads close once CI on develop shows my parts green."]
#: Messages that end on Sam: a question, an imperative, or a gate on his approval.
_ENDS_ON_SAM = [
    "I'll check the CI result once the shards finish. Which option do you want?",
    "I'll check the CI result once the shards finish. Say go and I'll tag.",
    "I'll check the CI result once the shards finish. Waiting on Sam for the tag.",
    "I'll check the CI result once the shards finish. The tag is Sam's call.",
    "I'll check the CI result once the shards finish. Let me know if you want the tag.",
    "I'll check the CI result once the shards finish. If you want, I'll start on the next bead.",
]

_CLASS_CASES = [
    "Develop CI is running on 1bd583657. I'll read the verdict once when it finishes.",
    "The planner is still running.",
    "Once both are green I commit the three files by explicit path.",
    "The PyPI publish run is still in progress.",
    "I'll check again later and close the bead if everything is green.",
    "Next I claim the bead and read develop CI on the tip.",
    "Moving on to the epic gate.",
    "The listing flap is next unless you'd rather I take something else.",
    "After it publishes (about 40 to 60 minutes), I'll run the battery.",
    "Closing the bake after 2026-10-26 13:46Z.",
    "I will retry in 25 minutes.",
    "I'll check the CI result once the shards finish.",
]

# --- review r1 of nexus-gyo4g: every probe phrasing (critic's probe_gyo*.py, reviewer's r.py), with its intended outcome ---

#: The user (any principal) is the actor of an approval, a confirmation or a return: allowed.
_GATED_ON_THE_USER = [
    "I'll push the tag once the user approves.",
    "I'll push the tag once the operator signs off.",
    "I'll merge when the maintainer gives the word.",
    "I'll push the tag once you approve.",
    "I'll push once it is cleared.",
    "I'll push once Sam is happy with it.",
    "I'll push once the user is happy with it.",
    "I'll push the tag once Sam confirms.",
    "I'll push the tag once the user confirms.",
    "I'll push the tag once he confirms.",
    "I'll proceed once the owner replies.",
    "I'll proceed after the user answers.",
    "I'll proceed once the user weighs in.",
    "I'll proceed once Sam weighs in.",
    "I'll proceed once Sam answers.",
    "I'll proceed when Sam is back.",
    "I'll proceed when the user is back.",
    "I'll proceed when the user returns.",
    "Waiting on the user.",
    "Waiting on Sam.",
    "I'll push the tag after the user okays it.",
    "I'll push the tag after Sam lets me know.",
    "I'll push the tag when told.",
    "I'll push once Sam's back.",
    "I'll push when I hear back.",
    "I'll push once confirmed.",
    "I'll push on confirmation.",
    "I'll push once the thumbs-up arrives.",
    "I'll push once the user is ready.",
]

#: A reviewer, critic, peer or Bob approves: the peer is the wake (allowed, like 'reports'); Sam approves: his is.
_PEER_OR_APPROVAL_WAKE = [
    "I'll merge after the reviewer approves.",
    "I'll merge after the review agent approves.",
    "I'll merge after review approves.",
    "I'll merge once the critic signs off.",
    "I'll merge once the peer approves.",
    "I'll merge once Bob approves.",
    "I'll merge once the reviewer reports.",
    "I'll push after the reviewer approves.",
    "I'll merge after the reviewer approves and Sam signs off.",
    "I'll merge after Sam approves.",
    "I'll merge after the PR is approved.",
    "I'll merge after the review is approved.",
    "I'll merge after the approval comes.",
    "I'll merge after approval.",
    "I'll check once the review agent reports back.",
    "Waiting on the reviewer.",
    "I'll wait for the reviewer to approve.",
    "Dispatching the reviewer now. I'll merge after the reviewer approves.",
]

#: Principals other than Sam: you, the user, the operator, the maintainer, the owner.
_PRINCIPAL_GATES = [
    "I'll push once you're happy with it.",
    "I'll push when you are ready.",
    "I'll push the tag whenever you say.",
    "I'll push the tag on your word.",
    "I'll push the tag when the user gives me the green light.",
    "I'll push the tag once the user has had a look.",
    "I'll push the tag when the user is satisfied.",
    "I'll push the tag, pending your review.",
    "Holding for the user.",
    "I'll hold the tag until the user decides.",
    "I'll proceed when the operator is available.",
    "I'll proceed once the user is available.",
    "I'll wait for the user's call.",
    "I'll wait for the user to approve.",
    "I'll cut the release when the user says go.",
    "I'll cut the release when you say go.",
    "I'll cut the release after the maintainer merges the PR.",
    "I'll check back after the user reviews.",
]

#: A gate on 'you' or the user, ending on or chaining to him.
_GATES_ON_YOU = [
    "Sam's ack is pending. The build is green. Then I'll check CI once the shards finish.",
    "Waiting on your ack. After that I'll push the tag.",
    "I'll hand you the tag command. Everything is green. After it publishes, I'll check the release.",
    "I'll hand you the tag command. Everything is green. After it publishes, I'll check the release once the shards finish.",
    "I'll check CI once the shards finish. The user needs to approve the tag.",
    "I'll check CI once the shards finish. Ready for your approval.",
    "I'll check CI once the shards finish. Waiting on your approval.",
    "I'll check CI once the shards finish. Waiting on the user's approval.",
    "I'll check CI once the shards finish. Say go and I'll push.",
    "I'll check CI once the shards finish. Awaiting approval.",
    "I'll check CI once the shards finish. Need a go-ahead.",
    "I'll check CI once the shards finish. The tag needs a nod from the user.",
    "Awaiting the user's go-ahead on the tag.",
    "I'll check the build when it finishes. Let me know if you want anything else.",
]

#: The turn is handed to Sam: his reply is the wake.
_HANDOVERS = [
    "The next step is Sam's. I'll push once he does it.",
    "Sam will cut the tag. I'll check the publish after.",
    "Sam has the tag to cut. After it publishes I'll verify.",
    "Tag cut is Sam's. I'll verify the publish afterwards.",
    "That one is for Sam. Afterwards I'll check CI.",
    "Sam is cutting the tag now. Then I'll check the publish.",
    "The release cut belongs to Sam. I'll verify the publish when it lands.",
    "Sam needs to cut the tag. Then I'll check the publish.",
    "Sam needs to look at the diff; I'll push after.",
    "Sam is reviewing. I'll merge when he is done.",
    "Sam is out until Monday. I'll pick this up then.",
    "Sam is away. I'll continue when he returns.",
    "Sam should look at this first. I'll push after that.",
    "The diff is with Sam. I'll push after he reads it.",
    "The decision is Sam's. I'll implement it once it lands.",
    "I've handed the tag command to Sam. I'll check the publish once it runs.",
    "Handed to Sam. I'll verify the publish after he runs it.",
    "Sam runs the tag. Once it publishes, I'll check the battery.",
    "Sam has the ball. I'll resume when it comes back.",
    "The ruling is with Sam. I'll continue after.",
    "The hold is on the maintainer. I'll push once it lifts.",
    "It's the operator's decision. I'll proceed after.",
    "It's up to Sam. I'll continue after.",
    "It is with the operator now. I'll push when they reply.",
    "Parked pending Sam. I'll push after.",
    "Parked on Sam; I'll continue after.",
    "Over to Sam. I'll verify afterwards.",
    "Over to you. I'll verify afterwards.",
    "Blocked on the maintainer's review. I'll merge after.",
]

#: Short handovers and chained then/accordingly.
_HANDOVER_SHORT = [
    "Over to Sam.",
    "It's up to Sam.",
    "Handed to Sam.",
    "The next step is Sam's.",
    "Tag cut is Sam's.",
    "Ready when you are. I'll push then.",
    "Your call. I'll proceed accordingly.",
    "I'll proceed once it is settled.",
    "I'll proceed once the request is granted.",
]

#: Nothing waits on the user: the promise stands.
_OFFERS_AND_NOISE = [
    "I'll merge after the reviewer finishes.",
    "I'll push after the review passes.",
    "I'll run the approval tests.",
    "Next I'll check the permission model.",
    "I'll check the decision once the tests finish.",
    "I'll read the CI result once the shards finish. Thanks!",
    "I'll check CI after the push. You can ignore this.",
    "Done. I'll check CI once the shards finish. See you then.",
    "Approved by the reviewer. Then I'll check CI once the shards finish.",
    "Sam approved it. I'll push the tag now.",
    "I'll push once Sam's call comes in; meanwhile I'll check CI in 5 minutes.",
    "Does that work? I'll check back in 10 minutes.",
    "I'll push after review.",
    "I'll proceed after the reviewer's verdict arrives.",
    "I'll proceed after the review agent returns.",
    "The reviewer is running. I'll merge after the reviewer approves.",
]

#: Ordinary state changes under a gate ('cleared', 'clears', 'settled', 'granted', 'permitted'): run and infrastructure waits.
_STATE_CHANGES = [
    "I'll rebase once the conflict is cleared.",
    "I'll retry if the permission error recurs.",
    "I'll rerun once the lock clears.",
    "I'll check the dashboard when the alert clears.",
    "I'll check again once the deploy is settled.",
    "I'll resume once the migration is granted a lease.",
    "I'll rerun after the decision-tree tests finish.",
    "I'll look again when the cache is cleared.",
    "I'll push once the rate limit clears.",
    "I'll retry when the port is permitted by the firewall.",
    "I'll look at the results after the nod handler is merged.",
    "I'll test it once the approval step lands.",
    "I'll check CI once the approval workflow run finishes.",
    "I'll push once it's good to go.",
]

#: Weak approval words with a non-person subject (review r1, finding 1).
_WEAK_TERMS = [
    "I'll check once the conflict is cleared.",
    "I'll rerun when the lock clears.",
    "I'll check after the pipeline settles.",
    "I'll proceed once the status is OK.",
    "I'll proceed once the health check is OK.",
    "I'll push once the deploy is cleared by the canary.",
    "I'll proceed after the token is granted by the auth server.",
    "I'll check once the rate limit clears.",
]

#: An approval stem that gives characters back must not defeat the test-subject lookahead (finding 3).
_BACKTRACKING = [
    "I'll check once the approval tests pass.",
    "I'll check once the permission-flow tests finish.",
    "I'll check after the permission tests run.",
    "I'll check once the authorization suite finishes.",
    "I'll check once the clearance job finishes.",
]

#: Merely naming Sam is not a wait; neither is 'Sam can review'.
_NAMING_SAM = [
    "I'll read the result once the approval-flow tests finish.",
    "I'll check CI once the approval tests pass.",
    "I'll check CI once the permission tests finish.",
    "I'll check once the decision log tests finish.",
    "I'll rerun after the ack handling tests pass.",
    "I'll ping Sam once the build finishes.",
    "I'll message Sam when CI finishes.",
    "I'll tell Sam the result once the shards finish.",
    "I'll read the shards once they finish, then ping Sam.",
    "I'll check Sam's PR when the shards finish.",
    "Sam's ack is pending. Meanwhile I'll check the shards once they finish.",
    "The user asked me to wait. I'll check CI once the shards finish.",
    "The user needs to approve. I'll check CI once the shards finish.",
    "Pushed the fix; Sam can review. After the shards finish, I'll check CI.",
    "Sam reads the board each morning; I'll check the thread tomorrow.",
]

#: Known false allow, kept visible: any clause with a bare "you" is dropped, so an offer to the user inside a clause
#: that also holds a promise lets the promise through. v1 behaves the same; flipping this row is a decision.
_KNOWN_FALSE_ALLOWS = ["I'll look at the logs, you can approve the plan."]

_CORPUS: list[tuple[str, str, bool]] = (
    [("SAM-ACTOR", t, False) for t in _SAM_ACTOR_TEXTS + _N1_GATED + _ENDS_ON_SAM]
    + [("SAM-ACTOR", t, False) for t in _GATED_ON_THE_USER + _PRINCIPAL_GATES + _GATES_ON_YOU + _HANDOVERS + _HANDOVER_SHORT]
    + [("PEER-WAKE", t, False) for t in _PEER_OR_APPROVAL_WAKE]
    + [("NOT-A-WAIT", t, True) for t in _OFFERS_AND_NOISE + _STATE_CHANGES + _WEAK_TERMS + _BACKTRACKING + _NAMING_SAM]
    + [("N2-test-subject", t, True) for t in _N2_TEST_SUBJECTS]
    + [("N3-courtesy", t, True) for t in _N3_COURTESY]
    + [("N4-unchained", t, True) for t in _N4_UNCHAINED]
    + [("TRUE", t, True) for t in _CLASS_CASES + _LIVE_BLOCKS]
)


@pytest.mark.parametrize(("group", "text", "blocks"), _CORPUS, ids=[f"{g}-{i}" for i, (g, _, _) in enumerate(_CORPUS)])
def test_corpus_row(group: str, text: str, blocks: bool) -> None:
    verdict = swg.evaluate_text(text)
    assert verdict.block is blocks, (group, text, verdict)


def test_corpus_sam_actor_group_stays_at_zero_blocked_and_is_not_vacuous() -> None:
    sam = [(t, swg.evaluate_text(t)) for g, t, _ in _CORPUS if g == "SAM-ACTOR"]
    assert len(sam) >= 150
    assert [t for t, v in sam if v.block] == []
    counts = Counter(g for g, _, _ in _CORPUS)
    assert counts["N2-test-subject"] >= 9 and counts["N3-courtesy"] >= 6 and counts["N4-unchained"] >= 6 and counts["NOT-A-WAIT"] >= 55 and counts["TRUE"] >= 12


@pytest.mark.parametrize("text", _KNOWN_FALSE_ALLOWS)
def test_known_false_allows_stay_visible(text: str) -> None:
    """Pins today's behaviour so a change to it is a decision, not an accident."""
    assert swg.evaluate_text(text).block is False


@pytest.mark.parametrize("text", _N1_GATED)
def test_a_gate_on_an_approval_is_gated_in_the_reminder_too(text: str) -> None:
    """The reminder never tells the model to 'do it now' for a gate clause outside the CI-wait class."""
    clause = next(c for c in swg.clauses(text) if swg.is_sam_gated(c))
    for cls in (swg.CLASS_PROMISE, swg.CLASS_TIMED):
        assert "do it" not in swg.block_reason(cls, clause), (cls, clause)


def test_reminder_keeps_do_it_for_a_plain_ci_wait_and_a_plain_promise() -> None:
    assert "do it" in swg.block_reason(swg.CLASS_CI_WAIT, "I'll check the CI result once the shards finish.")
    assert "do it" in swg.block_reason(swg.CLASS_PROMISE, "I'll check the build later.")
    assert "do it" not in swg.block_reason(swg.CLASS_PROMISE, "I'll rebase after the merge.")


def test_a_denied_run_state_resets_the_chain_but_a_status_sentence_does_not() -> None:
    """Review r2 N4. 'No suite is still running' matches the CI class and is denied: it ends what hung from the Sam
    clause. A sentence that matches no class stays transparent (fixture sel3-25 pins that: a status line sits
    between 'hand you the tag command' and 'After it publishes')."""
    gate = "Once you give it, I'll push the tag. "
    assert swg.evaluate_text(gate + "No suite is still running. After the shards finish, I'll check CI.").block is True
    assert swg.evaluate_text(gate + "The planner is idle. After the shards finish, I'll check CI.").block is False


def test_final_sam_is_set_only_for_a_question_an_imperative_or_an_approval_gate() -> None:
    for text in ("Thanks for your patience.", "See you then.", "I'll keep you posted.", "Your fix is in."):
        assert swg.evaluate_text("I'll check the build later. " + text).final_sam is False, text
    for text in ("Which one?", "Say go.", "Waiting on Sam for the tag.", "Once you approve, I'll push."):
        assert swg.evaluate_text("I'll check the build later. " + text).block is False, text


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
