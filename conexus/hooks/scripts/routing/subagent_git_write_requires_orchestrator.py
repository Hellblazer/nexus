#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-184 Gap-4 mechanization (nexus-s88vq, widened by nexus-ays2l): deny
index-writing AND working-tree-destroying git verbs from SUBAGENTS in the
shared tree.

Standing rule (``feedback_orchestration_friction_2026_07_15``): agents in
a shared tree NEVER ``git add``/``git commit`` — hand-back is diffs+paths;
the orchestrator commits pathspec-limited. The rule was prompt-enforced
only, and planner-186 committed in the shared tree anyway (20cd906e).

Mechanism: the PreToolUse payload carries ``agent_id`` IFF the call
originates from a subagent (documented hook schema; absent for the main
conversation). A subagent's Bash ``git commit``/``git add`` in the
PRIMARY checkout is denied with the hand-back protocol. Allowed:

- Main-conversation git writes (no ``agent_id``).
- Read-only git (status/diff/log/...) from anyone.
- Subagent commits inside a LINKED WORKTREE (``git rev-parse --git-dir``
  differs from ``--git-common-dir``): worktree-isolated agents own their
  tree and their local commits are the documented harvest choreography.
- A valid ``# routing-allow:`` escape (deliberate orchestrator-sanctioned
  exception, auditable in the routing log).

Fail mode is SPLIT by what is at stake (nexus-ays2l item 3, Hal ruling
2026-07-25):

- ``commit`` / ``add`` — hygiene. An undeterminable worktree state fails
  OPEN, because a flaky ``git rev-parse`` must never wedge agent work over
  tidiness. ``add`` mutates only the index and destroys nothing.
- ``checkout`` / ``restore`` / ``switch`` / ``stash`` / ``clean`` / ``reset`` /
  ``rm`` — destruction. An undeterminable worktree state fails CLOSED. The
  failure mode here is silent, unrecoverable loss of the orchestrator's
  uncommitted work, and "I could not tell whether this tree is shared" is not
  a good enough reason to permit that. The ``# routing-allow:`` escape
  remains.

nexus-3c92m (2026-08-20), corrected root cause: the FIRST hypothesis here
(that the incident guard was simply an older pinned plugin release predating
nexus-ays2l) was REFUTED by the actual transcript — the three wiping Bash
calls ran against TODAY's plugin and this hook returned
``permissionDecision=allow`` for each, live. ``switch`` (added below, for
parity with ``checkout <branch>``) was never the cause either. The real bug:
``_matched_write_subcommands``'s segment splitter matched ``&&``/``||``/``;``/
``|``/``then``/``do`` but never a bare NEWLINE, and ``_git_subcommand`` only
inspects a segment's FIRST token. A realistic multi-line Bash tool
invocation — ``cd dir\necho ...\npython3 - <<'EOF'\n...\nEOF\ngit checkout --
path`` — is joined by nothing but newlines, so the whole multi-line blob was
ONE segment whose first token was ``cd``; the ``git checkout`` many tokens
later in that same segment was never looked at. A second sibling incident
(``git add -N``/``git reset -- <pathspec>`` also silently allowed) traced to
the identical cause, confirmed by replaying the pre-fix splitter offline: the
single-line forms of both were already denied before this fix (``add``/
``reset`` are matched unconditionally, independent of arguments), only the
multi-line/heredoc-adjacent forms slipped through — not a separate
verb-matching gap. Fixed via ``_iter_heredoc_aware_lines`` (newline-aware
segmentation that also classifies heredoc bodies by consumer — a heredoc fed
to a real shell is scanned, one fed to ``python3``/``cat``/etc. is opaque
data and is skipped outright, so a heredoc merely PRINTING the text
``"git checkout"`` cannot false-positive) plus ``$(...)``/backtick
substitution extraction. This source change is itself inert until the next
release tag ships (the ``PENDING_RELEASE.md`` inertness class remains real
for OTHER installs even though it did not explain this particular incident);
see the ledger entry.

nexus-3c92m review round 2 (stacked review, 2026-08-20): both reviewers
falsified the round-1 fix by calling ``_matched_write_subcommands()``
directly (no git mutation). Fixed: the ``<<<`` here-string false-positive
(critic ship-blocker — see ``_HEREDOC_OPEN_RE``'s comment), path-qualified
heredoc consumers plus an expanded consumer list (code-review CRITICAL —
see ``_consumer_is_shell``), backslash line-continuation splitting ``git``
from its own subcommand (code-review CRITICAL — see
``_join_line_continuations``), ``<(...)``/``>(...)`` process substitution
never being extracted at all (code-review CRITICAL — see
``_PROCESS_SUB_RE``), and ``$(...)``/backtick extraction running against the
raw command instead of the heredoc-filtered text, which could false-deny
substitution-shaped TEXT sitting inside an opaque (e.g. python) heredoc body
(critic SIGNIFICANT — fixed in ``_matched_write_subcommands``).

nexus-3c92m review round 3 (2026-08-20): THREE MORE bypasses in the SAME
structural-parsing approach, found by direct construction: (1) CRLF after a
continuation backslash (``git \\\r\ncheckout``) — ``\r`` sat after the
backslash, so the trailing-backslash counter saw 0 backslashes and never
joined the lines. (2) A non-shell heredoc whose LAST body line ends in a
single backslash: ``_join_line_continuations`` ran on the raw text BEFORE
heredoc-aware segmentation, so it merged the terminator line into the
corrupted body line, the literal delimiter never appeared standalone, the
heredoc read as "unterminated," and EVERYTHING after — including a
destructive verb placed after the heredoc — got folded into the dropped
body. (3) Piping literal text into a shell (``printf 'git checkout -- f' |
sh``) — a pipe feeding stdin to ``sh``/``bash`` is the same risk as a
heredoc and got zero handling; heredoc-consumer classification was the ONLY
place this guard reasoned about "does untrusted text become executed shell
code," and a pipe isn't a heredoc.

THREE ROUNDS OF "add a case" IS THE SIGNAL TO CHANGE THE DESIGN, not to keep
patching structural parsing (Hal/coordinator ruling, round 4). The
underlying problem: every fix so far taught this guard ONE MORE shell
construct that can carry executable text (`&&`, `;`, newlines, heredocs,
`$(...)`, backticks, `<(...)`, `>(...)`, line continuation, pipes...) — an
unbounded list, because shell has an unbounded number of ways to get text
executed. A STRUCTURAL parser will always be one construct behind. So round
4 inverts the approach entirely:

PRIMARY RULE (structure-agnostic, fail-closed) — ``_primary_match`` /
``_GIT_RE`` / ``_VERB_RE``: over the RAW command text, after normalizing
line endings, collapsing backslash-newline continuations, collapsing
backslash-escapes, and stripping quote characters
(``_normalize_for_primary_scan`` — the ONLY "parsing" this rule does), deny
for subagents whenever the literal substring ``git`` occurs and a
write-verb word (checkout/restore/switch/reset/clean/stash/add/commit/
merge/rebase/cherry-pick/am/apply/push/rm/mv/worktree add|remove|prune/
branch -d|-D|-m/tag -d/update-ref/symbolic-ref/filter-branch/reflog
expire/gc/prune) occurs ANYWHERE AFTER the first ``git`` — no distance
bound, checked by two separate linear scans rather than one combined
regex. No segment splitting, no heredoc-consumer classification, no
substitution extraction, and (since round 5) no character-distance window
either — because ALL of those were exactly the surface area that grew a
new bypass every round it was re-reviewed. This makes the guard immune BY
CONSTRUCTION to any shell construct that merely relocates the same literal
text (heredocs, pipes, substitutions, continuations, quoting, CRLF,
backslash-escapes) OR pads the distance between them (flag chains, filler
text) — the scan does not care how ``git checkout`` ended up in the byte
stream, nor how far apart the two pieces sit, only that both are present.

The accepted cost is FALSE POSITIVES: a python heredoc that merely PRINTS
the string ``"git checkout"`` is now denied even though it executes nothing.
This is deliberate, not an oversight — for a SUBAGENT deny-guard the two
error directions are not symmetric. A false positive costs an agent a
rephrase or a hand-back to the orchestrator (cheap, recoverable, no data
lost). A false negative is this bead's own origin story: silent,
unrecoverable loss of the orchestrator's uncommitted work. The deny message
says so explicitly and suggests rephrasing without the literal git verb, or
handing the edit back to the orchestrator.

The worktree exemption is narrowed to match: it fires ONLY when
``_in_linked_worktree`` POSITIVELY PROVES (via real ``git rev-parse`` calls
against the actual ``cwd``, not by parsing ``-C``/``--work-tree`` flags out
of the command text) that this call runs inside a linked worktree. Every
other state — the primary checkout, OR an undeterminable worktree — now
falls through to the primary rule's verdict uniformly. This retires the old
commit/add-vs-destructive-verb fail-open/fail-closed split (nexus-ays2l item
3): that split existed to protect hygiene verbs from a flaky
``git rev-parse``, but the primary rule's whole point is that "I could not
prove this is safe" no longer earns a pass for ANY write verb, hygiene or
destructive.

SECONDARY (``_matched_write_subcommands``, kept from rounds 1-2): no longer
a security boundary. Its ONLY remaining job is to name the verb more
precisely in the deny message when its structured parse agrees with the
primary rule (e.g. reporting ``checkout`` instead of the primary regex's own
captured window text). When it disagrees — finds nothing, or finds a
different verb — the PRIMARY rule's decision and its own captured verb text
are what's used; the secondary parser never overrides the primary verdict in
either direction. Its own known interaction bug (round 3 finding #2, above)
is no longer a security issue because it no longer decides anything — at
worst it makes the deny message name a less precise verb, and the primary
rule still denies correctly regardless (verified: round 3's three bypasses
all deny cleanly under the primary rule with no changes to the secondary
parser at all).

Self-verification of this round-4 rewrite against the PRE-EXISTING test
suite (not a review finding) surfaced one more real gap before it shipped:
``git com"mit" -m msg`` genuinely executes as ``git commit -m msg`` (a shell
removes quote characters and fuses the surrounding text into one token), but
a pure contiguous-substring scan would see ``com`` and ``mit`` as two
separate words with no ``commit`` between them and miss it. Closed by
stripping quote characters in ``_normalize_for_primary_scan`` (see that
function's docstring for why blunt deletion is safe here — it can only ever
FUSE adjacent quoted/unquoted text, never manufacture a false adjacency
across text real whitespace already separates).

nexus-3c92m round 5 (code-review re-review of round 4, TWO MORE Criticals in
the PRIMARY rule itself — the 5th review round in a row): (1) MID-WORD
BACKSLASH ESCAPING was the same "shell removes a character and fuses
adjacent text" class just closed for quotes, left open for backslashes —
``git che\\ckout -- f``, ``g\\it checkout -- f``, ``git ad\\d -N f``, ``git
com\\mit -m msg``, ``git re\\set --hard`` all genuinely execute as their
unescaped form (confirmed against real bash: ``echo git che\\ckout`` prints
``git checkout``; a live, non-destructive ``git sta\\tus`` actually ran as
``git status``). Closed the same way as quote-stripping: ``_collapse_escapes``
in ``_normalize_for_primary_scan`` (see there). (2) The BOUNDED WINDOW was
ITSELF a measurable, exceedable structure — ``git -c user.name=<200 chars>
checkout -- f`` (an ordinary, syntactically valid git global flag padded
long enough) exceeded the ~160-char ceiling and allowed, confirmed to
genuinely execute against real git. This directly contradicted round 4's
own thesis ("the regex does not care how git checkout ended up in the byte
stream, only that it did") — a bounded window is EXACTLY the kind of
structure the redesign exists to eliminate, for the identical reason the
old structural parser kept losing across rounds 1-3: any dimension the
guard measures is a dimension something can exceed. Closed by REMOVING the
bound entirely — ``_primary_match`` (two unbounded linear searches, see
there) replaces the single windowed regex.

nexus-3c92m round 6 (code-review re-review of round 5, a 6th review round):
the primary rule could still be defeated by an EXPANSION spliced INSIDE a
token, one mechanism further out than rounds 4-5's quote/backslash fusion —
real bash confirmed ``g${x:-i}t``, ``g$(echo i)t``, ``git st$(echo a)tus``,
and ``g$'\151't`` all execute as ``git``, so ``git ch${x:-e}ckout -- f``
allowed. Closed with four ordered normalizer steps (see
``_normalize_for_primary_scan`` and the code comments near
``_expand_ansi_c_strings``/``_resolve_param_defaults``/
``_resolve_command_sub_literals`` for the full detail): decode ANSI-C
``$'...'`` strings to their literal characters; resolve parameter-expansion
DEFAULTS (``${name:-lit}`` and siblings) to their literal payload; resolve
``$(echo/printf ...)``/backtick-echo command substitutions whose argument
is itself a literal; then, for whatever expansion syntax SURVIVES all
three, apply an adjacency check.

Round 6's FIRST cut of that adjacency check was itself wrong — see the
round-7 correction immediately below and the ``_EXPANSION_CONSTRUCT_RE``
comment for the shipped design.

nexus-3c92m round 7 (coordinator's own correction of round 6, a 7th
review round): round 6's adjacency check denied ANY subagent Bash command
where a surviving expansion sat glued to a word character on EITHER side —
unconditionally, and independent of whether ``git`` was even present. That
was too broad: it would have denied completely ordinary, non-git subagent
usage like ``echo file${i}.txt``, ``cp "${dir}/a${n}.log" .``, and
``tar xf pkg${ver}.tgz``, none of which have anything to do with a git
write. Round 7 narrows the rule to check WHAT the glued letters actually
spell, not merely THAT something is glued — see the
``_EXPANSION_CONSTRUCT_RE`` comment and ``_find_spliced_expansion`` for the
full two-branch rule (an in-order, gap-allowed SUBSEQUENCE of ``git``
denies unconditionally; a subsequence of a SIMPLE destructive verb denies
only when the command also contains a literal, unspliced ``git``
elsewhere). This is narrower but still catches every round-6 exploit shape
directly (``git ch${x:-e}ckout`` resolves fully via the default-resolution
step above and needs no adjacency check at all; ``git ch$(cmd)ckout -- f``
joins to ``chckout``, an in-order subsequence of ``checkout``, with a
literal ``git`` present) while correctly allowing the ordinary interpolation
patterns above.

nexus-3c92m round 8 (round 7's own re-review, an 8th review round):
round 7's narrowing reopened a hole its OWN scoping choice created — the
``_SIMPLE_VERB_WORDS`` exclusion of compound/hyphenated verbs
(``worktree add/remove/prune``, ``branch -d/-D/-m``, ``tag -d``,
``update-ref``, ``symbolic-ref``, ``filter-branch``, ``reflog expire``,
``cherry-pick``), needed to avoid the ``file``/``filter-branch`` and
``ac``/``branch`` false positives, meant NONE of those verbs were covered
by the adjacency rule at all — and the primary contiguous-substring scan
can never match a compound verb whose own spelling is broken by an
unresolved expansion either, so no other layer covered them. Verified:
``git fil${x}ter-branch``, ``git worktree re${x}move w1``,
``git branch -${x}d b``, ``git tag -${x}d t``, ``git update-${x}ref``,
``git symbolic-${x}ref``, ``git reflog ${x}expire --all``,
``git cherry-${x}pick abc`` all allowed — a REGRESSION from round 6, which
caught every one of these on glue alone (round 6's flaw was denying glue
EVEN WITHOUT ``git`` present, not that it recognized glue itself).

Round 8 does not try to extend the word list — any FINITE list reopens the
identical false-positive class for whatever it's missing, which is exactly
how round 7 lost compound verbs in the first place. Instead it SPLITS on
git-presence (``_find_spliced_expansion``): branch A (no literal ``git``
anywhere in the normalized text) keeps round 7's subsequence-of-``git``
check UNCHANGED; branch B (``git`` present anywhere) reinstates round 6's
unconditional glue check, now SCOPED to git-containing commands only. Glue
alone needs no knowledge of what the verb IS to deny a splice touching it,
so a finite word list can never leave branch B with a gap the way branch
A's old ``_SIMPLE_VERB_WORDS`` sibling did. The accepted, EXPLICIT new cost:
``git log file${i}.txt`` (read-only, harmless) denies, because ``git`` is
present and ``${i}`` is glued to ``file`` — branch B does not check
verb-relevance, only glue. Legitimate whole-word usage
(``git diff -- "$(pwd)/f"``, ``git log $REV``) still allows either way:
neither construct is glued to a word character.

nexus-3c92m round 9 (round 8's own re-review, a 9th review round): round
8's KNOWN LIMITS claimed its residuals were "the ONLY ones that survive
verification" — that claim was itself FALSE, and round 8's re-review
proved it: Branch A (no literal ``git`` present) was defeated by CHAINING
two or more expansion constructs with NOTHING between them.
``g${a}${b}i${c}${d}t checkout -- t3.py`` allowed, and with all four
variables unset, real bash genuinely executes this as ``git checkout``.
Root cause: ``_adjacent_letter_fragments`` computed the touching-letter run
per INDIVIDUAL construct match; two directly-adjacent constructs each only
ever see ONE neighboring letter (the sibling construct blocks the run), so
neither reaches the length->=2 threshold alone even though the combined
reconstruction spans the target. This was a CORRECTNESS BUG in the
machinery, not an inherent limit — Branch B (glue-only, no length
threshold) was never affected, confirmed: a literal ``git`` present
anywhere makes the identical chained construct deny regardless.

Fixed two ways, kept together as defense in depth (not one replacing the
other):

1. ``_expansion_construct_runs`` groups any sequence of directly-touching
   constructs (zero characters between them) into one unit BEFORE
   computing the touching fragment, using the RUN's overall span rather
   than each match's own. This is NOT "lower the length threshold to 1" —
   that would reopen the round-6->round-7 false-positive regression
   (``echo file${i}.txt``'s trailing single-letter touch would then
   trivially subsequence-match); grouping only affects constructs that are
   ALREADY directly adjacent to each other, which ordinary interpolation
   (``${dir}/a${n}`` — ``/``-separated) never is.
2. The ZERO-EXPANSION PASS (``_delete_all_expansions`` + a second
   ``_primary_match`` call in ``body()``): a text with every remaining
   opaque construct deleted OUTRIGHT is scanned by the same primary
   git+verb rule. This is not a heuristic approximation — deleting an
   unresolved ``${a}`` is EXACTLY what real bash does with an unset
   variable, so this pass literally simulates the runtime condition that
   makes the chained-splice exploit work, independent of chain length or
   interleaving pattern, with no fragment reasoning involved at all.

KNOWN LIMITS, inherent to ANY text-substring-based guard and not expected to
be closed by this design. Two residuals (round 9 drops the "ONLY ones that
survive verification" exhaustiveness claim outright, since round 8's
identical claim was proven false one round later — this list states what
is currently known, not a completeness guarantee):

- ``$name`` WHOLE-WORD indirection where the runtime value is NOT
  empty-or-literal: ``git $VERB -- x`` where ``$VERB`` is genuinely SET (at
  runtime, outside this guard's visibility) to something like ``checkout``.
  The zero-expansion pass assumes emptiness (mirroring an unset/empty
  variable, which is exactly what the round-9 exploit class needed) and so
  does NOT catch a variable that is actually populated with dangerous
  content; Branch B denies on glue alone once ``git`` is present, but a
  WHOLE-WORD ``$VERB`` (surrounded by whitespace, exactly the shape a
  legitimate subagent uses, e.g. ``git log $REV``) is never glued to a word
  character on either side, so it is exempt from both branches too. Neither
  mechanism can know what a bare variable resolves to at runtime without
  executing the command.
- DECODE-THEN-EXEC: any write-verb delivered as runtime-assembled or
  encoded/obfuscated data that never places the literal substrings ``git``
  and a write-verb word anywhere in the RAW command text this hook actually
  receives — verified still ALLOWS post-round-9 (none of the resolution
  steps change this: there is no ``git`` substring to anchor on in the
  first place, since the payload is base64-encoded/opaque data, not
  literal text): ``eval $(printf '<base64 of "git checkout ...">' |
  base64 -d)`` and ``echo <base64> | base64 -d | sh`` both still allow.

ACCEPTED FALSE POSITIVES (over-blocking, not under-blocking — a different
class from KNOWN LIMITS above, listed separately so the two are never
conflated): both adjacency branches scan the WHOLE raw command text with
no proximity/segment requirement, so an unrelated, harmless ``git status``
anywhere in a multi-command line gates Branch B for a completely separate
expansion elsewhere on the same line (``git status && echo "add${item} to
list"`` denies) — the same already-accepted trade-off as ``git log
--grep=commit`` (rounds 4-5). Branch A fires on a joined fragment that
EXACTLY equals ``gi``/``gt``/``it``/``git`` (case-insensitive — the only
length->=2 subsequences a 3-letter target admits) even with NOTHING
git-related in the command: ``make i${n}t`` denies (joined fragment
``it``) despite having no ``git`` anywhere. Both narrow and judged
acceptable given this design's stated philosophy (false positives are
cheap; false negatives destroy data).

CLOSED in round 6, no longer limits (corrects earlier revisions of this
docstring that listed them as open): variable indirection via a resolvable
default or literal command substitution (``g=git; $g checkout -- x`` was
already noted as caught in the round-5 text since the literal substring
``git`` sits in the assignment; round 6 goes further — ``git
ch${x:-e}ckout -- x`` and ``git ch$(echo e)ckout -- x`` now resolve to their
literal reconstructed form and are caught directly by the primary scan, and
anything spliced-but-unresolvable, e.g. a bare ``${x}``/``$(python -c
...)``, is caught by the adjacency rule instead). A LEADING BACKSLASH —
``\\git checkout -- x`` — does NOT bypass this guard, under the old
structured parser or any primary-rule round; ``\b`` matches the transition
from the non-word backslash into the word ``git`` the same as it would
after any other non-word character, so the substring scan finds it
regardless. Shell parameter-expansion obfuscation, ``git${IFS}checkout --
x``, was NEVER actually a bypass under the round-4+ substring-scan design
either — IFS-splitting only matters to a TOKENIZING parser, and this design
never tokenizes; the literal substrings ``git`` and ``checkout`` are both
present in the raw text regardless of what character sits between them, so
this always denied. All three of these claims were carried over, unverified,
from before the design that made them true (or made them moot) existed, and
should have been dropped rather than restated — verify empirically before
adding a new KNOWN LIMITS entry, not just before removing one.

``run_hook(fail_closed=False)`` is unchanged: a crash in the hook ITSELF
still allows, since a broken guard must not brick every agent's Bash.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

RULE_NAME = "subagent_git_write_requires_orchestrator"

#: Index/history writers. These are a HYGIENE concern: ``git add`` mutates
#: only the index and destroys nothing, ``git commit`` makes history the
#: orchestrator should own. Undeterminable state fails OPEN for these — a
#: crash must never wedge agent work over tidiness.
_INDEX_WRITE_SUBCOMMANDS = {"commit", "add"}

#: WORKING-TREE DESTROYERS (nexus-ays2l). These delete an orchestrator's
#: uncommitted work outright. The original guard covered only the set above,
#: which is strictly narrower than the set that can cause damage: it blocked
#: the harmless-but-untidy verbs and permitted the destructive ones.
#:
#: Damage signature that produced this bead (2026-07-24): three silent
#: reversions of one file over ~10 minutes with two subagents live, siblings
#: edited in the same window untouched, and NO stash entry and NO reflog entry
#: — exactly the trace ``git checkout -- <path>`` leaves and ``git stash``
#: does not.
#:
#: ``checkout`` is denied outright rather than only for pathspec forms: a
#: subagent switching HEAD in a SHARED tree moves the ground under the
#: orchestrator too. Read-only comparison has other spellings
#: (``git show <ref>:<path>``), which this guard never touches.
_TREE_DESTRUCTIVE_SUBCOMMANDS = {
    "checkout", "restore", "switch", "stash", "clean", "reset", "rm",
}

_WRITE_SUBCOMMANDS = _INDEX_WRITE_SUBCOMMANDS | _TREE_DESTRUCTIVE_SUBCOMMANDS

#: nexus-3c92m round 4 — THE ACTUAL SECURITY BOUNDARY (see module docstring
#: for the full rationale). Structure-agnostic by design: no segment
#: splitting, no heredoc-consumer classification, no substitution
#: extraction. A write-verb word appearing anywhere within ~160 chars AFTER
#: the literal substring ``git`` is enough to deny, regardless of how that
#: text got into the command (heredoc body, pipe target, `$(...)`, quoted
#: string, backtick, process substitution, comment, print statement...).
#: Every alternative is matched inside one outer ``\b...\b`` so a short
#: token like ``am``/``rm``/``mv``/``gc`` cannot match as a substring of an
#: unrelated word (``same``, ``harm``, ``mvfoo``, ``legc`` never match).
_PRIMARY_VERB_ALT = (
    r"checkout|restore|switch|reset|clean|stash|add|commit|merge|rebase|"
    r"cherry-pick|am|apply|push|rm|mv|"
    r"worktree\s+(?:add|remove|prune)|"
    r"branch\s+-[dDm]|"
    r"tag\s+-d|"
    r"update-ref|symbolic-ref|filter-branch|"
    r"reflog\s+expire|gc|prune"
)

#: nexus-3c92m round 5 (code-review CRITICAL, round-4 re-review): the
#: bounded window above was ITSELF a measurable structure — a fixed
#: character-distance ceiling an attacker (or a long, syntactically valid
#: run of ordinary git global flags, e.g. ``git -c user.name=<200 chars>
#: checkout -- f``) can simply exceed. This directly contradicted round 4's
#: own stated thesis ("the regex does not care how git checkout ended up in
#: the byte stream, only that it did") — any dimension the guard measures is
#: a dimension something can exceed, which is exactly why the OLD structural
#: parser kept losing across rounds 1-3. Round 5 removes the bound entirely:
#: two separate linear searches (find the first ``git``, then search
#: UNBOUNDED for a verb token anywhere after it), not one combined regex
#: with a distance cap. There is no longer any distance to pad past.
_GIT_RE = re.compile(r"\bgit\b")
_VERB_RE = re.compile(r"\b(" + _PRIMARY_VERB_ALT + r")\b")


def _primary_match(normalized_text: str) -> re.Match[str] | None:
    """First verb-token match anywhere after the first ``git`` occurrence,
    or ``None``. Two linear passes, no catastrophic-backtracking risk: each
    is a plain alternation/boundary search with no nested quantifiers."""
    git_match = _GIT_RE.search(normalized_text)
    if git_match is None:
        return None
    return _VERB_RE.search(normalized_text, git_match.end())


#: Shell line-continuation (a trailing, unescaped ``\`` — optionally followed
#: by trailing spaces/tabs — immediately before a newline) is collapsed
#: entirely, exactly as a real shell would: the backslash-newline (and any
#: trailing whitespace before it) vanishes, joining the two physical lines
#: with no injected separator. This is the ONLY "parsing" the primary rule
#: does; it runs on already CRLF-normalized text (see
#: ``_normalize_for_primary_scan``) so a continuation followed by ``\r\n``
#: joins exactly like one followed by bare ``\n`` (nexus-3c92m round 3
#: finding #1 — CRLF sitting after the backslash defeated the round-2 fix's
#: trailing-backslash counter, which required the backslash to be the
#: literal last character).
_CONTINUATION_RE = re.compile(r"\\[ \t]*\n")

#: nexus-3c92m round 5 (code-review CRITICAL, round-4 re-review): mid-word
#: backslash escaping is the SAME "shell removes a character and fuses
#: adjacent text into one token" class already closed for quotes below, left
#: open for backslash-escapes — ``git che\ckout -- f``, ``g\it checkout``,
#: ``git ad\d -N f``, ``git com\mit -m msg``, ``git re\set --hard`` all real
#: shell escapes (confirmed: ``echo git che\ckout`` prints ``git checkout``;
#: a live ``git sta\tus`` actually ran as ``git status``). Two forms, tried
#: as one alternation so a genuinely escaped backslash (``\\``, which must
#: collapse to ONE literal backslash) is never mistaken for "backslash
#: escaping the following backslash as an ordinary char" and double-consumed:
#: ``\\`` -> ``\``; a lone ``\`` before ``[A-Za-z0-9_-]`` -> that character
#: alone (backslash dropped). Deliberately narrow to the character class the
#: reviewer's shapes actually exercise, not "any character" — same
#: blunt-but-safe-direction reasoning as quote stripping.
_ESCAPE_RE = re.compile(r"\\\\|\\([A-Za-z0-9_-])")


def _collapse_escapes(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        return "\\" if m.group(0) == "\\\\" else m.group(1)
    return _ESCAPE_RE.sub(_repl, text)


#: nexus-3c92m round 6 (code-review CRITICAL, round-5 re-review): expansions
#: SPLICED INSIDE a token are the SAME "shell removes/replaces a marker and
#: fuses adjacent text" class as rounds 4-5's quote and escape fixes, one
#: mechanism further out — real bash confirmed: ``g${x:-i}t``,
#: ``g$(echo i)t``, ``git st$(echo a)tus``, ``g$'\151't`` all execute as
#: ``git``, so ``git ch${x:-e}ckout -- f`` allowed pre-fix. Four steps, in
#: this specific order (see ``_normalize_for_primary_scan``): decode ANSI-C
#: ``$'...'`` strings to their literal characters; resolve parameter-
#: expansion DEFAULTS (``${name:-lit}`` and siblings) to their literal
#: payload; resolve ``$(echo/printf ...)``/backtick-echo command
#: substitutions whose argument is itself a literal; then, for whatever
#: expansion syntax SURVIVES all three resolutions (nested/opaque
#: substitutions, bare ``$name``/``${name}`` with no default — we cannot
#: know their runtime value), FAIL CLOSED if it sits glued to a word
#: character on either side. A spliced expansion has no honest use in a git
#: invocation; whole-word expansions (``"$(pwd)"``, ``$FILE``) are untouched.
#:
#: ANSI-C ``$'...'`` decode: octal ``\NNN`` (1-3 digits), hex ``\xHH``
#: (1-2 hex digits), ``\uHHHH``/``\UHHHHHHHH`` (unicode), and the common
#: single-character escapes (``\n``/``\t``/``\r``/``\a``/``\b``/``\e``/
#: ``\f``/``\v``/``\\``/``\'``/``\"``) — the whole ``$'...'`` construct,
#: including its delimiters, is replaced by the decoded literal. Runs
#: BEFORE the generic escape-collapse and quote-strip below: it depends on
#: the literal single-quote delimiter being intact (escape-collapse would
#: otherwise eat the backslash off an octal/hex escape like ``\151``,
#: corrupting the marker before this step ever sees it).
_ANSI_C_STRING_RE = re.compile(r"\$'([^']*)'")
_SIMPLE_ANSI_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
    "e": "\x1b", "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"',
}
_ANSI_C_ESCAPE_RE = re.compile(
    r"\\(?P<oct>[0-7]{1,3})"
    r"|\\x(?P<hex>[0-9A-Fa-f]{1,2})"
    r"|\\u(?P<uni4>[0-9A-Fa-f]{4})"
    r"|\\U(?P<uni8>[0-9A-Fa-f]{8})"
    r"|\\(?P<simple>[ntrabefv\\'\"])"
)


def _decode_ansi_c_escape(m: re.Match[str]) -> str:
    if m.group("oct"):
        return chr(int(m.group("oct"), 8) & 0xFF)
    if m.group("hex"):
        return chr(int(m.group("hex"), 16) & 0xFF)
    if m.group("uni4"):
        return chr(int(m.group("uni4"), 16))
    if m.group("uni8"):
        return chr(int(m.group("uni8"), 16))
    return _SIMPLE_ANSI_ESCAPES[m.group("simple")]


def _expand_ansi_c_strings(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        return _ANSI_C_ESCAPE_RE.sub(_decode_ansi_c_escape, m.group(1))
    return _ANSI_C_STRING_RE.sub(_repl, text)


#: Parameter-expansion DEFAULTS: ``${name:-lit}``, ``${name-lit}``,
#: ``${name:=lit}``, ``${name:+lit}`` all resolve to the literal ``lit``
#: text for scan purposes. This is the SAFE (conservative) reading for all
#: four forms, including ``:+`` (which real bash only substitutes when
#: ``name`` is set and non-empty) — we cannot know ``name``'s runtime value,
#: and assuming ``lit`` unconditionally is the fail-closed direction: it can
#: only ever cause an extra (accepted) false positive, never hide a real
#: substitution. ``lit`` itself is restricted to a plain-literal character
#: class (no nested ``{``/``}``/``$``/backtick) — anything more complex
#: doesn't match here and falls through to the adjacency check below
#: unresolved, which is also fail-closed if it turns out to be glued.
_PARAM_DEFAULT_RE = re.compile(
    r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+)(?::-|:=|:\+|-)([^{}$`]*)\}"
)


def _resolve_param_defaults(text: str) -> str:
    return _PARAM_DEFAULT_RE.sub(lambda m: m.group(1), text)


#: Command substitutions whose entire payload is a literal ``echo``/
#: ``printf`` invocation resolve to that literal (common flags like
#: ``-n``/``-e`` stripped, an optional single layer of ``'``/``"`` quoting
#: around the argument stripped). ``$(...)`` and backtick forms handled
#: separately since their closing delimiters differ and must not cross-match
#: (a backtick-opened span closing on a stray ``)`` would silently consume
#: the wrong text). Anything else — ``$(python -c ...)``, ``$(git status)``,
#: a NESTED substitution like ``$(echo $(echo e))`` (the inner ``$(echo e)``
#: breaks this regex's no-parens argument class) — is left untouched,
#: opaque, for the adjacency check to reason about instead of guessing.
_DOLLAR_PAREN_ECHO_RE = re.compile(
    r"\$\(\s*(?:echo|printf)\s+(?:-[A-Za-z]+\s+)*(['\"]?)([^()'\"]*)\1\s*\)"
)
_BACKTICK_ECHO_RE = re.compile(
    r"`\s*(?:echo|printf)\s+(?:-[A-Za-z]+\s+)*(['\"]?)([^`'\"]*)\1\s*`"
)


def _resolve_command_sub_literals(text: str) -> str:
    text = _DOLLAR_PAREN_ECHO_RE.sub(lambda m: m.group(2), text)
    return _BACKTICK_ECHO_RE.sub(lambda m: m.group(2), text)


def _normalize_for_primary_scan(command: str) -> str:
    """CRLF/CR -> LF, collapse continuations, decode/resolve expansions,
    collapse escapes, strip quotes.

    Order matters throughout:

    1. Normalizing line endings FIRST means a continuation backslash
       followed by ``\\r\\n`` is treated identically to one followed by bare
       ``\\n`` — the round-3 CRLF bypass no longer has anywhere to hide.
       Continuation collapse also guarantees no bare ``\\<newline>``
       sequence survives into later steps (a continuation IS exactly that
       shape).
    2. ANSI-C ``$'...'`` decode runs BEFORE escape-collapse: it depends on
       the literal ``'`` delimiters and on ``\\NNN``/``\\xHH`` markers being
       intact, both of which the later, generic escape-collapse would
       otherwise corrupt.
    3. Escape-collapse (round 5) runs before quote-strip so a stray
       backslash before an ordinary character (``git che\\ckout``) is fused
       the same way a stray quote would be.
    4. Parameter-expansion defaults and command-substitution literals
       (round 6) resolve next, while quotes (used as their own delimiters,
       e.g. ``$(echo "e")``) are still present to parse against.
    5. Quote-strip (round 4) runs LAST among the text transforms —
       deliberately after step 4, not before: quote removal can ITSELF
       create a new glued adjacency (``foo"$(pwd)"bar`` -> ``foo$(pwd)bar``)
       that the adjacency check (run by the caller on this function's
       output, see ``_find_spliced_expansion``) must see, not miss.

    Escape collapsing, quote stripping, and expansion resolution are all the
    SAME class of fix applied to different syntax: a real unquoted shell
    removes an escaping backslash, a quote mark, or an expansion marker and
    fuses the surrounding text into one token, so ``git com"mit" -m msg``,
    ``git com\\mit -m msg``, and ``git ch${x:-e}ckout -- f`` all genuinely
    execute ``git commit``/``git checkout``. Blunt, syntax-aware
    replacement (rather than full shell semantics) is deliberate throughout:
    each step only ever FUSES already-adjacent text or substitutes a
    provably-literal payload, it never invents a false ``git``-verb
    adjacency that was not already present modulo the resolved/stripped
    syntax.
    """
    text = command.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTINUATION_RE.sub("", text)
    text = _expand_ansi_c_strings(text)
    text = _collapse_escapes(text)
    text = _resolve_param_defaults(text)
    text = _resolve_command_sub_literals(text)
    return text.replace("'", "").replace('"', "")


#: nexus-3c92m round 7 (coordinator correction of round 6): the round-6
#: ADJACENCY RULE denied ANY expansion glued to a word character on EITHER
#: side, unconditionally — too broad, since it flagged ordinary subagent
#: usage with no exploit potential at all: ``echo file${i}.txt``,
#: ``cp "${dir}/a${n}.log" .``, ``tar xf pkg${ver}.tgz``. Round 7 narrowed
#: it to a letter-fragment SUBSEQUENCE check against ``git`` itself and a
#: SIMPLE (single-token) verb word list — but round 7's OWN re-review
#: (an 8th review round) found that narrowing reopened a hole: a finite
#: simple-verb word list can NEVER cover the compound/hyphenated verbs
#: (``worktree add/remove/prune``, ``branch -d/-D/-m``, ``tag -d``,
#: ``update-ref``, ``symbolic-ref``, ``filter-branch``, ``reflog expire``,
#: ``cherry-pick``) that were deliberately EXCLUDED from that list to avoid
#: the ``file``/``filter-branch`` and ``ac``/``branch`` false positives —
#: and the primary contiguous-substring scan (``_VERB_RE``) can never match
#: a compound verb whose own spelling is broken by an unresolved expansion
#: either. Neither layer covered ``git fil${x}ter-branch``,
#: ``git worktree re${x}move w1``, ``git branch -${x}d b``, etc. — all
#: allowed, a REGRESSION from round 6 (which caught these on glue alone,
#: before round 7 narrowed to "what does it spell").
#:
#: Round 8 resolves this not by extending the word list (any FINITE list
#: reopens the same class of false positive for whatever it's missing) but
#: by SPLITTING the rule on whether a literal ``git`` is present anywhere
#: in the (already fully normalized — ANSI-C/default/command-sub resolved)
#: text (``_find_spliced_expansion``):
#:
#: - Branch A, NO literal ``git`` present: unchanged from round 7 — take
#:   the maximal ``[A-Za-z]``-only runs touching the construct on each side
#:   (``_adjacent_letter_fragments``, letters only, not digits/underscore)
#:   and join them; deny only if that joined string (length >= 2) is an
#:   in-order (gaps allowed, case-insensitive) SUBSEQUENCE of the literal
#:   word ``git`` itself. This is what still allows ``echo file${i}.txt``/
#:   ``cp "${dir}/a${n}.log" .``/``tar xf pkg${ver}.tgz`` (none of their
#:   joined fragments are subsequences of the 3-letter target ``git``).
#: - Branch B, a literal ``git`` IS present anywhere: the round-6 rule,
#:   reinstated but now SCOPED to git-containing commands — deny if the
#:   construct sits glued to a WORD character (``[A-Za-z0-9_]``, not just
#:   letters) on either side, full stop, independent of what it spells.
#:   This is what closes the compound-verb gap: it does not need to KNOW
#:   the verb is ``filter-branch`` or ``worktree remove`` to deny a splice
#:   touching it, so a finite word list can never leave it uncovered. The
#:   accepted cost is a genuine false positive the coordinator named
#:   explicitly: ``git log file${i}.txt`` (a read-only, harmless command)
#:   denies, because ``git`` is present and ``${i}`` is glued to ``file``.
#:   Legitimate whole-word usage (``git diff -- "$(pwd)/f"``,
#:   ``git log $REV``) still allows: neither construct is glued to a word
#:   character on either side.
#:
#: Subsequence (branch A) rather than substring/prefix/suffix matching is
#: deliberately loose (accepts extra false positives) because gaps are
#: exactly what a splice attack introduces — the whole ``${x:-e}``
#: reconstruction of ``checkout`` from fragments ``ch``+``ckout`` produces a
#: joined string (``chckout``) that is NOT a contiguous substring of
#: ``checkout`` but very much IS an in-order subsequence of it. Branch B
#: needs no such matching at all — glue alone decides once ``git`` is
#: confirmed present, which is exactly what makes it immune to the
#: finite-word-list problem branch A's old sibling (round 7's dropped
#: ``_SIMPLE_VERB_WORDS`` branch) had.
#:
#: One level of nesting supported for ``$(...)`` (matches the required
#: ``$(echo $(echo e))`` shape) and ``${...}``; deeper nesting is a known,
#: accepted residual (same "not expected to be closed" class as the other
#: KNOWN LIMITS below) rather than something worth a recursive parser here.
_EXPANSION_CONSTRUCT_RE = re.compile(
    r"\$\((?:[^()]|\([^()]*\))*\)"
    r"|`[^`]*`"
    r"|\$\{[^{}]*\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$[0-9]+"
)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _adjacent_letter_fragments(text: str, start: int, end: int) -> str:
    """The maximal ``[A-Za-z]`` run touching *start* on the left, joined
    with the maximal such run touching *end* on the right.

    Deliberately a bounded character walk, NOT ``text[:start]`` fed to a
    ``$``-anchored regex: that slice is ``O(start)`` on every call, and with
    many expansion constructs scattered through a large command (adversarial
    perf case), the cost of repeatedly re-slicing an ever-larger prefix
    becomes ``O(n^2)`` overall — measured directly: 2+ SECONDS on a ~45KB
    command with 6000 constructs, against a 50ms budget. Walking outward
    from the match boundary costs only the fragment's own (small) length
    per call, independent of the match's position in the text.

    Still computed unconditionally in round 8 (even on the branch-B path,
    where it is NOT what decides the outcome) purely to name a plausible
    reconstructed fragment in the deny message — informational only there.
    """
    i = start
    while i > 0 and text[i - 1].isascii() and text[i - 1].isalpha():
        i -= 1
    j = end
    n = len(text)
    while j < n and text[j].isascii() and text[j].isalpha():
        j += 1
    return text[i:start] + text[end:j]


def _is_subsequence(query: str, target: str) -> bool:
    it = iter(target)
    return all(ch in it for ch in query)


#: nexus-3c92m round 9 (code-review CRITICAL, round-8 re-review): Branch A
#: was defeated by CHAINING two or more expansion constructs back-to-back
#: with NOTHING between them (``g${a}${b}i${c}${d}t checkout -- t3.py`` —
#: real bash with all four vars unset genuinely prints ``git checkout``).
#: ``_adjacent_letter_fragments`` computed the touching letter-run per
#: INDIVIDUAL ``_EXPANSION_CONSTRUCT_RE`` match; when two constructs sit
#: directly adjacent, each one's local view only ever picks up ONE
#: neighboring letter (the sibling construct blocks the run from extending
#: further), so NEITHER individual match's joined fragment reached the
#: required length-2 threshold, even though the full multi-hop
#: reconstruction spells the target. Root cause confirmed by hand: for
#: ``${a}${b}`` sitting between ``g`` and ``i``, ``${a}`` alone sees
#: left=``g``/right=`` `` (blocked by ``${b}`` immediately following) and
#: ``${b}`` alone sees left=`` ``/right=``i`` — both length-1, neither
#: reaches the threshold, though together they obviously span ``g``...``i``.
#:
#: Fixed by grouping matches into RUNS first: any sequence of
#: ``_EXPANSION_CONSTRUCT_RE`` matches with ZERO characters between them
#: (``matches[k+1].start() == matches[k].end()``) is one unit, and the
#: touching-fragment computation uses the RUN's overall span (first
#: match's start, last match's end) rather than each match's own —
#: ``_adjacent_letter_fragments`` itself is UNCHANGED, only what span it is
#: called with. This is NOT "lower the length threshold to 1", which would
#: reopen the round-6->round-7 false-positive regression (a single-letter
#: touching fragment, e.g. ``echo file${i}.txt``'s trailing `` `` before
#: ``.txt``, would trivially subsequence-match). Ordinary interpolation
#: (``${dir}/a${n}`` — ``/``-separated) is unaffected: a non-word character
#: between two constructs breaks the run, so they stay separate units with
#: their own (short) fragments, exactly as before.
#:
#: Applied uniformly to BOTH branches (not just A, which needed it): branch
#: B's per-construct glue check was already correct without this fix (ANY
#: individual construct's own glue already fires it, chain or no chain —
#: confirmed by the reviewer), but grouping keeps the joined-fragment TEXT
#: reported for messaging consistent between the two branches, "so the two
#: mechanisms agree" per the coordinator's round-9 instruction.
def _expansion_construct_runs(text: str) -> list[tuple[int, int]]:
    """Non-overlapping ``(start, end)`` spans, one per maximal run of
    directly-touching (zero characters between) expansion constructs."""
    matches = list(_EXPANSION_CONSTRUCT_RE.finditer(text))
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(matches)
    while i < n:
        run_start = matches[i].start()
        j = i
        while j + 1 < n and matches[j + 1].start() == matches[j].end():
            j += 1
        runs.append((run_start, matches[j].end()))
        i = j + 1
    return runs


def _find_spliced_expansion(normalized_text: str) -> str | None:
    """A string naming the reconstructed/glued expansion (RUN, possibly
    spanning several directly-touching constructs) that triggers a deny
    under the round-8/9 rule, or ``None``. See the module-level comment
    above ``_EXPANSION_CONSTRUCT_RE`` for the two-branch rule and the
    comment directly above for why runs (not individual matches) are used."""
    literal_git_present = _GIT_RE.search(normalized_text) is not None
    for start, end in _expansion_construct_runs(normalized_text):
        joined = _adjacent_letter_fragments(normalized_text, start, end)
        if literal_git_present:
            # Branch B: glue alone decides, regardless of what it spells —
            # this is what covers compound/hyphenated verbs a finite word
            # list can never fully enumerate.
            glued_before = start > 0 and _is_word_char(normalized_text[start - 1])
            glued_after = end < len(normalized_text) and _is_word_char(normalized_text[end])
            if glued_before or glued_after:
                return joined or normalized_text[start:end]
        elif len(joined) >= 2 and _is_subsequence(joined.lower(), "git"):
            # Branch A: no git anywhere -- only deny if the touching letters
            # could themselves spell "git".
            return joined
    return None


#: nexus-3c92m round 9 — the ZERO-EXPANSION PASS. The coordinator's
#: "principled closure": rather than trying to make the fragment heuristic
#: handle every chain length and interleaving pattern, build a SECOND text
#: with every remaining opaque expansion construct DELETED OUTRIGHT (not
#: replaced with anything) and run the SAME ``git``-then-verb scan
#: (``_primary_match``) on it too. Deleting ``${a}${b}`` between ``g`` and
#: ``i`` and ``${c}${d}`` between ``i`` and ``t`` in the round-9 repro
#: leaves the literal, contiguous text ``git checkout`` with REAL word
#: boundaries — caught directly by the existing primary scan, no fragment
#: reasoning needed at all. This is deliberately the same assumption real
#: bash makes for an UNSET (or empty) variable: ``${a}`` with ``a`` unset
#: expands to nothing, so this pass is not a heuristic approximation of the
#: exploit, it is a literal simulation of the exact runtime condition that
#: makes the exploit work. Kept ALONGSIDE the fragment rule (not replacing
#: it) as defense in depth — see body(): the primary gate denies if EITHER
#: the normalized text OR its zero-expansion form matches.
def _delete_all_expansions(text: str) -> str:
    return _EXPANSION_CONSTRUCT_RE.sub("", text)


#: Read-only spellings of otherwise-destructive verbs, allowlisted per the
#: bead's preference for allowlisting reads over blanket-denying the verb, so
#: a reviewer's ``git stash list`` keeps working.
_READ_ONLY_FORMS: dict[str, set[str]] = {
    "stash": {"list", "show"},
}

#: git global flags that take a VALUE argument before the subcommand
#: (``git -C path commit``); the value must be skipped when locating the
#: subcommand token.
_VALUED_GLOBAL_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _git_subcommand(tokens: list[str]) -> str | None:
    """Return the git subcommand of ``tokens`` (a shell segment), or None."""
    if not tokens or tokens[0] != "git":
        return None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in _VALUED_GLOBAL_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            # value-carrying --flag=value or boolean global flag
            i += 1
            continue
        return tok
    return None


#: nexus-3c92m (2026-08-20 incident #2): the intra-line splitter above never
#: covered bare NEWLINES. A realistic multi-line Bash tool invocation
#: (``cd dir\necho ...\npython3 - <<'EOF'\n...\nEOF\ngit checkout -- path``)
#: has no ``&&``/``;``/``then``/``do`` joining its statements at all — each
#: line is its own statement by bare newline, which is exactly how the
#: reviewer's real denied-by-nothing ``git checkout`` ran (verified against
#: the live transcript, not merely hypothesized: three Bash calls, each
#: ``permissionDecision=allow``, each shaped as a multi-line script with the
#: destructive verb on a LATER line after a heredoc body). ``_git_subcommand``
#: only inspects ``tokens[0]`` of whatever segment it is given, and the old
#: splitter treated the WHOLE multi-line blob as one segment — so
#: ``tokens[0]`` was ``cd``, and the ``git checkout`` later in the same
#: segment's token stream was never looked at.
#:
#: Heredoc bodies need their OWN handling rather than being swept up by a
#: blanket newline split: a heredoc fed to a real shell (``bash``/``sh``/...)
#: IS executable shell code and must be scanned; a heredoc fed to ``python3``,
#: ``cat``, ``psql``, etc. is opaque DATA to that program, not shell code, and
#: scanning it risks a false deny on a heredoc body that merely mentions
#: ``git checkout`` as a string literal (verified: this exact shape — a
#: python heredoc printing the text ``"git checkout -- t3.py"`` — must stay
#: ALLOWED). So heredoc bodies are classified by their consumer: shell
#: consumers are scanned, everything else is skipped outright (never
#: tokenized, never contributes a match either way).
#:
#: nexus-3c92m review round 2 (critic ship-blocker): the FIRST cut of this
#: regex, ``<<(-?)\s*...``, also matched inside the bash HERE-STRING operator
#: ``<<<`` whenever its RHS started with a letter/underscore — e.g.
#: ``cat <<< "hello"`` falsely opened a "heredoc" that swallowed every
#: following line (including a destructive verb) up to EOF as an unscanned
#: "body," since ``cat`` is not a recognized shell consumer. The negative
#: lookaround rejects a ``<<`` that is itself part of a longer run of ``<``
#: (``<<<``), leaving the genuine two-``<`` heredoc operator matched exactly
#: as before.
_HEREDOC_OPEN_RE = re.compile(
    r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2"
)

#: nexus-3c92m review round 2 (code-review CRITICAL #2): membership was exact
#: string equality against the bare command name, so a PATH-qualified
#: consumer (``/bin/bash <<EOF``) matched nothing and its heredoc body
#: (including a destructive verb) was silently dropped as "non-shell."
#: ``_consumer_is_shell`` below matches each head token's ``basename`` instead
#: of the raw token, so ``/bin/bash``, ``/usr/bin/env bash`` (a plain ``bash``
#: token already matched; ``env`` itself is now also in the set below so
#: ``env bash`` matches even before reaching the ``bash`` token),
#: ``/opt/homebrew/bin/zsh``, etc. are all recognized. The set is also
#: widened beyond the bare shell binaries to cover the other common ways a
#: heredoc body ends up interpreted as shell code: ``env``/``sudo`` (wrapper
#: prefixes -- the real consumer is a later token, already covered by the
#: any-token scan, but including the wrapper itself is harmless and cheap),
#: ``xargs`` (frequently paired with a shell target, e.g. ``xargs sh``, where
#: ``sh`` already matches -- ``xargs`` itself is included so a heredoc handed
#: straight to ``xargs`` without a following shell name still fails safe
#: toward scanning), and ``eval``/``source`` (shell builtins that execute
#: their input as shell code directly).
_SHELL_HEREDOC_CONSUMERS = {
    "bash", "sh", "zsh", "dash", "ksh",
    "env", "sudo", "xargs", "eval", "source",
}


def _consumer_is_shell(head: str) -> bool:
    return any(
        os.path.basename(tok) in _SHELL_HEREDOC_CONSUMERS
        for tok in re.findall(r"[\w./-]+", head)
    )


#: ``$(...)``/backtick command substitutions and ``<(...)``/``>(...)`` process
#: substitutions are not separate lines or ``&&``-joined segments, so neither
#: the newline walk nor the intra-line split ever sees their contents as an
#: independent segment. Extracted here (single nesting level — sufficient for
#: a heuristic guard, not a shell parser) and fed through the same matcher as
#: any other segment.
#:
#: nexus-3c92m review round 2 (critic SIGNIFICANT): the first cut ran these
#: regexes against the RAW ``command`` text, so a ``$(...)``/backtick span
#: sitting INSIDE an opaque (non-shell) heredoc body was still extracted and
#: matched — contradicting this module's own stated invariant that such a
#: body is data, not code. ``_matched_write_subcommands`` below now runs
#: these extractors against the heredoc-FILTERED text (the joined output of
#: ``_iter_heredoc_aware_lines``), not the original ``command``.
_SUBSHELL_RE = re.compile(r"\$\(([^()]*)\)")
_BACKTICK_RE = re.compile(r"`([^`]*)`")
_PROCESS_SUB_RE = re.compile(r"[<>]\(([^()]*)\)")

#: nexus-3c92m review round 2 (code-review CRITICAL #1): a physical-line
#: newline split treats shell line-continuation (a trailing, unescaped ``\``
#: immediately before the newline) as a hard statement boundary, splitting
#: ``git`` from its own subcommand across two segments — e.g.
#: ``"git \\\ncheckout -- t3.py"`` (real shell: one logical line,
#: ``git checkout -- t3.py``) produced segments ``["git \\", "checkout --
#: t3.py"]``, neither of which has ``git`` as tokens[0] together with the
#: subcommand. Joined BEFORE any newline-based segmentation (including
#: heredoc-open detection), mirroring real shell semantics: the trailing
#: backslash is removed and the next physical line is concatenated directly
#: (no injected separator — shell does not insert one either).
_TRAILING_BACKSLASH_COUNT_RE = re.compile(r"\\*$")


def _join_line_continuations(command: str) -> str:
    lines = command.split("\n")
    out: list[str] = []
    buf: str | None = None
    for line in lines:
        text = buf + line if buf is not None else line
        trailing = len(_TRAILING_BACKSLASH_COUNT_RE.search(text).group())
        if trailing % 2 == 1:
            # odd count: the LAST backslash is unescaped -> continuation.
            buf = text[:-1]
            continue
        out.append(text)
        buf = None
    if buf is not None:
        out.append(buf)  # unterminated continuation at end of text
    return "\n".join(out)


def _iter_heredoc_aware_lines(command: str) -> list[str]:
    """Physical lines of *command* with non-shell heredoc bodies dropped.

    The line that OPENS a heredoc is truncated at the ``<<`` operator (its
    head is still a normal candidate line — e.g. a pipeline feeding a
    heredoc). Body lines are included verbatim when the heredoc's opening
    line names a shell consumer, and omitted entirely otherwise. The
    terminator line itself is never emitted.
    """
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _HEREDOC_OPEN_RE.search(line)
        if not m:
            out.append(line)
            i += 1
            continue
        head = line[: m.start()]
        out.append(head)
        strip_tabs = bool(m.group(1))
        delimiter = m.group(3)
        consumer_is_shell = _consumer_is_shell(head)
        i += 1
        body: list[str] = []
        while i < n:
            candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
            if candidate == delimiter:
                i += 1  # consume the terminator line too
                break
            body.append(lines[i])
            i += 1
        else:
            # unterminated heredoc (truncated command text) — nothing more
            # to scan either way; the body collected so far is used below.
            pass
        if consumer_is_shell:
            out.extend(body)
        # else: opaque data to a non-shell consumer — dropped, never scanned.
    return out


def _matched_write_subcommands(command: str) -> set[str]:
    """Write subcommands present in *command*, minus read-only spellings.

    Returns the SET rather than a bool (nexus-ays2l) because the caller has to
    distinguish index-hygiene verbs from working-tree destroyers: they get
    different fail modes when the worktree state is undeterminable.
    """
    matched: set[str] = set()
    command = _join_line_continuations(command)
    heredoc_filtered_lines = _iter_heredoc_aware_lines(command)
    heredoc_filtered_text = "\n".join(heredoc_filtered_lines)
    pieces = list(heredoc_filtered_lines)
    pieces.extend(m.group(1) for m in _SUBSHELL_RE.finditer(heredoc_filtered_text))
    pieces.extend(m.group(1) for m in _BACKTICK_RE.finditer(heredoc_filtered_text))
    pieces.extend(m.group(1) for m in _PROCESS_SUB_RE.finditer(heredoc_filtered_text))
    segments: list[str] = []
    for piece in pieces:
        segments.extend(re.split(r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)", piece))
    for segment in segments:
        try:
            candidates = [shlex.split(segment, posix=True)]
        except ValueError:
            # nexus-2e874: never silently skip a segment shlex rejects for
            # unbalanced quoting — that direction fully bypassed the guard
            # (a stray quote in any argument made a subagent `git stash`
            # invisible). Degrade to rough token variants; a match in any
            # variant counts.
            candidates = _lib.degraded_token_variants(segment)
        for tokens in candidates:
            sub = _git_subcommand(tokens)
            if sub not in _WRITE_SUBCOMMANDS:
                continue
            if sub in _READ_ONLY_FORMS and _read_only_form(tokens, sub):
                continue
            matched.add(sub)
    return matched


def _read_only_form(tokens: list[str], sub: str) -> bool:
    """True iff this invocation is a read-only spelling (``git stash list``).

    Only the token immediately following the subcommand is considered, and
    only an exact match against the allowlist counts — so ``git stash`` bare
    (which STASHES, destroying the tree) is never mistaken for a read.
    """
    try:
        idx = tokens.index(sub)
    except ValueError:
        return False
    for tok in tokens[idx + 1:]:
        if tok.startswith("-"):
            continue
        return tok in _READ_ONLY_FORMS[sub]
    return False  # bare `git stash` — destructive


def _in_linked_worktree(cwd: str) -> bool | None:
    """True iff ``cwd`` is inside a linked git worktree (not the primary
    checkout). ``None`` when undeterminable (not a repo, git missing,
    timeout) — the caller treats None as fail-open."""
    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — undeterminable: fail open
        return None
    if git_dir.returncode != 0 or common.returncode != 0:
        return None
    gd = os.path.realpath(os.path.join(cwd, git_dir.stdout.strip()))
    cd = os.path.realpath(os.path.join(cwd, common.stdout.strip()))
    return gd != cd


def _deny_message(agent_type: str, verb_names: str, *,
                  undeterminable: bool = False) -> str:
    who = f"you are subagent `{agent_type or 'unknown'}`"
    head = (
        f"Subagents never run git write verbs ({verb_names}) in the shared "
        f"tree ({who}). This guard is intentionally STRUCTURE-AGNOSTIC "
        f"(nexus-3c92m round 4, after three rounds of a structural parser "
        f"growing a new bypass every round): it denies whenever `git` "
        f"appears near a write verb ANYWHERE in the raw command text, "
        f"regardless of shell structure — heredocs, pipes, substitutions, "
        f"line-continuations, quoting, and comments create no exemption. "
        f"A false positive here just costs a rephrase or a hand-back; a "
        f"false negative destroys uncommitted work outright (this bead's "
        f"own origin: a reviewer's `git checkout -- <file>` silently wiped "
        f"three uncommitted production fixes)."
    )
    if undeterminable:
        head += (
            "\nThis tree's worktree state could not be determined, and these "
            "verbs FAIL CLOSED regardless: an undeterminable tree is not a "
            "licence to proceed."
        )
    head += (
        "\nNeed to compare working-tree content against a committed ref "
        "instead of resetting to it? Falsify by comparison, never by "
        "mutation: `git show HEAD:<path> > /private/tmp/<scratch>/<name>` "
        "then `diff /private/tmp/<scratch>/<name> <path>`."
    )
    return (
        f"{head}\n"
        f"Hand back your changes as diffs + file paths via SendMessage; the "
        f"ORCHESTRATOR commits, pathspec-limited (RDR-184 Gap-4, "
        f"feedback_orchestration_friction).\n"
        f"Rephrase without the literal git verb, or hand the edit back to "
        f"the orchestrator to run.\n"
        f"Read-only git (status/diff/log/show/blame/rev-parse/ls-files) "
        f"stays allowed as long as the command text doesn't ALSO contain a "
        f"write-verb word near `git` (e.g. in a `--grep` pattern or a "
        f"comment) — that is an accepted false-positive cost of this design, "
        f"not a bug.\n"
        f"Worktree-isolated agents are exempt automatically when this guard "
        f"can POSITIVELY PROVE a linked worktree (a linked worktree is "
        f"yours to destroy); an undeterminable worktree state does not "
        f"qualify.\n"
        f"To override deliberately, append `# routing-allow: <reason>` "
        f"(>=8 chars)."
    )


def _spliced_expansion_deny_message(agent_type: str, detail: str, *,
                                    git_present: bool) -> str:
    who = f"you are subagent `{agent_type or 'unknown'}`"
    if git_present:
        head = (
            f"Expansion spliced inside a word; rephrase: expansions inside "
            f"words are not allowed in git commands; use a whole-word "
            f"variable, or hand the edit to the orchestrator ({who}). This "
            f"command contains `git`, and an expansion (near `{detail}`) "
            f"sits glued to a word character with no whitespace, quote, or "
            f"shell operator between. Real bash executes such splices AS "
            f"their resolved literal text, so this is indistinguishable "
            f"from whatever literal command it can produce at runtime — "
            f"including compound/hyphenated verbs (`filter-branch`, "
            f"`worktree remove`, `cherry-pick`, ...) that no finite word "
            f"list can fully enumerate, which is why this denies on the "
            f"GLUE ALONE once `git` is present, rather than trying to "
            f"guess what the splice spells (nexus-3c92m round 8)."
        )
    else:
        head = (
            f"Expansion spliced inside a word; rephrase with the literal "
            f"command or hand the edit to the orchestrator ({who}). The "
            f"letters immediately touching an expansion in this command "
            f"(`{detail}`) reconstruct `git` if the expansion resolves the "
            f"way it looks like it's meant to (nexus-3c92m round 8)."
        )
    head += (
        f"\nOrdinary interpolation stays allowed: `file${{i}}.txt`, "
        f"`\"${{dir}}/a${{n}}.log\"`, `$(pwd)/sub`, `git diff -- \"$(pwd)/f\"`, "
        f"and `git log $REV` are all untouched — a whole-word expansion "
        f"(surrounded by whitespace/quotes/operators, not glued to a word "
        f"character) is never denied by this rule, git-present or not.\n"
        f"To override deliberately, append `# routing-allow: <reason>` "
        f"(>=8 chars)."
    )
    return head


def body(payload: dict[str, Any]) -> None:
    agent_id = str(payload.get("agent_id") or "")

    if not agent_id:
        _lib.allow()  # main conversation — the rule targets subagents only

    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()

    normalized = _normalize_for_primary_scan(command)

    # ADJACENCY RULE (nexus-3c92m rounds 8-9): a SEPARATE gate, checked
    # before (and independent of) the git+verb scan below. Two branches
    # keyed on whether `git` is present anywhere — see the
    # `_EXPANSION_CONSTRUCT_RE` comment and `_find_spliced_expansion` for
    # the full rule.
    spliced_fragment = _find_spliced_expansion(normalized)

    # PRIMARY RULE (nexus-3c92m rounds 4-5, extended round 9): the actual
    # security boundary for git-write detection. See module docstring for
    # the full rationale. Skipped when `spliced_fragment` already denies,
    # since a spliced expansion could resolve to anything at runtime and
    # the git+verb scan's "no match" verdict on the CURRENT (unresolved)
    # text would be meaningless — the adjacency deny below is authoritative
    # regardless.
    #
    # Round 9: matched against BOTH the normalized text AND its
    # ZERO-EXPANSION form (`_delete_all_expansions` — every remaining
    # opaque construct deleted outright, simulating the "all vars unset"
    # runtime condition that makes a chained splice like
    # `g${a}${b}i${c}${d}t checkout` genuinely execute as `git checkout`).
    # Deny if EITHER matches — this is the "principled closure" that
    # catches any chain length or interleaving pattern without relying on
    # the fragment heuristic's own (now also fixed, see
    # `_expansion_construct_runs`) chain-grouping logic.
    primary_match = None
    if not spliced_fragment:
        primary_match = _primary_match(normalized) or _primary_match(
            _delete_all_expansions(normalized)
        )
    if not spliced_fragment and primary_match is None:
        _lib.allow()

    # Match FIRST, escape SECOND (the nexus-mzvwa.8 telemetry rule) — applies
    # to EITHER gate above.
    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
            escape_reason=_lib.extract_escape_reason(command),
        )
        _lib.allow()

    cwd = str(payload.get("cwd") or "") or os.getcwd()
    worktree = _in_linked_worktree(cwd)
    if worktree is True:
        # Linked worktree, POSITIVELY PROVEN: the agent owns its tree,
        # including destroying it. This is the ONLY exemption from either
        # gate's verdict, applied uniformly.
        _lib.allow()
    # worktree is False (primary checkout) OR None (undeterminable) — EITHER
    # GATE WINS either way (nexus-3c92m round 4; retires the old nexus-ays2l
    # item 3 fail-open-for-hygiene-verbs carve-out). "I could not prove this
    # tree is safe" no longer earns a pass. The `# routing-allow:` escape
    # above remains for deliberate exceptions.

    agent_type = str(payload.get("agent_type") or "")

    if spliced_fragment:
        git_present = _GIT_RE.search(normalized) is not None
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="deny", tool_name="Bash",
            command_fragment=command,
        )
        _lib.deny(
            _spliced_expansion_deny_message(
                agent_type, spliced_fragment, git_present=git_present,
            ),
            summary=(
                f"subagent Bash command denied: an expansion "
                f"({'glued to a word char, git present' if git_present else 'reconstructs ' + spliced_fragment}) "
                f"(nexus-3c92m round 8 adjacency rule)."
            ),
        )
        return

    # SECONDARY (structured parser): naming only, never decides. Preferred
    # when it agrees with the primary rule (more precise verb name); when it
    # finds nothing, fall back to the primary regex's own captured text.
    matched = _matched_write_subcommands(command)
    verb_names = ", ".join(sorted(matched)) if matched else primary_match.group(1)

    _lib.log_routing_event(
        rule=RULE_NAME, outcome="deny", tool_name="Bash",
        command_fragment=command,
    )
    _lib.deny(
        _deny_message(agent_type, verb_names, undeterminable=worktree is None),
        summary=(
            f"subagent git write verb ({verb_names}) blocked in the shared "
            f"tree by the structure-agnostic guard (nexus-3c92m round 4)."
        ),
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
