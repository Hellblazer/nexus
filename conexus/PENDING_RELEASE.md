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

**Deferring a straddling entry (nexus-2x3qy, nexus-rbmja.10).** A plugin-only
release cherry-picks commits from `develop` onto a branch off the last client
tag (`release` skill, "Plugin-only fast lane"). The eligibility gate reads the
bead ids named in those commits and refuses the release when any other commit
on `origin/develop` since the tag that names the same bead touches a path the
lane does not ship (`src/`, `service/`, `scripts/`, `.github/`, `mcpb/`,
`conexus/plans/`, `conexus/daemon/`, release machinery under `tests/`, and so
on). Attribution is by commit message, matched on the whole bead id, so a
commit that does not name its bead is not seen by that check. To keep a
straddling entry out of the release, move it under
`## Deferred to the next client release` below and do not cherry-pick its
commits, so the whole bead ships together at the next normal release. A
deferred entry is exempt from the release-window "ledger must be empty" rule
above -- still declared there is correct, not stale -- and stays exactly where
it is until moved back deliberately.

---
## Awaiting the next release (pinned: v7.90.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `prose-edit/skills/prose-cut/SKILL.md` (nexus-ger02.10, RDR-221 Step 3.1): new skill, `conexus-prose-edit:prose-cut`, that a model can invoke (`/prose-edit` is the author's own command and is not model-invocable): a cut-only run (`--mode cut-only --voice-card F --word-budget N`) that proposes removals only and prints `WORK=` and the filtered cuts, a `count` run that prints a page's prose words, and a `decide` run that records the caller's accept, reject and hold sets through `review.py apply`; it is allowed `brief.py` and `review.py` only, asks no question and opens no viewer; the site-page review gate calls it for the voice pass.
- `prose-edit/skills/prose-edit/SKILL.md` (nexus-ger02.10): the invocation and mode tables point `--mode cut-only`, `decide` and `count` to the prose-cut skill, and a file named `decide` or `count` is reached as `-- decide`; a command is run with nothing joined to it (no `;`, `&&`, `||`, `|` or `echo`), which the permission rule refuses.
- `prose-edit/skills/prose-edit/scripts/brief.py` (nexus-ger02.10): `parse`, `build` and `filter` take `--mode cut-only --voice-card --word-budget` (filter drops an edit that adds a word, keeps paragraph cuts, moves queries aside and reports `words`), `parse decide` maps to `review.py apply`, new `count` verb (the prose words of a page, the unit of `words.document`); a cut that adds any symbol (markup, or `&` or `/` for a word) is dropped as not-a-cut, and a kept cut that removes a negation (contractions included) or a limit, or leaves markdown emphasis unpaired, is warned; a cut-only HTML run also protects nav, headings, button and samp (a normal HTML run does not); `filter` in a cut-only work directory refuses to run without the cut-only flags.
- `prose-edit/skills/prose-edit/scripts/review.py` (nexus-ger02.10): `render` takes a session as cut-only when the proposal or the work directory says so, records `cut_only` in session.json and opens no viewer for it; `apply` refuses an accepted edit that is not a cut in a cut-only session, and the session log carries `cut_only`, `word_budget` and `words` with the applied cuts' count beside the proposed one.
- `prose-edit/agents/line-editor.md` (nexus-ger02.10): the protected list adds the nav, heading, button and samp elements of an HTML file when the brief says cut-only.
- `conexus/hooks/scripts/version_lockstep_action.py` (nexus-75w5d, P0): the plain `nx upgrade` the lockstep action starts on a version change is marked unattended, so the single-instance lock and the managed-service walk refusal apply to it too.

## Deferred to the next client release
