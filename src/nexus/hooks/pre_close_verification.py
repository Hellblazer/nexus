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

**THE DENY TEXT IS A CONTRACT.** The remedy block is quoted in 19 files --
the phase-review-gate skill, ``.beads/PRIME.md``, several RDR
post-mortems -- and duplicated verbatim nowhere, so a paraphrase breaks
the documentation-as-contract property those files rest on. It is carried
byte for byte and the test module asserts it against the SCRIPT's bytes
rather than against a copy living in the test.

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
    VALUE_FLAGS = {'--reason', '--reason-file', '-r',
                   '--description', '--notes', '-m'}
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
    FLAG_VALUE_RE = re.compile(
        r'(--reason|--description|--notes|-m)(=|\s+)(\x22[^\x22]*\x22?|\x27[^\x27]*\x27?|\S+)'
    )

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
            r = subprocess.run(['nx', 'scratch', 'list'], capture_output=True, text=True, timeout=_clamp_timeout())
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
    """
    data = payload if isinstance(payload, dict) else {}

    # 1. fast no-op: anything that is not a Bash call is not our business.
    if data.get("tool_name") != "Bash":
        return HookResult()

    tool_input = data.get("tool_input")
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "")
    elif isinstance(tool_input, str):
        command = tool_input
    if not command:
        return HookResult()

    # 2. which bd verb, if any. No verb, no gate.
    verbs = _bd_verbs(command)
    if not verbs["has_create"] and not verbs["has_close_or_done"]:
        return HookResult()

    return _run_gate(data, command, verbs)
