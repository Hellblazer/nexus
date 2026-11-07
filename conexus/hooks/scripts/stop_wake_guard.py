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
- either of those two fields is ABSENT or not a list. Unknown is not "nothing armed": a Claude Code
  build that does not send them would otherwise have this guard fire while a subagent or Monitor is
  running. Both fields were present on the build below, so this branch is a guard for older builds;
- the closing text (``last_assistant_message``) is empty or not text.

Otherwise only the TAIL of the text is read (the last ``_TAIL_CHARS`` characters, from a sentence
boundary): the end of a turn is where a promise lives, and a status line near the top must not
block a message that ends on a question to Sam. It is split into sentences and clauses (each
capped at ``_CLAUSE_CAP`` characters, which also bounds the regex cost). A clause is DROPPED when
its actor is Sam (it addresses "you", asks a question, says "say go and I'll", is a ``!`` command,
names Sam, or is gated on an approval: a gate word (once, when, after, until, pending, if, awaits, needs,
hold for) governing an approval predicate, however it is phrased: "once it is cleared", "when someone gives
the word", "pending the ack"; see ``is_sam_gated``. An approval word alone, as the subject of a test or a
log ("approval-flow tests", "the decision log"), gates nothing) or when it names a peer, mailbox, notification or restart as the wake, or when it is
a past-tense report. If the LAST clause asks Sam something, tells him to do something or gates on his
approval, the whole message is allowed: Sam's reply is the wake. A courtesy to him ("I'll keep you posted",
"see you then") is not such a clause. A block follows when any surviving clause is (a) a CI or run wait, (b) a promise to check
or do something later, or (c) a timed step (clock time, date, "in N minutes"). The block is
``{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": ...}}``, which continues the
turn like ``decision: block`` does but without the "Stop hook error occurred" notice Claude Code
shows for a block. Measured live on Claude Code 2.1.319, 2026-11-06; this behaviour has flipped
between Claude Code versions before, so re-check it at each release. If a version stops honouring
``additionalContext`` on a Stop the guard becomes a silent no-op (the safe direction). The text
quotes the clause and tells the model to arm a wake now or state plainly who the wake is, then end
the turn.

A clause that gates on Sam ("once you give it, I'll push the tag", "pending the ack") also drops the
follow-up clauses that hang from it ("After X, ...", "Then ...", "I'll proceed once that comes
through").

Scripted runs (``claude -p`` children that load the plugin) set ``NX_STOP_WAKE_GUARD=0``; nexus's own
children pass ``--setting-sources ""`` and load no plugin, so they never reach this hook.

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
#: Only the end of the message is read. 600 characters is two or three sentences: the closing
#: promise, plus the line that tells whether Sam is being asked something.
_TAIL_CHARS = 600
#: A clause longer than this is cut to its last characters before matching. The lookaheads in
#: ``_TIMED`` and ``_CI_WAIT`` are quadratic in clause length; at 400 characters that is milliseconds.
_CLAUSE_CAP = 400
#: Text prepared for the tail cut (fences and inline code are removed on this window).
_PREP_WINDOW = 4000
_BOUNDARY = re.compile(r"(?<=[.!?:])\s+|\n")

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


def _tail(text: str) -> str:
    """The last ``_TAIL_CHARS`` characters, from the first sentence boundary inside them."""
    if len(text) <= _TAIL_CHARS:
        return text
    cut = text[-_TAIL_CHARS:]
    match = _BOUNDARY.search(cut)
    return cut[match.end():] if match and match.end() < len(cut) else cut


def clauses(text: str) -> list[str]:
    """Sentences, then clauses, of the message tail, in order. Bullets and newlines end a sentence."""
    out: list[str] = []
    for line in _tail(_prepare(text[-_PREP_WINDOW:])).splitlines():
        line = _LEAD_MARKUP.sub("", line).strip()
        if not line:
            continue
        for sentence in _SENT_SPLIT.split(line):
            for clause in _CLAUSE_SPLIT.split(sentence):
                clause = clause.strip()
                if clause:
                    out.append(clause[-_CLAUSE_CAP:])
    return out


# ---------------------------------------------------------------------------
# Drops: Sam is the actor, a peer or notification is the wake, past-tense report
# ---------------------------------------------------------------------------

#: A clause that addresses Sam or asks him something. "you" in any form is enough; the one
#: exception is the default-proceed idiom "unless you'd rather ...", which names Sam only to
#: offer an override while the session carries on.
#: "Sam's ruling" is an object, not an actor; a bare "Sam" is still Sam.
_SAM_YOU = re.compile(r"\b(?:you|your|yours|you'd|you'll|you've|you're|yourself|sam)\b(?!')", re.I)
#: Sam named in any form ("Sam's" included). A clause that only names him is not dropped (it may be
#: about his work), but what hangs from it is his to unblock.
_SAM_NAME = re.compile(r"\b(?:sam|hal)(?:'s)?\b", re.I)
#: A gate word: the clause waits on whatever follows it ("once", "pending", "awaits", "needs", "hold for").
_GATE = re.compile(
    r"\b(?:once|when|whenever|after|until|till|upon|pending|if|unless|as soon as|awaits?|awaiting|"
    r"waiting (?:on|for)|standing by for|hold(?:s|ing)?(?: off)?(?: on| for| until)|needs?|requires?|"
    r"gated (?:on|by)|subject to|depends? on|contingent on|blocked on)\b",
    re.I,
)
#: What an approval predicate looks like, however it is phrased: a noun (approval, sign off, the nod, a
#: ruling), a verb (okays, greenlights, clears, grants, says so) or a participle (cleared, signed off,
#: settled, told to). A word on this list is NOT a gate by itself: it is one only under a gate word.
_APPROVAL_TERM = re.compile(
    r"(?<![\w-])(?:approv\w*|authori[sz]\w*|sign(?:s|ed|ing)?[- ]?off|go-?ahead|green[- ]?(?:light\w*|signal)|greenlit|"
    r"greenlights?|say-?so|says? so|rulings?|ruled|decisions?|decides?|decided|permission\w*|permitted|permits?|"
    r"acks?|acked|acknowledg\w*|consent\w*|blessing|nods?|nodded|(?:ok|okay)(?:s|ed|'d)?|clear(?:ed|s|ance)|"
    r"granted|grants?|settled|settles|told to|(?:gives?|gave|giving|get|got|has|have) the (?:word|nod|go|ok|okay)|"
    r"(?:the|a|his|her|their|your|sam's|operator's|owner's|release|ship|final) go\b"
    r"(?!\s+(?:build\w*|binary|toolchain|vet|mod|install|run|version|test\w*|\d)))"
    # An approval word that names a test, a log or a workflow is a subject, not a predicate.
    r"(?![- ]?(?:flows?|workflows?|tests?|suites?|logs?|handl\w*|banners?|tables?|modules?|handlers?|queues?|code|paths?|pipelines?)\b)",
    re.I,
)
#: Words that, between a gate and an approval term, show the gate is about a run or a store, not a person:
#: "after the backlog is cleared"; also a non-Sam actor ("after the reviewer approves").
_NOT_AN_APPROVER = re.compile(
    r"\b(?:finish\w*|complet\w*|pass\w*|green|publish\w*|runs?|builds?|ci|shards?|suite|jobs?|cache\w*|queues?|buffers?|"
    r"backlog|disk|logs?|reviewers?|critics?|bot|planner|agents?)\b",
    re.I,
)
#: Sam (the operator) acts: a possessive approval noun ("Sam's call"), or Sam with an approval verb.
_SAM_POSSESSIVE = re.compile(
    r"\b(?:sam|hal|operator|owner)'s (?:calls?|words?|nods?|ok|okay|choice|pick|say|go|approval|decisions?|rulings?|"
    r"sign-?off|go-?ahead|blessing|permission)\b",
    re.I,
)
_SAM_VERB = re.compile(
    r"\b(?:sam|hal|operator|the (?:operator|owner|user|human|maintainer))\b(?!')[^,.;:!?]{0,25}?\b(?:decid\w*|approv\w*|"
    r"rule[sd]?|sign\w*|okay\w*|ok\w*|greenlight\w*|authori[sz]\w*|merges?|clears?|cleared|permits?|says?|gives?|gave|calls?|"
    r"chooses|picks|nods?)\b",
    re.I,
)
#: "I'll record Sam's ruling" acts ON an approval already given; it is a step, not a wait. Only when
#: no gate word makes it a wait.
_APPROVAL_OBJECT = re.compile(
    r"\b(?:record|write|file|apply|implement|log|store|capture|document|note|save|enter|add)\b[^,.]{0,30}"
    r"\b(?:rulings?|decisions?)\b",
    re.I,
)
#: How far past a gate word the approval predicate may sit, and what ends the search.
_GATE_SPAN = 70
_SPAN_END = re.compile(r"[,.;:!?]")
_REMINDER_GATE = re.compile(r"\b(?:once|when|whenever|after|until|pending|if|awaits?)\b", re.I)


def _under_a_gate(clause: str, target: re.Pattern[str], *, screen: bool) -> bool:
    """True when a gate word governs ``target`` in this clause: the target sits within ``_GATE_SPAN``
    characters after the gate word, in the same phrase (no comma, stop or colon between them)."""
    for gate in _GATE.finditer(clause):
        window = _SPAN_END.split(clause[gate.end() : gate.end() + _GATE_SPAN], 1)[0]
        hit = target.search(window)
        if hit and not (screen and _NOT_AN_APPROVER.search(window[: hit.start()])):
            return True
    return False


_SAM_AFTER_GATE = re.compile(r"\b(?:sam|hal|operator)\b(?!')", re.I)


def is_sam_gated(clause: str) -> bool:
    """The clause waits on an approval that only Sam (the operator) gives, however it is phrased.

    Structural, not a word list: a gate word (once, when, after, until, pending, if, awaits, needs, ...)
    governing an approval predicate or Sam himself; an approval named as pending; Sam's possessive
    approval noun ("Sam's call"); or Sam as the actor of an approval verb ("Sam will decide"). An
    approval word alone ("approval-flow tests", "the decision log") is a test subject and gates nothing.
    """
    if _SAM_VERB.search(clause):
        return True
    if _SAM_POSSESSIVE.search(clause) and not (_APPROVAL_OBJECT.search(clause) and not _GATE_WORD.search(clause)):
        return True
    if _under_a_gate(clause, _APPROVAL_TERM, screen=True) or _under_a_gate(clause, _SAM_AFTER_GATE, screen=True):
        return True
    return bool(_APPROVAL_PENDING.search(clause))


#: "The approval is pending", "the ruling has not come": an approval named together with its open state.
_APPROVAL_PENDING = re.compile(
    _APPROVAL_TERM.pattern + r"[^,.;:!?]{0,30}?\b(?:pending|outstanding|awaited|needed|required|missing|open|unresolved|undecided|"
    r"not (?:yet )?(?:given|in|here|granted|arrived)|(?:isn't|hasn't|has not|is not) (?:yet )?(?:come|arrived|landed|given|granted|in))\b",
    re.I,
)
_GATE_WORD = re.compile(r"\b(?:once|when|after|until|before|pending|waiting|awaiting|needs?|gated|if|unless)\b", re.I)


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
#: A follow-up that hangs from the gate without starting on a chain word: "I'll proceed once that
#: comes through", "I'll push when it is granted", "I'll check the publish after".
_CHAIN_CONT = re.compile(
    r"\b(?:once|when|after|as soon as|until)\s+(?:that|this|it|they|he|she|then)\b"
    r"|\b(?:after|afterwards|then|later|pending)\s*[.!]?$|\bthen\b",
    re.I,
)

#: A peer, mailbox, notification or restart is named as the wake.
_PEER_NAMES = (
    "bob", "linda", "wren", "halcyon", "kimi", "marlowe", "tamsin", "steve", "steven", "kestrel",
    "elsinore", "conexus", "labrun", "peer", "peers", "mailbox", "notification", "notifications",
    "notified", "notify", "another session", "other session", "other sessions", "sibling", "restart",
    "restarts", "restarted", "restarting", "ready ask", "all-clear", "roll call", "go comes", "their",
    "reconnect", "reconnects", "reconnected", "resumes", "resumed", "compaction", "compacts", "compacted",
)
_PEER = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _PEER_NAMES) + r")\b"
    # "the go" is a peer's go-ahead, but "the go build" is the Go toolchain.
    r"|\bthe go\b(?!\s+(?:build\w*|binary|toolchain|vet|mod|install|run|version|test\w*|\d))|\b[a-z]+_[0-9a-z]{0,4}\d[0-9a-z]{0,4}\b"
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
    final_sam: bool = False  # the last clause addresses Sam, so a would-be block was allowed


#: A courtesy to Sam, not a request to him: "keep you posted", "update you", "report back to Sam", "see you
#: then", "thanks for your patience", "you will hear from me". It names him without handing him the turn.
_COURTESY = re.compile(
    r"\b(?:keep|keeping) (?:you|sam) (?:posted|informed|in the loop)\b|\bupdat(?:e|es|ing) (?:you|sam)\b|"
    r"\b(?:report(?:ing)?|get|circle|come|check|write) back to (?:you|sam)\b|\bsee you\b|\bthank(?:s| you)\b|"
    r"\byour patience\b|\byou(?:'ll| will) (?:hear|get|see) (?:from|back from) me\b|\bhear from me\b",
    re.I,
)


def _is_sam(clause: str) -> bool:
    if clause.endswith("?") or _SAM_IMPERATIVE.search(clause) or is_sam_gated(clause):
        return True
    if _SAM_YOU.search(clause) and not _SAM_DEFAULT_PROCEED.search(clause) and not _COURTESY.search(clause):
        return True
    return False


#: A closing clause that waits on Sam: his reply is the wake. "wait on you", "your call", "needs Sam".
_SAM_WAIT = re.compile(r"\bwait(?:s|ing)? (?:on|for) (?:you|sam|hal)\b|\byour (?:go|calls?|decision|approval)\b", re.I)


def _ends_on_sam(clause: str) -> bool:
    """The last clause asks Sam something, tells him to do something, or gates on his approval.

    A bare "you" is not enough: "I'll keep you posted", "See you then" and "Thanks for your patience" are
    courtesies, and the promise before them still has nothing to wake it.
    """
    if _SAM_DEFAULT_PROCEED.search(clause):
        return False
    return bool(
        clause.endswith("?")
        or _SAM_IMPERATIVE.search(clause)
        or _SAM_GATE.search(clause)
        or _SAM_WAIT.search(clause)
        or is_sam_gated(clause)
    )


def evaluate_text(text: str) -> Verdict:
    """Classify a closing text. Pure: no environment, no I/O."""
    sam = peer = past = 0
    #: 0 none; 1 a gate on "you" or a question (a follow-up that starts on a chain word hangs from it); 2 an
    #: approval gate on Sam (a follow-up that merely depends on "that" or "it" hangs from it too). A clause that
    #: only names Sam or an approval word chains nothing: "Sam can review. After CI, I'll check." still waits on CI.
    chain = 0
    parts = clauses(text)
    final_sam = bool(parts) and _ends_on_sam(parts[-1])
    for clause in parts:
        if _is_sam(clause):
            sam += 1
            if is_sam_gated(clause):
                chain = 2
            elif clause.endswith("?") or _SAM_GATE.search(clause):
                chain = 1
            else:
                chain = 0
            continue
        if chain and (_CHAIN_START.search(clause) or (chain == 2 and _CHAIN_CONT.search(clause))):
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
            chain = 1 if _WAIT_WORD.search(clause) else 0
            continue
        for cls, pattern in _CLASS_ORDER:
            if not pattern.search(clause):
                continue
            if cls == CLASS_CI_WAIT and _NEGATED_STATE.search(clause):
                # A run state that is denied ("nothing is still running") is a clause of its own: what hung from
                # the last Sam clause does not reach past it. A clause that matches no class stays transparent
                # (fixture sel3-25: a status sentence between "hand you the tag command" and "After it publishes").
                chain = 0
                continue
            if final_sam:
                # The message ends on Sam (a question, a hand-off, an approval to wait for): his reply is the wake.
                return Verdict(False, cls, clause, sam, peer, past, True)
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
    if not isinstance(tasks, list) or not isinstance(crons, list):
        # Unknown is not "nothing armed": a build that omits these fields would fire during a running subagent.
        return Decision(False, "armed-state-unknown", "", "")
    n_tasks, n_crons = len(tasks), len(crons)
    if n_tasks or n_crons:
        return Decision(False, "armed", "", f"tasks={n_tasks} crons={n_crons}")
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return Decision(False, "no-text", "", "")
    verdict = evaluate_text(text)
    detail = f"sam_dropped={verdict.sam_dropped} peer_dropped={verdict.peer_dropped} past_dropped={verdict.past_dropped}"
    if verdict.block:
        return Decision(True, verdict.cls, verdict.clause, detail)
    if verdict.final_sam:
        return Decision(False, "final-to-sam", "", f"{detail} would_block_class={verdict.cls}")
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
    # "Do it now" is wrong when the step may be gated on Sam (an approval gate, or his name) and for any clause
    # that carries a gate word outside the CI-wait class: those wait on something, and the model should say who.
    gated = is_sam_gated(quoted) or bool(_SAM_NAME.search(quoted)) or (cls != CLASS_CI_WAIT and bool(_REMINDER_GATE.search(quoted)))
    do_now = "" if gated else "If the step can be done right now, do it. "
    return (
        f'Stop-hook wake guard: your last message closes on {what} ("{quoted}"), and nothing is armed to wake this '
        "session: no background task, Monitor or scheduled wakeup is running. Arm the wake now, then end the turn: "
        "a Monitor or a background Bash until-loop for a run or CI, ScheduleWakeup or CronCreate for a timed step. "
        f"{do_now}If the wake is someone else (Sam, a peer, a notification), say "
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
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": block_reason(decision.reason, decision.clause)}}))
            sys.stdout.flush()
        else:
            _log("INFO", f"allow reason={decision.reason} {fields}")
    except Exception as exc:  # noqa: BLE001 -- the guard never fails a turn end
        _log("WARNING", f"allow reason=error error={type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
