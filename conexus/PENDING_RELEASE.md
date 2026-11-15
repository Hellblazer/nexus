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
## Awaiting the next release (pinned: v8.2.3)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/commands/rdr-fix.md`
  the fix range and fix-check title read the gate record's `blob:` key (nexus-3jjkg)
- `conexus/skills/rdr-accept-checklist/SKILL.md`
  accept checks `fix_check:` against the record's `blob:`, `commit:` only for older records (nexus-3jjkg)
- `conexus/skills/rdr-fix-checklist/SKILL.md`
  fix-check records are titled by the RDR file's blob after the disposition (nexus-3jjkg)
- `conexus/skills/rdr-gate-checklist/SKILL.md`
  the gate record carries `blob:`; `fix_check:` must equal it, or `commit:` for older records (nexus-3jjkg)
- `prose-edit/skills/prose-edit/SKILL.md` (nexus-scmcs, nexus-scmcs.2.12): the setup run is a guided wizard, and the run-time genre question, the accept and reject step and the step 13 offers are `AskUserQuestion` choice prompts instead of free-text questions; a path run reviews its edits as suggestions in the MarkupEditor app (open and confirm the document before the editor runs, translate, propose, wait for the author's decisions, apply, then undo and reopen), the Typora viewer is gone, and the setup run checks for the MarkupEditor propose_edits tool. Cut-only (nexus-ger02.10): the invocation and mode tables point `--mode cut-only`, `decide` and `count` to the prose-cut skill. Known gap, shipped by Sam's decision (2026-11-14, nexus-h72g3): the way out of a MarkupEditor document that has unsaved edits (the unsaved-changes prompt, Reload from Disk, the changed-on-disk dialog) is not yet checked against the app, so save or close such a document in MarkupEditor before a path run.
- `prose-edit/skills/prose-edit/markup-route.md` (nexus-h72g3): new file, the path-run text of the prose-edit skill (open the document in MarkupEditor, translate and propose, read the author's decisions, apply, return the app to the file, the voice-card offer and the setup check for the app), moved out of SKILL.md to keep it within its context budget; the skill reads it only on a path run, so a stdin run does not load it.
- `prose-edit/skills/prose-edit/scripts/brief.py` (nexus-scmcs): `setup` reports at most eight `genre_candidates` with `ready`, `try_path` and `unmapped_other` in place of `no_genre`; new `suggest-genre`; the genre description table the skill and the README share. Cut-only (nexus-ger02.10): `parse`, `build` and `filter` take `--mode cut-only --voice-card --word-budget` (filter drops an edit that adds a word, keeps paragraph cuts, moves queries aside and reports `words`), `parse decide` maps to `review.py apply`, new `count` verb (the prose words of a page, the unit of `words.document`); a cut that adds any symbol (markup, or `&` or `/` for a word) is dropped as not-a-cut, and a kept cut that removes a negation (contractions included) or a limit, or leaves markdown emphasis unpaired, is warned; a cut-only HTML run also protects nav, headings, button and samp (a normal HTML run does not); `filter` in a cut-only work directory refuses to run without the cut-only flags.
- `prose-edit/skills/prose-edit/scripts/memory.py` (nexus-scmcs, nexus-scmcs.2.11): `add-entry` refuses a `genre_map` entry whose genre is not one of the six and stores nothing from that call, so a mistyped genre cannot break later runs in a folder; the `viewer` verb and its record shape are removed (a `prose/viewer` record already in T2 is left alone and nothing reads it).
- `prose-edit/skills/prose-edit/scripts/review.py` (nexus-scmcs.2.5, nexus-scmcs.2.11): new `translate`, `proposed` and `decisions` verbs turn the editor's edits into MarkupEditor suggestions and the author's in-app decisions into `apply` flags; the marked-up copy, the viewer and `PROSE_EDIT_OPEN` are removed, `render` only sets the session up, and `apply` probes T2 through `status`; `decisions` treats a missing `propose.json` as nothing sent when the editor has no placed edit, so a query answered with a change in a run with no editor edits still decides and applies. Cut-only (nexus-ger02.10): `apply` refuses an accepted edit that is not a cut in a cut-only session (the session is cut-only when the proposal or the work directory says so) and the session log carries `cut_only`, `word_budget` and `words` with the applied cuts' count beside the proposed one.
- `prose-edit/skills/prose-edit/scripts/suggest.py` (nexus-scmcs.2.2): new module that translates Markdown edits into MarkupEditor `propose_edits` entries and maps the app's suggestion statuses to accept, reject and hold.

## Deferred to the next client release
