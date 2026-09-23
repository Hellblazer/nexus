# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PreToolUse close gate (RDR-215 bead nexus-q02nx.17).

Port of ``conexus/hooks/scripts/pre_close_verification_hook.sh`` (796
lines, ranked risk 3). It sits on every ``Bash`` tool call, fast-no-ops on
anything that is not a bd create/close/done, and for a close requires a
``review-completed`` marker in T1 scratch naming BOTH standing reviewers.

**MOVE, DO NOT REWRITE, and here that is literal.** Roughly four fifths of
the original was already Python, invoked through ``python3 -c`` from bash
plumbing. :func:`_bd_verbs` and :func:`_bead_ids` are those bodies carried
across mechanically -- extracted from the script BY a script, so a
transcription slip was not available as a failure mode. Both are patched
state machines rather than designs: nexus-2e874 added the shlex-failure
fallback, nexus-cr4lp the env-assignment and inline-override detection,
nexus-fv65m the tokenize-then-split ordering. Each exists because a
simpler version silently mis-tokenized a real command, and every one of
those defects failed OPEN.

**THE DENY TEXT IS A CONTRACT, AND THE REASON IS THE OPPOSITE OF
UBIQUITY.** The gate CONCEPT is referenced in roughly ten documents --
the phase-review-gate skill, ``.beads/PRIME.md``, several RDR
post-mortems -- but the remedy block itself is DUPLICATED NOWHERE.
Measured: before this port exactly one file on disk contained the string
``Close blocked: no review-completed``, namely the script.

That absence is what makes a paraphrase dangerous. If the text existed in
twenty places a reworded copy would disagree with nineteen of them and
someone would notice; because it exists once, a paraphrase is
undetectable and the documents referring to the gate quietly start
describing something the code no longer says. So it is carried byte for
byte and the test module asserts it against the SCRIPT's bytes rather
than against a copy living in the test -- there being no third copy to
appeal to.

(An earlier draft of this docstring said "the remedy block is quoted in
19 files", which inverted the argument for its own existence. The bead
says the CONCEPT is in 19 files and the strings are nowhere duplicated;
two sentences collapsed into one wrong one.)

**Fail-open stays fail-open.** A missing marker denies; an unreachable T1,
an absent ``nx``, a blown time budget all ALLOW with verification stamped
``unverified`` rather than ``passed``. A broken verification path must not
brick every close, and making this gate fail-closed is a decision the RDR
deliberately does not take.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time

from nexus._hook_runtime._io import HookResult

__all__ = ["run"]


def _bd_verbs(cmd: str) -> dict:
    """Which bd verb this command carries, and whether it inline-overrides.

    Carried verbatim. Segment-scoped: it matches only when ``bd`` is the
    command word of a segment, after any environment assignments.

    KNOWN LIMIT, measured at this port (nexus-q02nx.17) and NOT fixed
    here. The trigger is narrower than it first looks, and the narrow
    version is the useful one:

    * a quoted mention does NOT trip it. ``git commit -m "docs: bd close
      notes"`` is correctly ignored -- nexus-fv65m fixed that and
      ``TestMatcherTightening`` pins it.
    * an operator token inside HEREDOC BODY TEXT does. ``shlex`` has no
      concept of a heredoc, so an ``&&`` (or ``;``, ``|``, ``then``,
      ``do``) in the body becomes a genuine operator token, splits a
      segment, and the next segment starts with ``bd``.

    Measured both ways rather than assumed: quoted-in-``-m`` and
    quoted-in-``--reason`` are not detected; heredoc-body-with-``&&`` and
    a ``python3 -c`` string containing ``&& bd close`` both are. The
    command that first wrote this module was refused for exactly that
    reason.

    Fixing it is NOT applying nexus-fv65m's existing correction to a site
    that missed it -- that was the first read here and the probe
    disproved it. The harvester and the detector both already tokenize
    quote-aware; neither models heredocs, and teaching them to is a
    parser change rather than a reordering. A wrong attempt stops the
    gate detecting real closes, which fails OPEN, so this is recorded for
    a decision rather than patched under time pressure.
    """
    segments = re.split(r'(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)', cmd)
    has_create = False
    has_close_or_done = False
    inline_override = False
    env_assign_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
    for seg in segments:
        try:
            variants = [shlex.split(seg, posix=True)]
        except ValueError:
            # nexus-2e874: never silently skip a segment shlex rejects for
            # unbalanced quoting -- that direction made the verb never match,
            # so the whole hook fast-no-op'd and a stray quote in a --reason
            # value fully bypassed the close gate. Degrade to TWO rough
            # variants (quote-as-space keeps boundary-glued quotes splitting;
            # quote-removed keeps a quote INSIDE the verb from fracturing it,
            # e.g. b"d close) -- a match in either counts. Same posture as
            # the BEAD_IDS_JSON raw-scan fallback below.
            variants = [
                seg.replace('"', ' ').replace("'", ' ').split(),
                seg.replace('"', '').replace("'", '').split(),
            ]
        for tokens in variants:
            i = 0
            while i < len(tokens) and env_assign_re.match(tokens[i]):
                if tokens[i] == 'NX_REVIEW_GATE_OVERRIDE=1':
                    inline_override = True
                i += 1
            rest = tokens[i:]
            # Skip shell grouping that can precede the command word. A
            # brace group or a subshell puts `{` or `(` where `bd` would
            # otherwise be, so `{ bd close nexus-x; }` and
            # `(bd close nexus-x)` were both invisible to this detector
            # and closed without a marker. Measured both ways at bead
            # nexus-17i1n; found by a reviewer looking at a leading-brace
            # heuristic elsewhere in this file, not by a failing gate.
            #
            # Widening a DETECTOR is the safe direction here, unlike the
            # heredoc limit this function's docstring records: skipping a
            # grouping token can only make the gate see more closes, never
            # fewer, so the worst case is a deny that should have been an
            # allow — loud, and recoverable by the documented override.
            while rest and rest[0] in ('{', '('):
                rest = rest[1:]
            if len(rest) >= 2 and rest[0] == 'bd':
                if rest[1] == 'create':
                    has_create = True
                elif rest[1] in ('close', 'done'):
                    has_close_or_done = True
    return {
        "has_create": has_create,
        "has_close_or_done": has_close_or_done,
        "inline_override": inline_override,
    }


#: The flags whose VALUE is prose or a path, never a close target.
#: Hoisted to module scope at nexus-q02nx.24 so the regex below can be
#: DERIVED from it. These were two hand-kept literals inside ``_bead_ids``
#: and the port expanded only one: ``--reason-file`` and ``-r`` reached
#: the shlex path and not the malformed-quoting fallback, so an unbalanced
#: quote anywhere in a close command let a bead-id-shaped token inside a
#: reason-file path or a short-flag reason be harvested as a close target.
#: Two constants holding one fact, edited one at a time.
_VALUE_FLAGS = frozenset(
    {'--reason', '--reason-file', '-r', '--description', '--notes', '-m'}
)

#: Longest-first so ``--reason`` cannot claim ``--reason-file``'s prefix and
#: leave ``-file`` behind. Python's alternation backtracks and would recover
#: anyway; ordering it makes that independent of the engine's behaviour.
_FLAG_VALUE_RE = re.compile(
    '('
    + '|'.join(re.escape(f) for f in sorted(_VALUE_FLAGS, key=len, reverse=True))
    + r')(=|\s+)(\x22[^\x22]*\x22?|\x27[^\x27]*\x27?|\S+)'
)


def _bead_ids(cmd: str) -> list[str]:
    """Every literal bead id this command names as a close target.

    Carried verbatim with ONE correction. ``_scan_text`` ran on every
    non-flag token, a path included: once the worktree convention landed
    (``AGENTS.md`` section Worktrees), a command that changes directory
    into ``../nexus-wt/nexus-01`` before closing a bead harvested
    ``nexus-wt`` and ``nexus-01`` as close targets, and the gate demanded
    review markers for two directory names. Measured on this repo's own
    sessions; a sibling session's path yields ``nexus-c3``, which is
    bead-SHAPED and reads as a real id -- the version that costs an hour.

    The fix excludes tokens containing ``/``, because a bead id never
    does. DELIBERATELY NOT the tighter rule of scanning only bd segments,
    although :func:`_bd_verbs` already does exactly that and it would be
    more principled: getting segment detection wrong makes the harvester
    find NOTHING, and finding nothing routes to the INDETERMINATE branch,
    which ALLOWS. A mistake in the tight rule is silently permissive; a
    mistake in this one can only decline to scan a path. Every historical
    defect in this file failed open, so a fix must not add a new way.
    """
    VALUE_FLAGS = _VALUE_FLAGS
    BEAD_RE = re.compile(r'\bnexus-[a-z0-9]+\b', re.IGNORECASE)
    OPERATORS = {';', '&&', '||', '|', 'then', 'do'}
    seen, ids = set(), []

    def _scan_text(text):
        for m in BEAD_RE.finditer(text):
            tok = m.group(0).lower()
            if tok not in seen:
                seen.add(tok)
                ids.append(tok)

    # nexus-fv65m: tokenize the WHOLE command quote-aware first, then split on
    # operator TOKENS. The old order split the raw string on ';' / '|' / 'do'
    # / 'then' BEFORE tokenizing, so a semicolon or the word 'do' inside a
    # quoted --reason broke the quotes, shlex failed on both halves, and the
    # raw-scan fallback harvested every id-shaped word of the prose (a session
    # name, a peer's bead) as a close target.
    try:
        all_tokens = shlex.split(cmd, posix=True)
        segments = [[]]
        for tok in all_tokens:
            if tok in OPERATORS:
                segments.append([])
            else:
                segments[-1].append(tok)
        tokenized = [(seg, None) for seg in segments if seg]
    except ValueError:
        # Malformed quoting for the whole command: segment the raw string and
        # let each segment fall back to a raw scan where shlex still fails.
        tokenized = []
        for raw_seg in re.split(r'(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)', cmd):
            try:
                tokenized.append((shlex.split(raw_seg, posix=True), None))
            except ValueError:
                tokenized.append((None, raw_seg))

    # Malformed quoting means the flag value could not be isolated by shlex;
    # blank it textually (a value opened by an unbalanced quote runs to the
    # end of the segment) so the raw scan never reads --reason prose as targets.
    FLAG_VALUE_RE = _FLAG_VALUE_RE

    for tokens, raw in tokenized:
        if tokens is None:
            _scan_text(FLAG_VALUE_RE.sub(r'\1\2 ', raw))
            continue
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            bare = tok.split('=', 1)[0]
            if bare in VALUE_FLAGS:
                if '=' in tok:
                    # --reason=value form: flag and value are ONE token; skip
                    # it whole, do not scan.
                    i += 1
                    continue
                # --reason value form: skip the flag token AND its separate
                # value token.
                i += 2
                continue
            # nexus-q02nx.17: a path is not a bead id (see the docstring).
            if "/" not in tok:
                _scan_text(tok)
            i += 1
    return ids


def _coverage(bead_ids: list[str]) -> dict:
    """Which of *bead_ids* have a review-completed marker in T1 scratch.

    Carried from the script. Returns ``t1_reachable``, a per-id ``status``
    of covered/missing/uncertain/deadline/incomplete, the deadline used,
    and ``seen_names``.

    The deadline arithmetic is untouched: a whole-phase wall-clock budget
    from ``NX_CLOSE_GATE_DEADLINE_SECONDS`` (default 3.5s), and a per-call
    timeout clamped to the remaining budget with a 0.5s floor and a 15.0s
    ceiling. It exists because this runs inside a 5s PreToolUse ceiling and
    the hook would rather stop itself deterministically than be killed
    mid-check by the harness.
    """

    # nexus-4av2n round 3: wall-clock deadline for the WHOLE coverage phase,
    # overridable via NX_CLOSE_GATE_DEADLINE_SECONDS (test seam, mirrors the
    # push-gate's NX_PUSH_GATE_DEADLINE_SECONDS) so tests can trip it fast and
    # deterministically with a slow stub nx rather than waiting out 3.5s.
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: hooks fire on every tool call and a module-scope import of this pulls structlog + ~231 modules (measured 14ms -> 62ms); paid only when we actually spawn

    DEADLINE_SECONDS = float(os.environ.get('NX_CLOSE_GATE_DEADLINE_SECONDS', '3.5') or '3.5')
    _start = time.monotonic()

    def _deadline_exceeded():
        return (time.monotonic() - _start) >= DEADLINE_SECONDS

    def _clamp_timeout(floor=0.5, ceiling=15.0):
        remaining = max(0.0, DEADLINE_SECONDS - (time.monotonic() - _start))
        return max(floor, min(ceiling, remaining))

    _HEADER_RE = re.compile(r'^\[[0-9a-fA-F]+\]\s+(.*?)\s{2,}flagged=', re.IGNORECASE)

    def _tags(tags_line):
        m = _HEADER_RE.match(tags_line)
        body = m.group(1) if m else tags_line
        return [t.strip().lower() for t in body.split(',') if t.strip()]

    #: nexus-e3mak: a marker counts as coverage only when it NAMES THE COMPLETE
    #: required reviewer set. The gate's own remedy calls them out --
    #: 'Run the stacked reviewers (code-review-expert + substantive-critic)' --
    #: and its text says the critic is never optional, but the match was on the
    #: literal string 'review-completed' plus the bead id, so ANY entry carrying
    #: those satisfied it.
    #:
    #: 2026-08-26 (RG-C, nexus-utpuw.23): the dispatched code-review-expert wrote
    #: a handoff note for its sibling beginning
    #:     review-completed bead=nexus-utpuw.23 (RG-C reviewer 1/2: ...)
    #: It was honest -- it said 'reviewer 1/2' -- and the gate would have passed
    #: the close with the substantive-critic never dispatched. Same pathology as
    #: nexus-1f98p: a guard whose evidence is produced by the party it checks.
    #:
    #: POSITIVE NAMING, no grace path for the older marker form. Plugins load
    #: from an immutable pinned tag, so there is no mixed-version population to
    #: keep working: when the pin advances, this hook and the remedy text it
    #: prints ship together. A 'names some but not all' carve-out was drafted and
    #: DISCARDED -- the incident marker names reviewers by COUNT ('1/2'), not by
    #: agent type, so a rule keyed on partial naming would not have caught the
    #: very incident this bead is about.
    #:
    #: This raises the bar; it does not make the gate unforgeable. A reviewer can
    #: still name both. Nothing available today is independent AND bead-keyed:
    #: T1/T2 rows do carry an 'agent' column, but NOTHING in production sets
    #: NX_AGENT (only tests do) and CONTEXT_PROTOCOL merely INSTRUCTS agents to
    #: pass agent= -- so refusing on a self-declared author would rest the gate
    #: on a claim by the party it gates, which is this bead one level down.
    _REQUIRED_REVIEWERS = ('code-review-expert', 'substantive-critic')

    def _named_reviewers(text):
        low = (text or '').lower()
        return [r for r in _REQUIRED_REVIEWERS if r in low]

    def _names_full_set(text):
        return len(_named_reviewers(text)) == len(_REQUIRED_REVIEWERS)

    def _t1_covers(bead_id, tags_line, content_line):
        tags = _tags(tags_line)
        if 'review-completed' not in tags:
            return False
        if bead_id in tags:
            return True
        pat = re.compile(r'(?<![A-Za-z0-9-])' + re.escape(bead_id) + r'(?![A-Za-z0-9-])', re.IGNORECASE)
        return bool(pat.search(content_line))

    def _parse_entries(raw):
        lines = raw.splitlines()
        entries = []
        i = 0
        while i < len(lines):
            if lines[i].startswith('['):
                header = lines[i]
                content = lines[i + 1] if i + 1 < len(lines) else ''
                entries.append((header, content))
                i += 2
            else:
                i += 1
        return entries

    # nexus-fgekf: T2 memory is NOT consulted -- see the header block. The
    # _t2_covers/_t2_lookup pair and the per-id 'nx memory search' spawns are
    # deliberately GONE, not dormant. (No backticks in THIS comment: it lives
    # inside the double-quoted python3 -c string, where a backtick is live
    # bash command substitution -- the first draft of this very comment
    # EXECUTED nx memory search from inside the program that exists to prove
    # nothing calls it.)
    t1_reachable = False
    t1_timed_out = False
    t1_entries = []
    if shutil.which('nx'):
        try:
            r = run_bounded(['nx', 'scratch', 'list'], timeout=_clamp_timeout(), env=_nx_env())
            if r.returncode == 0:
                t1_reachable = True
                t1_entries = _parse_entries(r.stdout)
        except subprocess.TimeoutExpired:
            # Time budget, not capability: deny-on-indeterminate (round 3
            # doctrine) — distinct from rc!=0/nx-missing, which is a
            # capability gap (post-f7xyq that includes a dead CLI lease
            # failing loud).
            t1_timed_out = True
        except Exception:  # noqa: BLE001 — carried: the fail-open capability-gap arm
            pass

    status = {}
    seen_names = {}
    for bid in bead_ids:
        _t1_hits = [(t, c) for t, c in t1_entries if _t1_covers(bid, t, c)]
        if _t1_hits:
            _txt = ' '.join(t + ' ' + c for t, c in _t1_hits)
            if _names_full_set(_txt):
                status[bid] = 'covered'
                continue
            seen_names[bid] = _named_reviewers(_txt)
            status[bid] = 'incomplete'
            continue
        if t1_timed_out or _deadline_exceeded():
            status[bid] = 'deadline'
        elif not t1_reachable:
            status[bid] = 'uncertain'
        else:
            status[bid] = 'missing'
    return {
        "t1_reachable": t1_reachable,
        "status": status,
        "deadline_seconds": DEADLINE_SECONDS,
        "seen_names": seen_names,
    }


def _deny_message(
    missing: list[str],
    deadline_ids: list[str],
    incomplete: list[str],
    deadline_seconds: str,
    seen_names: dict,
) -> str:
    """The deny text, carried BYTE FOR BYTE.

    This is a contract, not a message. The remedy block is quoted in 19
    files and duplicated verbatim in none of them, so a paraphrase breaks
    what those files rely on. The test module asserts these bytes against
    the SCRIPT's own, not against a copy of them living in the test.
    """
    lines: list[str] = []
    lines = []
    if missing:
        lines.append('Close blocked: no review-completed marker found in T1 scratch for: ' + ' '.join(missing) + '. (T2 memory markers are no longer consulted — nexus-fgekf: an attestation must come from the closing session, not a durable store.)')
    if incomplete:
        lines.append(
            'Close blocked: a review-completed marker exists but does NOT name the full required reviewer set for: '
            + ' '.join(incomplete) + '. A marker must name BOTH code-review-expert AND substantive-critic '
            '(nexus-e3mak: a reviewer left a handoff note saying \'reviewer 1/2\' and it satisfied this gate '
            'with the critic never dispatched).'
        )
    if deadline_ids:
        lines.append(
            f"Close blocked: coverage could not be VERIFIED within the hook's {deadline_seconds}s wall-clock "
            f"budget for: " + ' '.join(deadline_ids) + ' (not confirmed missing -- just unchecked; the hook '
            'stops itself deterministically rather than risk the harness PreToolUse timeout killing it '
            'mid-check, nexus-4av2n round 3).'
        )
    lines.append(
        'Remedy: Run the marker write as a SEPARATE tool call -- this deny aborts the '
        'ENTIRE command, including any marker write bundled ahead of it '
        '(nexus-cr4lp F4). Run the stacked reviewers (code-review-expert + substantive-critic), then write the'
    )
    lines.append('marker to T1 scratch (this session; the MCP scratch tool and the CLI converge):')
    lines.append('  nx scratch put "review-completed: <bead-id> reviewers=code-review-expert,substantive-critic" --tags "review-completed,<bead-id>"')
    lines.append('The marker MUST name both reviewers; naming one (or neither) is refused. T2 memory markers do not satisfy this gate (nexus-fgekf).')
    lines.append('then re-run this close. A subagent hands the close back to its orchestrator instead of writing this marker itself (CONTEXT_PROTOCOL.md: the marker is reserved to the gate-owning session).')
    lines.append('An override (NX_REVIEW_GATE_OVERRIDE=1) exists for this gate, but only on explicit instruction from the user to use it -- it is not yours to reach for.')
    return chr(10).join(lines)


def _allow(context: str = "") -> HookResult:
    """The allow envelope, key order carried from the script's printf."""
    out: dict = {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
    if context:
        out["additionalContext"] = context
    return HookResult(stdout=json.dumps({"hookSpecificOutput": out}))


def _deny(reason: str) -> HookResult:
    """The deny envelope.

    Mirrors ``conexus/hooks/scripts/routing/_lib.py``'s ``deny_envelope``
    so the bash-native and python-native hook surfaces read identically to
    the model (``permissionDecisionReason``) and in the user's transcript
    (``systemMessage``, the reason's first line).

    ONE DEFECT FIXED HERE, and it is wider than the bead recorded. The
    ``bd create`` branch hand-built its own JSON instead of calling this,
    and the RDR names the missing ``permissionDecisionReason``. Reading
    the two side by side, it omits ``systemMessage`` as well -- so that
    deny reached the model but left the USER's transcript silent, which is
    arguably the more visible half. Both are fixed by routing through this
    helper, which is what the bead asked for; only the accounting changes.
    """
    summary = reason.split(chr(10), 1)[0]
    return HookResult(
        stdout=json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                    "reason": reason,
                },
                "systemMessage": summary,
            }
        )
    )


def _ids_with_status(status: dict, want: str) -> list[str]:
    return [bid for bid, st in status.items() if st == want]


def run(payload: dict | None) -> HookResult:
    """Decide whether this Bash call may proceed.

    LINEAR, IN THE SCRIPT'S ORDER, with an early return everywhere the
    script had an early ``exit 0``. Deliberately not refactored into a
    decision table however much better that would read: the branch order
    IS the semantics here, and every reordering is a chance to change
    which arm wins. "Move, do not rewrite" (RDR-215 Approach item 9)
    names this file.
    EVERY exit here EMITS an allow envelope rather than staying silent.
    The script calls its ``allow`` helper on each no-op path, and while a
    silent PreToolUse result means the same thing to the harness, it does
    not to the 67 tests that parse stdout -- ten of them failed on
    JSONDecodeError against an empty string. Silence and an explicit
    allow are the same DECISION and different OUTPUT, and the output is
    the part with consumers.
    """
    data = payload if isinstance(payload, dict) else {}
    # Record the session id BEFORE any branch: every `nx` subprocess this
    # hook spawns needs it forced, because the hook can run detached from
    # any live nx-mcp and would otherwise resolve a sibling session's
    # machine-wide pointer. See _nx_env.
    _SESSION_ID[0] = str(data.get("session_id") or "")

    # 1. fast no-op: anything that is not a Bash call is not our business.
    if data.get("tool_name") != "Bash":
        return _allow()

    tool_input = data.get("tool_input")
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "")
    elif isinstance(tool_input, str):
        # Two different strings reach this branch and they are NOT
        # interchangeable. A bare command ("bd close nexus-xxxxx") is
        # what a direct caller passes, and IS the command. JSON TEXT
        # holding the whole tool_input object is what an Any-typed
        # tool-tier parameter accepts, and reading THAT as the command is
        # how the gate went inert (bead nexus-17i1n): _bd_verbs looks for
        # a bd verb, finds none inside the JSON quoting, no verb means no
        # gate, close allowed.
        #
        # Default to the raw string and override ONLY on a successful
        # parse to an object. An earlier version of this branch switched
        # on a leading "{" instead, which is wrong for a reason a
        # reviewer caught rather than a test: `{ bd close nexus-x; }` is
        # a POSIX brace group, a perfectly ordinary command, and it
        # starts with a brace and is not JSON. That version emptied the
        # command and allowed the close -- the very failure this change
        # exists to close, reintroduced in a narrower shape. Parsing is
        # the actual question; the first character was only ever a proxy
        # for it.
        command = tool_input
        try:
            parsed = json.loads(tool_input)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            command = str(parsed.get("command") or "")
    if not command:
        return _allow()

    # 2. which bd verb, if any. No verb, no gate.
    verbs = _bd_verbs(command)
    if not verbs["has_create"] and not verbs["has_close_or_done"]:
        return _allow()

    return _run_gate(data, command, verbs)


# --- the bd create branch --------------------------------------------------

#: The three commitment markers a follow-up bead must carry when an RDR
#: close is active. Carried verbatim, matched case-insensitively, and
#: 'sprint or due' is one requirement satisfied by either word.
_CREATE_MARKERS = (
    ("reopens_rdr", ("reopens_rdr",)),
    ("sprint or due", ("sprint", "due")),
    ("drift_condition", ("drift_condition",)),
)


def _active_close_rdr() -> str:
    """The RDR number of an in-flight close, from T1 scratch, or "".

    Best-effort by design: no ``nx`` on PATH, an unreachable T1 or a
    malformed entry all yield "" and the create branch then allows. The
    script reaches for ``nx scratch list`` rather than search because the
    lookup is an exact tag match, not a semantic one.
    """
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: hooks fire on every tool call and a module-scope import of this pulls structlog + ~231 modules (measured 14ms -> 62ms); paid only when we actually spawn

    if shutil.which("nx") is None:
        return ""
    try:
        r = run_bounded(
            ["nx", "scratch", "list"],
            timeout=5.0,
            env=_nx_env(),
        )
    except Exception:  # noqa: BLE001 — carried: best-effort, absence is not a verdict
        return ""
    m = re.search(r"active-close[- :]+rdr[- :]*0*(\d+)", r.stdout, re.IGNORECASE)
    return m.group(1) if m else ""


def _nx_env(session_id: str = "") -> dict:
    """The environment every ``nx`` subprocess here needs.

    Carried from the script's own session handling: this hook can run
    DETACHED from any live nx-mcp, so it cannot rely on inheriting a
    session. It forces ``NX_SESSION_ID`` from the stdin payload -- the
    highest-priority tier of the resolution chain -- so ``nx scratch
    list`` resolves to the session actually running the hook rather than
    a sibling session's clobbered machine-wide pointer. It also opts back
    into the shared CLI fallback, because post-nexus-f7xyq an explicit
    NX_SESSION_ID with no live lease fails LOUD, and that is precisely
    the state a detached hook is in.

    Surfaced by the differential: without this the port read an empty
    session and every marker lookup came back unset.
    """
    env = dict(os.environ)
    sid = session_id or _SESSION_ID[0]
    if sid:
        env["NX_SESSION_ID"] = sid
        env["NX_T1_ALLOW_SHARED_FALLBACK"] = "1"
    return env


#: The session id for this dispatch, set once by run() from the payload.
#: A module-level slot rather than a parameter threaded through six
#: functions: the script used an exported environment variable for the
#: same reason, and the call graph is identical.
_SESSION_ID = [""]


def _create_parse(command: str) -> str:
    """``--title`` and ``--description`` values, joined. Carried."""
    title, desc = "", ""
    try:
        tokens = shlex.split(command)
        for i, t in enumerate(tokens):
            if t == "--title" and i + 1 < len(tokens):
                title = tokens[i + 1]
            elif t.startswith("--title="):
                title = t.split("=", 1)[1]
            elif t == "--description" and i + 1 < len(tokens):
                desc = tokens[i + 1]
            elif t.startswith("--description="):
                desc = t.split("=", 1)[1]
    except Exception:  # noqa: BLE001 — carried: a malformed command is not a denial
        pass
    return f"{title} {desc}"


def _create_gate(command: str) -> HookResult:
    """Advisory on ``bd create`` while an RDR close is active.

    Three outcomes, in the script's order: no active close allows
    silently; an active close the bead does not reference allows WITH an
    advisory; an active close it does reference requires all three
    commitment markers and denies naming the missing ones.
    """
    rdr = _active_close_rdr()
    if not rdr:
        return _allow()

    combined = _create_parse(command)
    rdr_int = rdr.lstrip("0") or rdr
    mentioned = re.search(
        rf"(^|[^0-9])0*{re.escape(rdr_int)}([^0-9]|$)|rdr-0*{re.escape(rdr_int)}",
        combined,
        re.IGNORECASE,
    )
    if not mentioned:
        return _allow(
            f"RDR close active for RDR-{rdr} \u2014 if this bead is a follow-up, "
            "add reopens_rdr/sprint/drift_condition metadata to the description."
        )

    lowered = combined.lower()
    missing = [
        label for label, words in _CREATE_MARKERS
        if not any(w in lowered for w in words)
    ]
    if not missing:
        return _allow()

    missing_display = "".join(f"- {m}{chr(10)}" for m in missing)
    reason = (
        f"Follow-up bead for RDR-{rdr} is missing required commitment "
        f"metadata.{chr(10)}Missing fields:{chr(10)}{missing_display}"
        f"Add these to the --description, e.g.:{chr(10)}"
        f"  reopens_rdr: {rdr}{chr(10)}"
        f"  sprint: implementation-2026-04{chr(10)}"
        f"  drift_condition: <what drift looks like>"
    )
    # Routed through the shared envelope. The script hand-built its own
    # JSON here and omitted permissionDecisionReason AND systemMessage;
    # see _deny's docstring.
    return _deny(reason)


# --- stamping --------------------------------------------------------------


def _stamp_ids(ids: list[str], state: str, reason: str) -> None:
    """Best-effort ``bd set-state`` per id. NEVER called on a deny path.

    A denied close must acquire no verification record at all -- that is
    the false-record fix this gate exists for. A failed stamp is LOUD on
    stderr rather than swallowed, so a broken ``bd`` at close time is
    observable instead of producing an audit record nobody can trust and
    nobody was told is missing.
    """
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: hooks fire on every tool call and a module-scope import of this pulls structlog + ~231 modules (measured 14ms -> 62ms); paid only when we actually spawn

    if not ids:
        return
    if shutil.which("bd") is None:
        _warn(f"bd not found on PATH \u2014 cannot stamp verification={state} "
              f"for: {' '.join(ids)}")
        return
    for bid in ids:
        try:
            r = run_bounded(
                ["bd", "set-state", bid, f"verification={state}", "--reason", reason],
                timeout=5.0,
            )
            if r.returncode != 0:
                _warn(f"could not stamp verification={state} for {bid}")
        except Exception:  # noqa: BLE001 — carried: a stamp failure never blocks
            _warn(f"could not stamp verification={state} for {bid}")


def _warn(message: str) -> None:
    """Straight to stderr, carrying the script's own wording.

    NOT through ``_emit``. PreToolUse stdout must stay pure JSON so stderr
    is the only channel either way, but the script's text is
    ``WARNING: ...`` and a test greps for that token. Routing it through
    structlog rewrote the line as ``level='warning' message='...'`` and
    the grep stopped matching -- the differential caught it. An
    operator-facing string that something greps is a contract like any
    other.
    """
    import sys  # noqa: PLC0415 — only the warn path pays it

    sys.stderr.write(f"WARNING: {message}{chr(10)}")


# --- the gate ---------------------------------------------------------------


def _run_gate(data: dict, command: str, verbs: dict) -> HookResult:
    """The decision table, in the script's order.

    Linear with early returns because the ORDER is the semantics. Every
    uncertain path allows: this gate fails open by design and the RDR
    deliberately does not revisit that.
    """
    if verbs["has_create"]:
        return _create_gate(command)

    # on_close must be enabled for the close half to gate at all.
    from nexus.hooks.stop_verification import _read_config  # noqa: PLC0415 — shared reader

    if _read_config().get("on_close") is not True:
        # SAY SO. This was a bare `return _allow()`, and a gate that
        # declines to gate without a word is indistinguishable from one
        # that checked and was satisfied — which is the whole failure
        # class this module has just been through (bead nexus-17i1n).
        #
        # It is also not a hypothetical here. `.nexus.yml` is gitignored
        # by design, so there is one per repo and it lives in the primary
        # checkout; every session in a worktree read DEFAULTS and took
        # this branch, silently, from the day the project moved to
        # one-session-one-worktree (bead nexus-634ye). Resolving the
        # config across worktrees is that bead's job. Being audible when
        # the answer is "not gating" is this line's, and the two are
        # independent — the config could be legitimately off, and a
        # reader still deserves to know that is why nothing happened.
        return _allow(
            "Close gate NOT run: verification.on_close is not enabled in "
            ".nexus.yml, so no review-completed marker was checked. This is "
            "an allow by configuration, not by verification."
        )

    ids = _bead_ids(command)
    if not ids:
        return _allow(
            "INDETERMINATE: no literal bead id (nexus-*) found anywhere in this "
            "bd close/done command \u2014 cannot check a review marker statically, "
            "so verification is NOT stamped. Prefer literal ids over shell "
            "variables so the review gate can verify coverage."
        )

    override = (
        os.environ.get("NX_REVIEW_GATE_OVERRIDE") == "1" or verbs["inline_override"]
    )

    result = _coverage(ids)
    status = result["status"]
    covered = _ids_with_status(status, "covered")
    missing = _ids_with_status(status, "missing")
    uncertain = _ids_with_status(status, "uncertain")
    deadline = _ids_with_status(status, "deadline")
    incomplete = _ids_with_status(status, "incomplete")

    if override and (missing or uncertain or deadline or incomplete):
        # THE TWO ID SETS ARE STAMPED DIFFERENTLY, and the three things
        # below key on NOT-COVERED rather than on every id in the command.
        # The bash did this (COVERED_SPACE -> "passed", NOT_COVERED_SPACE
        # -> "overridden") and the first port collapsed both onto `ids`,
        # so a bead that genuinely had a verified marker lost its true
        # state to an "overridden" it never earned and the escape log
        # claimed the bypass covered a bead that needed no covering --
        # a false record in the audit trail this gate exists to keep
        # honest. Restored at nexus-q02nx.24 from the deleted script.
        not_covered = missing + uncertain + deadline + incomplete
        _log_override_escape(not_covered, command)
        # The override still STAMPS, with its own state. Surfaced by the
        # differential: leaving the bypass unstamped would make an
        # overridden close indistinguishable from one that was never
        # gated, which is the record this gate exists to produce.
        if covered:
            _stamp_ids(covered, "passed", "review-completed marker verified at close")
        _stamp_ids(
            not_covered,
            "overridden",
            "NX_REVIEW_GATE_OVERRIDE=1; no confirmed review-completed coverage "
            f"in T1 scratch for: {' '.join(not_covered)}",
        )
        return _allow(
            "NX_REVIEW_GATE_OVERRIDE=1 \u2014 review gate bypassed for: "
            f"{' '.join(not_covered)}. Logged as a routing escape."
        )

    if missing or deadline or incomplete:
        # No stamp for ANY id on this path, covered ones included.
        return _deny(
            _deny_message(
                missing, deadline, incomplete,
                str(result["deadline_seconds"]), result["seen_names"],
            )
        )

    if uncertain:
        _stamp_ids(covered, "passed", "review-completed marker verified at close")
        _stamp_ids(
            uncertain, "unverified",
            "T1 scratch unreachable at close time (capability gap, not a "
            "time-budget issue)",
        )
        return _allow(
            "WARNING: could not verify review-completed coverage in T1 scratch "
            f"for {' '.join(uncertain)} \u2014 T1 unreachable (the nx binary is "
            "absent, or 'nx scratch list' failed; post-nexus-f7xyq that includes "
            "a dead CLI T1 lease failing loud, check 'nx doctor --check-t1'). "
            "Closing anyway (a broken verification path must not brick every "
            "bead close) but stamped verification=unverified for those ids, NOT "
            "passed. If review truly happened this is a capability gap, not a "
            "review gap. An override (NX_REVIEW_GATE_OVERRIDE=1) exists for this "
            "gate, but only on explicit instruction from the user to use it."
        )

    _stamp_ids(covered, "passed", "review-completed marker verified at close")
    return _allow(f"Review completed for {' '.join(ids)}.")


def _log_override_escape(ids: list[str], command: str) -> None:
    """Every override use is auditable, via the routing JSONL sink.

    Import is deferred to the override path only: the fast-no-op path must
    not pay for a sink it never writes to.

    THIS USED TO BE "the one part of this hook that does not port
    cleanly", and it no longer is (nexus-t9klx). The reasoning recorded
    here was that ``log_routing_event`` lived in
    ``conexus/hooks/scripts/routing/_lib.py`` -- plugin content, not the
    wheel -- with no wheel-side writer to call instead, so this function
    located the plugin's ``routing/`` directory off ``CLAUDE_PLUGIN_ROOT``
    or a checkout-relative path, pushed it onto ``sys.path``, and did a
    bare ``import _lib``.

    That premise is now false in both halves. The library moved into the
    wheel as ``nexus.hooks._routing_lib``, and the plugin copy was deleted
    once the last plugin script importing it was ported, so there IS a
    wheel-side writer and the directory this searched for holds no Python
    at all. The whole resolution dance goes with it: no env var to read,
    no unexpanded-``${...}`` guard, no ``sys.path`` mutation, no
    checkout-relative fallback that was absent once installed anyway.

    Found by deleting the plugin copy and watching this go quiet — the
    two ``TestF2EnvPrefixOverride`` cases stopped seeing an audit record.
    Which is the loudness below earning its keep: an ``ImportError`` here
    warns instead of vanishing. The bash this was ported from ended its
    import in a bare ``except: pass``, and for an AUDIT of override use
    that is the wrong direction -- the point is that a bypass leaves a
    trace, and a silently-missing trace is indistinguishable from a bypass
    that never happened.
    """
    try:
        from nexus.hooks import _routing_lib as _lib  # noqa: PLC0415 — only the override path pays this

        _lib.log_routing_event(
            rule="pre_close_verification_hook",
            outcome="escape",
            tool_name="Bash",
            command_fragment=command,
            escape_reason="NX_REVIEW_GATE_OVERRIDE=1: " + " ".join(ids),
        )
    except Exception as exc:  # noqa: BLE001 — never blocks the escape, but SAYS so
        _warn(f"override audit NOT recorded ({exc!r}). The bypass left no trace.")
