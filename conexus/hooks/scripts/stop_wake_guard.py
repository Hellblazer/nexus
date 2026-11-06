#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
# /// script
# requires-python = ">=3.12"
# ///
"""Stop hook: continue a turn once when it ends promising work that nothing will wake (nexus-4cgop).

A turn that closes with "I'll read the CI result once the shards finish" and arms nothing
leaves the session idle until some other message happens along: in the 2026-10-21..11-04
window about 1 turn end in 100 was such a stop, roughly 43-58 idle hours (T2
``nexus/compaction-continuation-research-2026-11-04 D turn ends``). This hook is the guard.

THE RULE. Exit 0 and print nothing (allow the stop) when any of these holds:

- ``NX_STOP_WAKE_GUARD=0`` (kill switch);
- ``stop_hook_active`` is true (Claude Code is already continuing because of a stop hook; one
  continuation per stop);
- ``background_tasks`` or ``session_crons`` in the input is non-empty (something is armed:
  a running Agent, Monitor, background Bash, ScheduleWakeup or CronCreate);
- the closing text (``last_assistant_message``) is empty or not text.

Otherwise the text is split into sentences and clauses. A clause is DROPPED when its actor is
Sam (it addresses "you", asks a question, says "say go and I'll", is a ``!`` command) or when it
names a peer, mailbox, notification or restart as the wake, or when it is a past-tense report.
A block follows when any surviving clause is (a) a CI or run wait, (b) a promise to check or do
something later, or (c) a timed step (clock time, date, "in N minutes"). The block is
``{"decision": "block", "reason": ...}``; the reason quotes the clause and tells the model to arm
a wake now or state plainly who the wake is, then end the turn.

A sentence that gates on Sam ("once you give it, I'll push the tag") also drops the "After X,
..." / "Then ..." sentences that chain from it.

Every decision is logged to ``<config>/logs/hook.log`` (allow reason, or block plus the matched
class) for measurement. Any error fails open (exit 0). The avoidable-permission-stop class (a
step the session could take, put to Sam as a question) is out of scope on purpose.

The script imports only the standard library: it runs at the end of every turn, so its import
cost is paid on every turn (``tests/hooks/test_stop_wake_guard.py`` pins the import list).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import NamedTuple

KILL_SWITCH_ENV = "NX_STOP_WAKE_GUARD"
_REASON_CLAUSE_CAP = 240
_LOG_CLAUSE_CAP = 140

# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LEAD_MARKUP = re.compile(r"^\s*(?:[-*+>|]+\s+|\d+[.)]\s+)+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[*_A-Z0-9-])")
_CLAUSE_SPLIT = re.compile(r";\s+|,\s+and\s+(?=(?:i|we|it|they|the|then|nothing|next)\b)|\s+[-—]{1,2}\s+", re.I)


def _prepare(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = _FENCE.sub(" ", text)
    text = text.replace("```", " ")  # a stray fence marker must not pair inline-code spans wrongly
    text = _INLINE_CODE.sub("code", text)
    return text.replace("**", "").replace("__", "")


def clauses(text: str) -> list[str]:
    """Sentences, then clauses, in order. Bullets and newlines end a sentence."""
    out: list[str] = []
    for line in _prepare(text).splitlines():
        line = _LEAD_MARKUP.sub("", line).strip()
        if not line:
            continue
        for sentence in _SENT_SPLIT.split(line):
            for clause in _CLAUSE_SPLIT.split(sentence):
                clause = clause.strip()
                if clause:
                    out.append(clause)
    return out


# ---------------------------------------------------------------------------
# Drops: Sam is the actor, a peer or notification is the wake, past-tense report
# ---------------------------------------------------------------------------

#: A clause that addresses Sam or asks him something. "you" in any form is enough; the one
#: exception is the default-proceed idiom "unless you'd rather ...", which names Sam only to
#: offer an override while the session carries on.
#: "Sam's ruling" is an object, not an actor; a bare "Sam" is still Sam.
_SAM_YOU = re.compile(r"\b(?:you|your|yours|you'd|you'll|you've|you're|yourself|sam)\b(?!')", re.I)
_SAM_DEFAULT_PROCEED = re.compile(r"\bunless you(?:'d| would| want| prefer)\b", re.I)
_SAM_IMPERATIVE = re.compile(
    r"^(?:please\b|say\b|tell me\b|let me know\b|want me\b|should i\b|shall i\b|do you\b|would you\b|"
    r"can you\b|could you\b|which\b.*\?|!)"
    r"|^(?:or |and |so )?(?:name|pick|point|give|send|choose|approve|reply|ping|confirm|merge|run)\b.*\band i'?ll\b",
    re.I,
)
#: A clause that gates on Sam (a condition about "you", a hand-off to you, a wait on you, a question)
#: makes the sentences that chain from it ("After it publishes, ...") Sam's too.
_SAM_GATE = re.compile(
    r"\b(?:once|when|if|after|until|before|as soon as)\b[^,.]{0,40}\byou\b|\b(?:hand|give|send|ask) you\b|"
    r"\bwait(?:s|ing)? (?:on|for) you|\byour (?:go|call|decision|approval)\b",
    re.I,
)
_WAIT_WORD = re.compile(r"\b(?:once|when|if|after|until|as soon as|waiting|waits?|holding)\b", re.I)
_CHAIN_START = re.compile(r"^(?:after|then|next|once|when|and then|afterwards|finally)\b", re.I)

#: A peer, mailbox, notification or restart is named as the wake.
_PEER_NAMES = (
    "bob", "linda", "wren", "halcyon", "kimi", "marlowe", "tamsin", "steve", "steven", "kestrel",
    "elsinore", "conexus", "labrun", "peer", "peers", "mailbox", "notification", "notifications",
    "notified", "notify", "another session", "other session", "other sessions", "sibling", "restart",
    "restarts", "restarted", "restarting", "ready ask", "all-clear", "roll call", "the go", "go comes", "their",
    "reconnect", "reconnects", "reconnected", "resumes", "resumed", "compaction", "compacts", "compacted",
)
_PEER = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _PEER_NAMES) + r")\b|\b[a-z]+_[0-9a-z]{0,4}\d[0-9a-z]{0,4}\b"
    r"|\b(?:once|when|until|after|if)\b[^.]{0,60}\b(?:reports?|replies|reply|answers?|responds?|sends?|posts?|"
    r"confirms?|announces?|hands?|tells?|says?|asks?)\b"
    r"|\bwaiting (?:on|for) (?:a |the )?(?:peer|session|agent|reply|answer|ack|message)\b"
    r"|\b(?:once|when|until|after)\b[^.]{0,40}\bthe script\b"
    r"|\b(?:it|they|he|she) will (?:send|tell|post|reply|ping|notify|message|let me)\b",
    re.I,
)

#: A past-tense report or a retrospective lesson, not a promise.
#: "I'll stay off engine work", "I'll hold": a restraint, not a step to take or to wake for.
_RESTRAINT = re.compile(r"\b(?:i'll|i will|i'm going to) (?:stay|keep|hold|avoid|not|leave|wait|remain|refrain)\b", re.I)
_PAST_REPORT = re.compile(
    r"^(?:(?:i|we)(?:'ve| have)? (?:told|asked|sent|messaged|noted|saved|wrote|written|replied|answered|mailed|"
    r"warned|filed|pushed|posted|logged|stored|recorded|added)\b|(?:i|we) (?:didn't|did not|haven't|have not)\b)"
    r"|\bnext time\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

_RUN_NOUN = r"(?:ci|checks?|runs?|suite|build|pipeline|jobs?|workflow|shards?|jar|publish(?:ing)?|deploy(?:ment)?|gate|battery|tests?|planner|reviewers?|agents?|monitors?)"
_RUN_STATE = (
    r"(?:still (?:running|in progress|in flight|going|building|queued|pending)|(?:is|are) (?:running|queued|in progress|"
    r"building|pending)|hasn't (?:finished|reported|started|printed|come back|finished)|not (?:yet )?(?:finished|done|"
    r"green|in)|in progress|in flight)"
)
_CI_WAIT = re.compile(
    r"\b" + _RUN_NOUN + r"\b[^.]{0,80}\b" + _RUN_STATE + r"|\b" + _RUN_STATE + r"\b[^.]{0,60}\b" + _RUN_NOUN + r"\b"
    r"|\b(?:once|when|after|as soon as|until)\b[^.]{0,60}\b(?:green|passes)\b"
    r"|(?=.*\b" + _RUN_NOUN + r"\b)\b(?:once|when|after|as soon as|until)\b[^.]{0,60}\b(?:finish(?:es|ed)?|completes?|publishes?)\b"
    r"|\bwhile (?:that|it|they) runs?\b|\bwait(?:ing)? for (?:ci|the (?:run|suite|build|checks?|jobs?))\b",
    re.I,
)
#: "no processes still running", "nothing in flight": a run state that is denied is not a wait.
_NEGATED_STATE = re.compile(
    r"\b(?:no|nothing|none|neither|without|never|not)\b[^.]{0,60}\b(?:running|in flight|in progress|queued|pending)\b", re.I
)
_PROMISE_VERBS = (
    r"check|read|watch|re-?check|look|pick|run|push|start|rebase|claim|take|confirm|open|merge|file|close|write|do|verify|"
    r"build|retry|re-?probe|continue|resume|proceed|finish|land|fix|record|load|go|use|put|compare|measure|review|audit|"
    r"commit|cut|bring|report|rerun|re-?run|follow|work|create|draft|schedule|test|poll|diff|cherry-pick|update"
)
_PROMISE = re.compile(
    r"\b(?:i'll|i will|i'm going to|i am going to|i shall)\s+(?:then\s+|also\s+|next\s+)?(?:" + _PROMISE_VERBS + r")\b"
    r"|\bcheck(?:ing)? back\b"
    r"|^(?:next|then)\b[:,]?\s+(?:i|we)\s+\w+"
    r"|^next\b[^.]{0,30}\b(?:is|are|comes)\b"
    r"|\bis next\b"
    r"|^moving on to\b"
    r"|\b(?:once|when|after|as soon as)\b[^.]{0,80}\bi\s+(?:" + _PROMISE_VERBS + r")\b"
    r"|\bwill (?:check|read|watch|re-?check|look|pick|follow up)\b",
    re.I,
)
_TIME = (
    r"\b\d{1,2}:\d\d\s*(?:z|utc|am|pm|pst|pdt|local)?\b|\b20\d\d-\d\d-\d\d\b|"
    r"\bin (?:about |roughly |around |another )?\d+(?:\s*(?:to|-)\s*\d+)?\s*(?:min(?:ute)?s?|h(?:ours?)?|hrs?|seconds?)\b|"
    r"\bat \d{1,2}\s?(?:am|pm)\b"
)
_TIMED = re.compile(
    r"(?=.*(?:" + _TIME + r"))(?=.*\b(?:will|'ll|after|once|until|no earlier than|then)\b)",
    re.I,
)

CLASS_CI_WAIT = "ci-wait"
CLASS_PROMISE = "promise"
CLASS_TIMED = "timed"
_CLASS_ORDER = ((CLASS_CI_WAIT, _CI_WAIT), (CLASS_PROMISE, _PROMISE), (CLASS_TIMED, _TIMED))


class Verdict(NamedTuple):
    block: bool
    cls: str  # "" when allowed
    clause: str  # the matched clause, "" when allowed
    sam_dropped: int
    peer_dropped: int
    past_dropped: int


def _is_sam(clause: str) -> bool:
    if clause.endswith("?") or _SAM_IMPERATIVE.search(clause):
        return True
    if _SAM_YOU.search(clause) and not _SAM_DEFAULT_PROCEED.search(clause):
        return True
    return False


def evaluate_text(text: str) -> Verdict:
    """Classify a closing text. Pure: no environment, no I/O."""
    sam = peer = past = 0
    chain = False
    for clause in clauses(text):
        if _is_sam(clause):
            sam += 1
            chain = clause.endswith("?") or bool(_SAM_GATE.search(clause))
            continue
        if chain and _CHAIN_START.search(clause):
            sam += 1
            continue
        if _RESTRAINT.search(clause):
            past += 1
            continue
        if _PAST_REPORT.search(clause):
            past += 1
            continue
        if _PEER.search(clause):
            peer += 1
            chain = bool(_WAIT_WORD.search(clause))
            continue
        for cls, pattern in _CLASS_ORDER:
            if pattern.search(clause) and not (cls == CLASS_CI_WAIT and _NEGATED_STATE.search(clause)):
                return Verdict(True, cls, clause, sam, peer, past)
    return Verdict(False, "", "", sam, peer, past)


# ---------------------------------------------------------------------------
# Decision over the hook input
# ---------------------------------------------------------------------------


class Decision(NamedTuple):
    block: bool
    reason: str  # allow reason, or the block class
    clause: str
    detail: str  # extra log fields


def decide(payload: object, env: dict[str, str]) -> Decision:
    if env.get(KILL_SWITCH_ENV, "").strip() == "0":
        return Decision(False, "kill-switch", "", "")
    if not isinstance(payload, dict):
        return Decision(False, "bad-input", "", "")
    if payload.get("stop_hook_active") is True:
        return Decision(False, "stop-hook-active", "", "")
    tasks, crons = payload.get("background_tasks"), payload.get("session_crons")
    n_tasks = len(tasks) if isinstance(tasks, list) else 0
    n_crons = len(crons) if isinstance(crons, list) else 0
    if n_tasks or n_crons:
        return Decision(False, "armed", "", f"tasks={n_tasks} crons={n_crons}")
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return Decision(False, "no-text", "", "")
    verdict = evaluate_text(text)
    detail = f"sam_dropped={verdict.sam_dropped} peer_dropped={verdict.peer_dropped} past_dropped={verdict.past_dropped}"
    if verdict.block:
        return Decision(True, verdict.cls, verdict.clause, detail)
    return Decision(False, "no-match", "", detail)


def block_reason(cls: str, clause: str) -> str:
    quoted = " ".join(clause.split())
    if len(quoted) > _REASON_CLAUSE_CAP:
        quoted = quoted[: _REASON_CLAUSE_CAP - 3] + "..."
    what = {
        CLASS_CI_WAIT: "a CI or run wait",
        CLASS_PROMISE: "a promise to check or do something later",
        CLASS_TIMED: "a timed step",
    }.get(cls, "an open step")
    return (
        f'Stop-hook wake guard: your last message closes on {what} ("{quoted}"), and nothing is armed to wake this '
        "session: no background task, Monitor or scheduled wakeup is running. Arm the wake now, then end the turn: "
        "a Monitor or a background Bash until-loop for a run or CI, ScheduleWakeup or CronCreate for a timed step. "
        "If the step can be done right now, do it. If the wake is someone else (Sam, a peer, a notification), say "
        "plainly in one sentence who it is, then end the turn."
    )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _config_dir() -> str:
    # Mirrors _endpoint_resolve.default_config_dir (tests/hooks/test_stop_wake_guard.py compares the two);
    # inlined so this per-turn hook does not import a sibling module.
    configured = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), ".config", "nexus")


def _log(level: str, message: str) -> None:
    """One line to ``<config>/logs/hook.log``. Never raises, writes nothing to stdout or stderr."""
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        logs = os.path.join(_config_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "hook.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}+00:00 {level} stop-wake-guard: {message}\n")
    except Exception:  # noqa: BLE001 -- logging must never fail the hook
        pass


def _read_payload() -> object:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 -- malformed input fails open
        return {}


def main() -> int:
    try:
        payload = _read_payload()
        decision = decide(payload, dict(os.environ))
        sid = ""
        if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
            sid = payload["session_id"][:8]
        fields = f"session={sid}" + (f" {decision.detail}" if decision.detail else "")
        if decision.block:
            clause = " ".join(decision.clause.split())[:_LOG_CLAUSE_CAP]
            _log("INFO", f'block class={decision.reason} {fields} clause="{clause}"')
            sys.stdout.write(json.dumps({"decision": "block", "reason": block_reason(decision.reason, decision.clause)}))
            sys.stdout.flush()
        else:
            _log("INFO", f"allow reason={decision.reason} {fields}")
    except Exception as exc:  # noqa: BLE001 -- the guard never fails a turn end
        _log("WARNING", f"allow reason=error error={type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
