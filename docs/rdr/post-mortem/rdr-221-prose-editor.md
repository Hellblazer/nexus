# RDR-221 post-mortem: the prose editor

## What the RDR set out to do

Prose in this repo had no editor that learned. Four gaps: most kinds of prose
(reference docs, CHANGELOG entries, commit messages) got no line editing at
all (Gap 1); Sam's corrections were repeated by hand because nothing
remembered them (Gap 2); reviews added qualifiers and nothing cut them (Gap 3);
voice had no anchor outside the `site-page` skill (Gap 4). The RDR proposed a
line editor that works from Sam's own exemplar passages per genre, proposes
numbered edits for the author to accept or reject, and keeps the rejections
and a style sheet in T2 so the next run does better.

## Implementation status

Implemented, closed 2026-11-22 with an override after three close critiques
(T2 `nexus_rdr/221-close-override-2026-11-22`). The editor shipped as the
opt-in plugin `conexus-prose-edit`: the `prose-edit` skill, the
model-invocable `prose-cut` skill used by the `site-page` review gate, and the
`line-editor` agent. Phase 2's four real edits passed on Sam's judgement. The
epic nexus-ger02 was closed by Sam on 2026-11-10 ("close as delivered"); the
RDR file stayed `accepted` for twelve days because nobody ran the close.

Gap disposition at close: Gap 2 mechanism solved, coverage partial; Gap 4
solved for four genres; Gap 3 accepted as reduced (only filler words are cut,
every other qualifier becomes a query, Sam's 2026-09-30 ruling); Gap 1 partly
solved (no how-to or commit-message genre, dropped by Sam on 2026-11-22).

## What diverged

1. **Amendment A1, unpack compression (Phase 1a).** The editor also proposes
   edits that lengthen over-compressed prose. Its only real-use test, the
   post-merge tally, was dropped by Sam on 2026-11-22, so the feature is
   closed unverified: its acceptance rests on three seeded-fixture runs.
   *Drift: unvalidated assumption.*
2. **Plugin, not project skill.** The RDR designed a project skill under
   `.claude/` and deferred a plugin; the plugin shipped (first commit
   cda821a61a, 2026-10-27) without the RDR recording the decision.
   Cut-only mode became its own skill, `prose-cut`, because `/prose-edit`
   cannot be invoked by the model. *Drift: under-specified architecture.*
3. **Review moved from Typora to MarkupEditor suggestions.** The marked-up
   copy and viewer the RDR describes were replaced; the follow-on epic
   nexus-scmcs carries that work. *Drift: framework API detail.*
4. **Gap 3 never had a mechanism.** No design section mapped to it, and Sam's
   qualifier ruling narrowed it to filler words. *Drift: scope
   underestimation.*
5. **Two genres dropped.** How-to and commit-message were named in the brief
   and never seeded; their bead was closed as done. *Drift: scope
   underestimation.*

## What to take from it

The editor works, and none of the three critiques found the scope cut
silently: the Problem Statement is unchanged since acceptance and every
reduction is now on the record. What went wrong is the pattern behind it:
delivery outran the record. The plugin move, the review-loop change, the
dropped genres and the dropped tally each happened in a bead or a chat and
never reached the RDR, and a closed epic let the RDR sit accepted with no
owner. Close the RDR when its epic closes, and amend it when a decision
changes what it describes, not at the end.

## What trails the close

Nothing owed by RDR-221. The beta work (the MarkupEditor review loop, its
stand-in harness nexus-scmcs.3) lives under nexus-scmcs, which has its own
record.
