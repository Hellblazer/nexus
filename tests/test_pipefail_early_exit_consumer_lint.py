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
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

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

# The sanctioned "discard the exit status" mitigation for display-only
# pipelines (release-sandbox.sh's existing idiom). Recognized as a
# non-violation shape rather than requiring a per-site exemption entry.
_OR_TRUE_RE = re.compile(r"\|\|\s*(?:true|:)\b")


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
    return False


def _early_exit_consumer_hits(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(1-based lineno, matched consumer description)`` pairs for
    every early-exit-consumer pipe stage in *lines* that is not guarded by
    a trailing ``|| true`` / ``|| :``.

    Line-based, like ``test_shell_continuation_lint.py``'s scanner --
    deliberately no full shell parser. Every real instance found across
    this repo (i66g4's ~12 sites, 6zxfb, wbeyi, and the full-repo sweep at
    authoring time) has the pipe and its consumer on the same physical
    line, so this is not a hypothetical simplification.
    """
    hits: list[tuple[int, str]] = []
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
            # The sanctioned display-only mitigation: exit status of the
            # whole pipeline is discarded, so a SIGPIPE-promoted failure
            # cannot abort the script here. `|| true` can trail ANY later
            # pipe stage (e.g. `cmd | head -3 | sed ... || true` -- the
            # 6zxfb shape, where head is a MIDDLE stage) and it still
            # neutralizes the promoted failure for every upstream stage,
            # so scan every segment from here to end of line, not just
            # this one.
            if any(_OR_TRUE_RE.search(s) for s in segments[idx:]):
                continue
            hits.append((i, stripped.strip()))
    return hits


def _sets_pipefail(text: str) -> bool:
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if _PIPEFAIL_SET_RE.search(line):
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


def test_sets_pipefail_recognizes_combined_flag_forms() -> None:
    assert _sets_pipefail("set -o pipefail\n")
    assert _sets_pipefail("set -eo pipefail\n")
    assert _sets_pipefail("set -Eeuo pipefail\n")
    assert _sets_pipefail("set -uo pipefail\n")
    assert not _sets_pipefail("set -eu\n")
    assert not _sets_pipefail("# set -o pipefail (example only)\n")


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
# Format: "relative/path.sh:LINENO" -> each block below documents WHY the
# entry is exempted rather than fixed in this pass. This set may only
# SHRINK (a line gets fixed and its entry removed) or grow with a new,
# individually-documented entry AND a conscious bump of
# `_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` in the same diff -- see
# `test_mode_declarations_are_explicit.py`'s ratchet for the pattern this
# mirrors. A bare growth with no rationale comment is exactly the
# grandfathering this mechanism exists to prevent.
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
_PIPEFAIL_EARLY_EXIT_EXEMPT: frozenset[str] = frozenset(
    {
        # --- tests/e2e/migration-rehearsal/*.sh (140 entries): Docker-
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
        "tests/e2e/migration-rehearsal/rehearse.sh:125",
        "tests/e2e/migration-rehearsal/rehearse.sh:161",
        "tests/e2e/migration-rehearsal/rehearse.sh:181",
        "tests/e2e/migration-rehearsal/rehearse.sh:187",
        "tests/e2e/migration-rehearsal/rehearse.sh:252",
        "tests/e2e/migration-rehearsal/rehearse.sh:274",
        "tests/e2e/migration-rehearsal/rehearse.sh:300",
        "tests/e2e/migration-rehearsal/rehearse.sh:301",
        "tests/e2e/migration-rehearsal/rehearse_acquire.sh:122",
        "tests/e2e/migration-rehearsal/rehearse_acquire.sh:134",
        "tests/e2e/migration-rehearsal/rehearse_acquire.sh:80",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:101",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:149",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:235",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:238",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:274",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:279",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:284",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:312",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:315",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:336",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:339",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:387",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:387",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:403",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:490",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:493",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:502",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:511",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:516",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:532",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:537",
        "tests/e2e/migration-rehearsal/rehearse_chash_window.sh:82",
        "tests/e2e/migration-rehearsal/rehearse_cold.sh:113",
        "tests/e2e/migration-rehearsal/rehearse_cold.sh:77",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:143",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:199",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:249",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:265",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:452",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:479",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:484",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:501",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:503",
        "tests/e2e/migration-rehearsal/rehearse_era_hop.sh:86",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:177",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:21",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:36",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:48",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:57",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:58",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:85",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:90",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:91",
        "tests/e2e/migration-rehearsal/rehearse_fullstack.sh:94",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:245",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:248",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:256",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:265",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:266",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:296",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:299",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:305",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:316",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:323",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:324",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:448",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:451",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:454",
        "tests/e2e/migration-rehearsal/rehearse_guided.sh:760",
        "tests/e2e/migration-rehearsal/rehearse_hole_punch.sh:101",
        "tests/e2e/migration-rehearsal/rehearse_hole_punch.sh:175",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:139",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:200",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:217",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:222",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:239",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:266",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:336",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:343",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:51",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:75",
        "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh:97",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:116",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:119",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:129",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:135",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:138",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:144",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:153",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:157",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:159",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:167",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:171",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:211",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:41",
        "tests/e2e/migration-rehearsal/rehearse_shakeout.sh:92",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:142",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:193",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:238",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:239",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:346",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:405",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:538",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:544",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:551",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:630",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:631",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:649",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:652",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:658",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:661",
        "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh:664",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:100",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:123",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:146",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:148",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:151",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:154",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:157",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:168",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:185",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:191",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:233",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:278",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:291",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:305",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:307",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:308",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:309",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:339",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:347",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:361",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:377",
        "tests/e2e/migration-rehearsal/rehearse_stranded.sh:80",
        "tests/e2e/migration-rehearsal/run.sh:506",
        "tests/e2e/migration-rehearsal/run.sh:537",
        "tests/e2e/migration-rehearsal/run.sh:551",
        "tests/e2e/migration-rehearsal/run.sh:563",
        "tests/e2e/migration-rehearsal/run.sh:579",
        # --- tests/e2e/mac-signed-binary-gate.sh (8 entries): needs an
        # actually-signed macOS binary + `spctl`/`codesign` on real macOS
        # to safely verify a rewrite of the signature-inspection logic.
        "tests/e2e/mac-signed-binary-gate.sh:117",
        "tests/e2e/mac-signed-binary-gate.sh:117",
        "tests/e2e/mac-signed-binary-gate.sh:118",
        "tests/e2e/mac-signed-binary-gate.sh:127",
        "tests/e2e/mac-signed-binary-gate.sh:135",
        "tests/e2e/mac-signed-binary-gate.sh:148",
        "tests/e2e/mac-signed-binary-gate.sh:149",
        "tests/e2e/mac-signed-binary-gate.sh:190",
        # --- service/native-smoke.sh (8 entries): native-image
        # release-only smoke; needs a real GraalVM native build to
        # safely verify a rewrite.
        "service/native-smoke.sh:126",
        "service/native-smoke.sh:219",
        "service/native-smoke.sh:351",
        "service/native-smoke.sh:443",
        "service/native-smoke.sh:481",
        "service/native-smoke.sh:487",
        "service/native-smoke.sh:490",
        "service/native-smoke.sh:76",
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
        "service/linux-native-verify.sh:43",
        # --- tests/e2e/fresh-install-mvv.sh (3 entries): release-battery
        # gate (AGENTS.md "Cutting a release" step 1b); needs a real
        # fresh-HOME wheel install to safely verify a rewrite of its
        # dist-info / version-banner sniffing.
        "tests/e2e/fresh-install-mvv.sh:281",
        "tests/e2e/fresh-install-mvv.sh:294",
        "tests/e2e/fresh-install-mvv.sh:452",
        # --- tests/e2e/local-index-memory-gate.sh (1 entry): owned by a
        # concurrent agent in the authoring session (nexus-wbeyi itself)
        # -- reported to that hand-off, not fixed here. This is a
        # SECOND, previously-unreported site distinct from the
        # already-fixed line 555: a `| head -1` inside a bare
        # `VAR=$(...)` assignment (propagates through errexit) found by
        # this lint's own authoring sweep.
        "tests/e2e/local-index-memory-gate.sh:849",
    }
)
_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING = 159


def test_pipefail_early_exit_exempt_ratchet() -> None:
    assert len(_PIPEFAIL_EARLY_EXIT_EXEMPT) == _PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING, (
        f"_PIPEFAIL_EARLY_EXIT_EXEMPT has {len(_PIPEFAIL_EARLY_EXIT_EXEMPT)} "
        f"entries, expected exactly {_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING}. "
        "This set may only shrink (fix the site and remove its entry) or "
        "grow with a documented per-entry rationale plus a conscious bump "
        "of `_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` in this file."
    )


def test_pipefail_early_exit_exempt_entries_are_live_violations() -> None:
    """Every exempt entry must still name a REAL, currently-detected
    violation -- a stale entry (the line was fixed, or moved, or the
    pipeline shape changed) is a free, unrationalized exemption slot for
    whoever edits this set next, exactly the failure the ratchet's exact-
    equality ceiling exists to prevent (mirrors
    ``test_mode_lint_exclude_nodeids_all_resolve``)."""
    dead: list[str] = []
    by_file: dict[str, set[int]] = {}
    for entry in _PIPEFAIL_EARLY_EXIT_EXEMPT:
        path, _, lineno = entry.rpartition(":")
        by_file.setdefault(path, set()).add(int(lineno))

    for rel_path, linenos in by_file.items():
        full = REPO_ROOT / rel_path
        if not full.is_file():
            dead.extend(f"{rel_path}:{n} -> no such file" for n in linenos)
            continue
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        hit_lines = {n for n, _ in _early_exit_consumer_hits(lines)}
        for n in sorted(linenos):
            if n not in hit_lines:
                dead.append(f"{rel_path}:{n} -> no early-exit-consumer pipe detected there")

    assert not dead, (
        f"{len(dead)} pipefail-early-exit exemption(s) no longer name a "
        "live violation:\n  " + "\n  ".join(dead) + "\n\nRetarget if the "
        "line moved, or delete the entry and lower "
        "`_PIPEFAIL_EARLY_EXIT_EXEMPT_CEILING` if it was fixed."
    )


# ── the sweep ────────────────────────────────────────────────────────────


def test_no_pipefail_script_pipes_into_an_early_exit_consumer() -> None:
    scripts = _tracked_shell_scripts()
    assert len(scripts) >= 10, f"suspicious sweep: only {len(scripts)} scripts enumerated"

    pipefail_scripts = 0
    violations: list[str] = []
    for script in scripts:
        text = script.read_text(encoding="utf-8", errors="replace")
        if not _sets_pipefail(text):
            continue
        pipefail_scripts += 1
        lines = text.splitlines(keepends=True)
        rel = script.relative_to(REPO_ROOT).as_posix()
        for lineno, snippet in _early_exit_consumer_hits(lines):
            entry = f"{rel}:{lineno}"
            if entry in _PIPEFAIL_EARLY_EXIT_EXEMPT:
                continue
            violations.append(f"{entry}  ({snippet})")

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
