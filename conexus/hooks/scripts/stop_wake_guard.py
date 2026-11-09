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
- the closing text (``last_assistant_message``) is empty or not text;
- every clause that would block is a develop-CI wait (it names CI, checks, runs, a workflow, a
  pipeline, shards, the push or a sha, and no PR, release, tag, main, reviewer, agent or deploy;
  :func:`is_develop_ci_clause`) and the session's nexus MCP server records a CI wake
  (nexus-1d6pz.11.5; :func:`ci_wake_state`): its channel waiter alive, fresh and not stalled, the
  client's initialize handshake seen, the conexus channel flag on the ``claude`` launch, and a
  ``board/ci/*--done`` topic subscribed. Covered clauses are skipped and the rest of the text is
  still read, so a covered CI wait never hides a later promise. With a live waiter and the
  subscription but the flag read as absent, the block names ``nx ci wait <sha>``, the background
  command that wakes a session without the channel. Two limits are accepted: the --done wake is
  best effort (a run set the board adapter could not fold posts nothing, and only ``nx ci wait``
  reads GitHub at its deadline), and a present flag does not prove Claude Code registered the
  channel (a plugin off the allowlist, an organisation policy, an unconfirmed development-channel
  dialog). The record names topics, not shas, so any ``board/ci/*--done`` topic counts.

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
"see you then") is not such a clause.

The gate is on a PRINCIPAL (Sam, you, the user, the operator, the owner, the maintainer, the human; one pattern,
``_PRINCIPAL``). Approval terms come in two kinds: strong ones (approval, sign off, the nod, the word, okays,
greenlit) gate under any gate word; weak ones (cleared, settled, granted, permitted, ok, decision, permission,
told, confirmed) gate only when their subject is a pronoun or a principal, so "once it is cleared" waits on a
person and "once the lock clears" waits on a run. A role word ("the user", "the owner", "the operator") is the person only when a verb, an auxiliary or the end of the
phrase follows it ("the user weighs in"); "the user migration" is code. A handover ("Over to Sam.", "Sam will cut
the tag.", "Sam is away.") passes the turn: will, going to or needs to plus a turn-passing verb (cut, tag, run, push,
approve, decide, review, merge, release), has the ball, it's his move. A habit or an ownership ("Sam runs the nightly
at 2am", "Sam owns the pipeline config") passes nothing. Merely naming Sam ("I'll ping Sam once the build finishes") and an offer ("Sam can review") are
not. A reviewer, critic, peer or named colleague who approves is a peer's wake, like one who reports. What hangs
from a Sam clause survives at most ``_CHAIN_GAP`` clauses that are neither Sam's nor chained. A block follows when any surviving clause is (a) a CI or run wait, (b) a promise to check
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
from collections.abc import Callable
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

#: A clause that addresses the user or asks him something. "you" in any form is enough; the one
#: exception is the default-proceed idiom "unless you'd rather ...", which names him only to offer an
#: override while the session carries on. Naming Sam ("I'll ping Sam once the build finishes") is NOT
#: addressing him: a name alone drops nothing. A handover to him is a separate, structural test
#: (``is_sam_gated``).
_SAM_YOU = re.compile(r"\b(?:you|your|yours|you'd|you'll|you've|you're|yourself)\b(?!')", re.I)

#: The principal: the person whose reply is the wake. One pattern for every rule below. Sam, Hal and "you" are
#: always the person. "the user", "the owner", "the operator" (a role word) count only when what follows is a
#: verb, an auxiliary, a function word or the end of the phrase: "the user weighs in", "the user is back" name the
#: person, "the user migration", "the owner index", "the operator reconcile loop" head a noun phrase about code.
_ROLE = r"(?:the (?:user|human|maintainer|owner|operator)|operator|owner)"
_FUNCTION_WORDS = (
    r"is|are|was|were|be|been|being|has|have|had|will|would|can|could|may|might|should|must|shall|does|did|do|to|and|or|"
    r"but|then|also|just|not|now|still|already|if|when|once|until|after|before|for|with|at|in|on|back|here|away|out|about|"
    r"from|by|as|that|who|which|me|it|us|gave|said|saw|went|told|came|took|got|made|ran|read|wrote|chose|spoke|left|sent|"
    r"met|knew|held|kept|let|put|set"
)
#: Plural nouns that end in -s and would pass for a third-person verb: a backstop, pinned by corpus rows.
_NOUN_BACKSTOP = (
    r"tests?|logs?|scripts?|events?|permissions?|settings?|records?|accounts?|sessions?|agents?|stor(?:y|ies)|stores?|models?|"
    r"ids|names|configs?|preferences?|objects?|interfaces?|inputs?|tables?|schemas?|flows?|counts?|services?|jobs?|builds?|"
    r"checks?|files?|users?|roles?|groups?|teams?|bots?"
)
_ROLE_FOLLOWED_BY_A_VERB = (
    r"(?=$|[^\w\s-]|\s*$|'(?:ll|re|ve|d)\b|\s+(?:" + _FUNCTION_WORDS + r")\b|\s+\w+(?:ed|ly)\b"
    r"|\s+(?!\w*(?:ss|us|is)\b)\w+s\b)(?!\s+(?:" + _NOUN_BACKSTOP + r")\b)"
)
_PRINCIPAL = r"(?:sam|hal|you|" + _ROLE + _ROLE_FOLLOWED_BY_A_VERB + r")"
_PRINCIPAL_NAME = r"(?:sam|hal|the (?:user|human|maintainer|owner|operator)|operator|owner)"
_P = r"\b" + _PRINCIPAL + r"\b(?!'s\b)"
_P_RE = re.compile(_P, re.I)

#: A gate word: the clause waits on whatever follows it ("once", "pending", "awaits", "needs", "hold for"). "unless" is
#: not one: "unless you'd rather ..." is the default-proceed idiom, the session carries on.
_GATE = re.compile(
    r"\b(?:once|when|whenever|after|until|till|upon|pending|if|as soon as|awaits?|awaiting|"
    r"wait(?:s|ing)? (?:on|for)|standing by for|hold(?:s|ing)?(?: off)?(?: on| for| until)|needs?|requires?|"
    r"gated (?:on|by)|subject to|depends? on|contingent on|blocked on)\b"
    r"|\bon(?=\s+(?:confirmation|approval|sign-?off|authori[sz]ation|the (?:word|nod|go)\b))",
    re.I,
)

#: An approval word that names a test, a log or a workflow is a subject, not a predicate. The lookahead only
#: works when the word cannot give characters back, so every open-ended stem below is possessive (``\w*+``).
_NOT_A_SUBJECT = (
    r"(?!\w)(?![- ]?(?:flows?|workflows?|tests?|suites?|logs?|handl\w*+|banners?|tables?|modules?|handlers?|queues?|code|"
    r"paths?|pipelines?|jobs?|steps?|runs?|checks?|errors?|failures?|models?|polic\w++|ui|screens?|dialogs?|buttons?|trees?|"
    r"lists?|files?|configs?)\b)"
)
#: STRONG terms: an approval whatever its subject. Under any gate word they gate on a person ("once approved",
#: "pending the nod", "when someone gives the word"). A word on this list is NOT a gate by itself.
_STRONG_SRC = (
    r"approv\w*+|authori[sz]\w*+|sign(?:s|ed|ing)?[- ]?off|go-?ahead|green[- ]?(?:light\w*+|signal)|greenlit|greenlights?|"
    r"say-?so|says? so|rulings?|ruled|acks?|acked|acknowledg\w*+|consent\w*+|blessing|nods?|nodded|"
    r"okay(?:s|ed)|oks|ok'd|(?:the|an?|your|his|her|their) (?:ok|okay)|(?:ok|okay) from|thumbs?[- ]up|hear(?:s|d)? back|"
    r"(?:gives?|gave|giving|get|got|has|have) the (?:word|nod|go|ok|okay)|"
    r"(?:the|a|his|her|their|your|sam's|operator's|owner's|release|ship|final) go\b"
    r"(?!\s+(?:build\w*+|binary|toolchain|vet|mod|install|run|version|test\w*+|\d))"
)
#: WEAK terms: ordinary state-change words ("cleared", "settled", "granted", "ok", "decision", "permission"). They
#: gate on a person only when the subject is a pronoun or a principal ("once it is cleared", "when Sam decides"), or
#: the term is a noun that arrives ("when a decision comes"). "Once the lock clears" and "the token is granted
#: by the auth server" are run and infrastructure waits.
_WEAK_SRC = (
    r"clear(?:ed|s|ance)|settled|settles|granted|grants?|permitted|permits?|(?:ok|okay)|decisions?|decides?|decided|"
    r"permission\w*+|told(?: to)?|confirm\w*+"
)
_STRONG_TERM = re.compile(r"(?<![\w-])(?:" + _STRONG_SRC + r")" + _NOT_A_SUBJECT, re.I)
_WEAK_TERM = re.compile(r"(?<![\w-])(?:" + _WEAK_SRC + r")" + _NOT_A_SUBJECT, re.I)

#: Who the gate sits on, between a gate word and the term: a run or a store (backstop), or someone who is not
#: the principal ("the reviewer approves", "once Bob approves" are a peer's wake, not Sam's).
_NOT_AN_APPROVER = re.compile(
    r"\b(?:finish\w*|complet\w*|pass\w*|green|publish\w*|runs?|builds?|ci|shards?|suite|jobs?|cache\w*|queues?|buffers?|"
    r"backlog|disk|logs?|checks?|pipelines?|rollouts?|clusters?|limits?|locks?|alerts?|tokens?|status|conflicts?)\b",
    re.I,
)
_NON_SAM_ACTOR = re.compile(
    r"\b(?:reviewers?|critics?|peers?|agents?|bots?|planner|review(?!\s+(?:is|was|has been))|bob|linda|wren|halcyon|kimi|marlowe|tamsin|steve|steven|"
    r"kestrel|elsinore|labrun|mailbox)\b",
    re.I,
)
_SUBJECT_SPLIT = re.compile(r"\b(?:and|or|but|then|while)\b", re.I)
_AUX = r"(?:is|are|was|were|be|been|being|has|have|had|gets?|got|also|finally|officially|explicitly|formally|properly|fully|really|actually|just|now|yet|already|still|only|ever|not|then)"
_WEAK_SUBJECT = re.compile(
    r"(?:(?:it|that|this|they|he|she|we|someone|somebody|anyone|whoever|everything|" + _PRINCIPAL + r"|"
    r"the (?:request|release|plan|proposal|change|pr|tag|merge|push|patch|design|rfc|rdr|go)|"
    r"my (?:request|change|pr|proposal|plan))(?:'s|'re|'ve|'d)?\s*)?(?:" + _AUX + r"\s*)*",
    re.I,
)
_ARRIVES = re.compile(r"\s+(?:comes?|arrives?|lands?|drops?|is (?:in|back|given|made|recorded|available))\b", re.I)
_WEAK_NOUN = ("decision", "permission", "ok", "confirm", "clearance")

#: The user acts: a possessive approval noun ("Sam's call"), or the principal with an approval verb. A modal of
#: permission between them ("Sam can review", "you can approve") is an offer, not a wait.
_SAM_POSSESSIVE = re.compile(
    r"\b" + _PRINCIPAL_NAME + r"'s (?:calls?|words?|nods?|ok|okay|choice|pick|say|go|approval|decisions?|rulings?|"
    r"sign-?off|go-?ahead|blessing|permission)\b",
    re.I,
)
_SAM_VERB = re.compile(
    _P + r"(?:(?!\b(?:can|could|may|might)\b)[^,.;:!?]){0,25}?\b(?:decid\w*+|approv\w*+|"
    r"rule[sd]?|sign\w*+|okay\w*+|ok\w*+|greenlight\w*+|authori[sz]\w*+|merges?|clears?|cleared|permits?|says?|gives?|gave|calls?|"
    r"chooses|picks|nods?)\b",
    re.I,
)
#: Turn-passing actions only: what the user does next, not what he habitually does or owns.
_TURN_VERBS = (
    r"cut|tag|run|push|approve|decide|review|merge|release|look|sign|ship|deploy|pick|choose|confirm|answer|reply|respond"
)
_TURN_PROGRESSIVE = r"cutting|tagging|running|pushing|merging|reviewing|approving|deciding|releasing|signing|shipping|deploying"
#: The turn is handed over: "Over to Sam.", "It's up to Sam.", "Handed to Sam.", "The diff is with Sam.", "The next
#: step is Sam's.", "Sam will cut the tag.", "Sam needs to look at it.", "Sam is reviewing.", "Sam is away.", "Sam's
#: back". A modal of permission ("Sam can review") is not a handover; will, must, should, needs to, going to and the
#: progressive are.
_HANDOVER = re.compile(
    r"\b(?:over|up|parked|belongs?|falls?)\s+(?:to|with|on|for)\s+" + _P
    + r"|\b(?:handed|handing|passed|passing|left|deferred)\s+(?:[\w']+\s+){0,5}?(?:to|with)\s+" + _P
    + r"|\b(?:is|are|was|now|goes|sits|lies)\s+(?:with|on|for|to)\s+" + _P
    + r"|\b(?:is|are|it's|that's|'s)\s+" + _PRINCIPAL_NAME + r"'s\s*[.!]?$"
    r"|" + _P + r"\s+(?:will|'ll|shall|must|should|has to|have to|needs? to|is going to|is about to|are going to|going to|to)\s+"
    r"(?:\w+ly\s+)?(?:" + _TURN_VERBS + r")\b"
    r"|" + _P + r"\s+(?:is|are)\s+(?:" + _TURN_PROGRESSIVE + r")\b"
    r"|" + _P + r"\s+(?:runs|cuts|tags|pushes|merges|ships|releases)\s+(?:the |a )?(?:tag|release|cut|install|command|migration|it|this|that|push|merge|deploy)\b(?!\s+(?:at|every|each|daily|nightly))"
    r"|" + _P + r"\s+has the (?:ball|tag|release|call|decision|move)\b"
    r"|\b(?:it'?s|it is)\s+(?:his|her|their|your|" + _PRINCIPAL_NAME + r"'s)\s+(?:move|turn)\b|\b(?:your|" + _PRINCIPAL_NAME + r"'s)\s+(?:move|turn)\b"
    r"|\b" + _PRINCIPAL_NAME + r"(?:'s|\s+is|\s+are)\s+(?:back|away|out|gone|offline|unavailable|available|free|ready|happy|"
    r"satisfied|around|here|returns?|returned|returning)\b",
    re.I,
)
#: "I'll record Sam's ruling" acts ON an approval already given; it is a step, not a wait. Only when
#: no gate word makes it a wait.
_APPROVAL_OBJECT = re.compile(
    r"\b(?:record|write|file|apply|implement|log|store|capture|document|note|save|enter|add)\b[^,.]{0,30}"
    r"\b(?:rulings?|decisions?)\b",
    re.I,
)
#: How far past a gate word the predicate may sit, and what ends the search.
_GATE_SPAN = 70
_SPAN_END = re.compile(r"[,.;:!?]")
_REMINDER_GATE = re.compile(r"\b(?:once|when|whenever|after|until|pending|if|awaits?)\b", re.I)


def _windows(clause: str):
    for gate in _GATE.finditer(clause):
        yield _SPAN_END.split(clause[gate.end() : gate.end() + _GATE_SPAN], 1)[0]


#: "Blocked on the maintainer's review", "until Sam's answer": the principal's own act, under a gate.
_POSSESSED_ACT = re.compile(
    r"\b" + _PRINCIPAL_NAME + r"'s (?:review|answer|reply|verdict|response|input|feedback|look|confirmation|thumbs?[- ]up|read)\b", re.I
)


def _gate_level(window: str) -> int:
    """0 nothing; 1 a gate on "you" alone; 2 a gate on an approval predicate or on a named principal."""
    if _POSSESSED_ACT.search(window):
        return 2
    level = 0
    hit = _P_RE.search(window)
    if hit:
        level = 1 if hit.group(0).lower() == "you" else 2
    for pattern, strong in ((_STRONG_TERM, True), (_WEAK_TERM, False)):
        for hit in pattern.finditer(window):
            subject = _SUBJECT_SPLIT.split(window[: hit.start()])[-1].strip()
            if _NOT_AN_APPROVER.search(subject) or _NON_SAM_ACTOR.search(subject):
                continue
            if strong or _WEAK_SUBJECT.fullmatch(subject):
                return 2
            if hit.group(0).lower().startswith(_WEAK_NOUN) and _ARRIVES.match(window, hit.end()):
                return 2
    return level


def sam_gate_level(clause: str) -> int:
    """How strongly the clause waits on the user: 0 not at all; 1 a condition on "you" ("Waiting on you", "Ready when
    you are"); 2 an approval or a handover (what hangs from it, even "once it lands", is his to unblock)."""
    if _COURTESY.search(clause):
        return 0
    if _SAM_VERB.search(clause) or _HANDOVER.search(clause):
        return 2
    if _SAM_POSSESSIVE.search(clause) and not (_APPROVAL_OBJECT.search(clause) and not _GATE_WORD.search(clause)):
        return 2
    if _APPROVAL_PENDING.search(clause):
        return 2
    return max((_gate_level(w) for w in _windows(clause)), default=0)


def is_sam_gated(clause: str) -> bool:
    """The clause waits on an approval that only the user (Sam, the operator) gives, however it is phrased.

    Structural, not a word list: a gate word (once, when, after, until, pending, if, awaits, needs, ...)
    governing the principal or an approval predicate on a person; an approval named as pending; a possessive
    approval noun ("Sam's call"); the principal as the actor of an approval verb ("Sam will decide"); or the turn
    handed over ("Over to Sam.", "Sam will cut the tag.", "Sam is away."). An approval word alone
    ("approval-flow tests", "the decision log"), a state change ("once the lock clears") and a bare mention of
    Sam ("I'll ping Sam once the build finishes") gate nothing.
    """
    return sam_gate_level(clause) > 0


#: "The approval is pending", "the ruling has not come": an approval named together with its open state.
_APPROVAL_PENDING = re.compile(
    r"(?<![\w-])(?:" + _STRONG_SRC + r"|decisions?|permission\w*+)" + _NOT_A_SUBJECT
    + r"[^,.;:!?]{0,30}?\b(?:pending|outstanding|awaited|needed|required|missing|open|unresolved|undecided|"
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
    r"\b(?:once|when|if|after|until|before|as soon as)\b[^,.]{0,40}" + _P + r"|\b(?:hand|give|send|ask) " + _P + r"|"
    r"\bwait(?:s|ing)? (?:on|for) " + _P + r"|\byour (?:go|call|decision|approval)\b",
    re.I,
)
#: Clauses that may sit between a gate on Sam and the follow-up that hangs from it.
_CHAIN_GAP = 2
_WAIT_WORD = re.compile(r"\b(?:once|when|if|after|until|as soon as|waiting|waits?|holding)\b", re.I)
#: A follow-up that ends on a pointer back at the gate: "I'll push then.", "I'll proceed accordingly."
_CHAIN_TAIL = re.compile(r"\b(?:then|accordingly|afterwards|after that|from there)\s*[.!]?$", re.I)
_CHAIN_START = re.compile(r"^(?:after|then|next|once|when|and then|afterwards|finally)\b", re.I)
#: A follow-up that hangs from the gate without starting on a chain word: "I'll proceed once that
#: comes through", "I'll push when it is granted", "I'll check the publish after".
_CHAIN_CONT = re.compile(
    r"\b(?:once|when|after|as soon as|until)\s+(?:that|this|it|he|she|then)\b"
    # "they" is the user or a peer ("when they reply"), not the shards ("once they finish").
    r"|\b(?:once|when|after|as soon as|until)\s+they\b(?!\s+(?:finish\w*|complete\w*|pass\w*|run\b|build\w*|go\b))"
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
    r"confirms?|announces?|hands?|tells?|says?|asks?|approves?|signs? off|okays?|greenlights?)\b"
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
_SAM_WAIT = re.compile(r"\bwait(?:s|ing)? (?:on|for) " + _P + r"|\byour (?:go|calls?|decision|approval)\b", re.I)


def _ends_on_sam(clause: str) -> bool:
    """The last clause asks Sam something, tells him to do something, or gates on his approval.

    A bare "you" is not enough: "I'll keep you posted", "See you then" and "Thanks for your patience" are
    courtesies, and the promise before them still has nothing to wake it.
    """
    if _SAM_DEFAULT_PROCEED.search(clause) or _COURTESY.search(clause):
        return False
    return bool(
        clause.endswith("?")
        or _SAM_IMPERATIVE.search(clause)
        or _SAM_GATE.search(clause)
        or _SAM_WAIT.search(clause)
        or is_sam_gated(clause)
    )


def evaluate_text(text: str, ci_covered: "Callable[[str], bool] | None" = None) -> Verdict:
    """Classify a closing text. Pure: no environment, no I/O. *ci_covered*, when given, names the CI-wait
    clauses a wake already covers (nexus-1d6pz.11.5): each is skipped and the rest of the text is still read,
    so a covered CI wait never hides a later promise or timed step."""
    sam = peer = past = 0
    #: 0 none; 1 a gate on "you" or a question (a follow-up that starts on a chain word hangs from it); 2 an
    #: approval gate on Sam (a follow-up that merely depends on "that" or "it" hangs from it too). A clause that
    #: only names Sam or an approval word chains nothing: "Sam can review. After CI, I'll check." still waits on CI.
    chain = 0
    #: A chain survives at most ``_CHAIN_GAP`` clauses that are neither Sam's nor chained: a status line or two
    #: between the gate and its follow-up (fixture sel3-25), not a paragraph.
    gap = 0
    parts = clauses(text)
    final_sam = bool(parts) and _ends_on_sam(parts[-1])
    for clause in parts:
        if _is_sam(clause):
            sam += 1
            gap = 0
            level = sam_gate_level(clause)
            if level:
                chain = level
            elif clause.endswith("?") or _SAM_GATE.search(clause):
                chain = 1
            else:
                chain = 0
            continue
        if chain and (_CHAIN_START.search(clause) or _CHAIN_TAIL.search(clause) or (chain == 2 and _CHAIN_CONT.search(clause))):
            sam += 1
            gap = 0
            continue
        gap += 1
        if gap > _CHAIN_GAP:
            chain = 0
        if _RESTRAINT.search(clause):
            past += 1
            continue
        if _PAST_REPORT.search(clause):
            past += 1
            continue
        if _PEER.search(clause):
            peer += 1
            chain = 1 if _WAIT_WORD.search(clause) else 0
            gap = 0
            continue
        for cls, pattern in _CLASS_ORDER:
            if not pattern.search(clause):
                continue
            if cls == CLASS_CI_WAIT and _NEGATED_STATE.search(clause):
                # A run state that is denied ("no suite is still running") is a clause of its own: what hung from
                # the last Sam clause does not reach past it. A clause that matches no class stays transparent
                # up to ``_CHAIN_GAP`` clauses.
                chain = 0
                continue
            if cls == CLASS_CI_WAIT and ci_covered is not None and ci_covered(clause):
                break  # covered by an armed wake: on to the next clause
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


# ---------------------------------------------------------------------------
# A CI wake the MCP server armed (nexus-1d6pz.11.5)
# ---------------------------------------------------------------------------
#
# A session subscribed to a CI board's --done topic, with the channel loaded, is woken when its
# runs complete: a CI wait at the end of a turn is covered, though nothing shows in
# background_tasks. The guard learns that from the record the session's nexus MCP server writes
# for `nx doctor` (`<config>/tuple-watch/channel-status.d/<session id>`, nexus.mcp.channel
# write_channel_status, rewritten at every park of at most 25 s), never from the model's words:
# the waiter alive and fresh, the claude launch flag present (channel_flag, read by
# nexus.channel_launch), and a board/ci/*--done topic subscribed. A subscription without the flag
# is not a wake (its posts wait for the next prompt), and the block then names `nx ci wait <sha>`,
# the background command that is. A running `nx ci wait` is a background task, armed already.

CI_WAKE_ARMED = "armed"
CI_WAKE_NO_CHANNEL = "subscribed-no-channel"
CI_WAKE_NONE = "none"
CLASS_CI_WAIT_NO_CHANNEL = "ci-wait-no-channel"
#: A status record older than this is a waiter that stopped, or a server that is gone.
CI_WAKE_FRESH_S = 120.0
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CI_DONE_TOPIC = re.compile(r"board/ci/[^/]+--done")


#: A CI-wait clause the develop --done wake can cover: it names CI, checks, runs, a workflow, a pipeline,
#: shards, the push or a sha, and nothing a develop push's wake does not carry (a PR, a release, a tag, main,
#: another repository). "The reviewers are still running" or "the index build is in progress" is a CI-wait
#: clause to the text classifier and no CI wake at all, so it is never covered.
_DEVELOP_CI_NOUN = re.compile(
    r"\b(?:ci|checks?|runs|(?:the|ci|workflow|develop) run|workflows?|pipelines?|shards?|push(?:ed)?|sha|develop)\b", re.I
)
#: A second step riding in the same clause ("CI is still running, then I'll check the dashboard"): the wake
#: covers the CI wait, not the step after it.
_SECOND_STEP = re.compile(r"\bthen\b[^.]{0,40}\b(?:i'll|i will|i'm going to|we'll)\b", re.I)
_NOT_DEVELOP_CI = re.compile(
    r"\b(?:pr|prs|pull requests?|release|tag|tags|main|reviewers?|agents?|monitors?|planner|deploy(?:ment)?|"
    r"other repo(?:sitory)?|staging|production)\b",
    re.I,
)


def is_develop_ci_clause(clause: str) -> bool:
    """Whether *clause* waits on the kind of CI a develop push's --done wake reports."""
    return (
        bool(_DEVELOP_CI_NOUN.search(clause))
        and not _NOT_DEVELOP_CI.search(clause)
        and not _TIMED.search(clause)
        and not _SECOND_STEP.search(clause)
    )


def _server_config_dir(env: dict[str, str]) -> str:
    """The directory the MCP server writes under: nexus.config.nexus_config_dir's rule exactly
    (NEXUS_CONFIG_DIR, stripped, else ~/.config/nexus), not this hook's log-dir rule."""
    override = env.get("NEXUS_CONFIG_DIR", "").strip()
    return override or os.path.join(os.path.expanduser("~"), ".config", "nexus")


def ci_wake_state(session_id: object, env: dict[str, str], *, now: float | None = None) -> str:
    """CI_WAKE_ARMED, CI_WAKE_NO_CHANNEL (a live waiter with a --done subscription in a session read as
    launched without the channel flag) or CI_WAKE_NONE. Any doubt (no record, unreadable, stale or from the
    future, waiter not alive, the client's initialize handshake not seen or a send stalled, the flag
    unreadable) is CI_WAKE_NONE, which blocks as before."""
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        return CI_WAKE_NONE
    path = os.path.join(_server_config_dir(env), "tuple-watch", "channel-status.d", session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            status = json.load(handle)
        age = (time.time() if now is None else now) - os.path.getmtime(path)
    except (OSError, ValueError):
        return CI_WAKE_NONE
    if not isinstance(status, dict) or not 0 <= age <= CI_WAKE_FRESH_S:
        return CI_WAKE_NONE
    topics = status.get("board_topics")
    if not isinstance(topics, list) or not any(isinstance(t, str) and _CI_DONE_TOPIC.fullmatch(t) for t in topics):
        return CI_WAKE_NONE
    if status.get("alive") is not True or status.get("gate_stalled") is True:
        return CI_WAKE_NONE
    if status.get("channel_flag") is False:
        return CI_WAKE_NO_CHANNEL
    if status.get("channel_flag") is not True or status.get("gate_open") is not True:
        return CI_WAKE_NONE
    return CI_WAKE_ARMED


def decide(payload: object, env: dict[str, str], *, now: float | None = None) -> Decision:
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
    if verdict.block and verdict.cls == CLASS_CI_WAIT:
        wake = ci_wake_state(payload.get("session_id"), env, now=now)
        if wake == CI_WAKE_ARMED:
            covered = evaluate_text(text, ci_covered=is_develop_ci_clause)
            if not covered.block:
                return Decision(False, "ci-wake-armed", "", detail)
            verdict = covered
        elif wake == CI_WAKE_NO_CHANNEL and is_develop_ci_clause(verdict.clause):
            return Decision(True, CLASS_CI_WAIT_NO_CHANNEL, verdict.clause, detail)
    if verdict.block:
        return Decision(True, verdict.cls, verdict.clause, detail)
    if verdict.final_sam:
        return Decision(False, "final-to-sam", "", f"{detail} would_block_class={verdict.cls}")
    return Decision(False, "no-match", "", detail)


def block_reason(cls: str, clause: str) -> str:
    quoted = " ".join(clause.split())
    if len(quoted) > _REASON_CLAUSE_CAP:
        quoted = quoted[: _REASON_CLAUSE_CAP - 3] + "..."
    if cls == CLASS_CI_WAIT_NO_CHANNEL:
        return (
            f'Stop-hook wake guard: your last message closes on a CI wait ("{quoted}"). This session is subscribed to '
            "a CI board's --done topic, but it was not launched with the conexus channel, so that post will not wake "
            "it. Run `nx ci wait <sha>` as a background command now (its exit is the wake and its line is the result), "
            "then end the turn."
        )
    what = {
        CLASS_CI_WAIT: "a CI or run wait",
        CLASS_PROMISE: "a promise to check or do something later",
        CLASS_TIMED: "a timed step",
    }.get(cls, "an open step")
    # "Do it now" is wrong when the step may be gated on the user (an approval gate or a handover) and for any clause
    # that carries a gate word outside the CI-wait class: those wait on something, and the model should say who.
    gated = is_sam_gated(quoted) or (cls != CLASS_CI_WAIT and bool(_REMINDER_GATE.search(quoted)))
    do_now = "" if gated else "If the step can be done right now, do it. "
    return (
        f'Stop-hook wake guard: your last message closes on {what} ("{quoted}"), and nothing is armed to wake this '
        "session: no background task, Monitor or scheduled wakeup is running. Arm the wake now, then end the turn: "
        + ("`nx ci wait <sha>` as a background command for a develop push's CI, a Monitor or a background Bash "
           "until-loop for another run, " if cls == CLASS_CI_WAIT else
           "a Monitor or a background Bash until-loop for a run or CI, ")
        + "ScheduleWakeup or CronCreate for a timed step. "
        f"{do_now}If the wake is someone else (the user, a peer, a notification), say "
        "plainly in one sentence who it is, then end the turn."
    )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _config_dir(env: dict[str, str] | None = None) -> str:
    # Mirrors _endpoint_resolve.default_config_dir (tests/hooks/test_stop_wake_guard.py compares the two);
    # inlined so this per-turn hook does not import a sibling module.
    env = os.environ if env is None else env
    configured = env.get("NEXUS_CONFIG_DIR") or env.get("NX_CONFIG_DIR")
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
