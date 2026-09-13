# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Repo-wide lint: no ``pipefail``-set shell script may pipe a producer
into an early-exit consumer (``grep -q``, ``grep -m<N>``, ``head``).

THE DEFECT CLASS (nexus-i66g4 / nexus-6zxfb / nexus-wbeyi — this is the
THIRD recurrence, filed to mechanize a fourth from ever landing):

    <command> | grep -q <pattern>

under ``set -o pipefail`` can false-FAIL the whole pipeline even when the
match is found. ``grep -q`` (and ``grep -m<N>``, and ``head``) exit as soon
as they have what they need and close their read end of the pipe. If the
producer is still writing — a multi-line ``click.echo``-per-line subprocess,
or a single ``echo``/``printf`` whose payload exceeds the pipe's buffer
capacity — the still-writing producer takes SIGPIPE and exits non-zero.
``pipefail`` then promotes THAT failure over the consumer's own (possibly
successful) exit status, so a pipeline that "found the pattern" can still
report failure.

Three confirmed instances:

  - nexus-i66g4 (CLOSED P1, landed 5f9a85da): ``echo $VAR | grep -q`` across
    ~12 sites in ``tests/e2e/upgrade-shakeout.sh`` + ``release-sandbox.sh``.
    Needed a DEGRADED pipe buffer to fire (1131B into a 512B pipe).
  - nexus-6zxfb (CLOSED P1): ``release-sandbox.sh`` step 7,
    ``echo "$X" | head -3 | sed ...`` — ``head`` as a MIDDLE pipeline stage,
    not just the last. Same degraded-buffer dependency.
  - nexus-wbeyi (P1, this file's reason for existing):
    ``tests/e2e/local-index-memory-gate.sh:555``,
    ``nx daemon service status | grep -qiE "health.*ok|...|running"``. This
    one is DETERMINISTIC — the producer emits ~30 lines and grep exits at
    line ~13, so it fires on any healthy machine, no degradation needed.
    Landed 3 days after i66g4 closed. "Nothing in the repo prevents
    instance four" (Hal, 2026-08-10) — this lint is that prevention.

CRITICAL CORRECTION carried into this docstring so it isn't re-learned the
hard way a fourth time: the discriminating variable is ``pipefail``, NOT the
shell. Verified empirically: bash 5.3.9 + pipefail -> rc=120; bash without
pipefail -> rc=0; zsh WITH ``setopt pipefail`` -> rc=120; zsh without ->
rc=0. zsh is NOT safe by default absence of the bug; the shell is irrelevant,
only whether pipefail is active.

SANCTIONED FIX (i66g4's own fix, reused by wbeyi): eliminate the pipe.
  - Fixed producer already in a variable: ``[[ "$VAR" == *substring* ]]``
    for a literal match, or ``[[ "$VAR" =~ regex ]]`` for an ERE (bash's
    ``=~`` uses the same POSIX ERE dialect as ``grep -E``, so a
    ``grep -qE 'a|b|c'`` pattern carries over as ``[[ "$VAR" =~ (a|b|c) ]]``
    almost verbatim).
  - Live subprocess: capture to a variable FIRST (command substitution
    drains to EOF, so the early-exit hazard cannot occur), THEN apply the
    same bash-native match: ``OUT="$(cmd)"; [[ "$OUT" =~ pattern ]]``.
  - Display-only pipelines that don't gate control flow (e.g. truncating a
    log for pretty-printing) may instead suffix ``|| true`` — this
    neutralizes the SIGPIPE-promoted failure without needing to eliminate
    the pipe, and is the existing idiom already used elsewhere in this repo
    (e.g. ``release-sandbox.sh``'s ``| head -N | sed ... || true`` sites).
    This lint recognizes and does not flag that shape.

SCOPE (design decision, see nexus-wbeyi remediation): every tracked ``*.sh``
file, filtered to those that actually ``set -o pipefail`` (or an equivalent
combined-flag form, e.g. ``set -euo pipefail``). The precondition filter is
load-bearing, not decorative: a script that never sets pipefail cannot
exhibit this hazard (a failed pipe stage silently doesn't propagate), so
including such files would be exactly the "flags files where the hazard
cannot occur" failure mode that gets a lint muted. This is why the scope is
NOT hardcoded to ``tests/e2e/`` alone (where all three real incidents
landed) — ``scripts/*.sh`` and ``conexus/hooks/scripts/*.sh`` both
independently set pipefail in multiple files (verified at authoring time:
14/16 and 3/11 respectively), so the hazard can occur there too, and a lint
that only watched the three known-bitten files would have been blind to
the NEXT directory it surfaces in.

CONSUMERS (design decision): ``grep -q`` (any flag cluster containing
``q``: ``-qi``, ``-iq``, ``-qE``, ``-qiE`` etc.), ``grep -m<N>`` (early-exit
after N matches), and ``head`` (bare or ``-N``/``-n N``/``-c N`` — all
early-exit once the line/byte budget is met). Deliberately NOT ``tail``
(reads to EOF, drains the pipe, cannot trigger this) and NOT ``grep -c``
(must scan every line to produce an accurate count, also drains). Widening
to more exotic early-exit readers (``sed -n '/pat/q'``, ``awk '{exit}'``) is
left for a future pass if one of those is what fires instance five — this
repo already had one false "vacuous gate" finding land in the same week
this lint was authored, and a lint that guesses too broadly trains people to
add exemptions rather than fix the shape.

EXEMPTIONS: a ratchet, mirroring ``tests/test_mode_declarations_are_explicit
.py`` (RDR-109) — an exact-equality ceiling on a per-entry-documented
exclusion set, never a bare unbounded allowlist. See
``_PIPEFAIL_EARLY_EXIT_EXEMPT`` below.

``|| true`` RATCHET (closes a substantive-critique gap on this file, 2026-
08-10 hardening pass): the sanctioned display-only mitigation above is no
longer a bare, untracked escape hatch. A trailing ``|| true``/``|| :`` on
an early-exit-consumer pipe segment now REQUIRES a matching entry in
``_PIPEFAIL_OR_TRUE_SITES`` (same exact-equality-ceiling ratchet shape as
``_PIPEFAIL_EARLY_EXIT_EXEMPT``) — an untracked ``|| true`` site is
flagged by the sweep exactly like an unexempted violation, just with a
different remediation message. Worth naming explicitly, because it is the
reason this ratchet exists at all: appending ``|| true`` to a pipe used as
an ``if``/``elif`` CONDITION does not merely neutralize the pipefail
hazard, it makes that condition UNCONDITIONALLY TRUE — the untracked
escape hatch would silently convert a real assertion into a vacuous pass,
precisely this repo's dominant defect class. ``_if_condition_or_true_bug_
hits`` / the ``test_no_if_condition_neutralizes_its_own_pipe_with_or_true``
sweep below catch that specific shape as a hard, always-a-bug failure (not
merely advisory) — verified zero live occurrences in this repo at
authoring time, so the gate starts clean.

TRANSITIVE ``source``/``.`` PRECONDITION: a script that never declares
``pipefail`` itself but ``source``s (or ``. ``s) a repo-local lib that DOES
is exactly as exposed to this hazard as if it declared pipefail directly —
confirmed real: ``scripts/validate/03-cli.sh`` and
``scripts/validate/04-hooks.sh`` both ``source "$(dirname "$0")/lib.sh"``,
and ``lib.sh`` sets ``set -uo pipefail``; neither file declared pipefail
itself, so both were invisible to the precondition filter before this fix.
``_sets_pipefail`` now resolves exactly ONE level of ``source``/``.`` for a
small set of repo-local path idioms (``$(dirname "$0")/…``,
``$(dirname "${BASH_SOURCE[0]}")/…``, ``$HERE/…``, ``$SCRIPT_DIR/…``,
``$REPO_ROOT/…``) — anything else (a dynamic/generated path like
``"${SANDBOX_ENV}"`` or ``"${CREDS_FILE}"``) is left unresolved rather than
guessed at, which only means such a script is not YET recognized as
pipefail-set via that hop; it does not create a false negative beyond what
already existed. DISCLOSED BOUND, not silently assumed: this is exactly
one hop, not a transitive closure. Verified at authoring time that one
hop is sufficient for the WHOLE current tree — none of the repo-local libs
sourced anywhere in this corpus (``scripts/validate/lib.sh``,
``tests/e2e/lib.sh``, ``tests/e2e/lib/lock.sh``,
``tests/e2e/lib/expectations.sh``) itself sources a further file, so there
is no live 2-hop chain today. If a future lib begins sourcing another lib,
that second hop is invisible to this precondition filter until this scope
is revisited — a disclosed limitation, not a silent gap.

ADDITIONAL DISCLOSED-DEFERRED INSTANCES: beyond ``sed -n '/pat/q'`` /
``awk '{exit}'`` (already disclosed above), a ``while read`` / bare
``read`` loop consuming a pipe and exiting early via ``break`` is the same
defect class and is NOT covered by this lint. Verified zero live
occurrences in this repo at authoring time (dormant, not exploited) — left
for a future pass on the same "don't guess broader than a demonstrated
instance" principle as the sed/awk carve-out, rather than silently
unmentioned. ``grep``'s long-form early-exit flags (``--quiet``,
``--max-count=<N>``) and ``grep -l``/combined ``-l`` clusters (list-
matching-filenames — early-exit like ``-q``, stops after the first match
per input) ARE now covered (trivial additions to
``_grep_flags_are_early_exit``; a long-form spelling escaping a short-form
detector was a real hole, not a considered scope decision).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests._lint_line_anchor import resolve_anchor, resolve_ledger

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

# Detects a file setting pipefail via `set -o pipefail`, `set -eo pipefail`,
# `set -Eeuo pipefail`, `set -uo pipefail`, `set -u -o pipefail` (SEPARATE
# flag tokens -- confirmed present in this repo, e.g.
# tests/e2e/lib/harness_lock_test.sh; a first cut of this regex requiring
# the `o`-bearing flag to be the FIRST token right after `set` silently
# missed this shape during authoring), etc. Two alternatives: a combined
# flag cluster containing `o` immediately followed by `pipefail`, or a
# standalone `-o pipefail` token pair anywhere after `set` (with any
# number of other flag-looking tokens in between).
_PIPEFAIL_SET_RE = re.compile(
    r"\bset\s+[-+][A-Za-z]*o[A-Za-z]*\s+pipefail\b"
    r"|\bset\s+(?:[-+]\S+\s+)*-o\s+pipefail\b"
)

# A pipe SEGMENT (the text between one unquoted `|` and the next, or EOL)
# that opens with an early-exit consumer.
_CONSUMER_START_RE = re.compile(r"^(?P<cmd>x?e?f?grep|head)\b(?P<rest>.*)$")

# A `-m<N>` / `-m <N>` early-exit form on a grep flag cluster.
_GREP_DASH_M_RE = re.compile(r"(?:^|\s)-\w*m\s*\d|(?:^|\s)-m\s+\d")

# Long-form spellings of the same early-exit flags the short forms above
# already catch -- ``--quiet`` (== ``-q``) and ``--max-count=<N>`` (== ``-m
# <N>``). A long-form spelling escaping a short-form detector is a real
# detection hole, not a scope decision (gap #3).
_GREP_QUIET_LONG_RE = re.compile(r"(?:^|\s)--quiet\b")
_GREP_MAX_COUNT_LONG_RE = re.compile(r"(?:^|\s)--max-count=\d")

# `-l` / combined clusters containing `l` (`grep -l`, `grep -il`, ...) --
# list matching FILE NAMES, stopping after the first match per input file.
# Same early-exit hazard shape as `-q` (gap #3).
_GREP_DASH_L_RE = re.compile(r"(?:^|\s)-\w*l\w*(?:\s|$)")

# The sanctioned "discard the exit status" mitigation for display-only
# pipelines (release-sandbox.sh's existing idiom). Recognizing this SHAPE
# is not, by itself, a free pass any more -- see the `_PIPEFAIL_OR_TRUE_
# SITES` ratchet below (gap #1): every guarded hit still needs a tracked,
# reviewed entry.
#
# NOTE (found while hardening this file, 2026-08-10): the naive
# `r"\|\|\s*(?:true|:)\b"` never matches the `:` alternative -- `\b`
# requires a word/non-word transition, but `:` is itself non-word, so a
# trailing `:` followed by anything else non-word (end of line, a space,
# a `;`) has non-word on BOTH sides of that position and no boundary
# exists. `\b` is scoped to `true` only below; `:` needs no boundary
# check (it is not a word-character prefix of some longer token grep/
# shell would care about here).
_OR_TRUE_RE = re.compile(r"\|\|\s*(?:true\b|:)")


def _split_pipe_segments(line: str) -> list[str]:
    """Split *line* on unquoted, single (not ``||``) ``|`` characters.

    Quote- and escape-aware so a literal ``|`` inside a grep ERE pattern
    (extremely common: ``grep -qE 'a|b|c'``) is not mistaken for a shell
    pipeline boundary -- a naive ``line.split('|')`` truncates mid-pattern
    on exactly the shape most of this repo's real violations use.

    ``$(...)`` command substitution gets its OWN fresh quote context, even
    when it sits inside an enclosing double-quoted string -- bash parses
    ``"$(cmd1 | cmd2)"`` with ``cmd1 | cmd2`` as a real pipeline despite
    the outer ``"..."``, and this is exactly the shape most of this repo's
    ``VAR="$(producer | head -1)"`` value-extraction sites use. Without
    this, the entire ``$(...)`` body reads as "inside double quotes" and
    every pipe inside it is invisible to the scanner -- confirmed to
    silently blind the sweep on real sites during authoring (e.g.
    ``GOT_VER="$(nx --version 2>&1 | grep -oE '...' | head -1)"``).

    Not a full shell parser (deliberately, matching this repo's other
    shell lints' scope) -- just enough quote/substitution tracking to get
    pipe-boundary detection right, which is the one thing that actually
    changes which lines get flagged.
    """
    segments: list[str] = []
    current: list[str] = []
    in_squote = False
    # dquote_stack[-1] is whether the CURRENT nesting frame is inside a
    # `"..."`. A `$(` pushes a fresh (unquoted) frame regardless of the
    # enclosing frame's quote state; its matching `)` pops back.
    dquote_stack: list[bool] = [False]
    # Parallel to dquote_stack (minus the base frame): counts unmatched
    # `(` seen since the corresponding `$(` opened, so a `)` that closes a
    # nested subshell/grouping inside the substitution doesn't get
    # mistaken for the substitution's own closing paren.
    paren_depth_stack: list[int] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        in_dquote = dquote_stack[-1]
        if in_squote:
            current.append(c)
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n:
                current.append(c)
                current.append(line[i + 1])
                i += 2
                continue
            if c == '"':
                dquote_stack.pop()
                if not dquote_stack:
                    dquote_stack = [False]
                current.append(c)
                i += 1
                continue
            if c == "$" and i + 1 < n and line[i + 1] == "(":
                current.append("$(")
                dquote_stack.append(False)
                paren_depth_stack.append(0)
                i += 2
                continue
            current.append(c)
            i += 1
            continue
        # Outside any quote (top-level frame, or an unquoted $(...) frame).
        if c == "'":
            in_squote = True
            current.append(c)
            i += 1
            continue
        if c == '"':
            dquote_stack.append(True)
            current.append(c)
            i += 1
            continue
        if c == "$" and i + 1 < n and line[i + 1] == "(":
            current.append("$(")
            dquote_stack.append(False)
            paren_depth_stack.append(0)
            i += 2
            continue
        if c == "(" and paren_depth_stack:
            paren_depth_stack[-1] += 1
            current.append(c)
            i += 1
            continue
        if c == ")" and paren_depth_stack:
            if paren_depth_stack[-1] > 0:
                paren_depth_stack[-1] -= 1
            else:
                paren_depth_stack.pop()
                dquote_stack.pop()
            current.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            current.append(c)
            current.append(line[i + 1])
            i += 2
            continue
        if c == "#":
            # An unquoted '#' starts a trailing comment -- nothing past
            # it is a pipe boundary.
            current.append(line[i:])
            break
        if c == "|":
            if i + 1 < n and line[i + 1] == "|":
                current.append("||")
                i += 2
                continue
            segments.append("".join(current))
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    segments.append("".join(current))
    return segments


def _grep_flags_are_early_exit(rest: str) -> bool:
    """True if *rest* (the text after ``grep``/``egrep``/``fgrep``) carries
    an early-exit flag: ``-q`` (in any combined cluster) or ``-m<N>``.

    Deliberately excludes ``-c`` (count) even though it is often combined
    with other flags -- ``grep -c`` must scan every line to produce an
    accurate count, so it drains the pipe like ``tail`` does and cannot
    trigger this hazard.
    """
    # Tokens up to the first token that doesn't start with '-' (the
    # pattern argument begins there).
    tokens = rest.split()
    flag_tokens: list[str] = []
    for tok in tokens:
        if tok.startswith("-"):
            flag_tokens.append(tok)
        else:
            break
    flag_blob = " ".join(flag_tokens)
    if re.search(r"(?:^|\s)-\w*q\w*(?:\s|$)", flag_blob):
        return True
    if _GREP_DASH_M_RE.search(flag_blob):
        return True
    if _GREP_QUIET_LONG_RE.search(flag_blob):
        return True
    if _GREP_MAX_COUNT_LONG_RE.search(flag_blob):
        return True
    if _GREP_DASH_L_RE.search(flag_blob):
        return True
    return False


def _iter_early_exit_consumer_segments(lines: list[str]):
    """Yield ``(1-based lineno, snippet, guarded)`` for every pipe segment
    in *lines* that opens with an early-exit consumer (``grep -q`` /
    ``grep -m<N>`` / ``grep --quiet`` / ``grep --max-count=<N>`` /
    ``grep -l`` / ``head``), where ``guarded`` is True iff a trailing
    ``|| true`` / ``|| :`` appears anywhere from that segment to the end
    of the line (the sanctioned display-only mitigation -- `|| true` can
    trail ANY later pipe stage, e.g. `cmd | head -3 | sed ... || true`,
    the 6zxfb shape where head is a MIDDLE stage, and it still neutralizes
    the promoted failure for every upstream stage, so every segment from
    here to end of line is checked, not just this one).

    Shared core for `_early_exit_consumer_hits` (unguarded view) and
    `_or_true_guarded_early_exit_hits` (guarded view) so the two can never
    silently drift apart.

    Line-based, like ``test_shell_continuation_lint.py``'s scanner --
    deliberately no full shell parser. Every real instance found across
    this repo (i66g4's ~12 sites, 6zxfb, wbeyi, and the full-repo sweep at
    authoring time) has the pipe and its consumer on the same physical
    line, so this is not a hypothetical simplification.
    """
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        segments = _split_pipe_segments(line)
        # segments[0] is never pipe-fed (nothing precedes it); every
        # segment from index 1 onward was reached via a real unquoted `|`.
        for idx in range(1, len(segments)):
            seg = segments[idx]
            stripped = seg.lstrip()
            m = _CONSUMER_START_RE.match(stripped)
            if m is None:
                continue
            cmd = m.group("cmd")
            rest = m.group("rest")
            if cmd == "head":
                early_exit = True
            else:
                early_exit = _grep_flags_are_early_exit(rest)
            if not early_exit:
                continue
            guarded = any(_OR_TRUE_RE.search(s) for s in segments[idx:])
            yield i, stripped.strip(), guarded


def _early_exit_consumer_hits(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(1-based lineno, matched consumer description)`` pairs for
    every early-exit-consumer pipe stage in *lines* that is NOT guarded by
    a trailing ``|| true`` / ``|| :``. A guarded hit is not silently
    dropped -- it is a candidate for `_or_true_guarded_early_exit_hits`
    below, which the `_PIPEFAIL_OR_TRUE_SITES` ratchet enforces against.
    """
    return [
        (i, snippet)
        for i, snippet, guarded in _iter_early_exit_consumer_segments(lines)
        if not guarded
    ]


def _or_true_guarded_early_exit_hits(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(1-based lineno, matched consumer description)`` pairs for
    every early-exit-consumer pipe stage in *lines* that IS guarded by a
    trailing ``|| true`` / ``|| :`` -- the mirror image of
    `_early_exit_consumer_hits`. Used to enforce the `_PIPEFAIL_OR_TRUE_
    SITES` ratchet (gap #1): every guarded site must be a tracked,
    reviewed entry, never a silent, untracked escape hatch.
    """
    return [
        (i, snippet)
        for i, snippet, guarded in _iter_early_exit_consumer_segments(lines)
        if guarded
    ]


_IF_LINE_RE = re.compile(r"^\s*(?:if|elif)\b")
_THEN_TAIL_RE = re.compile(r";\s*then\s*(?:#.*)?$")


def _if_condition_or_true_bug_hits(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(1-based lineno, full stripped line)`` for the always-
    true-condition bug shape: ``if <producer> | <early-exit consumer> ||
    true; then`` (or ``elif``).

    Appending ``|| true`` to a pipe used as an ``if``/``elif`` CONDITION
    does not merely hide the pipefail hazard the way the same suffix does
    on a display-only pipeline -- it makes the condition succeed
    UNCONDITIONALLY, deleting the assertion outright. This is flagged as
    a hard failure (`test_no_if_condition_neutralizes_its_own_pipe_with_
    or_true` below), not merely advisory, because it is always a bug with
    no legitimate use, and the sweep found zero live occurrences at
    authoring time.

    Deliberately narrow: single physical line, ``if``/``elif`` ... ``;
    then`` on the same line. The two-line ``if cond\\nthen`` form is not
    covered -- no live instance of that shape combined with this bug
    exists in this repo at authoring time, and widening beyond a
    demonstrated instance repeats this file's own documented anti-pattern
    of guessing too broadly (see the sed/awk carve-out above).
    """
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        if not _IF_LINE_RE.match(line):
            continue
        if not _THEN_TAIL_RE.search(line):
            continue
        for _, _, guarded in _iter_early_exit_consumer_segments([line]):
            if guarded:
                hits.append((i, line.strip()))
                break
    return hits


# `source`/`.` line -> resolved path idioms this repo actually uses,
# resolved relative to the SOURCING file's own directory (`$(dirname
# "$0")`, `$(dirname "${BASH_SOURCE[0]}")`, `$HERE`, `$SCRIPT_DIR` -- all
# four are, by this repo's own convention, defined at the top of the
# sourcing script as that script's own directory) or to the repo root
# (`$REPO_ROOT`). Anything else (a dynamic/generated path like
# `"${SANDBOX_ENV}"`) is deliberately left unresolved -- see the
# TRANSITIVE `source`/`.` PRECONDITION docstring section above.
_SOURCE_LINE_RE = re.compile(r"^\s*(?:source|\.)\s+\S")
_SOURCE_DIRNAME_RE = re.compile(
    r'\$\(\s*dirname\s+"?\$(?:0|\{?BASH_SOURCE\[0\]\}?)"?\s*\)\s*/\s*(?P<rest>[^"\'\s]+)'
)
_SOURCE_SAME_DIR_VAR_RE = re.compile(r'\$\{?(?:HERE|SCRIPT_DIR)\}?/(?P<rest>[^"\'\s]+)')
_SOURCE_REPO_ROOT_VAR_RE = re.compile(r'\$\{?REPO_ROOT\}?/(?P<rest>[^"\'\s]+)')


def _resolve_one_level_sourced_path(line: str, sourcing_file: Path) -> Path | None:
    """Resolve a ``source``/``.`` line to the sourced file's path, for the
    small set of repo-local path idioms this repo actually uses. Returns
    ``None`` if *line* is not a ``source``/``.`` line, or its target
    expression does not match a recognized idiom (a dynamic/generated
    path -- left unresolved, not guessed at).
    """
    if not _SOURCE_LINE_RE.match(line):
        return None
    m = _SOURCE_DIRNAME_RE.search(line) or _SOURCE_SAME_DIR_VAR_RE.search(line)
    if m:
        return sourcing_file.parent / m.group("rest")
    m = _SOURCE_REPO_ROOT_VAR_RE.search(line)
    if m:
        return REPO_ROOT / m.group("rest")
    return None


def _sets_pipefail(text: str, *, file_path: Path | None = None) -> bool:
    """True if *text* sets ``pipefail`` directly, OR (when *file_path* is
    given) *text* ``source``s/``. ``s a repo-local file (resolved one
    level, see `_resolve_one_level_sourced_path`) that itself sets
    ``pipefail`` (gap #2 -- e.g. ``scripts/validate/03-cli.sh`` sourcing
    ``scripts/validate/lib.sh``).
    """
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if _PIPEFAIL_SET_RE.search(line):
            return True
    if file_path is None:
        return False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        sourced = _resolve_one_level_sourced_path(line, file_path)
        if sourced is None or not sourced.is_file():
            continue
        sourced_text = sourced.read_text(encoding="utf-8", errors="replace")
        for sline in sourced_text.splitlines():
            if sline.lstrip().startswith("#"):
                continue
            if _PIPEFAIL_SET_RE.search(sline):
                return True
    return False


def _tracked_shell_scripts() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.splitlines() if p]


# ── falsification controls (non-vacuity) ────────────────────────────────


def test_detector_catches_grep_q_pipeline() -> None:
    bad = 'nx daemon service status 2>&1 | grep -qiE "health.*ok|running" && healthy=1\n'
    assert _early_exit_consumer_hits(bad.splitlines(keepends=True)) == [
        (1, 'grep -qiE "health.*ok|running" && healthy=1')
    ]


def test_detector_catches_head_as_middle_pipeline_stage() -> None:
    """The 6zxfb shape: head is not the last stage."""
    bad = 'echo "$MEMORY_GET_OUT" | head -3 | sed \'s/^/  /\'\n'
    hits = _early_exit_consumer_hits(bad.splitlines(keepends=True))
    assert [h[0] for h in hits] == [1]
    assert hits[0][1].startswith("head -3")


def test_detector_catches_grep_dash_m() -> None:
    bad = 'nx daemon service status | grep -m1 "healthy"\n'
    assert _early_exit_consumer_hits(bad.splitlines(keepends=True)) == [
        (1, 'grep -m1 "healthy"')
    ]


def test_detector_catches_bare_head() -> None:
    bad = "nx catalog stats 2>&1 | head\n"
    hits = _early_exit_consumer_hits(bad.splitlines(keepends=True))
    assert [h[0] for h in hits] == [1]


def test_detector_ignores_tail() -> None:
    """tail reads to EOF -- it drains the pipe and cannot SIGPIPE the
    producer. Must never be flagged (over-broadening into tail was
    explicitly out of scope)."""
    benign = 'echo "$SEARCH_OUT" | tail -10 | sed \'s/^/  /\'\n'
    assert _early_exit_consumer_hits(benign.splitlines(keepends=True)) == []


def test_detector_ignores_grep_dash_c() -> None:
    """grep -c must scan every line to produce an accurate count -- it
    drains like tail, not an early-exit consumer."""
    benign = "uv run pytest --collect-only -q | grep -cE '::' || true\n"
    assert _early_exit_consumer_hits(benign.splitlines(keepends=True)) == []


def test_detector_ignores_non_piped_grep_q() -> None:
    """grep -q reading directly from a file/arg (no producer piping into
    it) carries zero SIGPIPE risk -- only a `|` immediately before the
    consumer creates the hazard."""
    benign = 'grep -q "in_progress" "$LOGFILE"\n'
    assert _early_exit_consumer_hits(benign.splitlines(keepends=True)) == []


def test_detector_ignores_or_true_guarded_pipeline() -> None:
    """The established display-only mitigation (release-sandbox.sh
    idiom): a trailing `|| true` discards the pipeline's exit status, so
    pipefail's promoted SIGPIPE failure can never abort the script here."""
    benign = "nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true\n"
    assert _early_exit_consumer_hits(benign.splitlines(keepends=True)) == []


def test_detector_ignores_comment_lines() -> None:
    benign = "# see: nx daemon service status | grep -qE 'healthy'\n"
    assert _early_exit_consumer_hits(benign.splitlines(keepends=True)) == []


def test_detector_catches_grep_quiet_long_form() -> None:
    """gap #3: `--quiet` is the long-form spelling of `-q` -- a long-form
    spelling escaping a short-form detector is a real hole."""
    bad = 'nx daemon service status | grep --quiet "healthy"\n'
    assert _early_exit_consumer_hits(bad.splitlines(keepends=True)) == [
        (1, 'grep --quiet "healthy"')
    ]


def test_detector_catches_grep_max_count_long_form() -> None:
    """gap #3: `--max-count=N` is the long-form spelling of `-mN`."""
    bad = 'nx daemon service status | grep --max-count=1 "healthy"\n'
    assert _early_exit_consumer_hits(bad.splitlines(keepends=True)) == [
        (1, 'grep --max-count=1 "healthy"')
    ]


def test_detector_catches_grep_dash_l() -> None:
    """gap #3: `grep -l` (list matching FILE NAMES) stops after the first
    match per input file -- same early-exit hazard shape as `-q`."""
    bad = 'nx catalog stats 2>&1 | grep -l "ERROR"\n'
    assert _early_exit_consumer_hits(bad.splitlines(keepends=True)) == [
        (1, 'grep -l "ERROR"')
    ]


def test_or_true_guarded_hits_detects_untracked_escape_hatch() -> None:
    """gap #1 falsification control: construct a genuine control-flow-
    gating pipe with `|| true` tacked on (the exact untracked-escape-hatch
    shape gap #1 exists to close) and confirm `_or_true_guarded_early_
    exit_hits` -- the function the sweep's ratchet enforcement is built
    on -- actually flags it as a guarded hit requiring registration."""
    bad = 'healthy="$(nx daemon service status | grep -qiE "healthy" || true)"\n'
    hits = _or_true_guarded_early_exit_hits(bad.splitlines(keepends=True))
    assert hits == [(1, 'grep -qiE "healthy" || true)"')]
    # And, critically, it is NOT also reported as an unguarded violation --
    # the two views are mutually exclusive by construction.
    assert _early_exit_consumer_hits(bad.splitlines(keepends=True)) == []


def test_if_condition_or_true_detects_always_true_bug() -> None:
    """gap #1 (compounding irony) falsification control: `if <pipe> ||
    true; then` is ALWAYS a bug -- the `|| true` makes the condition
    unconditionally succeed, not merely neutralize a pipefail hazard."""
    bad = 'if nx daemon service status | grep -qiE "healthy" || true; then\n'
    hits = _if_condition_or_true_bug_hits(bad.splitlines(keepends=True))
    assert [h[0] for h in hits] == [1]

    elif_bad = '  elif cmd | head -1 || : ; then\n'
    hits = _if_condition_or_true_bug_hits(elif_bad.splitlines(keepends=True))
    assert [h[0] for h in hits] == [1]


def test_if_condition_or_true_ignores_non_condition_or_true() -> None:
    """The legitimate display-only `|| true` shape (no `if`/`elif`, no
    `; then`) must never be flagged by the always-true-bug detector --
    only the actual bug shape is in scope."""
    benign = "nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true\n"
    assert _if_condition_or_true_bug_hits(benign.splitlines(keepends=True)) == []


def test_if_condition_or_true_ignores_if_without_early_exit_consumer() -> None:
    """`grep -c` (never early-exit) inside an `if ... || true; then` is
    not this bug -- `-c` drains the pipe like `tail`, so there is no
    early-exit consumer to have been neutralized in the first place."""
    benign = 'if cmd | grep -c pattern || true; then\n'
    assert _if_condition_or_true_bug_hits(benign.splitlines(keepends=True)) == []


def test_sets_pipefail_recognizes_combined_flag_forms() -> None:
    assert _sets_pipefail("set -o pipefail\n")
    assert _sets_pipefail("set -eo pipefail\n")
    assert _sets_pipefail("set -Eeuo pipefail\n")
    assert _sets_pipefail("set -uo pipefail\n")
    assert not _sets_pipefail("set -eu\n")
    assert not _sets_pipefail("# set -o pipefail (example only)\n")


def test_sets_pipefail_detects_transitive_source_of_pipefail_lib() -> None:
    """gap #2: scripts/validate/03-cli.sh and 04-hooks.sh set no pipefail
    directly, but both `source "$(dirname "$0")/lib.sh"`, which does. One-
    level source resolution must recognize both as pipefail-set."""
    for rel in ("scripts/validate/03-cli.sh", "scripts/validate/04-hooks.sh"):
        path = REPO_ROOT / rel
        text = path.read_text(encoding="utf-8")
        # Confirm the premise: the file does NOT declare pipefail itself --
        # the whole point of this test is that only the transitively-
        # sourced lib.sh does.
        assert not any(
            _PIPEFAIL_SET_RE.search(line)
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ), f"{rel} now sets pipefail directly -- update this test's premise"
        assert not _sets_pipefail(text), (
            f"{rel} should not be recognized as pipefail-set WITHOUT "
            "source resolution -- update this test's premise"
        )
        assert _sets_pipefail(text, file_path=path), (
            f"{rel} sources scripts/validate/lib.sh (which sets pipefail "
            "via `set -uo pipefail`) but is not recognized as "
            "pipefail-set through one-level source resolution"
        )


def test_sets_pipefail_source_resolution_does_not_guess_dynamic_paths() -> None:
    """A `source "${SOME_DYNAMIC_VAR}"` line (generated/runtime path, not
    one of the recognized repo-local idioms) must be left unresolved, not
    guessed at -- confirms `_resolve_one_level_sourced_path` returns None
    rather than raising or matching something unintended."""
    text = 'source "${SANDBOX_ENV}"\nnx catalog stats\n'
    fake_path = REPO_ROOT / "scripts" / "rdr152-sandbox" / "prod-copy.sh"
    assert not _sets_pipefail(text, file_path=fake_path)


def test_scope_precondition_a_script_with_the_hazard_shape_but_no_pipefail_is_not_flagged() -> None:
    """Structural proof of the scope decision itself: the exact i66g4/
    wbeyi hazard SHAPE, in a script that never sets pipefail, must not be
    reported by the full per-file pipeline below -- because the hazard
    genuinely cannot fire there (a mid-pipe failure is silently absorbed
    without pipefail). This is what keeps the lint from "flagging files
    where the hazard cannot occur" per the scope design note."""
    text = (
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'nx daemon service status 2>&1 | grep -qiE "health.*ok|running" && healthy=1\n'
    )
    assert not _sets_pipefail(text)
    # (the hazard-shape detector itself is pipefail-agnostic by design --
    # the precondition is applied by the caller, exercised in the full
    # sweep test below via the real file set)


# ── ratchet exemption set ───────────────────────────────────────────────
#
# Format: (relative/path.sh, content-anchor) -> each block below documents
# WHY the entry is exempted rather than fixed in this pass. This set may
# only SHRINK (a line gets fixed and its entry removed) or grow with a
# new, individually-documented entry AND a conscious bump of
# `_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` in the same diff -- see
# `test_mode_declarations_are_explicit.py`'s ratchet for the pattern this
# mirrors. A bare growth with no rationale comment is exactly the
# grandfathering this mechanism exists to prevent.
#
# CONTENT-KEYED, not line-keyed (nexus-vkpr3): the second tuple element
# is the exempted line's own stripped source text PLUS its nearest
# preceding non-blank line (the MANDATORY two-line minimum -- see
# `tests._lint_line_anchor`'s MANDATORY TWO-LINE MINIMUM section for
# why a single line, even one unique in today's snapshot, is not
# enough), resolved to its CURRENT line number by
# `tests._lint_line_anchor.resolve_anchor` on every run. Before this
# conversion, this exact set was the incident that filed nexus-vkpr3: two
# unrelated same-day insertions into rehearse_package_upgrade.sh (+11-13
# lines, then +17 more) shifted all twelve of that file's entries, and
# the recovery only avoided silently mis-exempting an unreviewed site
# because it retargeted every entry BY CONTENT rather than by an
# arithmetic line offset. The per-block comments below carry only a
# one-sentence summary of any pre-conversion retargeting history (the
# original sprawling "Retargeted ... shifting every site below by +N"
# paragraphs, one per historical edit, are gone) plus each block's
# substantive exemption reason -- an insertion above any of these
# entries no longer requires retargeting anything, since the anchor
# tracks the content, not the number.
#
# nexus-wbeyi remediation sweep (2026-08-10): every entry below is a REAL,
# confirmed instance of the defect class (not a false positive) that was
# deliberately NOT hand-fixed in this pass because the containing script
# requires live infrastructure (a running Docker rehearsal harness, a
# signed macOS binary, a live nx service, a tmux-driven interactive Claude
# Code session) to safely verify the fix did not change behavior --
# blind-editing pipefail-gated control flow in the release-battery
# rehearsal scripts (tests/e2e/migration-rehearsal/*.sh, the pre-tag
# REQUIRED gate per AGENTS.md "Engine-service release") without executing
# the actual harness risks silently breaking a release gate, which is a
# worse outcome than a tracked, rationale-carrying exemption. Follow-up:
# nexus-wbeyi itself already tracks the two local-index-memory-gate.sh
# sites (owned by a concurrent agent in the authoring session, reported
# not fixed here per that session's explicit hand-off boundary); the
# remainder should be split into a dedicated remediation bead scoped to
# "run tests/e2e/migration-rehearsal/run.sh --shakeout as the fix's own
# verification" so each transform is checked against a real rehearsal
# rather than reviewed by inspection alone.
_PIPEFAIL_EARLY_EXIT_EXEMPT: frozenset[tuple[str, tuple[str, ...]]] = frozenset(
    {
        # --- tests/e2e/migration-rehearsal/*.sh (138 entries): Docker-
        # rehearsal-only, pre-tag release battery (AGENTS.md
        # "Engine-service release"). Every line matches the *exact*
        # wbeyi shape (`nx ... | grep -q...` gating control flow, or
        # `head -N` value-extraction inside a bare `VAR=$(...)` that DOES
        # propagate through errexit -- verified empirically: `x=$(false)`
        # under `set -e` aborts; only `local x=$(...)` swallows it, and
        # none of these sites use `local`). Not hand-fixed in this pass:
        # blind-editing pipefail-gated control flow across the release-
        # battery rehearsal scripts without executing the actual Docker
        # harness (`tests/e2e/migration-rehearsal/run.sh --shakeout`)
        # risks silently breaking a release gate, which is worse than a
        # tracked, rationale-carrying exemption. Follow-up: split into a
        # dedicated remediation bead scoped to "run --shakeout as the
        # fix's own verification" so each transform is checked against a
        # real rehearsal rather than reviewed by inspection alone.
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('for i in $(seq 1 30); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('if nx memory put "comprehensive shakeout $MARK widget sprocket note" -p ddshakeout -t "note-$MARK" --tags rehearsal >"$DD" 2>&1; then', 'if nx memory search "$MARK" 2>/dev/null | grep -q "$MARK"; then ok "T2 memory put+search round-trip"')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('| nx store put - -c rehearsal -t "shakeout-$MARK" --tags rehearsal >"$DD" 2>&1; then', 'if nx search "widgets and sprockets for retrieval" --corpus knowledge -m 5 2>/dev/null | grep -q "$MARK"; then')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('# T3 collection listing reflects the new knowledge collection', 'if nx collection list 2>/dev/null | grep -qi "knowledge"; then ok "nx collection list shows the knowledge collection"')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('# signed bundle `nx init` extracted (<config>/pg-bundle/**/bin/psql).', 'PSQL="$(find "$HOME/.config/nexus/pg-bundle" -type f -name psql 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('if grep -qiE "database is locked|deadlock|lock.*timeout|HTTP 5[0-9][0-9]|connection refused|could not connect|MEMFAIL|STOREFAIL|BURSTFAIL" "$errlog"; then', 'bad "concurrency errors under tandem load"; note "$(grep -iE \'locked|deadlock|timeout|5[0-9][0-9]|refused|FAIL\' "$errlog" | sort | uniq -c | head -6 | tr \'\\n\' \' \')"')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('# Assertion 3: service healthy after the storm (no CPU-peg/stall/crash).', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running|status.*ok"; then ok "service healthy after stress"')),
        ("tests/e2e/migration-rehearsal/rehearse.sh", ('if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running|status.*ok"; then ok "service healthy after stress"', 'else bad "service unhealthy after stress"; note "$(nx daemon service status 2>&1 | head -3)"; fi')),
        ("tests/e2e/migration-rehearsal/rehearse_acquire.sh", ('for _ in $(seq 1 10); do', 'if nx search "acquire-gate probe pgvector" 2>&1 | grep -qi "acquire-gate-probe"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_acquire.sh", ('for _ in $(seq 1 30); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_cold.sh", ('[ "$GU_RC" = 0 ] && ok "nx guided-upgrade exited 0" || bad "nx guided-upgrade exited $GU_RC"', 'printf \'%s\' "$GU_OUT" | grep -q "Migration VERIFIED and unlocked" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_cold.sh", ('for _ in $(seq 1 30); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('for _ in $(seq 1 "$tries"); do', 'if "$REAL_NX" daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('say "Stage 4 — package upgrade to the working tree (the ONLY manual step in the story)"', 'WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('printf \'%s\\n\' "$DRY_OUT" | sed \'s/^/       /\'', 'if printf \'%s\' "$DRY_OUT" | grep -q "rung \'substrate-etl\' pending"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('# P4.2, live: the everyday output must not advertise a verb demoted out of --help.', 'if printf \'%s\' "$UP_OUT" | grep -qE \'nx (guided-upgrade|migrate-to-service|migration-audit)\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('[ "$UP2_RC" = 0 ] && ok "the second nx upgrade exited 0" || bad "the second nx upgrade exited $UP2_RC"', 'if printf \'%s\' "$UP2_OUT" | grep -q "rung \'substrate-etl\' converged and verified"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('printf \'%s\\n\' "$DOC_OUT" | grep -iE \'upgrade ladder|chunk-id era\' | sed \'s/^/       /\' || true', 'if printf \'%s\' "$DOC_OUT" | grep -qiE \'pending upgrade rung\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('fi', 'if printf \'%s\' "$DOC_OUT" | grep -qE \'nx (guided-upgrade|migrate-to-service|migration-audit)\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ("# this leg's first run pass over zero migrated collections.", 'if ! printf \'%s\' "$DOC_OUT" | grep -qi \'chunk-id era\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('bad "nx doctor printed no chunk-id era census row — the census did not run, so the debt assertion below would pass vacuously"', 'elif printf \'%s\' "$DOC_OUT" | grep -qi \'legacy chunk ids\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_era_hop.sh", ('fi', 'GOT_VER="$(nx --version 2>&1 | grep -oE \'[0-9]+\\.[0-9]+\\.[0-9]+\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('# 8. Service healthy after the full-stack run.', 'nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && ok "service healthy after full-stack run" || bad "service unhealthy"')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"', 'claude --version >/dev/null 2>&1 && ok "claude CLI installed ($(claude --version 2>&1 | head -1))" || bad "claude CLI missing"')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('healthy=0', 'for i in $(seq 1 30); do nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && { healthy=1; break; }; sleep 2; done')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('# signed bundle `nx init` extracted (<config>/pg-bundle/**/bin/psql).', 'PSQL="$(find "$HOME/.config/nexus/pg-bundle" -type f -name psql 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('authout="$(claude -p \'Reply with exactly the token AUTHOK and nothing else.\' --dangerously-skip-permissions 2>&1)"', 'if printf \'%s\' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('if printf \'%s\' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"', 'else bad "claude -p auth failed — cannot drive the MCP/extraction"; note "$(printf \'%s\' "$authout" | head -3 | tr \'\\n\' \' \')"; say "ABORT (no claude auth)"; printf \'REHEARSAL FAILED\\n\'; exit 1; fi')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('note "claude workload tail: $(printf \'%s\' "$wlout" | tail -3 | tr \'\\n\' \' \' | cut -c1-280)"', 'printf \'%s\' "$wlout" | grep -q "WORKLOADDONE" && ok "MCP workload completed (claude drove the tools)" || bad "MCP workload did not finish cleanly"')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('sleep 3', 'if nx collection list 2>/dev/null | grep -qi "knowledge"; then ok "store_put materialized a knowledge collection (MCP tools really executed)"; STORED_OK=1')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('if nx collection list 2>/dev/null | grep -qi "knowledge"; then ok "store_put materialized a knowledge collection (MCP tools really executed)"; STORED_OK=1', 'else bad "no knowledge collection — claude did NOT actually call store_put (MCP connect / allowedTools issue)"; note "$(nx collection list 2>&1 | head -3 | tr \'\\n\' \' \')"; STORED_OK=0; fi')),
        ("tests/e2e/migration-rehearsal/rehearse_fullstack.sh", ('# 3c. nx_answer produced a grounded composed answer (from the workload).', 'printf \'%s\' "$wlout" | grep -qiE "widget|sprocket|gadget" && ok "nx_answer (MCP) returned a grounded composed answer" || note "nx_answer answer not evident in workload output"')),
        ("tests/e2e/migration-rehearsal/rehearse_hole_punch.sh", ('for _ in $(seq 1 30); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_hole_punch.sh", ('[ "$GU_RC" = 0 ] && ok "nx guided-upgrade exited 0" || { bad "nx guided-upgrade exited $GU_RC"; say "ABORT"; exit 1; }', 'printf \'%s\' "$GU_OUT" | grep -q "Migration VERIFIED and unlocked" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('for i in $(seq 1 "$tries"); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('say "Stage 4 — PACKAGE upgrade only (uv pip install --reinstall <$UPGRADE_TARGET_LABEL wheel>)"', 'WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('if [ -n "$TARGET_RELEASE" ]; then', 'GOT_CLIENT_VER="$(nx --version 2>&1 | grep -oE \'[0-9]+\\.[0-9]+\\.[0-9]+\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('# engine-naming error (branch b) — never a bare/opaque failure.', 'if printf \'%s\' "$SKEW_PUT" | grep -qiE "converg|engine"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('elif [ "$SKEW_GET_RC" != 0 ]; then', 'if printf \'%s\' "$SKEW_GET" | grep -qiE "converg|engine"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('fi', 'elif ! printf \'%s\' "$SKEW_GET" | grep -q "$SKEW_MARKER"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('[ "$RS_RC" = 0 ] && ok "nx daemon restart-stale exited 0" || bad "nx daemon restart-stale exited $RS_RC"', 'printf \'%s\' "$RS_OUT" | grep -q "converged engine" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('# did nothing on this box before the /proc fallback -- assert it ran.', 'printf \'%s\' "$RS_OUT" | grep -q "$SKEW_SHAPE" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('POST_ID="$(printf \'%s\\n\' "$POST_PUT" | sed -n \'s/^Stored: \\([0-9a-fA-F-]\\{8,\\}\\).*/\\1/p\' | tail -1)"', 'if [ -n "$POST_ID" ] && nx scratch get "$POST_ID" 2>/dev/null | grep -q "$POST_MARKER"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('PRE_CONTENT="$(nx scratch get "$PRE_ID" 2>&1)"', 'if printf \'%s\' "$PRE_CONTENT" | grep -q "$MARKER"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ("printf '       [status --json]\\n'", "nx daemon service status --json 2>&1 | head -40 | sed 's/^/       /'")),
        ("tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh", ('fi', 'GOT_VER="$(nx --version 2>&1 | grep -oE \'[0-9]+\\.[0-9]+\\.[0-9]+\' | head -1)"')),
        # gap-8/gap-15 (T2 [22511]): the former health-poll site here
        # (`nx daemon service status | grep -qiE ... && { healthy=1;
        # break; }`) was fixed by appending `|| true` and moved to the
        # `_PIPEFAIL_OR_TRUE_SITES` guarded ratchet below (13 entries ->
        # 12). Pre-nexus-vkpr3 this block also carried a multi-paragraph
        # line-shift retargeting history (the `-e` addition, then
        # nexus-l8xnz's Phase F header growth); dropped, since content
        # anchors make retargeting unnecessary going forward.
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('out="$("$@" 2>&1)" || true', 'if printf \'%s\' "$out" | grep -qiE "$want"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('UNMINTED_OUT="$(NX_T1_SESSION=unminted-probe nx scratch list 2>&1)" || true', 'if printf \'%s\' "$UNMINTED_OUT" | grep -q "Traceback"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('printf \'%s\\n\' "$UNMINTED_OUT" | sed \'s/^/       | /\' | tail -6', 'elif printf \'%s\' "$UNMINTED_OUT" | grep -q "nx daemon service start"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('printf \'# Shakeout probe\\n\\nThe amaranthine zeppelin quotient verifies retrieval.\\n\' > "$PROBE_MD"', 'if nx store put "$PROBE_MD" --collection knowledge__shakeout --title "shakeout-probe" --tags shakeout 2>&1 | grep -q "Stored:"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('sleep 2', 'nx search "amaranthine zeppelin quotient" --corpus knowledge -m 2 2>/dev/null | grep -q "shakeout-probe" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('DEL_OUT="$(yes | nx store delete --title "shakeout-probe" --collection knowledge__shakeout 2>&1)" || true', 'if printf \'%s\' "$DEL_OUT" | grep -qiE "delet"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('fi', 'nx search "amaranthine zeppelin quotient" --corpus knowledge -m 1 2>/dev/null | grep -q "shakeout-probe" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('PLAN_SEED_OUT="$(nx plan reseed 2>&1)" || true  # gap-15: content-checked below, not rc-gated', 'printf \'%s\' "$PLAN_SEED_OUT" | grep -qE "Seeded [0-9]+ new builtin row" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('PLAN_LIST_OUT="$(nx plan list 2>&1)" || true  # gap-15: content-checked below, not rc-gated', 'if printf \'%s\' "$PLAN_LIST_OUT" | grep -qiE "builtin"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('# Catalog + collections + taxonomy + doctor surfaces', 'nx catalog stats 2>/dev/null | grep -qE "Documents:" && ok "catalog stats" || bad "catalog stats"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('DOCTOR_OUT="$(nx doctor 2>&1)" || true  # gap-15: content (traceback presence) checked below, not rc-gated', 'printf \'%s\\n\' "$DOCTOR_OUT" | grep -q "Traceback" && bad "doctor raised a traceback" || ok "doctor runs traceback-free"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('fi', 'nx search "flux capacitor array" --corpus docs -m 2 2>/dev/null | grep -qi "doc" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"', 'claude --version >/dev/null 2>&1 && ok "claude CLI installed ($(claude --version 2>&1 | head -1))" || bad "claude CLI missing"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('healthy=0', 'for i in $(seq 1 30); do nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && { healthy=1; break; }; sleep 2; done')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('LADDER_OUT="$(nx doctor 2>&1)"', 'if printf \'%s\' "$LADDER_OUT" | grep -qi "no pending rung"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('if printf \'%s\' "$LADDER_OUT" | grep -qi "no pending rung"; then', 'ok "upgrade ladder converged at init ($(printf \'%s\' "$LADDER_OUT" | grep -io \'no pending rung[a-z ]*([0-9]* registered)\' | head -1))"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('if [ -z "$RUN_LOG" ]; then', 'RUN_LOG="$(find "$HOME/.config/nexus/logs" -maxdepth 1 -name \'index-*.log\' -newer "$MARKER_FILE" 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('# nexus-e9ru2 class: T3-only assertions miss a broken catalog write).', 'if nx catalog list 2>/dev/null | grep -q "$MARK\\|big_filler\\|small_sentinel\\|notes"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('# identifier — so a failure here means retrieval is actually broken.', 'if nx search "widgets and sprockets" --corpus code -c -m 5 2>/dev/null | grep -q "small_sentinel"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('fi', 'if nx search "$MARK widgets sprockets" --corpus docs -c -m 5 2>/dev/null | grep -q "$MARK"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('if [ "${PDF_INDEXED:-0}" = 1 ]; then', 'if nx search "shakeout-e2e pdf sentinel $MARK" --corpus docs -c -m 5 2>/dev/null | grep -q "$MARK"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('authout="$(claude -p \'Reply with exactly the token AUTHOK and nothing else.\' --dangerously-skip-permissions 2>&1)"', 'if printf \'%s\' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('if printf \'%s\' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"', 'else bad "claude -p auth failed — cannot drive the MCP tool surface"; note "$(printf \'%s\' "$authout" | head -3 | tr \'\\n\' \' \')"; say "ABORT (no claude auth)"; printf \'SHAKEOUT-E2E FAILED\\n\'; exit 1; fi')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('note "claude workload tail: $(printf \'%s\' "$wlout" | tail -4 | tr \'\\n\' \' \' | cut -c1-320)"', 'printf \'%s\' "$wlout" | grep -q "WORKLOADDONE" && ok "MCP workload completed (claude drove the tools)" || bad "MCP workload did not finish cleanly"')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('sleep 2', 'if nx collection list 2>/dev/null | grep -qi "knowledge"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('fi', 'printf \'%s\' "$wlout" | grep -qiE "widget|sprocket|gadget" && ok "MCP search/nx_answer output is grounded (widget/sprocket/gadget present)" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('|| bad "MCP workload output does not mention widget/sprocket/gadget — not grounded"', 'printf \'%s\' "$wlout" | grep -q "$MARK" && ok "MCP query tool retrieved the Step 2 repo corpus sentinel ($MARK) — document-level catalog-aware retrieval of the REAL corpus works" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", ('|| bad "MCP query tool output does not contain the Step 2 corpus sentinel ($MARK) — the query leg of Step 4 is unproven"', 'nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && ok "service healthy after the full shakeout" || bad "service unhealthy after the shakeout"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('fi', 'GOT_VER="$(nx --version 2>&1 | grep -oE \'[0-9]+\\.[0-9]+\\.[0-9]+\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('say "Stage 4 — package-upgrade to the working tree (uv pip install --reinstall <worktree wheel>)"', 'WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('|| bad "nx doctor exited 0 on a stranded box — the fatal check did not gate the exit code"', 'printf \'%s\' "$DOCTOR_OUT" | grep -q "unmigrated pre-PG data" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('&& ok "message names the pre-PG data" || bad "message missing \'unmigrated pre-PG data\'"', 'printf \'%s\' "$DOCTOR_OUT" | grep -q "conexus==$PIN_RELEASE" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('|| bad "message missing the exact pin \'conexus==$PIN_RELEASE\'"', 'printf \'%s\' "$DOCTOR_OUT" | grep -q \'run `nx upgrade` there\' \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('|| bad "message missing the exact verb clause \'run \\`nx upgrade\\` there\'"', 'printf \'%s\' "$DOCTOR_OUT" | grep -q "upgrade back to this version" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('|| bad "message missing the third-hop clause"', 'printf \'%s\' "$DOCTOR_OUT" | grep -qE \'\\[stranded-install\\]\' \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('|| bad "nx init exited 0 on a stranded box"', 'printf \'%s\' "$INIT_OUT" | grep -q "conexus==$PIN_RELEASE" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('# perfectly silent box.', 'if printf \'%s\' "$FRESH_OUT" | grep -qE \'\\[stranded-install\\]|This install carries unmigrated\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('fi', 'printf \'%s\' "$FRESH_OUT" | grep -qi "stranded pre-pg install: no unmigrated" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('fi', 'GOT_VER2="$(nx --version 2>&1 | grep -oE \'[0-9]+\\.[0-9]+\\.[0-9]+\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('# the retrievability assert in Stage 10b below.', 'printf \'%s\\n%s\' "$INIT_PIN_OUT" "$UPGRADE_OUT" | grep -qE "rung \'substrate-etl\'.*(converged and verified|verified)" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('SEARCH_OUT="$(nx search "onnx chunk" 2>&1)"', 'if printf \'%s\' "$SEARCH_OUT" | grep -q "onnx chunk"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('POST_DOCTOR="$(nx doctor 2>&1)"', 'printf \'%s\' "$POST_DOCTOR" | grep -qi "upgrade ladder: no pending rungs" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('&& ok "upgrade ladder reports no pending rungs post-migration" \\', '|| note "doctor\'s ladder-summary wording differs — see raw output above if this matters: $(printf \'%s\' "$POST_DOCTOR" | grep -i \'upgrade ladder\' | head -3)"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('|| note "doctor\'s ladder-summary wording differs — see raw output above if this matters: $(printf \'%s\' "$POST_DOCTOR" | grep -i \'upgrade ladder\' | head -3)"', 'printf \'%s\' "$POST_DOCTOR" | grep -qi "migration reports" \\')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('printf \'%s\' "$POST_DOCTOR" | grep -qi "migration reports" \\', '&& note "migration-reports check: $(printf \'%s\' "$POST_DOCTOR" | grep -i \'migration reports\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('fi', 'GOT_VER3="$(nx --version 2>&1 | grep -oE \'[0-9]+\\.[0-9]+\\.[0-9]+\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('printf \'%s\\n\' "$HOP3_DOCTOR_OUT" | grep -i stranded | sed \'s/^/       /\'', 'if printf \'%s\' "$HOP3_DOCTOR_OUT" | grep -qE \'\\[stranded-install\\]|This install carries unmigrated\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('HOP3_INIT_OUT="$(nx init --yes 2>&1)"; HOP3_INIT_RC=$?', 'if printf \'%s\' "$HOP3_INIT_OUT" | grep -qE \'\\[stranded-install\\]|This install carries unmigrated|Refusing to initialize\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('HOP3_CLI_OUT="$(nx doctor --help 2>&1)"', 'if printf \'%s\' "$HOP3_CLI_OUT" | grep -qE \'\\[stranded-install\\]\'; then')),
        ("tests/e2e/migration-rehearsal/rehearse_stranded.sh", ('for i in $(seq 1 "$tries"); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        # run.sh (7 entries): the CHASH_WINDOW leg's own wheel-pick site
        # (deleted with the leg, nexus-lgdel.l2) aside, the remaining
        # sites are two `VAR="$(... | head -1)"` version extractions
        # (self version from pyproject, REQUIRED_ENGINE_VERSION from the
        # source), a `sort -V | head -1` lower-of-two-versions pick
        # (`sort` cannot emit before consuming all input, so `head` can
        # never truncate a still-writing producer here), and a wheel-pick
        # `cp "$(ls -t dist/conexus-*.whl | head -1)"` inside the
        # stage_wheel() seam. Pre-nexus-vkpr3 this block carried a long
        # line-shift retargeting history across several unrelated run.sh
        # edits (nexus-c00dw's lease wiring, the 7.15.0 engine-comparison
        # walk, nexus-mfage's --artifacts option block); dropped, since
        # content anchors make retargeting unnecessary going forward.
        ("tests/e2e/migration-rehearsal/run.sh", ("| sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\\([0-9]*\\), *\\([0-9]*\\), *\\([0-9]*\\)).*/\\1.\\2.\\3/p' \\", '| head -1')),
        ("tests/e2e/migration-rehearsal/run.sh", ('local self_version cur_engine rel tuple', 'self_version="$(sed -n \'s/^version = "\\(.*\\)"/\\1/p\' "$(pwd)/pyproject.toml" | head -1)"')),
        ("tests/e2e/migration-rehearsal/run.sh", ('self_version="$(sed -n \'s/^version = "\\(.*\\)"/\\1/p\' "$(pwd)/pyproject.toml" | head -1)"', 'cur_engine="$(sed -n \'s/^REQUIRED_ENGINE_VERSION[^(]*(\\([0-9]*\\), *\\([0-9]*\\), *\\([0-9]*\\)).*/\\1.\\2.\\3/p\' "$(pwd)/src/nexus/engine_version.py" | head -1)"')),
        ("tests/e2e/migration-rehearsal/run.sh", ('[ "$tuple" = "$cur_engine" ] && continue', 'if [ "$(printf \'%s\\n%s\\n\' "$tuple" "$cur_engine" | sort -V | head -1)" = "$tuple" ]; then')),
        ("tests/e2e/migration-rehearsal/run.sh", ('tuple="$(git show "v$rel:src/nexus/engine_version.py" 2>/dev/null \\', '| sed -n \'s/^REQUIRED_ENGINE_VERSION[^(]*(\\([0-9]*\\), *\\([0-9]*\\), *\\([0-9]*\\)).*/\\1.\\2.\\3/p\' | head -1)"')),
        # nexus-og52j: stage_wheel/stage_native moved verbatim out of run.sh
        # into lib/stage_artifacts.sh (a sourced file) so a unit test could
        # drive stage_native directly; this anchor followed it.
        ("tests/e2e/migration-rehearsal/lib/stage_artifacts.sh", ('if [ -n "$ARTIFACTS" ]; then cp "$ARTIFACT_WHEEL" "$1/"', 'else cp "$(ls -t dist/conexus-*.whl | head -1)" "$1/"; fi   # keep real PEP 427 name')),
        # --- tests/e2e/mac-signed-binary-gate.sh (7 entries): needs an
        # actually-signed macOS binary + `spctl`/`codesign` on real macOS
        # to safely verify a rewrite of the signature-inspection logic.
        ("tests/e2e/mac-signed-binary-gate.sh", ('SIGINFO="$(codesign -dv --verbose=4 "$BIN" 2>&1 || true)"', 'if echo "$SIGINFO" | grep -q "TeamIdentifier=" && ! echo "$SIGINFO" | grep -q "TeamIdentifier=not set"; then')),
        ("tests/e2e/mac-signed-binary-gate.sh", ('if echo "$SIGINFO" | grep -q "TeamIdentifier=" && ! echo "$SIGINFO" | grep -q "TeamIdentifier=not set"; then', 'ok "Developer ID signed ($(echo "$SIGINFO" | grep -o \'TeamIdentifier=[^ ]*\' | head -1))"')),
        ("tests/e2e/mac-signed-binary-gate.sh", ('# this gate is vacuous — the JNI loads would succeed for the wrong reason.', 'if echo "$SIGINFO" | grep -qE "flags=.*runtime"; then')),
        ("tests/e2e/mac-signed-binary-gate.sh", ('ENTS="$(codesign -d --entitlements - --xml "$BIN" 2>/dev/null || true)"', 'if echo "$ENTS" | grep -q "com.apple.security.cs.disable-library-validation"; then')),
        ("tests/e2e/mac-signed-binary-gate.sh", ('# "Killed: 9" further down.', 'if spctl -a -t exec -vv "$BIN" 2>&1 | tee "$WORK/spctl.out" | grep -q "accepted"; then')),
        ("tests/e2e/mac-signed-binary-gate.sh", ('if spctl -a -t exec -vv "$BIN" 2>&1 | tee "$WORK/spctl.out" | grep -q "accepted"; then', 'ok "spctl accepted ($(grep -o \'source=.*\' "$WORK/spctl.out" | head -1))"')),
        ("tests/e2e/mac-signed-binary-gate.sh", ('bad "LIBRARY VALIDATION REFUSAL detected in the smoke log — this IS nexus-2oh5q:', '$(grep -iE \'not valid for use in process|library validation|UnsatisfiedLinkError|code signature.*invalid\' "$SMOKE_LOG" | head -3)"')),
        # --- service/native-smoke.sh (8 entries): native-image
        # release-only smoke; needs a real GraalVM native build to
        # safely verify a rewrite. Pre-nexus-vkpr3 this block carried a
        # line-shift retargeting history across three unrelated edits
        # (nexus-cm5km's smoke-probe extraction, nexus-9gaj7's launch-line
        # comment, nexus-ft04v.16's embed-probe growth); dropped, since
        # content anchors make retargeting unnecessary going forward.
        ("service/native-smoke.sh", ('echo "version: $VER"', 'echo "$VER" | grep -qE \'"schema_changeset_count":[1-9]\' || { echo "FAIL: migration did not apply"; tail -40 /tmp/native-smoke-svc.log; exit 1; }')),
        ("service/native-smoke.sh", ('PUT_RESP=$(curl -fsS "${T1[@]}" "${J[@]}" -X POST -d \'{"id":"native-smoke-t1-id","session_id":"native-smoke-t1","content":"t1 native smoke","tags":"","flagged":false}\' "$U/v1/t1/put")', 'echo "$PUT_RESP" | grep -q \'"id"\' && echo "  ok   t1/put (INSERT) -> 200" || { echo "  FAIL t1/put -> $PUT_RESP"; fail=1; }')),
        ("service/native-smoke.sh", ('rm -rf "$T1_PY_TMPDIR"', 'if echo "$PY_OUT" | grep -q "^OK$"; then')),
        ("service/native-smoke.sh", ('rm -rf "$T2_PY_TMPDIR"', 'if echo "$PY_OUT" | grep -q "^OK$"; then')),
        ("service/native-smoke.sh", ('if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log; then', 'echo "FAIL: native runtime error in service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log | head; fail=1')),
        ("service/native-smoke.sh", ('else', 'echo "  FAIL voyage mode not selected:"; grep embedding_mode_banner /tmp/native-smoke-voyage.log | head; fail=1')),
        ("service/native-smoke.sh", ('else', 'echo "  FAIL egress proxy not configured from HTTPS_PROXY:"; grep egress_proxy /tmp/native-smoke-voyage.log | head; fail=1')),
        ("service/native-smoke.sh", ('if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-voyage.log; then', 'echo "FAIL: native runtime error in voyage-mode service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-voyage.log | head; fail=1')),
        # --- service/linux-native-verify.sh:43 (1 entry): a GENUINE
        # FALSE POSITIVE, not a "needs live infra" deferral -- the
        # matched pipe (`native-image --version | head -1`) sits inside a
        # single-quoted `bash -c '...'` string passed as the entrypoint
        # command to `docker run` (line 41). That nested bash starts a
        # FRESH shell inside the container with its own `set -e` (line 42
        # of the heredoc body) and never inherits this outer script's
        # `set -uo pipefail` (line 8) -- shell options do not cross a
        # `bash -c` invocation boundary. The hazard this lint exists to
        # catch genuinely cannot fire here. Left unfixed deliberately
        # (fixing a non-bug is needless churn) and documented as a known
        # lint LIMITATION: the file-level `_sets_pipefail` scan cannot
        # distinguish a nested `bash -c '...'` / heredoc execution
        # context from the top-level script body. Verified this is
        # isolated: no other exempted file in this set uses `bash -c` or
        # `sh -c` (checked at authoring time via
        # `grep -ln 'bash -c\\|sh -c'` across every file contributing to
        # this exemption set).
        ("service/linux-native-verify.sh", ('set -e', 'echo "=== container: $(native-image --version | head -1) ==="')),
        # --- tests/e2e/fresh-install-mvv.sh (2 entries): release-battery
        # gate (AGENTS.md "Cutting a release" step 1b); needs a real
        # fresh-HOME wheel install to safely verify a rewrite of its
        # dist-info sniffing. The former third entry (the `--version`
        # banner sniff) was FIXED rather than exempted -- it needed no
        # live infra, being a plain capture-then-parameter-expansion.
        # Pre-nexus-vkpr3 this block was retargeted once (nexus-gqrg0
        # round 2, an unrelated earlier section growing +72 lines);
        # dropped, since content anchors make retargeting unnecessary.
        ("tests/e2e/fresh-install-mvv.sh", ('SITE_PACKAGES="$("$PROBE_PYTHON" -c \'import sysconfig; print(sysconfig.get_path("purelib"))\')"', 'MCP_DIST_INFO="$(find "$SITE_PACKAGES" -maxdepth 1 -name \'mcp-*.dist-info\' 2>/dev/null | head -1)"')),
        ("tests/e2e/fresh-install-mvv.sh", ('fi', 'CONEXUS_DIST_INFO="$(find "$SITE_PACKAGES" -maxdepth 1 -name \'conexus-*.dist-info\' 2>/dev/null | head -1)"')),
        # --- tests/e2e/local-index-memory-gate.sh (1 entry): owned by a
        # concurrent agent in the authoring session (nexus-wbeyi itself)
        # -- reported to that hand-off, not fixed here. This is a
        # SECOND, previously-unreported site distinct from the
        # already-fixed line 555: a `| head -1` inside a bare
        # `VAR=$(...)` assignment (propagates through errexit) found by
        # this lint's own authoring sweep.
        ("tests/e2e/local-index-memory-gate.sh", ('if [ -z "$RUN_LOG" ]; then', 'RUN_LOG="$(find "$ISOLATED_CONFIG_DIR/logs" -maxdepth 1 -name \'index-*.log\' -newer "$MARKER_FILE" 2>/dev/null | head -1)"')),
        # --- tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh
        # (10 entries, nexus-z0ylb): the CANDIDATE-MIGRATION rehearsal --
        # a locally-built candidate's Liquibase walk over a POPULATED
        # store, hand-swapped in against a running floor engine (the
        # nexus-eo3qv-disclosed gap). Same established idioms as its
        # closest template, rehearse_chash_window.sh (deleted at
        # nexus-lgdel.l2, its own leg retired), reused verbatim where the
        # shape matches -- every site below mirrors that template's
        # identical shape (WHEEL `ls | head -1`, the `_wait_healthy`
        # status poll, a captured-output
        # `printf | grep -q` marker/string check gating an if/else, and a
        # diagnostic `printf | head -N | sed` dump inside a failure
        # branch that runs strictly AFTER the real grep -q decision has
        # already been made).
        # Two shape notes worth keeping (not retargeting mechanics): the
        # Stage 3d topic-count parse (`grep -oE ... | grep -oE ... |
        # head -1`) is a genuine control-flow-gating pipe (feeds the
        # loud-abort decision on zero parsed topics), EXEMPT rather than
        # `|| true`; the MVV's two `printf | grep -q` marker/string
        # checks and their diagnostic `printf | head -8 | sed` dump are
        # the same established if/else-gating shape as the rest of this
        # file. Pre-nexus-vkpr3 this block carried a three-round
        # line-shift retargeting history (the live-acceptance
        # remediation, nexus-lgdel.l2's leg deletion, nexus-ft04v.10's
        # Stage 3e block); dropped, since content anchors make
        # retargeting unnecessary going forward.
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('test -x "$SVC_NATIVE_DIR/nexus-service" && ok "candidate native binary staged at $SVC_NATIVE_DIR (positioned only at Stage 4)" || { bad "candidate binary missing at $SVC_NATIVE_DIR"; exit 1; }', 'WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('for _ in $(seq 1 "$tries"); do', 'if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('# asserts, when discovery genuinely produced nothing.', 'TOTAL_TOPICS="$(printf \'%s\' "$DISCOVER_OUT" | grep -oE \'Total: [0-9]+ topics\' | grep -oE \'[0-9]+\' | head -1)"')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('PRE_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"', 'if printf \'%s\' "$PRE_SEARCH" | grep -q "candmigmarker1populate"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('else', 'printf \'%s\\n\' "$PRE_SEARCH" | head -8 | sed \'s/^/       /\'')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('printf \'%s\\n\' "$RESTALE_OUT" | sed \'s/^/       /\'', 'if printf \'%s\' "$RESTALE_OUT" | grep -qi "engine: converged"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('POST_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"', 'if printf \'%s\' "$POST_SEARCH" | grep -q "candmigmarker1populate"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('else', 'printf \'%s\\n\' "$POST_SEARCH" | head -8 | sed \'s/^/       /\'')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('printf \'%s\\n\' "$DOC_OUT" | grep -iE \'upgrade ladder|engine convergence\' | sed \'s/^/       /\' || true', 'if printf \'%s\' "$DOC_OUT" | grep -q "Traceback"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('fi', 'if printf \'%s\' "$DOC_OUT" | grep -qi "pending upgrade rung"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('fi', 'if printf \'%s\' "$DOC_OUT" | grep -qi "engine convergence pending"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('note "MVV clause 2/3 — register(embedding_model=voyage-code-3, content_type=code, owner=$MVV_CODE_OWNER, name=$MVV_CODE_NAME) -> $MVV_REGISTER_OUT"', 'if printf \'%s\' "$MVV_REGISTER_OUT" | grep -q "RESULT:STATUS=422"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('MVV_SEARCH_OUT="$(nx search "$MVV_MARKER" --corpus code -m 3 2>&1)"', 'if printf \'%s\' "$MVV_SEARCH_OUT" | grep -q "$MVV_MARKER"; then')),
        ("tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh", ('else', 'printf \'%s\\n\' "$MVV_SEARCH_OUT" | head -8 | sed \'s/^/       /\'')),
    }
)
# The ceiling has moved several times as real sites were added (a new
# GOT_CLIENT_VER extraction, rehearse_candidate_migration.sh's ten
# entries, its Stage 3d topic-count parse, its MVV Stage 3e additions)
# and removed (rehearse_chash_window.sh and rehearse_guided.sh deleted
# whole-file at nexus-lgdel.l2); see git blame on this constant for the
# historical count derivation. 135 is the current live count.
_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING = 135


def test_pipefail_early_exit_exempt_ratchet() -> None:
    assert len(_PIPEFAIL_EARLY_EXIT_EXEMPT) == _PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING, (
        f"_PIPEFAIL_EARLY_EXIT_EXEMPT has {len(_PIPEFAIL_EARLY_EXIT_EXEMPT)} "
        f"entries, expected exactly {_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING}. "
        "This set may only shrink (fix the site and remove its entry) or "
        "grow with a documented per-entry rationale plus a conscious bump "
        "of `_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` in this file."
    )


def _resolve_content_keyed_sites(
    entries: frozenset[tuple[str, tuple[str, ...]]],
) -> tuple[dict[str, set[int]], list[str]]:
    """Resolve every ``(path, content-anchor)`` entry in *entries* to its
    CURRENT live line number (nexus-vkpr3). Returns ``(by_file,
    problems)``: ``by_file`` maps path -> the set of resolved line
    numbers; a ``problems`` entry (STALE or AMBIGUOUS) means the anchor
    did not resolve at all -- a caller must treat that as a hard
    failure, never as "nothing to exempt here". Shared by both
    ``_PIPEFAIL_EARLY_EXIT_EXEMPT`` and ``_PIPEFAIL_OR_TRUE_SITES``."""
    items = [(path, content, None) for path, content in entries]
    resolved, problems = resolve_ledger(REPO_ROOT, items)
    by_file: dict[str, set[int]] = {}
    for path, lineno, _payload in resolved:
        by_file.setdefault(path, set()).add(lineno)
    return by_file, problems


def test_pipefail_early_exit_exempt_entries_are_live_violations() -> None:
    """Every exempt entry's content anchor must resolve to a unique,
    current line (nexus-vkpr3: neither STALE -- the anchor text no
    longer occurs -- nor AMBIGUOUS -- it occurs more than once), and
    that resolved line must still name a REAL, currently-detected
    violation. Either failure is a free, unrationalized exemption slot
    for whoever edits this set next, exactly what the ratchet's exact-
    equality ceiling exists to prevent (mirrors
    ``test_mode_lint_exclude_nodeids_all_resolve``)."""
    by_file, problems = _resolve_content_keyed_sites(_PIPEFAIL_EARLY_EXIT_EXEMPT)
    assert not problems, (
        f"{len(problems)} _PIPEFAIL_EARLY_EXIT_EXEMPT anchor(s) failed to "
        "resolve:\n  " + "\n  ".join(problems) + "\n\nRetarget with the "
        "line's current stripped text if it moved (insertions above it "
        "do not require this), or delete the entry and lower "
        "`_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` if it was fixed."
    )

    dead: list[str] = []
    for rel_path, linenos in by_file.items():
        full = REPO_ROOT / rel_path
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        hit_lines = {n for n, _ in _early_exit_consumer_hits(lines)}
        for n in sorted(linenos):
            if n not in hit_lines:
                dead.append(f"{rel_path}:{n} -> no early-exit-consumer pipe detected there")

    assert not dead, (
        f"{len(dead)} pipefail-early-exit exemption(s) resolve to a line "
        "that is no longer a live violation:\n  " + "\n  ".join(dead)
        + "\n\nDelete the entry and lower "
        "`_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` if it was fixed."
    )


def test_exempt_anchor_survives_insertion_above_it(tmp_path: Path) -> None:
    """THE nexus-vkpr3 REGRESSION, against THIS file's real scanner
    (``_early_exit_consumer_hits``): inserting lines above an exempted
    site must not silently move the exemption onto a DIFFERENT,
    unreviewed early-exit-consumer pipe that happens to shift into the
    old stored line number -- this is precisely the incident the bead
    reports (a WHEEL-extraction line, never reviewed, slid onto a
    version-probe line's stale exemption after an unrelated insertion).
    """
    sample = tmp_path / "sample.sh"
    original = (
        "#!/usr/bin/env bash\n"
        "set -o pipefail\n"
        'WHEEL="$(ls dist/conexus-*.whl | head -1)"\n'          # DECOY, never exempted
        "noop\n"
        'GOT_CLIENT_VER="$(nx --version 2>&1 | grep -oE "[0-9.]+" | head -1)"\n'  # VICTIM, exempted
    )
    sample.write_text(original, encoding="utf-8")

    victim_content = (
        'GOT_CLIENT_VER="$(nx --version 2>&1 | grep -oE "[0-9.]+" | head -1)"',
    )
    lineno, err = resolve_anchor(tmp_path, "sample.sh", victim_content)
    assert (lineno, err) == (5, "")

    hits_before = dict(_early_exit_consumer_hits(original.splitlines(keepends=True)))
    assert set(hits_before) == {3, 5}

    # Insert exactly enough lines above both sites to shift the DECOY
    # (originally line 3) onto VICTIM's OLD stored line number (5) --
    # the exact shape of the nexus-vkpr3 incident.
    shift = lineno - 3
    assert shift > 0
    modified = "\n".join(f"# inserted {i}" for i in range(shift)) + "\n" + original
    sample.write_text(modified, encoding="utf-8")

    hits_after = dict(_early_exit_consumer_hits(modified.splitlines(keepends=True)))
    modified_lines = modified.splitlines()
    assert 5 in hits_after and "WHEEL" in modified_lines[4], (
        "fixture stopped demonstrating the hazard -- the WHEEL decoy "
        "line must now sit at VICTIM's old stored line number (5)"
    )

    # A stale line-number key (5) would now silently exempt the WHEEL
    # decoy under the version-probe's own rationale. The content anchor
    # instead resolves to VICTIM's real, shifted location and nothing
    # else.
    lineno, err = resolve_anchor(tmp_path, "sample.sh", victim_content)
    assert err == ""
    assert lineno == 5 + shift
    assert lineno != 5  # never the decoy's (post-shift) line


# ── ratchet: `|| true`-guarded early-exit-consumer sites (gap #1) ──────────
#
# The `|| true` (or `|| :`) suffix is the sanctioned display-only
# mitigation (see SANCTIONED FIX in the module docstring) -- but appending
# it to a genuine early-exit pipe silently defeated detection with zero
# review friction UNLESS every site is itself tracked here, exact-equality
# ratcheted exactly like `_PIPEFAIL_EARLY_EXIT_EXEMPT` above. Untracked,
# any author could suffix `|| true` onto a real control-flow-gating pipe;
# worse, appending `|| true` to a pipe used as an `if`/`elif`/`&&`
# CONDITION makes that condition UNCONDITIONALLY TRUE, so the escape
# hatch would not merely hide the pipefail hazard, it would convert the
# assertion into a vacuous pass -- precisely this repo's dominant defect
# class. A per-entry rationale is required (not just a count ceiling):
# unlike the EXEMPT set above (where every entry is provably a real,
# still-detected VIOLATION and the risk is only "which ones", here the
# whole point is proving each site is display-only, which cannot be
# verified mechanically -- only a human review, recorded as a rationale
# comment, closes that gap.
#
# Every entry below was verified at authoring time (2026-08-10) to be a
# genuine display-only pipeline: none sits inside an `if`/`elif`/`&&`/
# `||` CONTROL-FLOW position, and none feeds a variable that later gates
# a pass/fail decision -- each truncates or extracts output for
# human-readable pretty-printing / cosmetic summary counters only.
#
# CONTENT-KEYED, not line-keyed (nexus-vkpr3) -- same shape and same
# rationale as `_PIPEFAIL_EARLY_EXIT_EXEMPT` above: the second tuple
# element is a content anchor (one or more trailing stripped source
# lines) resolved to its CURRENT line by `resolve_anchor`, immune to an
# insertion above the site.
_PIPEFAIL_OR_TRUE_SITES: frozenset[tuple[str, tuple[str, ...]]] = frozenset(
    {
        # scripts/rdr152-sandbox/prod-copy.sh (3 entries): each truncates
        # a per-table ETL error dump to 20 lines for terminal
        # readability inside a manual ops runbook (RDR-152 sandbox
        # refresh) -- not gating any pass/fail decision; `nx storage
        # migrate`'s own exit code / summary further down is what
        # actually surfaces failure, this is supplementary diagnostic
        # noise-truncation only.
        ("scripts/rdr152-sandbox/prod-copy.sh", ('uv run nx storage migrate telemetry \\', '--db "${PROD_MEMORY_DB}" \\', '--service-url "${NX_SERVICE_URL}" 2>&1 | grep -v "row_failed" | head -20 || true')),
        ("scripts/rdr152-sandbox/prod-copy.sh", ('--catalog-db "${PROD_CATALOG_DIR}/.catalog.db" \\', '--service-url "${NX_SERVICE_URL}" 2>&1 | grep -v "row_failed" | head -20 || true')),
        ("scripts/rdr152-sandbox/prod-copy.sh", ('uv run nx storage migrate taxonomy \\', '--db "${PROD_MEMORY_DB}" \\', '--service-url "${NX_SERVICE_URL}" 2>&1 | grep -v "row_failed" | head -20 || true')),
        # tests/containers/lib/verdict.sh:40 (1 entry): extracts a junit
        # <testsuite> attribute string to accumulate the run's test totals.
        #
        # Moved here from tests/containers/fanout.sh:158 when the verdict
        # logic was extracted so its tests could drive the real aggregation
        # (nexus-uq3xs). The rationale ALSO changed and the old one is not
        # merely relocated: it said these counters were "cosmetic" and that
        # the pass/fail decision came only from each shard's `$rc`. That is
        # no longer true -- `total_t` is now load-bearing for the
        # non-vacuity check and the NX_FANOUT_MIN_TESTS floor.
        #
        # The `|| true` is still correct, for a different reason. A missing
        # or malformed junit yields an empty `suite_tag`, so nothing is
        # added to the totals -- and a run whose totals are short is exactly
        # what the zero-tests check, the reported-shard-count check, and the
        # floor exist to fail on. Swallowing the grep's status cannot hide a
        # short run; it produces one, and the checks below catch it.
        ("tests/containers/lib/verdict.sh", ('if [ -f "$xml" ]; then', 'suite_tag="$(grep -o \'<testsuite [^>]*>\' "$xml" | head -1 || true)"')),
        # tests/e2e/migration-rehearsal/rehearse_shakeout.sh (2 entries):
        # one prints staleness/skip diagnostic lines for human eyeballing
        # in a "run-2 log tail for diagnosis" block -- the actual
        # indexed-content-searchable assertion runs on the next
        # (unrelated) line via a fresh, unguarded `nx search | grep -qi`.
        # The other is the Phase A health-poll loop, `nx daemon service
        # status | grep -qiE "health.*ok|status.*live" && { healthy=1;
        # break; } || true` -- a poll-and-retry loop body, not an
        # `if`/`elif` condition (so `_if_condition_or_true_bug_hits` does
        # not apply): the EXPECTED "not yet healthy" iteration is a bare
        # `&&` failure with no trailing `||`, which `set -e` would abort
        # on at iteration 1 without this guard. The real pass/fail
        # assertion is the SEPARATE post-loop line, `[ "$healthy" = 1 ]
        # && ok ... || { bad ...; exit 1; }` -- this guarded site itself
        # gates nothing. Pre-nexus-vkpr3 this block was retargeted twice
        # by line-shift arithmetic; dropped, since content anchors make
        # retargeting unnecessary going forward.
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('note "run-2 staleness/skip lines:"', 'grep -iE "stale|skip|unchanged|cache" "$IDX2" | sed \'s/^/       | /\' | head -10 || true')),
        ("tests/e2e/migration-rehearsal/rehearse_shakeout.sh", ('# polling up to 30 times. `|| true` restores the intended poll-and-wait.', 'nx daemon service status 2>&1 | grep -qiE "health.*ok|status.*live" && { healthy=1; break; } || true')),
        # tests/e2e/release-sandbox.sh (3 entries): the already-commented
        # `|| true: head is an early-exit consumer...` idiom this file's
        # own docstring cites as the sanctioned shape -- readback for
        # human eyeballing only; the actual FAIL/bad decision for each
        # surrounding block is made from a separately-captured variable
        # or a dedicated gate elsewhere, never from these truncated
        # echoes. Pre-nexus-vkpr3 this block was retargeted four times
        # across unrelated edits (the 7.15.0 release fix, a $HOME-
        # activation comment rewrite, --check-schema's per-check args
        # array, nexus-gqrg0's widened MinerU filter); dropped, since
        # content anchors make retargeting unnecessary going forward.
        ("tests/e2e/release-sandbox.sh", ('# bookkeeping below (nexus-6zxfb, same class as nexus-i66g4).', 'echo "$MEMORY_GET_OUT" | head -3 | sed \'s/^/  /\' || true')),
        ("tests/e2e/release-sandbox.sh", ('else', 'echo "$MEMORY_GET_OUT" | head -3 | sed \'s/^/  /\' || true')),
        ("tests/e2e/release-sandbox.sh", ('# over the same catalog state runs explicitly at 11/11 below.', "nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true")),
    }
)
_PIPEFAIL_OR_TRUE_SITES_CEILING = 9


def test_pipefail_or_true_sites_ratchet() -> None:
    assert len(_PIPEFAIL_OR_TRUE_SITES) == _PIPEFAIL_OR_TRUE_SITES_CEILING, (
        f"_PIPEFAIL_OR_TRUE_SITES has {len(_PIPEFAIL_OR_TRUE_SITES)} "
        f"entries, expected exactly {_PIPEFAIL_OR_TRUE_SITES_CEILING}. "
        "This set may only shrink (fix the site -- eliminate the pipe or "
        "remove the `|| true` -- and remove its entry) or grow with a "
        "documented per-entry rationale (why the site is genuinely "
        "display-only, not control-flow-gating) plus a conscious bump of "
        "`_PIPEFAIL_OR_TRUE_SITES_CEILING` in this file."
    )


def test_pipefail_or_true_sites_are_live_or_true_guarded_hits() -> None:
    """Every `_PIPEFAIL_OR_TRUE_SITES` entry's content anchor must resolve
    to a unique, current line (nexus-vkpr3: neither STALE nor
    AMBIGUOUS), and that resolved line must still name a REAL,
    currently-detected `|| true`-guarded early-exit-consumer pipe -- same
    liveness discipline as `test_pipefail_early_exit_exempt_entries_are_
    live_violations` above, for the same reason: a stale entry is a free,
    unrationalized escape-hatch slot for whoever edits this set next."""
    by_file, problems = _resolve_content_keyed_sites(_PIPEFAIL_OR_TRUE_SITES)
    assert not problems, (
        f"{len(problems)} _PIPEFAIL_OR_TRUE_SITES anchor(s) failed to "
        "resolve:\n  " + "\n  ".join(problems) + "\n\nRetarget with the "
        "line's current stripped text if it moved (insertions above it "
        "do not require this), or delete the entry and lower "
        "`_PIPEFAIL_OR_TRUE_SITES_CEILING` if the `|| true` was removed."
    )

    dead: list[str] = []
    for rel_path, linenos in by_file.items():
        full = REPO_ROOT / rel_path
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        hit_lines = {n for n, _ in _or_true_guarded_early_exit_hits(lines)}
        for n in sorted(linenos):
            if n not in hit_lines:
                dead.append(f"{rel_path}:{n} -> no `|| true`-guarded early-exit-consumer pipe detected there")

    assert not dead, (
        f"{len(dead)} `|| true`-guarded-site entr{'y' if len(dead) == 1 else 'ies'} "
        "resolve to a line that is no longer a live guarded hit:\n  "
        + "\n  ".join(dead)
        + "\n\nDelete the entry and lower "
        "`_PIPEFAIL_OR_TRUE_SITES_CEILING` if the `|| true` was removed "
        "(or the pipe eliminated)."
    )


# ── the sweep ────────────────────────────────────────────────────────────


def test_no_pipefail_script_pipes_into_an_early_exit_consumer() -> None:
    scripts = _tracked_shell_scripts()
    assert len(scripts) >= 10, f"suspicious sweep: only {len(scripts)} scripts enumerated"

    # Resolved once, outside the per-script loop -- an anchor that fails
    # to resolve (STALE/AMBIGUOUS) excludes nothing here (fail-safe
    # toward MORE reported violations, never fewer); the dedicated
    # liveness tests above are what make a resolution failure loud in
    # its own right.
    exempt_by_file, _exempt_problems = _resolve_content_keyed_sites(_PIPEFAIL_EARLY_EXIT_EXEMPT)
    or_true_by_file, _or_true_problems = _resolve_content_keyed_sites(_PIPEFAIL_OR_TRUE_SITES)

    pipefail_scripts = 0
    violations: list[str] = []
    or_true_violations: list[str] = []
    for script in scripts:
        text = script.read_text(encoding="utf-8", errors="replace")
        if not _sets_pipefail(text, file_path=script):
            continue
        pipefail_scripts += 1
        lines = text.splitlines(keepends=True)
        rel = script.relative_to(REPO_ROOT).as_posix()
        exempt_linenos = exempt_by_file.get(rel, set())
        or_true_linenos = or_true_by_file.get(rel, set())
        for lineno, snippet in _early_exit_consumer_hits(lines):
            if lineno in exempt_linenos:
                continue
            violations.append(f"{rel}:{lineno}  ({snippet})")
        for lineno, snippet in _or_true_guarded_early_exit_hits(lines):
            if lineno in or_true_linenos:
                continue
            or_true_violations.append(f"{rel}:{lineno}  ({snippet})")

    # Non-vacuity: the pipefail-precondition filter must actually be
    # letting a meaningful subset of the corpus through, not silently
    # filtering everything to zero (which would make this sweep pass on
    # an empty scan, indistinguishable from "no violations").
    assert pipefail_scripts >= 20, (
        f"suspicious sweep: only {pipefail_scripts} tracked shell scripts "
        "set pipefail -- the pipefail-precondition filter may be broken"
    )

    assert not violations, (
        "the following pipefail-set shell script(s) pipe a producer into "
        "an early-exit consumer (grep -q / grep -m<N> / head) -- under "
        "pipefail, if the producer is still writing when the consumer "
        "exits early, the producer's SIGPIPE gets promoted over the "
        "consumer's own exit status (nexus-i66g4 / nexus-6zxfb / "
        "nexus-wbeyi class). Fix: eliminate the pipe -- capture the "
        "producer's output to a variable first (a command substitution "
        "drains to EOF, so it cannot trigger this), then match with "
        "`[[ \"$VAR\" == *substring* ]]` (literal) or "
        "`[[ \"$VAR\" =~ regex ]]` (ERE -- carries over almost verbatim "
        "from a `grep -E` pattern). For a genuinely display-only pipeline "
        "that doesn't gate control flow, suffix `|| true` instead. If "
        "this site is a real exemption (needs live infra to verify), add "
        "it to `_PIPEFAIL_EARLY_EXIT_EXEMPT` in this file with a "
        "documented rationale and bump the ceiling in the same diff:\n  "
        + "\n  ".join(violations)
    )

    assert not or_true_violations, (
        "the following pipefail-set shell script(s) neutralize an "
        "early-exit-consumer pipe with a trailing `|| true` / `|| :` "
        "WITHOUT a tracked `_PIPEFAIL_OR_TRUE_SITES` entry -- this is the "
        "untracked-escape-hatch gap the ratchet exists to close: a bare "
        "`|| true` can silently defeat this lint's real purpose (and, "
        "worse, if the pipe is an `if`/`elif` CONDITION, `|| true` makes "
        "that condition UNCONDITIONALLY TRUE -- see "
        "`test_no_if_condition_neutralizes_its_own_pipe_with_or_true`). "
        "If this really is a display-only pipeline, add it to "
        "`_PIPEFAIL_OR_TRUE_SITES` in this file with a documented "
        "rationale (why it does not gate any pass/fail decision) and "
        "bump `_PIPEFAIL_OR_TRUE_SITES_CEILING` in the same diff. "
        "Otherwise, remove the `|| true` and fix the pipe like a normal "
        "violation:\n  " + "\n  ".join(or_true_violations)
    )


def test_no_if_condition_neutralizes_its_own_pipe_with_or_true() -> None:
    """The always-true-condition bug shape (see `_if_condition_or_true_bug
    _hits`'s docstring) is a hard failure across EVERY tracked shell
    script, not just pipefail-set ones -- `||` runs regardless of
    pipefail, so this bug is independent of the pipefail precondition
    that gates the rest of this file's sweep."""
    scripts = _tracked_shell_scripts()
    assert len(scripts) >= 10, f"suspicious sweep: only {len(scripts)} scripts enumerated"

    violations: list[str] = []
    for script in scripts:
        text = script.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        rel = script.relative_to(REPO_ROOT).as_posix()
        for lineno, snippet in _if_condition_or_true_bug_hits(lines):
            violations.append(f"{rel}:{lineno}  ({snippet})")

    assert not violations, (
        "the following if/elif condition(s) pipe an early-exit consumer "
        "and then suffix `|| true` (or `|| :`), which makes the "
        "CONDITION UNCONDITIONALLY TRUE regardless of what the pipe "
        "actually matched -- this is not a pipefail-hazard mitigation "
        "here, it deletes the assertion outright. Fix: remove the `|| "
        "true`/`|| :` (the condition should fail loud on a real grep/head "
        "non-match), or restructure so the pipe result is captured to a "
        "variable BEFORE the `if`:\n  " + "\n  ".join(violations)
    )
