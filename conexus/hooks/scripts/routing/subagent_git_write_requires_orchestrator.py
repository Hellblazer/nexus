#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# PEP 723 metadata: `uv run` reads the interpreter floor from here, so no shell-
# special `>=` has to ride in hooks.json argv (nexus-efk2h). This script refuses
# Python 3.11 and older below.
# /// script
# requires-python = ">=3.12"
# ///
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
``_first_git_command`` / ``_VERB_RE``: over the RAW command text, after normalizing
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

nexus-0r5l8 (round 10 of this guard's history, Sam's option A ruling,
2026-09-29): ``git`` used to arm the primary rule as ANY ``\\bgit\\b`` in the
text, so a path or word that merely contained ``git`` (``nexus-git-policy.py``,
``.git/hooks``, ``~/git/nexus``, ``git-workflow.md``, ``git.py``) followed by
an ordinary word (``reset``, ``add``, ``commit``) denied, and agents routed
around the guard with globs. Now ``git`` arms it only as the git COMMAND:
``_first_git_command`` cuts the normalized text into path-ish tokens and
requires a token whose basename is ``git`` or ``git-<word>`` (``git``,
``/usr/bin/git``, ``git-checkout``). The verb scan after that token, the
normalizer, both adjacency branches (which now key on the same test) and the
zero-expansion pass are unchanged, so every bypass catalogued above still
denies. What was NOT built is a command-POSITION analysis: ``printf 'git
checkout f' | sh``, ``eval 'git checkout f'`` and ``sh -c '...'`` carry
``git`` in argument position and are told from ``echo git add`` only by
parsing the shell, the unbounded surface rounds 1-3 lost to. So a standalone
``git`` token in ANY position arms the scan (unsure means deny); accepted
false positives are ``echo git add``, a directory literally named ``git``
(``cd ~/git && echo add``, ``ls ~/git/ && echo add``) and ``git~`` editor
backup names. ``=`` and ``$`` end a token, keeping ``g=git; $g checkout``
armed; ``+ % # ~`` are NOT token characters, so ``printf 'git%s checkout f' ''
| sh``, ``sed 's/x/git/;s/$/ checkout f/' | sh`` (the trailing ``/`` is
stripped before the basename), ``x=git~; ${x%~} checkout f`` and ``printf
'git~ checkout f' | tr -d '~' | sh`` arm too. The g-flag form ``sed
's/x/git/g;...'`` does NOT arm: its token's basename is ``g``.
There is one copy of this module (the plugin script ``hooks.json`` runs);
``tests/test_routing_subagent_git_write.py`` drives it as a subprocess.

Premise correction: the bead reported ``.github`` as the false-positive
driver. It never was: ``\\bgit\\b`` does not match inside ``github`` (no word
boundary before the ``h``). The measured driver was an absolute repo path,
``/Users/<u>/git/nexus``, whose bare ``/git/`` segment armed the scan for any
later ``add``/``commit``/``checkout`` word, plus ``git-push-develop.sh``
(which arms again) and ``.git/`` paths.

ACCEPTED FALSE NEGATIVES of round 10 (the mirror of the false positives
above; a path segment ``/git/`` used to arm the scan by accident, and that
incidental coverage is gone): a git wrapper or alias whose
basename is not ``git``-prefixed (``./my-git checkout f``, ``x-git``), a
``git.py`` script, and case variants ``GIT`` / ``Git`` (a case-insensitive
filesystem runs ``/usr/bin/GIT`` as git; the scan was case-sensitive before
this change too, so that gap is pre-existing, not new). ``gh pr checkout`` and
``hub checkout`` were on this list until nexus-0l6zh (below).

nexus-7mgnd: the verb side got a narrow, measured loosening, not a general one.
The verb is matched with plain ``\\b`` boundaries, as it always was, so a verb
glued to a path character (``feature/x-add-thing``, ``docs/add.md``,
``scratchpad/commit-abc``, ``nx-bare-checkout``) still denies after a ``git``
token. A first cut dropped those with a lookbehind and the adversarial review
executed the hole: the shell builds words at runtime, so ``git $(basename
/commit)``, ``git `basename /commit```, ``git -c alias.c="$(basename
/commit)" c``, ``git $(sed s~x~commit~ <<< x)``, ``... | xargs -I{} git {}``,
``g=.../git-core/git; $g-commit`` and ``git $(basename /x/checkout) -b nb``
all moved HEAD (they are pinned as the ``manufactured_verbs`` family in the
tests). The loosenings that remain are literals only, each measured as a false
positive in the 402-row replay of logged denials, and ALL of them are withheld
whenever ``_may_recombine`` finds a way for the command to rebuild a word
(a ``$``, backtick or process substitution, a redirection to a file, a pipe
into anything but a read-only filter, or an interpreter/executor word):

- ``merge-base`` / ``merge-tree`` (read-only; 21 of the 23 rows the replay
  still frees), ``merge-file`` and ``merge-base-x`` still deny;
- the whole tokens ``--no-commit-id`` and ``--abbrev-commit`` (2 rows);
- a hook file ``<dir>/.git/hooks/<hookname>`` (``post-commit``, ``post-merge``
  ...) as a whole token.

Replay result: of 402 logged denials the first cut freed 34, this cut frees 23
(21 ``merge-base``, 2 ``--abbrev-commit``) and re-denies the 11 glued ones
(``---clean above---``, ``scratchpad/commit-x``, ``nx-bare-checkout``, a hook
path built from ``$d``, ``--no-commit-id`` inside ``$(...)``). Running a hook
file (``.git/hooks/post-commit`` as a command) executes whatever it holds,
like any script a subagent may already run; the exemption only stops the
path's TEXT from reading as a verb. ACCEPTED FALSE
POSITIVES, listed so they are not rediscovered: every verb glued to a path or
word character after a ``git`` token (``git log feature/x-add-thing``, ``git
log docs/add.md``, ``git branch nexus-96-restore``, ``echo ---clean---``),
and every exempt literal inside a command that can recombine text
(``git diff $(git merge-base A B)``, ``git merge-base A B | sh``). Known gap of
the recombination gate: an interpreter missing from ``_EXECUTOR_RE`` could
still rebuild ``merge`` from ``merge-base`` or ``git commit`` from a hook
path, in a command that also carries one of the exempt literals. One
PRE-EXISTING hole, found while measuring and not touched here: ``git $(echo
-commit | cut -c2-) -m x`` allows on origin/develop too, because
``_resolve_command_sub_literals`` reads ``-commit`` as an echo flag.

nexus-0l6zh: HEAD-moving wrappers. A subagent in the shared checkout is also
denied ``gh pr checkout`` / ``gh pr co`` / ``gh co`` (gh's default alias),
``gh repo sync``, ``gh pr merge`` and ``gh pr close`` with ``-d`` /
``--delete-branch`` (they switch to the default branch and delete the local
one), ``gh issue develop`` with ``-c`` / ``--checkout``, ``hub sync`` and any
git verb after a ``hub`` COMMAND word (a git wrapper: ``hub checkout
<PR-URL>``). gh arms after a ``gh`` command token (bare, ``.exe``, ``./gh`` or
``.../bin/gh``) through one linear token walk (``_gh_match``); hub arms only in
command position (``_first_hub_command``), so ``cd hub && rm x``, ``echo hub;
rm -rf x``, ``docker pull hub && mv a b`` and ``ls hub add`` are not denied.
Both pass inside a provable linked worktree, like git. An expansion in the
subcommand slot (``gh pr $(basename /checkout) 5``, ``gh $(basename /pr)
checkout 5``) is a wildcard and denies. Scope: gh arming covers the local-tree
movers only; user-defined aliases are not followed (``gh alias set co2 'pr
checkout'`` denies on that line, a later ``gh co2 5`` does not); a whole-word
variable holding the subcommand (``p=checkout; gh pr $p 5``) is a known limit;
and ``gh`` subcommands that only change the remote (``gh pr create``,
``gh pr merge`` without ``-d``, ``gh pr update-branch``, ``gh api``) are not
guarded, which is inconsistent with the refusal of ``git push`` and is the
stated price. Accepted false positives: ``echo gh pr checkout`` and a
``.../bin/gh`` directory. ``gh pr checkout=5`` is no gh command and allows.

nexus-dsr6a: ``git config`` WRITES deny (``core.hooksPath`` redirects the hooks
that run on the orchestrator's later commits; ``user.*`` rewrites authorship).
READS stay allowed (``--get*``, ``--list``/``-l``, ``--show-origin``/``-scope``, a
bare key, ``git config get|list``) in PLAIN text, judged on the RAW command: no
quote, backslash, ``{ } * ? [``, ``$``, backtick or recombination. In any other
text a standalone ``config`` word after ``git``, or one inside a substitution,
denies, except a get read made of exact flags. A linked worktree is exempt from
the verb scan but not from a config write that reaches shared state (only
``--worktree`` writes of a key that runs no code pass there, in plain text, and
only if no other repository is named). The decision, the
``--global``/``--system``/``--file`` choice and the known limits are in the block
comment above ``_CONFIG_STRICT_RE``. The
change only ADDS a denial after the unchanged verb scan, so nothing that denied
before allows now.

``run_hook(fail_closed=False)`` is unchanged: a crash in the hook ITSELF
still allows, since a broken guard must not brick every agent's Bash.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from typing import Any, NamedTuple

# RDR-215 nexus-q02nx.21: hooks.json now launches this script with a bare
# `python3`, so PATH decides the interpreter. Put back the resolution
# `_run_python_hook.sh` used to perform, before anything that needs 3.12
# or `nexus` is imported. See _interpreter.py for what is at stake.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _exec_path  # noqa: E402 -- must follow the sys.path insert
import _interpreter  # noqa: E402 -- must follow the sys.path insert

_interpreter.reexec_if_needed()

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
_VERB_ALT_HEAD = r"checkout|restore|switch|reset|clean|stash|add|commit|"
_VERB_ALT_TAIL = (
    r"|rebase|"
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
#:
#: nexus-7mgnd (second cut): the verb is matched with plain ``\b`` word
#: boundaries, exactly as on origin/develop. A first cut dropped a verb glued
#: to a path character (``[\w./~-]``) with a lookbehind, and the adversarial
#: review executed its hole: the lookbehind judges how the TEXT is adjacent,
#: but the shell builds words at RUNTIME, so ``git $(basename /commit)``,
#: backtick ``basename /commit``, ``git -c alias.c="$(basename /commit)" c``,
#: ``git $(sed s~x~commit~ <<< x)``, ``echo ./commit | cut -c3- | xargs -I{}
#: git {}``, ``g=.../git-core/git; $g-commit`` and ``git $(basename
#: /x/checkout) -b nb`` all allowed and moved HEAD. A glued verb is therefore
#: scanned like any other; the price is a set of accepted false positives
#: (``git log feature/x-add-thing``, ``git log docs/add.md``, ``git branch
#: nexus-96-restore``, ``echo ---clean above---``).
_VERB_RE = re.compile(r"\b(" + _VERB_ALT_HEAD + "merge" + _VERB_ALT_TAIL + r")\b")

#: nexus-7mgnd: the ONLY loosening of the verb scan, and it is used only when
#: ``_may_recombine`` says the command has no way to turn a literal into a
#: different word. ``merge-base`` and ``merge-tree`` are read-only, and 25 of
#: the 402 denials logged 2026-08-20..09-05 were ``git merge-base
#: --is-ancestor``. ``merge-file``, ``merge-base-x`` and the rest still deny.
#: Without the gate, ``git $(basename merge-base | cut -c1-5) topic`` would
#: build ``git merge`` out of the exempt literal.
_VERB_RE_READ_ONLY = re.compile(
    r"\b(" + _VERB_ALT_HEAD + r"merge(?!-(?:base|tree)(?![\w./~-]))" + _VERB_ALT_TAIL + r")\b"
)

#: nexus-7mgnd: three literal token shapes that measured as false positives in
#: the 402-row replay and that no shell can turn into an executed word UNLESS
#: the command can recombine text (``_may_recombine``): ``--no-commit-id`` and
#: ``--abbrev-commit`` (flags of ``git diff-tree`` / ``git log``) and a hook
#: file path ``<dir>/.git/hooks/<hookname>`` (``post-commit``, ``post-merge``,
#: ``pre-push`` ...). Each is a WHOLE token: preceded by the start of the text,
#: whitespace, ``;``, ``&`` or ``(``, and followed by the end, whitespace,
#: ``;``, ``&`` or ``)``. A neighbour that is anything else (``x.git/hooks/``,
#: ``--no-commit-id=1``, ``--abbrev-commit-x``) is not the token and still
#: scans. The path prefix is plain path characters only, so a brace, glob or
#: expansion in it is never stripped. In the replay these tokens sat inside
#: ``$(...)``, a ``for`` loop over ``$d`` or a ``| grep`` pipeline in most
#: rows, so only the pure-literal rows are fixed; the rest keep denying.
_HOOK_NAMES = (
    r"applypatch-msg|pre-applypatch|post-applypatch|pre-commit|pre-merge-commit|"
    r"prepare-commit-msg|commit-msg|post-commit|pre-rebase|post-checkout|"
    r"post-merge|pre-push|pre-receive|update|post-receive|post-update|"
    r"post-rewrite|pre-auto-gc|push-to-checkout|reference-transaction|"
    r"sendemail-validate|fsmonitor-watchman|post-index-change"
)
_EXEMPT_TOKEN_RE = re.compile(
    r"(?<![^\s;&(])(?:--no-commit-id|--abbrev-commit|"
    r"(?:[\w.~/-]*/)?\.git/hooks/(?:" + _HOOK_NAMES + r")(?:\.sample)?)(?![^\s;&)])"
)

#: nexus-7mgnd: can this command build a word the text does not spell? The
#: exemptions above are literals; a shell or an interpreter can rewrite a
#: literal into another word (``cut -c1-5`` turns ``merge-base`` into
#: ``merge``, ``sed 's/.*post-/git /'`` turns a hook path into ``git commit``).
#: This gate answers yes, and every exemption is then withheld, when ANY of
#: these holds, all read from the normalized text:
#:
#: - a capture or expansion: ``$`` (``$(...)``, ``${...}``, ``$name``), a
#:   backtick, or a process substitution ``<(`` / ``>(``;
#: - an output redirection other than to ``/dev/null`` or an fd (``2>&1``),
#:   since a file can be written and then run;
#: - a pipe whose next stage starts with a word outside ``_SAFE_PIPE_SINKS``
#:   (read-only filters that cannot execute their input or write a file);
#: - an executor or interpreter word (``sh``, ``bash``, ``xargs``, ``eval``,
#:   ``source``, a leading ``.``, ``python``, ``perl``, ``ruby``, ``node``,
#:   ``awk``, ``sed`` ...), anywhere in the text.
#:
#: What it cannot see: an interpreter missing from ``_EXECUTOR_RE``. The list
#: is open-ended by nature; the miss costs a deny->allow only for a command
#: that also carries one of the exempt literals.
_CAPTURE_RE = re.compile(r"[$`]|<\(|>\(")
_SAFE_REDIRECT_RE = re.compile(
    r"&>>?\s*/dev/null(?![\w./-])|(?<!\d)\d*>>?\s*/dev/null(?![\w./-])|(?<!\d)\d*>&\d+"
)
_PIPE_STAGE_RE = re.compile(r"(?<!\|)\|(?!\|)&?\s*([^\s;&|()<>]*)")
_SAFE_PIPE_SINKS = frozenset(
    {"head", "tail", "grep", "egrep", "fgrep", "wc", "cut", "tr", "cat", "less", "more"}
)
_EXECUTOR_RE = re.compile(
    r"(?<![\w.-])(?:sh|bash|zsh|dash|ksh|csh|tcsh|fish|pwsh|powershell|xargs|eval|"
    r"source|parallel|python[\d.]*|perl|ruby|node(?:js)?|deno|bun|php|lua|[gmn]?awk|"
    r"sed|osascript|expect|tclsh|groovy|jshell|irb|swift|emacs|vim|nvim|gdb|lldb)(?![\w.-])"
)
_DOT_SOURCE_RE = re.compile(r"(?:^|[;&|(\n])\s*\.\s")


def _may_recombine(text: str) -> bool:
    """True when *text* has any way to build a word it does not spell (see the
    block comment above). One regex pass per clause, all linear."""
    if _CAPTURE_RE.search(text):
        return True
    if ">" in _SAFE_REDIRECT_RE.sub("", text):
        return True
    for m in _PIPE_STAGE_RE.finditer(text):
        if m.group(1) not in _SAFE_PIPE_SINKS:
            return True
    return bool(_EXECUTOR_RE.search(text) or _DOT_SOURCE_RE.search(text))


#: nexus-0r5l8 (Sam's option A, 2026-09-29): ``git`` arms the scan only as the
#: git COMMAND, not as a substring of a path or word. The old ``\bgit\b`` armed
#: it for ``nexus-git-policy.py``, ``.git/hooks``, ``~/git/nexus``,
#: ``git-workflow.md`` and ``git.py``, so ``cat nexus-git-policy.py | grep
#: reset`` denied.
#:
#: A TOKEN rule, not a position analysis. The text is cut into path-ish tokens
#: (``_PATHISH_TOKEN_RE``: word characters plus ``. - /``; whitespace, the
#: already-stripped quotes, ``; & | ( ) < > = $ { } ` ! + % # ~`` and everything
#: else end a token, so ``git%s``, ``git+``, ``git#`` and ``git~`` all leave a
#: bare ``git`` token behind: text the shell builds can strip the extra
#: character, e.g. ``x=git~; ${x%~} checkout f`` and ``printf 'git~ checkout
#: f' | tr -d '~' | sh`` both run git). Trailing ``/`` is stripped, then ``git`` arms the
#: scan when the token's BASENAME matches ``_GIT_BASENAME_RE``: ``git``, any
#: ``git-<letters and hyphens>`` including a bare ``git-`` (``git-checkout``,
#: ``git-core/git-``, whose ``${p}add`` completion is a spliced command), and
#: any of those with a ``.exe`` / ``.cmd`` / ``.bat`` / ``.com`` / ``.sh``
#: suffix (``git.exe``, ``git-lfs.exe``, ``git.sh`` wrappers, and
#: ``scripts/git-push-develop.sh``, the sanctioned push wrapper, which a
#: subagent must not run in the shared tree). Tokens like
#: ``nexus-git-policy.py``, ``.git``, ``foo.git``, ``git.py``,
#: ``git-workflow.md`` and ``~/git/nexus`` do not qualify. ``=`` and ``$``
#: end a token on purpose: ``g=git; $g checkout`` must still arm.
#:
#: Position is deliberately NOT part of the rule. ``printf 'git checkout f' |
#: sh``, ``eval 'git checkout f'`` and ``sh -c 'git checkout f'`` all carry
#: ``git`` in ARGUMENT position (the shell that runs it sits elsewhere in the
#: text), and telling them from ``echo git`` needs a shell parser, which is
#: the unbounded surface the round-4 redesign exists to avoid. When unsure
#: whether a standalone ``git`` token is command-position, it arms the scan.
#: The accepted false positives are therefore ``echo git add`` and a bare
#: directory named ``git`` (``cd ~/git && ...``); the fixed false positives are
#: every path or word that merely CONTAINS ``git``.
_PATHISH_TOKEN_RE = re.compile(r"[\w.\-/]+")
_GIT_BASENAME_RE = re.compile(
    r"git(?:-[A-Za-z-]*)?(?:\.(?:exe|cmd|bat|com|sh))?"
)


def _first_git_command(text: str) -> int | None:
    """Index just past the ``git`` of the first git-command token in *text*
    (see ``_PATHISH_TOKEN_RE``), or ``None``.

    The returned index is the end of the literal ``git``, not of the token,
    so the verb scan still sees ``checkout`` inside ``git-checkout``. One
    linear pass: ``finditer`` over tokens, an ``rfind`` and an anchored
    ``fullmatch`` per token that contains ``git`` at all. Git only: ``hub`` and
    ``gh`` have their own walks, so a bare ``hub`` or ``gh`` word never arms
    the git-only gates built on this (``_find_spliced_expansion``).
    """
    for m in _PATHISH_TOKEN_RE.finditer(text):
        tok = m.group().rstrip("/")
        if "git" in tok:
            base_start = tok.rfind("/") + 1
            if _GIT_BASENAME_RE.fullmatch(tok, base_start):
                return m.start() + base_start + 3
    return None


#: nexus-0l6zh: ``hub`` is a git wrapper that forwards every git verb
#: (``hub checkout <PR-URL>``, ``hub merge``, ``hub sync``), so it arms the same
#: verb scan as ``git``, but ONLY in command position (``_first_hub_command``):
#: a bare ``hub`` that is an argument or a directory name (``cd hub && rm x``,
#: ``echo hub; rm -rf x``, ``docker pull hub && mv a b``, ``ls hub add``) must
#: not arm it. Only a bare ``hub`` (or ``hub.exe``), or one under a ``bin/``
#: directory, qualifies as the command word.
_HUB_TOKEN_RE = re.compile(r"(?:\./|.*/bin/)?hub(?:\.exe)?")
_GH_TOKEN_RE = re.compile(r"(?:\./|.*/bin/)?gh(?:\.exe)?")
#: Words and delimiters for the command-position walk. ``\n`` is a delimiter
#: (a new command starts on the next line); other whitespace is skipped.
_CMD_WALK_RE = re.compile(r"\n|[^\s;&|(){}`]+|[;&|(){}`]")
_CMD_DELIMS = frozenset(";&|(){}`\n")
#: A word after which the NEXT word is again a command: wrappers and shell
#: keywords, any ``NAME=value`` assignment, and a ``-c`` / ``-lc`` flag
#: (``sh -c 'hub checkout x'``, quotes already stripped).
_CMD_INTRODUCERS = frozenset(
    {"env", "command", "exec", "sudo", "doas", "time", "nohup", "xargs", "eval",
     "builtin", "then", "do", "else", "elif", "if", "while", "until", "!"}
)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=")
_SHELL_C_FLAG_RE = re.compile(r"-[A-Za-z]*c")
#: ``hub sync`` is hub's own verb (not a git one): it fast-forwards local
#: branches, the current one included.
_HUB_SYNC_RE = re.compile(r"\s+sync(?![\w./~-])")


def _first_hub_command(text: str) -> int | None:
    """Index just past the first ``hub`` COMMAND word in *text*, or ``None``.

    Command position means: the start of the text; right after ``;``, ``&&``,
    ``||``, ``|``, ``(`` (so ``$(``), a backtick, ``{``, or a newline; or after
    a word in ``_CMD_INTRODUCERS`` (``env``, ``sudo``, ``exec``, ``command``,
    ``xargs``, ``then`` ..., an assignment, ``sh -c``). Used only by
    ``_primary_match``; the git-only helpers never see it. One linear pass.
    """
    cmd_pos = True
    for m in _CMD_WALK_RE.finditer(text):
        w = m.group()
        if w in _CMD_DELIMS:
            cmd_pos = True
            continue
        if cmd_pos:
            tok = w.rstrip("/")
            if "hub" in tok and _HUB_TOKEN_RE.fullmatch(tok):
                return m.start() + len(tok)
        cmd_pos = (
            w in _CMD_INTRODUCERS
            or _ASSIGNMENT_RE.match(w) is not None
            or _SHELL_C_FLAG_RE.fullmatch(w) is not None
        )
    return None


#: nexus-0l6zh: ``gh`` subcommands that move HEAD or rewrite the working tree
#: of the checkout they run in, surveyed from ``gh help`` (gh 2.102):
#:
#: - always: ``pr checkout`` (alias ``pr co``), ``co`` (gh's DEFAULT alias for
#:   ``pr checkout``; ``gh co 5``, ``gh -R o/r co 5``), ``repo sync``
#:   (fast-forwards or hard-resets the local branch; with an ``owner/repo``
#:   argument it only touches the remote, which this over-arms on purpose);
#: - with ``-d`` / ``--delete-branch``: ``pr merge`` and ``pr close`` (both
#:   check out the default branch and delete the local one);
#: - with ``-c`` / ``--checkout``: ``issue develop``.
#:
#: Scope: gh arming is limited to the local-tree movers. Commands that only
#: change the remote (``gh pr create``, ``gh pr merge`` without ``-d``,
#: ``gh pr update-branch``, ``gh api``) are NOT guarded, which is inconsistent
#: with this guard's refusal of ``git push`` and is the stated price of not
#: guarding the whole gh surface. A USER-DEFINED alias (``gh alias set co2 'pr
#: checkout'`` then ``gh co2 5``) is not seen; the alias line itself carries
#: ``pr checkout`` and denies, a later ``gh co2`` does not. A whole-word
#: variable holding the subcommand (``p=checkout; gh pr $p 5``) is a known
#: limit, the same as ``git $p``.
#:
#: One left-to-right walk over the words after the first ``gh`` command token
#: (``_gh_match``). A phrase is HEAD [flag [value]]* SUBCOMMAND, where HEAD is
#: ``pr`` / ``repo`` / ``issue`` anywhere after ``gh``, or the ``gh`` command
#: itself (for ``co``), a flag is ``-x`` / ``--long`` / ``--long=v`` and takes
#: at most one value word, and ``; & | ( ) < >`` end a phrase. The walk keeps a
#: SET of live (head, after-flag) states, at most eight, so a hostile run of
#: repeated heads (``pr -a pr -a pr ...``) costs the same constant per word as
#: any other input: linear in the text, with no regex backtracking over the
#: flag gap. (The first cut used a regex flag gap and was measured quadratic:
#: 7.8 s at 24 KB, past the 5 s hook timeout, which fails open.)
#:
#: An expansion in the subcommand slot (``gh pr $(basename /checkout) 5``,
#: ``gh $(basename /pr) checkout 5``, ``gh pr ch${x}ckout``) is a WILDCARD: it
#: could spell anything, so a wildcard right after ``pr`` / ``repo`` denies and
#: a wildcard right after ``gh`` stands for any head.
_GH_FLAG_RE = re.compile(r"--?[A-Za-z0-9][\w-]*(?:=\S*)?")
_GH_WALK_RE = re.compile(r"[^\s;&|()<>]+|[;&|()<>]")
_GH_DELIMS = frozenset(";&|()<>")
_GH_ALWAYS: dict[str, frozenset[str]] = {
    "pr": frozenset({"checkout", "co"}),
    "repo": frozenset({"sync"}),
    "gh": frozenset({"co"}),
}
#: subcommand -> the kind of flag that makes it move the tree.
_GH_CONDITIONAL: dict[str, dict[str, str]] = {
    "pr": {"merge": "delete", "close": "delete"},
    "issue": {"develop": "checkout"},
}
_GH_CONDITIONAL_NAMES = {
    "delete": "gh pr merge/close --delete-branch",
    "checkout": "gh issue develop --checkout",
}
_GH_HEADS = ("pr", "repo", "issue")
_WILDCARD = "\x00"


def _gh_command_ends(text: str) -> list[int]:
    """End index of every ``gh`` command token in *text* (bare ``gh``,
    ``gh.exe``, ``./gh`` or ``.../bin/gh``). Same token walk as
    ``_first_git_command``; a directory merely named ``gh`` arms it too, the
    accepted cost of the no-position-analysis design."""
    ends: list[int] = []
    for m in _PATHISH_TOKEN_RE.finditer(text):
        tok = m.group().rstrip("/")
        if "gh" in tok and _GH_TOKEN_RE.fullmatch(tok):
            ends.append(m.end())
    return ends


def _gh_head_flag(word: str, kind: str) -> bool:
    """True when *word* is the flag that makes a conditional subcommand move
    the tree: ``--delete-branch[=v]`` / a short cluster holding ``d`` for
    ``delete``; ``--checkout[=v]`` / a cluster holding ``c`` for ``checkout``.
    A word test, not a regex over the whole text, so it cannot backtrack."""
    long_flag = "--delete-branch" if kind == "delete" else "--checkout"
    if word == long_flag or word.startswith(long_flag + "="):
        return True
    letter = "d" if kind == "delete" else "c"
    return (
        len(word) > 1 and word[0] == "-" and word[1:].isascii()
        and word[1:].isalpha() and letter in word[1:]
    )


def _gh_match(text: str) -> str | None:
    """The first HEAD-moving ``gh`` phrase after the first ``gh`` command
    token, as a short name for the deny message, or ``None``. See the block
    comment above for the phrase grammar and why this is a linear walk.

    Two walks, each linear: over the text as written (so a phrase INSIDE
    ``$(...)`` or a backtick span is seen), and over a copy whose expansions
    are replaced by a wildcard word (so a phrase assembled BY an expansion is
    seen)."""
    if "gh" not in text:
        return None
    hit = _gh_walk(text, wildcards=False)
    if hit is None and _EXPANSION_CONSTRUCT_RE.search(text) is not None:
        hit = _gh_walk(_EXPANSION_CONSTRUCT_RE.sub(_WILDCARD, text), wildcards=True,
                       armed_by_text=text)
    return hit


def _gh_walk(t: str, *, wildcards: bool, armed_by_text: str | None = None) -> str | None:
    """One walk of *t* (see ``_gh_match``). *armed_by_text* is the original
    text when *t* is its wildcard copy: a ``gh`` that sits only inside an
    expansion (``$(which gh) pr checkout 5``) still arms the scan, from the
    start of *t*."""
    ends = _gh_command_ends(t)
    if not ends and not (wildcards and _gh_command_ends(armed_by_text or "")):
        return None
    start = ends[0] if ends else 0
    live: set[tuple[str, bool]] = set()  # (head, a flag was just seen)
    armed_kinds: set[str] = set()
    pending = 0
    for m in _GH_WALK_RE.finditer(t, start):
        w = m.group()
        while pending < len(ends) and m.start() >= ends[pending]:
            live.add(("gh", False))  # the gh command itself heads `gh co`
            pending += 1
        if w in _GH_DELIMS:
            live.clear()
            continue
        for kind in armed_kinds:
            if _gh_head_flag(w, kind):
                return _GH_CONDITIONAL_NAMES[kind]
        wild = _WILDCARD in w
        is_flag = not wild and _GH_FLAG_RE.fullmatch(w) is not None
        nxt: set[tuple[str, bool]] = set()
        for head, flagged in live:
            if wild:
                if not flagged:
                    if head == "pr" or head == "repo":
                        return f"gh {head} <expansion>"
                    if head == "issue":
                        armed_kinds.add("checkout")
                    elif head == "gh":
                        nxt.update((h, False) for h in _GH_HEADS)
                else:
                    nxt.add((head, False))  # the value of the flag
                continue
            if w in _GH_ALWAYS.get(head, ()):
                return "gh co" if head == "gh" else f"gh {head} {w}"
            kind = _GH_CONDITIONAL.get(head, {}).get(w)
            if kind is not None:
                armed_kinds.add(kind)
            if is_flag:
                nxt.add((head, True))
            elif flagged and not w.startswith("-"):
                nxt.add((head, False))  # the flag's one value word
        if w in _GH_HEADS and not wild:
            nxt.add((w, False))
        live = nxt
    return None


#: nexus-dsr6a: ``git config`` joins the guarded verbs, but not as a word in
#: ``_VERB_RE``: ``config`` is also a directory, a file stem and a search term
#: (``git diff -- conexus/config/``, ``git log --grep config``), and the read
#: forms (``--get``, ``--list``, a bare key, ``git config get``) are things a
#: subagent needs. What a write does is the reason it is guarded:
#: ``core.hooksPath`` redirects which hook code runs on the orchestrator's later
#: commits, merges and checkouts, and ``user.*`` rewrites authorship (the
#: 2026-09-12 identity overwrite).
#:
#: ONE DECISION, made from the RAW command: can the scan trust the words it
#: sees? Quote stripping, brace expansion, escapes, ``$(echo X)`` resolution and
#: the segment split all change what the words are, and every earlier form of
#: this guard failed by trusting a count or a cursor over text the shell reads
#: differently (rounds 1 to 3 each closed one spelling and the next review found
#: another). So the judgment is not made from normalized text.
#:
#: nexus-gvg6t: a quote-aware tokenizer (``_config_mask``) reads the raw command
#: once, left to right. Text the shell cannot expand is DATA: a single-quoted
#: string, a double-quoted string with no ``$``, backtick or backslash in it, a
#: quoted-delimiter heredoc body (``<<'EOF'``, ``<<"EOF"``, ``<<\EOF``) and the
#: ``"$(cat <<'EOF' ... EOF )"`` idiom, whose output is the heredoc body. Each
#: data string becomes ONE word of the text the cursor reads: its own text when
#: that is a plain token (``"config"``, ``'--get'``), else the opaque word ``Q``,
#: so a quoted value with spaces, ``;`` or ``(`` counts as exactly one argument,
#: which is what the shell passes. A comment (``#`` at the start of a word, to the
#: end of the line) is dropped: its words are not arguments. The tokenizer GIVES
#: UP, and the command takes the strict path as before, when anything it cannot
#: read exactly is outside data: a backslash, ``$``, backtick, ``{ } * ? [``, CR,
#: an unbalanced quote, an unquoted heredoc delimiter or a heredoc with no
#: terminator line. A quote glued to a word joins it, as in the shell (``con"fig"``
#: reads ``config``, ``x'a b'`` is one word). After the
#: tokenizer the text must still pass ``_may_recombine`` (an executor, a pipe into
#: a non-filter, a redirect to a file, ``<(``), judged on the text WITHOUT the
#: data, so ``sed`` or ``sh`` inside a commit message does not matter and an
#: ``sh -c '...'`` outside it does.
#:
#: Data is trusted as data, not as code. A consumer that runs its argument as a
#: command (``ssh host '...'``, ``git submodule foreach '...'``, ``git rebase -x
#: '...'``, ``tmux send-keys '...'``) would turn a quoted ``git config`` into a
#: write, so every data string is also read as command text (``_config_mask``'s
#: ``data``): a ``git config`` write spelled out in it denies, with the same
#: cursor. Prose such as ``fix git config handling in hooks`` reads as a write
#: there (a key and a value) and denies; ``git config`` with one word after it,
#: or none, is a read and passes. A consumer joins its arguments and the quotes
#: can split a spelling across strings, so in a command that names a consumer
#: (``_CONSUMER_RE``) the words of each simple command are also read joined, quotes
#: removed (``_JoinTrack``, ``_config_joined_hit``): ``ssh host 'g''it config' k v``,
#: ``tmux send-keys 'git config' ' user.name x'``. A command of more than 64 words
#: keeps two copies of a repeated word and, if still too long, denies when it names
#: git and ``config`` (a repeat cannot change a verdict past two copies; a count
#: the reader cannot prove is refused). Limit, taken on purpose: tmux key names
#: (``Space``) and an argument built by the consumer's own shell are not modelled.
#:
#: A quoted word that is not a plain word is ``Q`` in the cursor's text (``-Q`` when
#: it starts with a dash). A ``Q`` where the subcommand may be (``git
#: '--git-dir=.git' config``, ``git 'x y' config``) counts as a possible ``config``
#: and as another repository, and a ``-Q`` after the key is a possible scope flag.
#:
#: - CURSOR (plain apart from data): ``_config_relaxed_match`` splits the text
#:   into simple commands (the split is exact once data is one word) and finds a
#:   ``config`` word that is the git SUBCOMMAND (the first word after git that is
#:   not a global option or its value; a ``config`` after another subcommand,
#:   ``git grep config``, ``git log -S config``, is that command's argument and
#:   is not the verb), or a ``git-config`` program. It denies unless the words
#:   that follow are a PROVEN read (``_config_read_kind``).
#: - STRICT (anything else): ``_config_strict_match``. Any standalone ``config``
#:   word after the first git (or hub) command denies (``_CONFIG_STRICT_RE``),
#:   whatever follows it, and so does a ``config`` inside a ``$( )``, backtick or
#:   ``${ }`` span in the raw command, with the path narrowing off (``config.py``,
#:   ``x-config``, ``/config``: how a shell builds the word,
#:   ``$(basename /x/config.py .py)``, ``$(echo x-config | cut -d- -f2)``), and so
#:   does a ``git-config`` program token (``git-config user.name "$x"``; the
#:   dash hides ``config`` from the standalone test). "Standalone" means not glued
#:   to a word character, ``.``, ``-`` or ``/`` outside a span, so
#:   ``src/config.py``, ``conexus/config/``, ``~/.config``, ``.git/config``,
#:   ``db-config.sh`` and ``config_cmd`` do not count, while ``HEAD:config`` and
#:   ``=config`` do. One exemption: a standalone ``config`` followed by a PROVEN
#:   GET READ (``--get*``, ``-l``/``--list``, ``get``/``list``) whose flags are
#:   all exact read flags with no valued flag, in a command with no ``$``,
#:   backtick or recombination anywhere, passes. A get read cannot write whatever
#:   follows it, and every word before the get flag is an exact flag, so nothing
#:   the shell rewrites can change what git runs. Price: a command with a ``$``,
#:   a backslash or a brace expansion beside a bare-key read denies.
#:
#: A config command is allowed only as a proven read. The read test is a
#: whitelist, so anything it does not know denies: only exact flags from
#: ``_CONFIG_READ_FLAGS`` (git accepts abbreviated long options, so ``--unse`` is
#: not a read), at most one positional unless a get or list flag (or the
#: ``get``/``list`` subcommand) is present, and no first positional of
#: ``set``/``unset``/``rename-section``/``remove-section``/``edit``. git stops
#: reading options at the first positional (measured on 2.53), so ``git config
#: user.name --global`` is a write.
#:
#: Aliases: a ``-c alias.<x>=...``, ``--config-env alias.<x>=VAR``,
#: ``GIT_CONFIG_KEY_<n>=alias.<x>`` or ``GIT_CONFIG_PARAMETERS`` carrying an alias
#: denies outright (``_CONFIG_ALIAS_INJECT_RE``): the alias value can name
#: ``config`` through a variable the text never spells (``CFG=config git
#: --config-env=alias.c=CFG c user.name x``), and a subagent has no need to
#: define one on the command line.
#:
#: ``--global`` and ``--system`` WRITES deny: they rewrite identity and hooks
#: for every repository on the box, the shared checkout included. ``--file`` /
#: ``-f`` / ``--blob`` writes deny too. The bead excepted ``--file`` outside the
#: repo, but a text-only guard cannot show a path is outside it (``.git/config``,
#: ``~/.gitconfig``, ``.gitmodules`` and a symlink under ``/tmp`` are all
#: reachable), and a subagent has no need to write a config file there. READS
#: with every one of those flags pass in plain text.
#:
#: Linked worktree: a worktree is exempt from the verb scan, but NOT from a
#: config write that reaches shared state. A plain ``git config`` there writes
#: the common ``.git/config`` the primary reads (``extensions.worktreeConfig``
#: is enabled in this repo), and ``--global``/``--system``/``--local`` reach it
#: or the whole box. Allowed from a worktree: reads, and ``--worktree`` writes
#: (the per-worktree file; git refuses ``--worktree`` in a repo with linked
#: worktrees unless ``extensions.worktreeConfig`` is on, measured on git 2.53,
#: so it cannot reach the common file) on the cursor path when the command names
#: no other repository: no ``-C``, ``--git-dir``, ``--work-tree``, and no
#: ``GIT_DIR=``, ``GIT_COMMON_DIR=`` or ``GIT_WORK_TREE=`` assignment anywhere in
#: the raw command, quoted or not (bare, via ``env`` or exported), any of which
#: points git at the primary and writes ITS ``config.worktree``. ``--worktree``
#: must precede the first positional and no scope flag, abbreviated or not, may
#: appear. The KEY must be on an allowlist of keys that run no code
#: (``_CONFIG_WORKTREE_KEYS``); anything else, a key or a form with no key,
#: denies even with ``--worktree``, because the orchestrator commits there and
#: a key such as ``core.hooksPath``, ``url.<base>.insteadOf`` or
#: ``interactive.diffFilter`` runs or redirects code for whoever does.
#:
#: Out of scope for this verb guard: raw writes to ``.git/config`` or
#: ``.git/hooks/*`` (``printf ... >> .git/config``, ``sed -i``, ``tee``), and
#: ``cd``/``pushd`` into another checkout first (the tool call's cwd decides, as
#: for every other verb). A separate bead covers raw file writes.
#:
#: Known limits, the same kind as a variable-held ``checkout``: a config verb the
#: text never spells after ``git`` (``c=config; git $c user.name x``,
#: ``git con{fig,}``, ``git con*`` with a file named ``config`` in the cwd), an
#: alias defined in a config file, a substitution nested more than one level
#: deep (``_CONFIG_SPAN_RE`` reads one level), and a data string that a consumer
#: runs after building the verb from a variable the string never spells.
_CONFIG_STRICT_RE = re.compile(
    r"(?<![\w.\-/])config(?![\w.\-/])|(?<=/)config(?=/?[)`}])"
)
#: Inside a substitution span the narrowing is off: ``config`` bounded by no
#: letter or digit counts (``config.py``, ``x-config``, ``/config``, ``x_config``).
_CONFIG_SPAN_RE = re.compile(r"\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`|\$\{[^{}]*\}")
_CONFIG_IN_SPAN_RE = re.compile(r"(?<![A-Za-z0-9])config(?![A-Za-z0-9])")
#: Raw characters that change a word, a word boundary or a segment: a quote, a
#: backslash, CR (normalization turns it into a command separator, bash keeps it
#: as a character), brace expansion and globs. ``$`` and backtick are
#: ``_CAPTURE_RE``.
_CONFIG_NONPLAIN_RE = re.compile(r"""['"\\\r{}*?\[]""")
_CONFIG_SEGMENT_SPLIT_RE = re.compile(r"[;&|()\n]")
_CONFIG_REDIRECT_RE = re.compile(r"(?<!\d)\d*<{1,3}\s*[^\s;&|()<>]*")
_DASHED_CONFIG_RE = re.compile(r"git-config(?:\.(?:exe|cmd|bat|com|sh))?")
#: The same program, in text the cursor cannot read (the strict path has no cursor).
#: ``config`` in ``git-config`` is glued to ``-``, which ``_CONFIG_STRICT_RE``
#: excludes, so the quoted forms ``git-config user.name "$x"`` need their own.
#: Only in COMMAND position: at the start, after a command break, behind
#: assignments and the words that hand a command on (``sudo``, ``env``,
#: ``command``, ``exec``, ``if``/``then``). An argument is not a program:
#: ``grep -n "git-config" docs/*.md`` searches for the name. The run of prefix
#: words cannot cross a break, so an attempt ends at the next break: linear.
_DASHED_CONFIG_CMD_RE = re.compile(
    r"(?:^|[;&|(){}`\n])[ \t]*"
    r"(?:(?:[A-Za-z_]\w*=[^\s;&|(){}`]*|sudo|doas|env|command|exec|nice|nohup|time|"
    r"xargs|builtin|if|then|else|elif|do|while|until|!)[ \t]+)*"
    r"(?:[^\s;&|(){}`]*/)?git-config(?:\.(?:exe|cmd|bat|com|sh))?(?![\w.\-])"
)
_WORD_RE = re.compile(r"\S+")
#: Words and breaks for the argument walk of the get-read exemption: a break
#: character ends the command, a newline included.
_CONFIG_ARG_WALK_RE = re.compile(r"[^\s;&|()<>`]+|[;&|()<>`\n]")
_CONFIG_ARG_BREAKS = frozenset(";&|()<>`\n")
#: Words the exemption reads before it gives up and denies: a cap keeps the walk
#: linear when a command holds thousands of ``config`` words.
_CONFIG_ARG_WORDS_CAP = 32
#: ``-c alias.x=``, ``--config-env alias.x=``, ``GIT_CONFIG_KEY_n=alias.x`` and an
#: alias inside ``GIT_CONFIG_PARAMETERS`` (a value holds several quoted entries,
#: so the scan crosses spaces; the bound keeps a repeated prefix linear).
_CONFIG_ALIAS_INJECT_RE = re.compile(
    r"(?i)(?<![\w-])-c\s+alias\.|--config-env(?:=|\s+)alias\.|"
    r"GIT_CONFIG_KEY_\d+=\s*alias\.|GIT_CONFIG_PARAMETERS=[^\n;&|]{0,400}?alias\."
)
#: Variables that point git at another repository.
_GIT_REPO_ENV_RE = re.compile(r"(?<!\w)GIT_(?:DIR|COMMON_DIR|WORK_TREE)=")


def _repo_env_named(text: str) -> bool:
    """True when *text* assigns a repository-pointing variable, as written or
    once its quotes are gone (``env GIT_DIR"=/x" git ...``, ``GIT_WORK_TREE'='/x``,
    ``G'IT_DIR'=/x``: the shell joins those into the assignment)."""
    if _GIT_REPO_ENV_RE.search(text) is not None:
        return True
    if "'" not in text and '"' not in text:
        return False
    return _GIT_REPO_ENV_RE.search(text.replace("'", "").replace('"', "")) is not None
#: A segment with more ``config`` verbs than this denies without being read: each
#: one re-reads the arguments after it, and a cap keeps the walk linear.
_CONFIG_MAX_WORDS_PER_SEGMENT = 8
_CONFIG_GET_FLAGS = frozenset({
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color",
    "--get-colorbool", "-l", "--list",
})
#: Flags that read, or only choose how a value is shown or which file is read.
_CONFIG_READ_FLAGS = frozenset({
    "--show-origin", "--show-scope", "--name-only", "--null", "-z",
    "--includes", "--no-includes", "--global", "--system", "--local",
    "--worktree", "--bool", "--int", "--bool-or-int", "--path", "--expiry-date",
    "--no-type",
})
#: Flags whose next word is a value, not a key.
_CONFIG_VALUED_FLAGS = frozenset({"--file", "-f", "--blob", "--default", "--type"})
_CONFIG_JOINED_PREFIXES = ("--file=", "--blob=", "--default=", "--type=")
#: The ``git config get`` / ``git config list`` spellings (git 2.46+) add these.
_CONFIG_SUBCOMMAND_READ_FLAGS = frozenset({
    "--all", "--regexp", "--fixed-value", "--show-names", "--no-show-names",
})
_CONFIG_SUBCOMMAND_JOINED_PREFIXES = ("--url=", "--value=")
_CONFIG_WRITE_SUBCOMMANDS = frozenset({
    "set", "unset", "rename-section", "remove-section", "edit",
})
_CONFIG_READ_SUBCOMMANDS = frozenset({"get", "list"})
#: Scope flags a ``--worktree`` write must not carry. Git takes any unique prefix
#: of a long option, so the test is a prefix test (``_config_scope_flag``).
_CONFIG_SCOPE_LONG_FLAGS = ("--global", "--system", "--local", "--file", "--blob")
#: Keys a ``--worktree`` write may set in a linked worktree (nexus-gvg6t). An
#: ALLOWLIST: the earlier denylist of code-running keys was open-ended
#: (``url.<base>.insteadOf``, ``interactive.diffFilter``, ``mergetool.<t>.path``,
#: ``difftool.<t>.path``, ``submodule.<n>.update``, ``remote.<n>.uploadpack``,
#: ``uploadpack.packObjectsHook``, ``core.worktree``, ``core.bare`` all passed),
#: so a key is refused unless it is named here. Lowercased; section and variable
#: names are case-folded, and every key below is a value git reads and never runs.
_CONFIG_WORKTREE_KEYS = frozenset({
    # Which paths the working tree checks out; a boolean the checkout reads.
    "core.sparsecheckout", "core.sparsecheckoutcone",
    # Whether commits are signed; a boolean (the signing PROGRAM is gpg.program,
    # which is not allowed).
    "commit.gpgsign",
    # Display and diff behavior, each a number, a boolean or a fixed word.
    "core.abbrev", "core.quotepath", "color.ui", "diff.renames",
    "merge.conflictstyle", "status.showuntrackedfiles",
})
#: ``user.name``, ``user.email``, ``user.signingKey`` and ``user.useConfigOnly``:
#: authorship strings and a key id, read and never run. One level only, so
#: ``user.<sub>.<var>`` does not match.
_CONFIG_WORKTREE_USER_RE = re.compile(r"user\.[a-z0-9-]+")
#: Flags that leave the key and value alone or only choose how a value is stored.
_CONFIG_WORKTREE_FLAGS = frozenset({
    "--worktree", "--add", "--replace-all", "--unset", "--unset-all", "--bool",
    "--int", "--bool-or-int", "--path", "--no-type", "--fixed-value",
})
_CONFIG_WORKTREE_VALUED = frozenset({"--type", "--comment", "--default"})
_CONFIG_WORKTREE_JOINED = ("--type=", "--comment=", "--default=")
#: git's global options that take the NEXT word as their value. ``--exec-path``
#: and the like take none (a value needs ``=``). Of these, the ones that point
#: git at another repository.
_GIT_VALUED_GLOBALS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--attr-source", "--super-prefix",
})
_GIT_OTHER_REPO_GLOBALS = frozenset({"-C", "--git-dir", "--work-tree"})


#: nexus-gvg6t: the tokenizer's stop characters, outside data: everything that
#: opens data (a quote, ``<<``, ``#``), ends a line (a heredoc body starts there)
#: or that the tokenizer cannot read exactly (it gives up on all the rest).
_MASK_STOP_RE = re.compile(r"""['"\\$`{}*?\[\r\n#]|<<""")
#: Characters that end a word, for the one place the tokenizer asks whether an
#: empty string is alone (``_config_mask``).
_MASK_BOUNDARY = frozenset(";&|()<>")
_MASK_DELIM = r"""(?:'([\w.\-]+)'|"([\w.\-]+)"|\\([\w.\-]+))"""
_MASK_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*" + _MASK_DELIM + r"(?=[\s;&|()<>]|$)")
_MASK_CAT_HEREDOC_RE = re.compile(
    r"\$\([ \t]*cat[ \t]+<<(-?)[ \t]*" + _MASK_DELIM + r"[ \t]*\n"
)
_MASK_CAT_CLOSE_RE = re.compile(r'[ \t]*\)"')
_MASK_WORD_RE = re.compile(r"[\w.\-/]+")
#: A double-quoted string the shell cannot expand: no ``$`` or backtick, and a
#: backslash only as an escape pair (``\"``, ``\\``, or a literal ``\|``). An
#: unrolled loop, so a long string is one linear pass. No match (an unclosed
#: quote, a ``$``, a backtick) means the tokenizer gives up.
_MASK_DQUOTE_RE = re.compile(r'"([^"\\$`]*(?:\\.[^"\\$`]*)*)"', re.DOTALL)
#: Heredocs opened on one line before the first newline; more gives up.
_MASK_MAX_PENDING = 16
#: The joined-words reader (``_JoinTrack``): word and break boundaries, and the
#: words one command may hold before repeats are collapsed and the rest refused.
_MASK_PLAIN_SPLIT_RE = re.compile(r"(\s+|[;&|()]+)")
_JOIN_MAX_WORDS = 64
_JOIN_OVERFLOW = "\x00"
#: What a data string stands for in the cursor's text: a word that is not a plain
#: word is ``Q``, and ``-Q`` when it starts with a dash, so the cursor sees an
#: option it cannot read rather than a positional.
_MASK_OPAQUE = "Q"
_MASK_OPAQUE_OPTION = "-Q"


class _Masked(NamedTuple):
    """*text*: the command with each data string replaced by one word and each
    comment dropped. *data*: the data strings that name ``git``, to be read as
    command text too."""

    text: str
    data: tuple[str, ...]
    joined: tuple[str, ...] = ()


class _JoinTrack:
    """The shell words of each simple command that holds a quoted string, for
    ``_config_joined_hit``. A word is the glued run of its unquoted text and its
    quoted bodies, with the quotes gone (``g''it`` is ``git``); a command ends at
    a break character or a newline. A command with no quoted word is dropped
    when it ends, so a plain command costs one split and no string."""

    __slots__ = ("words", "cur", "quoted", "out")

    def __init__(self) -> None:
        self.words: list[str] = []
        self.cur: list[str] = []
        self.quoted = False
        self.out: list[list[str]] = []

    def feed(self, text: str) -> None:
        parts = _MASK_PLAIN_SPLIT_RE.split(text)
        for k, part in enumerate(parts):
            if k % 2 == 0:
                if part:
                    self.cur.append(part)
                continue
            self.word_end()
            if "\n" in part or not part.isspace():
                self.command_end()

    def quote(self, body: str) -> None:
        self.cur.append(body)
        self.quoted = True

    def word_end(self) -> None:
        if self.cur:
            self.words.append("".join(self.cur))
            self.cur = []

    def command_end(self) -> None:
        self.word_end()
        if self.quoted and self.words:
            self.out.append(self.words)
        self.words = []
        self.quoted = False

    def joined(self) -> tuple[str, ...]:
        self.command_end()
        res: list[str] = []
        for words in self.out:
            if len(words) > _JOIN_MAX_WORDS:
                # Runs of one word repeated: two copies decide every verdict a
                # repeat can change (the positional count saturates at two), so
                # a run keeps two. What is still too long is refused unread.
                kept: list[str] = []
                run = 0
                for w in words:
                    run = run + 1 if kept and kept[-1] == w else 1
                    if run <= 2:
                        kept.append(w)
                if len(kept) > _JOIN_MAX_WORDS:
                    res.append(_JOIN_OVERFLOW + " ".join(kept))
                    continue
                words = kept
            res.append(" ".join(words))
        return tuple(res)


def _heredoc_end(text: str, p: int, delim: str, dash: bool) -> tuple[int, int] | None:
    """For a heredoc body that starts at *p* (the start of a line): ``(start of
    the delimiter line, index past that line and its newline)``, or ``None`` when
    no line is *delim*. The terminator must be the whole line (with *dash*, after
    any tabs). One pass: ``find`` jumps between candidate lines."""
    n = len(text)
    size = len(delim)
    pos = p
    if not dash:
        while True:
            if text.startswith(delim, pos) and (pos + size == n or text[pos + size] == "\n"):
                return pos, min(pos + size + 1, n)
            k = text.find("\n" + delim, pos)
            if k < 0:
                return None
            pos = k + 1
    while pos <= n:
        k = pos
        while k < n and text[k] == "\t":
            k += 1
        if text.startswith(delim, k) and (k + size == n or text[k + size] == "\n"):
            return pos, min(k + size + 1, n)
        nl = text.find("\n", k)
        if nl < 0:
            return None
        pos = nl + 1
    return None


def _data_squash(body: str) -> str:
    """*body* with the characters a consumer's shell would drop from inside a word
    removed (quotes, backslashes), so ``gi""t`` and ``g\\it`` name ``git``."""
    return body.replace("'", "").replace('"', "").replace("\\", "")


def _config_mask(command: str) -> _Masked | None:
    """Read *command* once, left to right, and return it with every data string
    replaced by one word and every comment dropped (see the block comment above
    ``_CONFIG_STRICT_RE``), or ``None`` when something outside data cannot be read
    exactly. Linear: a regex jumps to the next stop character, and each quote,
    heredoc body and comment is skipped with one ``find``."""
    n = len(command)
    out: list[str] = []
    data: list[str] = []
    pending: list[tuple[str, bool]] = []
    track = _JoinTrack()
    prev = ""  # the last character of the text so far; "" at the start
    i = 0
    while True:
        m = _MASK_STOP_RE.search(command, i)
        j = n if m is None else m.start()
        if j > i:
            out.append(command[i:j])
            track.feed(command[i:j])
            prev = command[j - 1]
        if m is None:
            break
        i = j
        c = m.group()
        if c == "\n":
            out.append("\n")
            track.feed("\n")
            prev = "\n"
            i += 1
            for delim, dash in pending:
                end = _heredoc_end(command, i, delim, dash)
                if end is None:
                    return None
                body = command[i:end[0]]
                if "git" in body or "git" in _data_squash(body):
                    data.append(body)
                i = end[1]
            pending = []
        elif c == "\\" and command.startswith("\\\n", i):
            i += 2  # a line continuation: the shell joins the two lines, so do we
        elif c == "#":
            if prev == "" or prev.isspace():
                e = command.find("\n", i)
                i = n if e < 0 else e
            else:
                out.append("#")
                track.feed("#")
                prev = "#"
                i += 1
        elif c == "<<":
            hm = _MASK_HEREDOC_RE.match(command, i)
            if hm is not None and len(pending) < _MASK_MAX_PENDING:
                pending.append((hm.group(2) or hm.group(3) or hm.group(4), hm.group(1) == "-"))
                out.append("<<Q")
                track.feed("<<Q")
                prev = "Q"
                i = hm.end()
            elif command.startswith("<<<", i):
                out.append("<<<")
                track.feed("<<<")
                prev = "<"
                i += 3
            else:
                return None
        elif c in "'\"":
            if c == "'":
                e = command.find("'", i + 1)
                if e < 0:
                    return None
                body = content = command[i + 1:e]
                end_at = e + 1
            elif command.startswith("$(", i + 1):
                cm = _MASK_CAT_HEREDOC_RE.match(command, i + 1)
                if cm is None:
                    return None
                found = _heredoc_end(
                    command, cm.end(), cm.group(2) or cm.group(3) or cm.group(4),
                    cm.group(1) == "-",
                )
                if found is None:
                    return None
                cl = _MASK_CAT_CLOSE_RE.match(command, found[1])
                if cl is None:
                    return None
                body = command[cm.end():found[0]]
                content = body.rstrip("\n")
                end_at = cl.end()
            else:
                dm = _MASK_DQUOTE_RE.match(command, i)
                if dm is None:
                    return None
                body = content = dm.group(1)
                end_at = dm.end()
            if content == "":
                # An empty string is a word only when nothing is glued to it:
                # `user.name ''` passes an empty argument, `con''fig` is `config`.
                before = command[i - 1:i] if i else ""
                after = command[end_at:end_at + 1]
                alone = (not before or before.isspace() or before in _MASK_BOUNDARY) and (
                    not after or after.isspace() or after in _MASK_BOUNDARY
                )
                word = _MASK_OPAQUE if alone else ""
            elif _MASK_WORD_RE.fullmatch(content):
                word = content
            else:
                word = _MASK_OPAQUE_OPTION if content.startswith("-") else _MASK_OPAQUE
            out.append(word)
            # A glued empty string emits no character but is still part of a word
            # (`''#` is the word `#`, not a comment), so what follows is not at a
            # word start.
            prev = word[-1] if word else "'"
            track.quote(content)
            i = end_at
            if "git" in body or "git" in _data_squash(body):
                data.append(body)
        else:
            return None
    if pending:
        return None
    text = "".join(out)
    if _CONFIG_NONPLAIN_RE.search(text) is not None:
        return None
    return _Masked(text, tuple(data), track.joined())


class _ConfigView(NamedTuple):
    """What the config rule may trust about a command, judged on the RAW text.

    *raw*: the command as written. *cursor*: plain apart from data, so the segment
    split and the subcommand cursor of ``_config_relaxed_match`` are exact on
    *text* (see ``_config_mask``); *data* holds the data strings that name
    ``git``. *reads*: nothing can rebuild text (no capture, executor, file
    redirect or non-filter pipe), so a proven get read may be excused in text that
    is not plain. *memo*: the cursor verdict by worktree flag, so the two scans of
    ``_scan_command`` read the command once."""

    raw: str
    cursor: bool
    reads: bool
    text: str = ""
    data: tuple[str, ...] = ()
    memo: dict[bool, bool] | None = None
    joined: tuple[str, ...] = ()


def _config_view(command: str, relaxed: bool) -> _ConfigView:
    """The config view of *command*; *relaxed* is ``not _may_recombine`` of its
    normalized form."""
    reads = relaxed and _CAPTURE_RE.search(command) is None
    masked = _config_mask(command)
    if masked is not None and not _may_recombine(masked.text):
        return _ConfigView(
            command, True, reads, masked.text, masked.data, {}, masked.joined,
        )
    return _ConfigView(command, False, reads)


def _config_known_read_flag(a: str) -> bool:
    return (
        a in _CONFIG_GET_FLAGS or a in _CONFIG_VALUED_FLAGS or a in _CONFIG_READ_FLAGS
        or a.startswith(_CONFIG_JOINED_PREFIXES)
    )


def _config_scope_flag(a: str) -> bool:
    """True for ``--global``/``--system``/``--local``/``--file``/``--blob``, any
    unique-prefix spelling of them, ``--file=x`` and ``-f``, ``-fx``."""
    if _MASK_OPAQUE in a and a.startswith("-"):
        return True  # a quoted option word: it may be any of them
    if a.startswith("--"):
        head = a.split("=", 1)[0]
        return len(head) >= 3 and any(f.startswith(head) for f in _CONFIG_SCOPE_LONG_FLAGS)
    return a.startswith("-f")


def _config_worktree_key(word: str) -> bool:
    """True when *word* is a key a ``--worktree`` write may set (see
    ``_CONFIG_WORKTREE_KEYS``)."""
    k = word.lower()
    return k in _CONFIG_WORKTREE_KEYS or _CONFIG_WORKTREE_USER_RE.fullmatch(k) is not None


def _config_worktree_scoped(args: list[str]) -> bool:
    """True when *args* write the per-worktree config file and nothing wider:
    ``--worktree`` before the first positional, no scope flag anywhere (a scope
    flag after the first positional is a value to this git, a flag to another,
    so it denies too), only flags that change nothing but how the value is stored
    (``_CONFIG_WORKTREE_FLAGS``), and a first positional, the KEY, that is on the
    allowlist. A form with no key (``-e``, ``--rename-section``, a subcommand)
    and any flag or key not named denies."""
    seen = False
    key: str | None = None
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        i += 1
        if _config_scope_flag(a):
            return False
        if key is not None:
            continue  # a value or pattern; only a scope flag matters, checked above
        if a.startswith("-"):
            if a == "--worktree":
                seen = True
            elif a in _CONFIG_WORKTREE_VALUED:
                i += 1
            elif a not in _CONFIG_WORKTREE_FLAGS and not a.startswith(_CONFIG_WORKTREE_JOINED):
                return False
        elif a in _CONFIG_WRITE_SUBCOMMANDS or a in _CONFIG_READ_SUBCOMMANDS:
            return False
        else:
            key = a
    return seen and key is not None and _config_worktree_key(key)


def _config_read_kind(args: list[str], *, valued_ok: bool = True) -> str | None:
    """``"get"`` for a get or list read, ``"bare"`` for a bare-key read, ``None``
    unless *args*, the words after a ``config`` word, are a proven read.

    Left to right, so a valued flag takes its next word whatever it looks like
    (``--file --get k v`` writes ``k`` to a file named ``--get``). In the legacy
    form git stops reading options at the first positional (``git config
    user.name --global`` sets ``user.name`` to the text ``--global`` and
    ``git config a.b.c --type=bool`` sets ``a.b.c`` to ``--type=bool``, both
    measured on git 2.53), so a dash word after it is a COUNTED positional, not
    a flag; an unknown one still denies, in case another git reads it as a flag.
    The ``get``/``list`` subcommands parse their own options throughout.

    The count of words is only trusted in plain text (the caller's job). With
    *valued_ok* False a valued flag (``-f x``, ``--file=x``, ``--type=bool``)
    is not a read at all: its value is free text a quote can give whitespace.
    """
    get_mode = False
    sub_read = False
    positionals = 0
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        i += 1
        if a.startswith("-"):
            if positionals and not sub_read:
                if not _config_known_read_flag(a):
                    return None
            elif a in _CONFIG_GET_FLAGS:
                get_mode = True
                continue
            elif a in _CONFIG_VALUED_FLAGS:
                if not valued_ok:
                    return None
                i += 1
                continue
            elif _config_known_read_flag(a):
                if not valued_ok and a.startswith(_CONFIG_JOINED_PREFIXES):
                    return None
                continue
            elif sub_read and (
                a in _CONFIG_SUBCOMMAND_READ_FLAGS
                or a.startswith(_CONFIG_SUBCOMMAND_JOINED_PREFIXES)
            ):
                continue
            else:
                return None
        elif positionals == 0 and not get_mode:
            if a in _CONFIG_WRITE_SUBCOMMANDS:
                return None
            if a in _CONFIG_READ_SUBCOMMANDS:
                sub_read = True
        positionals += 1
    if get_mode or sub_read:
        return "get"
    return "bare" if positionals <= 1 else None


def _config_write_form(args: list[str], *, worktree: bool = False) -> bool:
    """True unless *args* are a proven read. With *worktree* (a proven linked
    worktree, and the command names no other repository) a ``--worktree`` write
    of a key that runs no code is not one either."""
    if _config_read_kind(args) is not None:
        return False
    return not (worktree and _config_worktree_scoped(args))


class _GitGlobals:
    """Where the git SUBCOMMAND is, for one git command word.

    The subcommand is the first word after git that is not a global option or
    that option's value. ``at_subcommand`` advances a cursor over the words up
    to a ``config`` word and says whether that word is the subcommand (a
    ``config`` after ``log``/``grep`` is an argument; one that is the VALUE of
    ``-C`` is a directory). The cursor only moves forward, so a segment costs
    one pass however many ``config`` words it holds. Exact only on PLAIN text."""

    __slots__ = ("pos", "skip", "sub", "other_repo", "quoted")

    def __init__(self, pos: int) -> None:
        self.pos = pos
        self.skip = False  # the next word is the value of a global option
        self.sub = False  # a subcommand word was seen
        self.other_repo = False  # -C / --git-dir / --work-tree named a repository
        # A quoted word stood where the subcommand may be (nexus-gvg6t review B1):
        # `git '--git-dir=.git' config` hides an option, `git 'x y' config` any word.
        self.quoted = False

    def at_subcommand(self, seg: str, end: int) -> bool:
        if not self.sub:
            for m in _WORD_RE.finditer(seg, self.pos, end):
                w = m.group()
                if self.skip:
                    self.skip = False
                elif w in _GIT_VALUED_GLOBALS:
                    self.skip = True
                    self.other_repo = self.other_repo or w in _GIT_OTHER_REPO_GLOBALS
                elif _MASK_OPAQUE in w:
                    # An opaque word in a global-option slot: an option that may
                    # name another repository, or the subcommand itself. Either
                    # way the `config` after it may be the verb.
                    self.other_repo = True
                    if not w.startswith("-"):
                        self.sub = True
                        self.quoted = True
                        break
                elif w.startswith("-"):
                    if w.startswith(("--git-dir=", "--work-tree=", "-C")):
                        self.other_repo = True
                else:
                    self.sub = True
                    break
        self.pos = end
        return self.quoted or (not self.sub and not self.skip)


def _config_relaxed_match(text: str, *, worktree: bool = False) -> bool:
    """True when PLAIN *text* holds a ``config`` command that is not a proven
    read (see the block comment above). Linear: one split, one token pass per
    segment with a forward-only cursor for the subcommand, and at most
    ``_CONFIG_MAX_WORDS_PER_SEGMENT`` argument reads each. With *worktree*, a
    ``--worktree`` write (and no other repository named, by option or by
    ``GIT_DIR=``) is not a hit."""
    text = _SAFE_REDIRECT_RE.sub(" ", text)
    text = _CONFIG_REDIRECT_RE.sub(" ", text)
    if worktree and _GIT_REPO_ENV_RE.search(text) is not None:
        worktree = False
    for seg in _CONFIG_SEGMENT_SPLIT_RE.split(text):
        if "config" not in seg:
            continue
        gl: _GitGlobals | None = None
        words = 0
        for m in _PATHISH_TOKEN_RE.finditer(seg):
            tok = m.group().rstrip("/")
            base = tok[tok.rfind("/") + 1:]
            s = m.start()
            verb = False
            exempt = worktree
            if _DASHED_CONFIG_RE.fullmatch(base):
                verb = True
            elif tok == "config":
                if s > 0 and seg[s - 1] == "=":
                    # `-c core.x=config` sets a VALUE. An alias that names config
                    # is `_CONFIG_ALIAS_INJECT_RE`'s; `--grep=config` is a search
                    # term.
                    pass
                elif gl is not None:
                    # A word glued to the front cannot be told from the
                    # subcommand, so it counts.
                    verb = (s > 0 and not seg[s - 1].isspace()) or gl.at_subcommand(seg, s)
                    exempt = worktree and not gl.other_repo
            if verb:
                words += 1
                if words > _CONFIG_MAX_WORDS_PER_SEGMENT:
                    return True
                if _config_write_form(seg[m.end():].split(), worktree=exempt):
                    return True
            if "git" in base and _GIT_BASENAME_RE.fullmatch(base):
                gl = _GitGlobals(m.end())
            elif "hub" in base and _HUB_TOKEN_RE.fullmatch(tok):
                gl = _GitGlobals(m.end())
    return False


def _config_get_read(text: str, pos: int) -> bool:
    """True when the words after *pos* (just past a ``config`` word), up to the
    first command break, are a proven GET read made only of exact read flags
    (``_config_read_kind`` with *valued_ok* False). More than
    ``_CONFIG_ARG_WORDS_CAP`` words is unprovable and False."""
    args: list[str] = []
    for m in _CONFIG_ARG_WALK_RE.finditer(text, pos):
        w = m.group()
        if w in _CONFIG_ARG_BREAKS:
            break
        if len(args) == _CONFIG_ARG_WORDS_CAP:
            return False
        args.append(w)
    return _config_read_kind(args, valued_ok=False) == "get"


def _config_strict_match(text: str, view: _ConfigView, git_end: int) -> bool:
    """True when *text*, which is NOT plain, holds a ``config`` word after the
    first git (or hub) command at *git_end* that is not an excused get read, a
    ``git-config`` program token, or the raw command holds ``config`` inside a
    substitution span."""
    if _DASHED_CONFIG_CMD_RE.search(text) is not None:
        return True
    for m in _CONFIG_STRICT_RE.finditer(text, git_end):
        if not (view.reads and _config_get_read(text, m.end())):
            return True
    raw = view.raw
    if "$" in raw or "`" in raw:
        raw_end = _first_git_command(raw) or 0
        for sp in _CONFIG_SPAN_RE.finditer(raw):
            if sp.end() > raw_end and _CONFIG_IN_SPAN_RE.search(sp.group()) is not None:
                return True
    return False


#: Characters that make a data string's words unprovable when a consumer runs it
#: as a command: ``$``, brace and glob characters (``git config {user.name,x}``
#: is one word to the cursor and two to a shell). A backtick is left out: prose
#: in a commit message is full of markdown backticks, and ``_BACKTICK_ECHO_RE``
#: already resolves the ``echo``/``printf`` form.
_CONFIG_DATA_META_RE = re.compile(r"[${}*?\[\r]")


def _config_data_hit(data: tuple[str, ...], worktree: bool) -> bool:
    """True when a data string that names ``git`` holds a ``git config`` write if
    a consumer runs it as a command (``ssh host 'git config user.name x'``,
    ``git submodule foreach 'git config ...'``, ``tmux send-keys '...'``). The
    string is read like the whole command: the cursor on its normalized form
    (quotes, escapes and ``$(echo config)`` resolved), or, when it holds a
    character the cursor cannot count (``_CONFIG_DATA_META_RE``), the strict
    rule. A string with no git command or no ``config`` after quote removal is
    skipped without a scan."""
    for d in data:
        n = _normalize_for_primary_scan(d)
        ends = [
            e for e in (_first_git_command(n), _first_hub_command(n)) if e is not None
        ]
        if not ends or "config" not in n:
            continue
        if _CONFIG_DATA_META_RE.search(d) is None:
            if _config_relaxed_match(n, worktree=worktree and not _repo_env_named(d)):
                return True
        elif _config_strict_match(n, _ConfigView(d, False, False), min(ends)):
            return True
    return False


#: Programs and git verbs that run an argument as a command (nexus-gvg6t review
#: B4). A shell word of such a command is read joined to its neighbours, because
#: the consumer joins its arguments: no single string need hold the whole write.
_CONSUMER_RE = re.compile(
    r"(?<![\w.\-])(?:ssh|mosh|tmux|screen|foreach|filter-branch|rebase|bisect|xargs|"
    r"parallel|watch|xterm|difftool|mergetool|nohup|timeout|nice|setsid|flock|stdbuf|"
    r"sudo|doas|runuser)(?![\w.\-])"
)


def _config_joined_hit(joined: tuple[str, ...], worktree: bool) -> bool:
    """True when a command that has quoted words and names a consumer holds a
    ``git config`` write once its words are joined with their quotes removed
    (``ssh host 'git config' user.name x``, ``git rebase -x 'g''it config ...'``,
    ``tmux send-keys 'git config' ' user.name x'``). *joined* is
    ``_Masked.joined``: one string per simple command that has a quoted word,
    each word's pieces glued and the words separated by one space. A string the
    mask marked as too long to read (``_JOIN_OVERFLOW``) denies when it names git
    and ``config`` after normalization: a count it cannot prove is a count it
    refuses. Text with a character the cursor cannot count takes the strict rule."""
    for j in joined:
        over = j.startswith(_JOIN_OVERFLOW)
        if over:
            j = j[len(_JOIN_OVERFLOW):]
        if _CONSUMER_RE.search(j) is None:
            continue
        n = _normalize_for_primary_scan(j)
        ends = [
            e for e in (_first_git_command(n), _first_hub_command(n)) if e is not None
        ]
        if not ends or "config" not in n:
            continue
        if over:
            return True
        if _CONFIG_DATA_META_RE.search(j) is None:
            if _config_relaxed_match(n, worktree=worktree and not _repo_env_named(j)):
                return True
        elif _config_strict_match(n, _ConfigView(j, False, False), min(ends)):
            return True
    return False


def _config_cursor_hit(view: _ConfigView, worktree: bool) -> bool:
    """The cursor verdict for a command ``_config_mask`` could read: a non-read
    ``config`` command in the masked text, or in the data strings read as command
    text (``ssh host 'git config user.name x'``). Memoized by *worktree*: the
    caller asks for the normalized and the zero-expansion text, which are the
    same command here. A repository-pointing variable anywhere in the raw
    command, quoted or not, withdraws the ``--worktree`` exemption."""
    memo = view.memo if view.memo is not None else {}
    hit = memo.get(worktree)
    if hit is None:
        wt = worktree and not _repo_env_named(view.raw)
        hit = (
            _config_relaxed_match(view.text, worktree=wt)
            or _config_data_hit(view.data, wt)
            or _config_joined_hit(view.joined, wt)
        )
        memo[worktree] = hit
    return hit


def _config_match(
    normalized_text: str, view: _ConfigView, *, worktree: bool = False,
) -> str | None:
    """``"config"`` when *normalized_text* holds a git (or hub) command and a
    ``config`` command that is not a proven read, else ``None``. See the block
    comment above ``_CONFIG_STRICT_RE``. *view* is the RAW-text judgment of
    whether the text is plain; *worktree* admits ``--worktree`` writes in plain
    text."""
    ends = [
        e for e in (_first_git_command(normalized_text), _first_hub_command(normalized_text))
        if e is not None
    ]
    if not ends:
        return None
    if _CONFIG_ALIAS_INJECT_RE.search(normalized_text) is not None:
        return "config"
    if view.cursor:
        hit = _config_cursor_hit(view, worktree)
    else:
        hit = _config_strict_match(normalized_text, view, min(ends))
    return "config" if hit else None


def _config_hit(
    normalized_text: str, view: _ConfigView, *, worktree: bool = False,
) -> str | None:
    """``_config_match`` over the normalized text and its zero-expansion form."""
    if view.cursor:  # no expansion to delete: the two forms are one command
        return _config_match(normalized_text, view, worktree=worktree)
    return _config_match(normalized_text, view, worktree=worktree) or _config_match(
        _delete_all_expansions(normalized_text), view, worktree=worktree,
    )


def _primary_match(
    normalized_text: str, *, relaxed: bool = False, view: _ConfigView | None = None,
) -> str | None:
    """The first write verb, HEAD-moving ``gh`` phrase or non-read ``git
    config`` in *normalized_text*, as a short name, or ``None``. The verb scan
    (``_verb_match``) runs first and is unchanged; ``config`` (nexus-dsr6a)
    only ever ADDS a denial after it, so no command that denied before allows
    now. Without a *view* the text is treated as not plain."""
    hit = _verb_match(normalized_text, relaxed=relaxed)
    if hit is not None:
        return hit
    if view is None:
        view = _ConfigView(normalized_text, False, False)
    return _config_match(normalized_text, view)


def _verb_match(normalized_text: str, *, relaxed: bool = False) -> str | None:
    """Name of the first write verb after the first git (or hub) COMMAND
    token, else of the first HEAD-moving ``gh`` phrase (``_gh_match``), or
    ``None``. A few linear passes, no catastrophic-backtracking risk: token
    walks, then a plain alternation/boundary search with no nested
    quantifiers.

    *relaxed* is the caller's ``not _may_recombine(text)``: only then are the
    read-only exemptions honoured (``merge-base`` / ``merge-tree`` via
    ``_VERB_RE_READ_ONLY``, and the whole-token literals of
    ``_EXEMPT_TOKEN_RE`` blanked out before the scan).
    """
    text = normalized_text
    verb_re = _VERB_RE
    if relaxed:
        text = _EXEMPT_TOKEN_RE.sub(" ", text)
        verb_re = _VERB_RE_READ_ONLY
    git_end = _first_git_command(text)
    if git_end is not None:
        m = verb_re.search(text, git_end)
        if m is not None:
            return m.group(1)
    hub_end = _first_hub_command(text)
    if hub_end is not None:
        m = verb_re.search(text, hub_end)
        if m is not None:
            return m.group(1)
        if _HUB_SYNC_RE.match(text, hub_end) is not None:
            return "hub sync"
    return _gh_match(text)


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
    literal_git_present = _first_git_command(normalized_text) is not None
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
        trailing_match = _TRAILING_BACKSLASH_COUNT_RE.search(text)
        assert trailing_match is not None  # `\\*$` matches the empty string at the end
        trailing = len(trailing_match.group())
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
        # PATH-only lookup: this runs with cwd=<the project>, where a planted
        # git.exe would win a bare-name spawn on Windows (finding A).
        git = _exec_path.which_off_cwd("git")
        if git is None:
            return None
        # git prints paths as UTF-8 on every platform; the locale's cp1252 would
        # garble a non-ASCII checkout path (nexus-ikoqj).
        git_dir = subprocess.run(
            [git, "rev-parse", "--git-dir"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        common = subprocess.run(
            [git, "rev-parse", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except Exception:  # noqa: BLE001 — undeterminable: fail open
        return None
    if git_dir.returncode != 0 or common.returncode != 0:
        return None
    gd = os.path.realpath(os.path.join(cwd, git_dir.stdout.strip()))
    cd = os.path.realpath(os.path.join(cwd, common.stdout.strip()))
    return gd != cd


def _deny_message(agent_type: str, verb_names: str, *,
                  undeterminable: bool = False, in_worktree: bool = False) -> str:
    who = f"you are subagent `{agent_type or 'unknown'}`"
    head = (
        f"Subagents never run git write verbs ({verb_names}) in the shared "
        f"tree ({who}); that includes `gh`/`hub` commands that move HEAD or "
        f"rewrite the working tree (gh pr checkout, gh repo sync). This guard is intentionally STRUCTURE-AGNOSTIC "
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
    if "config" in verb_names.split(", "):
        head += (
            "\n`git config` writes are denied (nexus-dsr6a): `core.hooksPath` "
            "redirects the hooks that run on the orchestrator's later commits, "
            "and `user.*` rewrites authorship, in this tree and, with "
            "--global/--system, in every repository on the box. Reads stay "
            "allowed: `git config --get <key>`, `--get-all`, `--get-regexp`, "
            "`--list`/`-l`, `--show-origin`, a bare `git config <key>`, and "
            "`git config get|list`, as long as the command cannot rebuild text "
            "(no `$`, backtick, executor or non-filter pipe stage outside "
            "quotes) and carries no other write flag. Quoted text and a "
            "quoted-delimiter heredoc are read as data, so a commit message "
            "that says `fix(config): x` and `git grep \"config\"` pass; a "
            "backslash, `{ } * ? [` or `$` outside quotes makes the word "
            "count unprovable, so only a `--get*`/`--list`/`get`/`list` read "
            "made of exact flags passes with them. Quoted text that spells "
            "out a `git config <key> <value>` write denies too (it could be "
            "run by `ssh` or `git submodule foreach`). Defining an alias on "
            "the command line (`-c alias.x=`, `--config-env alias.x=`) is "
            "denied too. Need a config change? Hand it back to the "
            "orchestrator to run."
        )
        if in_worktree:
            head += (
                "\nA linked worktree is yours to rewrite, but a plain `git "
                "config` there writes the common `.git/config` the primary "
                "checkout reads. Only `git config --worktree <key> <value>` "
                "(the per-worktree file; no -C/--git-dir/GIT_DIR= may name "
                "another repository, and the key must be one that runs no "
                "code: `user.*`, `core.sparseCheckout`, `commit.gpgsign` and a "
                "few display keys) and reads pass. If `config` is only a "
                "word in a commit message or a search term, reword the "
                "message, or pass it with `git commit -F <file>`."
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
        f"Hand back your changes as diffs + file paths -- a background "
        f"dispatch does that via SubagentHandback or SendMessage before idling; a foreground "
        f"one's final message IS the hand-back, no separate report call "
        f"needed -- so the ORCHESTRATOR commits, pathspec-limited "
        f"(RDR-184 Gap-4, feedback_orchestration_friction).\n"
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
        f"qualify. The cwd of the tool call decides that, and a `cd <wt>` or "
        f"`git -C <wt>` inside the command does not count: dispatch with "
        f"isolation: worktree instead.\n"
        f"An escape (`# routing-allow: <reason>`) exists for this "
        f"guard, but only on the user's explicit instruction to use "
        f"it -- it is not yours to reach for."
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
        f"An escape (`# routing-allow: <reason>`) exists for this "
        f"guard, but only on the user's explicit instruction to use "
        f"it -- it is not yours to reach for."
    )
    return head


#: The chas test host's ssh aliases (nexus-yc7fs, Sam 2026-10-14). A git write
#: sent there runs in chas's own clone, never in the checkout this guard protects.
_REMOTE_TEST_HOSTS: frozenset[str] = frozenset({"chas", "chas-test"})

#: ssh flags that run nothing locally. Every other option is refused, since
#: ProxyCommand, LocalCommand, -F (a config that can set either) and -J (a
#: jump) can all start a process on this machine or route somewhere else.
_SAFE_SSH_FLAG_LETTERS: frozenset[str] = frozenset("nqtT")
_SAFE_SSH_O_OPTIONS: frozenset[str] = frozenset(
    {"connecttimeout", "batchmode", "serveraliveinterval", "serveralivecountmax"}
)
_SAFE_SSH_O_VALUE_RE = re.compile(r"[A-Za-z0-9]+")

#: The only characters accepted outside single quotes. Everything else that a
#: shell treats specially there is refused: ``$`` (every expansion), backtick,
#: ``( ) { } [ ] * ? ~ ^ ! #`` (substitution, brace and glob expansion, zsh
#: flags and qualifiers, history, comments), backslash, ``; & | < >`` and line
#: breaks. With those gone, zsh and bash both read each word literally, so the
#: words shlex sees are exactly the words ssh receives. ``<`` is refused too
#: (review round 3): the shell removes a redirection word, so in ``ssh chas-test
#: </dev/null -oProxyCommand=...`` the loop below took ``</dev/null`` for the
#: remote command while ssh received the option. ``ssh -n`` does what
#: ``</dev/null`` did. ``=`` stays for ``-oKey=value``; zsh's word-initial
#: ``=cmd`` only expands to a command's path, which never starts with ``-``.
_LITERAL_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \t_./:@,+-="
)

#: Remote commands that would leave chas for another host. A best-effort list:
#: the carve-out's job is to stop a subagent's git touching this checkout, and
#: chas reaching back to this machine is the one route to it worth naming.
_ONWARD_HOP_WORDS: tuple[str, ...] = ("ssh", "scp", "sftp", "rsync")


def _read_literally(command: str) -> bool:
    """True when the shell (zsh or bash) reads every word of ``command``
    literally: nothing outside single quotes expands, substitutes, globs or
    chains, so no second local command can start and no word can turn into an
    extra ssh option.

    An allowlist, not a blocklist (nexus-yc7fs review round 2). The blocklist
    draft missed words between the host and the remote command that a shell
    rewrites into ``-oProxyCommand=...`` (an unset ``$X``, a brace or glob
    pattern, ``$'...'``), and zsh's ``$~PWD`` glob qualifiers. Inside single
    quotes nothing is checked: both shells treat it as literal, and it is the
    remote command, which runs on chas. Inside double quotes the same
    allowlist applies, plus a literal single quote.
    """
    state = ""  # "", "'" or '"'
    for ch in command:
        if state == "'":
            if ch == "'":
                state = ""
        elif ch == "'" and state == "":
            state = "'"
        elif ch == '"':
            state = "" if state == '"' else '"'
        elif ch in _LITERAL_CHARS or (state == '"' and ch == "'"):
            continue
        else:
            return False
    return state == ""


def _safe_ssh_o_value(value: str) -> bool:
    key, sep, val = value.partition("=")
    return bool(sep) and key.lower() in _SAFE_SSH_O_OPTIONS and bool(
        _SAFE_SSH_O_VALUE_RE.fullmatch(val)
    )


def _remote_test_host_command(command: str) -> str | None:
    """The chas host a subagent command runs on, when the WHOLE command is one
    ssh invocation whose only work happens there; otherwise None.

    nexus-yc7fs: the primary rule matches ``git`` plus a write verb anywhere
    in the text, so ``ssh chas-test 'git -C <wt> add ...'`` was denied though
    its git runs in chas's clone. The carve-out is deliberately narrow: the
    shell reads the command literally (``_read_literally``), the first word is
    ssh, every option, before or after the host (OpenSSH parses both until the
    remote command starts), is one that runs nothing locally, the host is a
    chas alias (``user@`` allowed), and the remote command does not name an
    onward hop. Known limit: a remote command can still reach other hosts by
    means this does not name; the guard protects against a subagent's git, not
    against a hostile one.
    """
    if not _read_literally(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens or tokens[0] not in ("ssh", "/usr/bin/ssh"):
        return None

    host: str | None = None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            if tok.startswith("-o"):
                if len(tok) == 2:
                    i += 1
                    value = tokens[i] if i < len(tokens) else ""
                else:
                    value = tok[2:]
                if not _safe_ssh_o_value(value):
                    return None
            elif not tok[1:] or not set(tok[1:]) <= _SAFE_SSH_FLAG_LETTERS:
                return None
            i += 1
            continue
        if host is not None:
            break  # the remote command starts here; ssh parses no options after it
        host = tok
        i += 1

    if host is None or host.rsplit("@", 1)[-1] not in _REMOTE_TEST_HOSTS:
        return None
    if any(word in tok for tok in tokens[i:] for word in _ONWARD_HOP_WORDS):
        return None
    return host


def _scan_command(
    command: str, normalized: str,
) -> tuple[str | None, str | None, bool, _ConfigView]:
    """``(spliced_fragment, primary_match, relaxed, config_view)`` for a Bash
    *command* and its ``_normalize_for_primary_scan`` form: the verdict of both
    gates, before any exemption.

    ADJACENCY RULE (nexus-3c92m rounds 8-9): a SEPARATE gate, checked before
    (and independent of) the git+verb scan. Two branches keyed on whether `git`
    is present anywhere — see the `_EXPANSION_CONSTRUCT_RE` comment and
    `_find_spliced_expansion` for the full rule.

    PRIMARY RULE (nexus-3c92m rounds 4-5, extended round 9): the actual
    security boundary for git-write detection. See module docstring for the
    full rationale. Skipped when the adjacency rule already denies, since a
    spliced expansion could resolve to anything at runtime and the git+verb
    scan's "no match" verdict on the CURRENT (unresolved) text would be
    meaningless — the adjacency deny is authoritative regardless.

    Round 9: matched against BOTH the normalized text AND its ZERO-EXPANSION
    form (`_delete_all_expansions` — every remaining opaque construct deleted
    outright, simulating the "all vars unset" runtime condition that makes a
    chained splice like `g${a}${b}i${c}${d}t checkout` genuinely execute as
    `git checkout`). Deny if EITHER matches — this is the "principled closure"
    that catches any chain length or interleaving pattern without relying on
    the fragment heuristic's own (now also fixed, see
    `_expansion_construct_runs`) chain-grouping logic.

    nexus-7mgnd: the read-only exemptions (`merge-base`, three literal tokens)
    apply only when `_may_recombine` finds nothing in the command that could
    rebuild a word (*relaxed*). It is judged on the NORMALIZED text, which
    still holds every `$`/backtick; the zero-expansion form has those deleted,
    so it must not decide this for itself.

    nexus-dsr6a: *config_view* is the config rule's judgment of the RAW command
    (is it plain, may a get read be excused). Normalized text has already had
    quotes stripped and ``$(echo X)`` resolved, which a ``git config`` read test
    that counts words cannot see; see the block comment above
    `_CONFIG_STRICT_RE`.
    """
    spliced_fragment = _find_spliced_expansion(normalized)
    relaxed = not _may_recombine(normalized)
    view = _config_view(command, relaxed)
    primary_match = None
    if not spliced_fragment:
        primary_match = _primary_match(
            normalized, relaxed=relaxed, view=view,
        ) or _primary_match(
            _delete_all_expansions(normalized), relaxed=relaxed, view=view,
        )
    return spliced_fragment, primary_match, relaxed, view


def body(payload: dict[str, Any]) -> None:
    agent_id = str(payload.get("agent_id") or "")

    if not agent_id:
        _lib.pass_through()  # main conversation — the rule targets subagents only

    command = _lib.get_bash_command(payload)
    if not command:
        _lib.pass_through()

    # The chas carve-out (nexus-yc7fs): one ssh command whose git runs on the
    # test host. Checked before both gates because neither can tell a remote
    # git from a local one; `_remote_test_host_command` refuses anything that
    # could also run locally, so this admits nothing the gates exist to stop.
    if _remote_test_host_command(command) is not None:
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="allow_remote_test_host", tool_name="Bash",
            command_fragment=command,
        )
        _lib.pass_through()

    normalized = _normalize_for_primary_scan(command)

    spliced_fragment, primary_match, _relaxed, config_view = _scan_command(command, normalized)
    if not spliced_fragment and primary_match is None:
        _lib.pass_through()

    # Match FIRST, escape SECOND (the nexus-mzvwa.8 telemetry rule) — applies
    # to EITHER gate above.
    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
            escape_reason=_lib.extract_escape_reason(command),
        )
        _lib.pass_through()

    # The project comes from the payload, then CLAUDE_PROJECT_DIR. Never from the
    # process cwd: uv launches this script with `--directory ${CLAUDE_PLUGIN_ROOT}`
    # (finding C, nexus-f9bgu.36), so that is the plugin root. Neither given means
    # undeterminable, which the branch below treats like the primary checkout.
    cwd = str(payload.get("cwd") or "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    worktree = _in_linked_worktree(cwd) if cwd else None
    wt_config = False
    if worktree is True:
        # Linked worktree, POSITIVELY PROVEN: the agent owns its tree,
        # including destroying it. This is the ONLY exemption from either
        # gate's verdict. It does NOT cover a `git config` write that reaches
        # shared state (nexus-dsr6a): a plain `git config`, `--global`,
        # `--system` and `--local` write the common config the primary reads.
        # `--worktree` writes pass (git refuses them in a repo with linked
        # worktrees unless extensions.worktreeConfig is on, so they cannot
        # reach the common file); reads pass.
        wt_config = _config_hit(normalized, config_view, worktree=True) is not None
        if not wt_config:
            _lib.pass_through()
    # worktree is False (primary checkout) OR None (undeterminable) — EITHER
    # GATE WINS either way (nexus-3c92m round 4; retires the old nexus-ays2l
    # item 3 fail-open-for-hygiene-verbs carve-out). "I could not prove this
    # tree is safe" no longer earns a pass. The `# routing-allow:` escape
    # above remains for deliberate exceptions.

    agent_type = str(payload.get("agent_type") or "")

    if spliced_fragment and not wt_config:
        git_present = _first_git_command(normalized) is not None
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
    if wt_config:
        # Only the config rule applies in a worktree; the verbs it exempts must
        # not be named in the deny.
        verb_names = "config"
    else:
        matched = _matched_write_subcommands(command)
        # Holds by control flow: a None primary_match with nothing spliced
        # passed through above, and the spliced path returned before this point.
        assert primary_match is not None
        verb_names = ", ".join(sorted(matched)) if matched else primary_match

    _lib.log_routing_event(
        rule=RULE_NAME, outcome="deny", tool_name="Bash",
        command_fragment=command,
    )
    _lib.deny(
        _deny_message(
            agent_type, verb_names, undeterminable=worktree is None, in_worktree=wt_config,
        ),
        summary=(
            f"subagent git write verb ({verb_names}) blocked in the shared "
            f"tree by the structure-agnostic guard (nexus-3c92m round 4)."
        ),
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
