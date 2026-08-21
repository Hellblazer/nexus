# Pending release: plugin changes that are NOT live yet

`.claude-plugin/marketplace.json` pins `plugins[].source.ref` to an immutable
release tag. Claude Code loads this plugin's hooks, commands, skills, and agents
from **that tag**, not from your working tree. So every change below is merged
on `develop` and **inert in every running session** until the next release ships
and users install it.

This file is the acknowledgement ledger for that gap. It exists because the gap
is otherwise invisible: on 2026-07-25 a subagent ran `git stash -u` in a shared
tree and the guard that covers exactly that verb did not fire, because the
coverage had landed hours earlier and the installed plugin was still `v6.18.1`.
Three guards had been merged, closed as "mechanized", and were protecting
nothing.

**Rules, enforced by `tests/test_plugin_release_drift_ledger.py`:**

- Every file under the behavioural surface that differs from the pinned tag MUST
  be listed here. Adding a guard without declaring it fails the suite.
- When a release ships and the pin advances, drift goes to zero and this list
  MUST be emptied. A stale entry also fails the suite, so the ledger cannot
  quietly become fiction.
- Do NOT "fix" a failure by deleting entries. The entry is the honest statement
  that the thing is not yet live.

**Do not use this to justify skipping a release.** If a guard matters enough to
mechanize, it matters enough to ship.

---


## Awaiting the next release (pinned: v7.13.0)

- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` —
  nexus-3c92m, nine review rounds culminating in a DESIGN CHANGE (round 4)
  that then took five more rounds to harden. Round 6: the round-4/5 primary
  rule could still be defeated by an EXPANSION spliced inside a word — real
  bash confirmed `g${x:-i}t`, `g$(echo i)t`, `git st$(echo a)tus`, and
  `g$'\151't` all execute as `git`, so `git ch${x:-e}ckout -- f` allowed.
  Closed with four ordered normalizer steps: decode ANSI-C `$'...'` strings
  to their literal characters; resolve parameter-expansion defaults
  (`${name:-lit}` and siblings) to their literal payload; resolve
  `$(echo/printf ...)`/backtick-echo command substitutions whose argument
  is itself a literal; then, for whatever expansion syntax survives
  (nested/opaque substitutions, or a bare `$name`/`${name}` with no
  default), an adjacency check.

  Round 6's FIRST cut of that adjacency check was itself too broad — it
  denied ANY subagent Bash command where a surviving expansion sat glued
  to a word character on EITHER side, unconditionally, regardless of
  whether `git` was even present. That would have denied ordinary,
  entirely non-git subagent usage: `echo file${i}.txt`,
  `cp "${dir}/a${n}.log" .`, `tar xf pkg${ver}.tgz`. Round 7 (the
  coordinator's own correction) narrowed it to check WHAT the glued letters
  spell, via a SIMPLE (single-token) destructive-verb word list — but
  round 7's OWN re-review (an 8th round) found that narrowing reopened a
  hole: the compound/hyphenated verbs deliberately EXCLUDED from that word
  list (`worktree add/remove/prune`, `branch -d/-D/-m`, `tag -d`,
  `update-ref`, `symbolic-ref`, `filter-branch`, `reflog expire`,
  `cherry-pick`) ended up covered by NEITHER the adjacency rule NOR the
  primary scan (which can't match a verb whose spelling is broken by an
  unresolved expansion) — `git fil${x}ter-branch`, `git worktree
  re${x}move w1`, `git branch -${x}d b`, and five more all allowed, a
  REGRESSION from round 6 (which caught these on glue alone).

  Round 8 resolves this not by extending the word list (any finite list
  reopens the identical false-positive class for whatever it's missing —
  exactly how round 7 lost the compound verbs) but by splitting the rule on
  git-presence instead: branch A (no literal `git` anywhere in the
  normalized text) keeps round 7's subsequence-of-`git` check unchanged —
  this is what still allows `echo file${i}.txt`/`cp "${dir}/a${n}.log"
  .`/`tar xf pkg${ver}.tgz`/`x=$(pwd)/sub`. Branch B (`git` present
  anywhere) reinstates round 6's unconditional glue check, now SCOPED to
  git-containing commands only — glue alone denies, independent of what the
  glued text spells, which is exactly what closes the compound-verb gap: it
  needs no word list to cover `filter-branch` or `worktree remove`, so
  there is nothing finite to be incomplete. The accepted, EXPLICIT new
  false positive: `git log file${i}.txt` (read-only, harmless) now denies,
  because `git` is present and `${i}` is glued to `file` — branch B does
  not check verb-relevance. Legitimate whole-word usage
  (`git diff -- "$(pwd)/f"`, `git log $REV`) still allows either way.
  Round 8's KNOWN LIMITS claimed its residuals were "the ONLY ones that
  survive verification" — round 8's OWN re-review (a 9th round) proved that
  claim FALSE: Branch A was defeated by CHAINING two or more expansion
  constructs with NOTHING between them —
  `g${a}${b}i${c}${d}t checkout -- t3.py` allowed, and with all four
  variables unset, real bash genuinely executes this as `git checkout`.
  Root cause: the touching-letter-fragment computation ran per INDIVIDUAL
  construct match, so two directly-adjacent constructs each only ever saw
  ONE neighboring letter (the sibling construct blocked the run), never
  reaching the length->=2 threshold alone even though the combined
  reconstruction spans the target — a correctness bug in the machinery
  itself, not an inherent limit (Branch B, unaffected, already caught the
  identical chain whenever `git` was literally present).

  Round 9 closes this two ways, kept together as defense in depth: (1) the
  fragment computation now groups any RUN of directly-touching constructs
  (zero characters between them) into one unit before computing the
  touching fragment, using the run's overall span rather than each
  construct's own — NOT "lower the length threshold to 1", which would
  reopen the round-6-to-round-7 false-positive regression; (2) a new
  ZERO-EXPANSION PASS deletes every remaining opaque construct outright and
  re-runs the primary git+verb scan on that text too — a literal simulation
  of what real bash does with an unset variable (not a heuristic
  approximation), so it catches any chain length or interleaving pattern
  regardless of the fragment logic. `${dir}/a${n}` (`/`-separated, not a
  chain) and all prior must-ALLOW examples re-verified unaffected; perf
  re-verified at 1MB (previously only tested to 100KB), ~20ms.

  Documented residual, re-verified across rounds 6-9 (round 9 drops the
  "ONLY ones that survive verification" exhaustiveness claim outright,
  since round 8's identical claim proved false one round later): whole-word
  `$name` indirection where the runtime value is NOT empty-or-literal
  (`git $VERB -- x` where `$VERB` is genuinely set to something dangerous —
  the zero-expansion pass assumes emptiness, which is what closes the
  round-9 exploit class specifically, but cannot help when a variable is
  actually populated), and any write-verb delivered as runtime-assembled or
  encoded/obfuscated data (base64-decode-then-eval/pipe). Separately (an
  accepted FALSE POSITIVE class, not an under-blocking limit): no
  proximity/segment requirement on either branch (`git log --grep=commit`
  from rounds 4-5's same trade-off), and Branch A denying on an exact
  `gi`/`gt`/`it`/`git` match with no `git` anywhere in the command
  (`make i${n}t`).

  Rounds 1-5 (unchanged from the prior entry): rounds 1-3 kept patching a
  STRUCTURAL parser (segment splitting, heredoc-consumer classification,
  substitution extraction) that grew a new bypass every round it was
  re-reviewed — CRLF-after-continuation, a corrupted heredoc terminator that
  swallowed a verb placed after the heredoc, and piping literal text into a
  shell all defeated it in round 3 alone. Round 4 ruled that three rounds of
  "add a case" is the signal to change the design, not patch it again, and
  replaced the structural parser as the DECIDING mechanism with a
  structure-agnostic proximity scan: after normalizing line endings and
  collapsing backslash-continuations, deny for subagents whenever the
  literal substring `git` sits near a write-verb word, ANYWHERE in the raw
  command text — no segment splitting, no heredoc logic, no substitution
  extraction. Round 4's OWN first cut still had two gaps in the new
  mechanism itself, found by round-4's re-review: quote characters could
  fuse two half-words into a matchable verb the raw scan would otherwise
  miss (self-found before shipping, `git com"mit"`), and — found by the
  reviewer — mid-word BACKSLASH escaping did the identical thing
  (`git che\ckout`) and the original bounded ~160-character window was
  ITSELF a measurable, exceedable structure (`git -c user.name=<200 chars>
  checkout` padded past it). Round 5 closed both: escape characters are now
  stripped the same way quotes are, and the window is gone entirely — two
  unbounded linear searches (`git` anywhere, verb anywhere after it) replace
  the single bounded regex. This is immune BY CONSTRUCTION to any bypass
  that merely relocates the same literal text via a different shell
  construct, or pads the distance between the two pieces. The accepted cost
  is new false positives versus the pre-round-4 design: `git stash list`/
  `git stash show` (previously allowlisted read-only spellings), `git log
  --grep=commit` (verb-shaped text in an argument value), and any command
  whose worktree state is undeterminable (the old commit/add hygiene-verb
  fail-open carve-out is retired — the ONLY exemption left is a positively
  proven linked worktree) now deny. Genuine documented residual (verified,
  not assumed): a write-verb assembled at runtime or delivered as
  base64-encoded/obfuscated data, which never places the literal substrings
  `git`/verb in the raw command text at all, still allows — this is inherent
  to any text-substring guard and not expected to close. Until the next
  release tag ships, subagents on the currently-installed plugin keep
  getting the pre-round-4 structural-parser behavior, including all of
  round 1-3's now-fixed bypasses — the exact inertness class this ledger
  exists to surface.
- `conexus/hooks/scripts/routing/registry.yaml` — rationale text for
  `subagent_git_write_requires_orchestrator` updated to mention the
  nexus-3c92m `switch` addition (docs-only; no behavioral effect on its own).
- `conexus/skills/composition-probe/SKILL.md`, `conexus/skills/cli-controller/SKILL.md`, `conexus/skills/orchestration/SKILL.md`
  — nexus-c00dw, commit `e974d23b5`: the documented subagent Maven runner is
  now `scripts/mvnw-leased.sh test -Dtest=<class>` (single-builder lease for
  `service/target`) instead of a bare `mvn test`; rule text only, no hook or
  tool behaviour change. Until the next release tag ships, installed copies
  still show the bare invocation.
- `conexus/hooks/scripts/routing/_lib.py` and `sn/hooks/scripts/routing/_lib.py`
  — nexus-pfuns, commit `02ef7ee63`: the fallback routing-log path
  (`_DEFAULT_LOG_PATH`) is now resolved by a call-time `_default_log_path()`
  instead of a module-level constant frozen at import, so the routing-log
  destination a hook subprocess writes to honours `NX_ROUTING_LOG_PATH` and
  `HOME` as they are at the moment the hook actually runs, not as they were
  when the interpreter first imported `_lib`. Until the next release tag
  ships, subagents on the currently-installed plugin still resolve the
  fallback path at import time.
