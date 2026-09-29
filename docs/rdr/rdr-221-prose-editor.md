---
title: "Prose Editor: an Example-Anchored Line Editor with Memory in T2"
id: RDR-221
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-27
accepted_date:
related_issues: []
---

# RDR-221: Prose Editor: an Example-Anchored Line Editor with Memory in T2

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

## Problem Statement

This project writes prose of several kinds: RDRs, reference docs, how-to pages
under `web/`, long-form essays under `docs/exploration/`, CHANGELOG entries,
commit messages. Line editing (the pass that works sentence by sentence and
paragraph by paragraph: cutting, reordering, making the claim lead) happens in
two places only. One is the `site-page` skill's voice pass, which works well.
The other is Sam, correcting drafts by hand, one document at a time. Nothing
carries a correction made on one document to the next.

### Enumerated gaps to close

#### Gap 1: No standing line editor

Outside `web/` and the essays, no step edits a sentence. The RDR gate's
readability check warns and cannot fail; reference docs, CHANGELOG entries and
commit messages get no pass at all (Discovery 4). What ships unedited
includes a 121-word sentence in `docs/storage-tiers.md` and CHANGELOG entries
of 150 to 250 words that cannot be skimmed. A code change pasted a docstring
into a published page as three paragraphs, each sentence true, when the
reader needed one table row and one sentence (Discovery 4).

#### Gap 2: Corrections do not carry forward

Sam's memory notes record more than twenty distinct prose corrections since
July: lead with the point, no em dashes in new prose, no trope headings, one
term per concept, and others. Two became tools; one of those two was reverted.
One correction recurred within days on another page in the same series
(Discovery 3). Each document starts from zero.

#### Gap 3: Review adds and nothing cuts

Reviewers see errors, not surplus, so each review round adds qualifiers and
none removes them. One page's critique record shows seven fact-check rounds
that added qualifiers and never cut one (Discovery 2). The only counterweight
is `site-page`'s voice pass, and only for `web/` pages and essays.

#### Gap 4: Voice has no anchor outside one skill

The essays carry a deliberate voice. `site-page` protects it with an exemplar
paragraph from Sam and a voice card (a few lines naming point of view,
audience and register), and that exemplar moved the voice further than any
review round did (Discovery 1). Nothing keeps exemplars or voice cards
between documents or makes them available to other kinds of prose.

## Relationship to Prior RDRs

| Prior work | Relationship | What it means for this one |
| --- | --- | --- |
| nexus-ptwm2 (a prose lint, ratchet, gate and policy draft, reverted) | Precedent | Mechanical enforcement and sweeps of existing prose degraded it. This design has no lint, gate, ratchet or sweep. |
| The `writing-clearly` guidance skill (withdrawn) | Precedent | A rules file a model executes without a human deciding each case could not be written safely. Here Sam accepts or rejects every edit, and the guidance is written as diagnostics with their exceptions stated. |

## Context

### Background

Sam asked for an editing skill on 2026-09-27. Research and a five-critic
review followed; this RDR is the scope that survived them. Research records
are in T3 collection `writing-craft` and T2 project `nexus_rdr`
(`221-research-*`, `221-deepdive-*`).

### Technical Environment

- `.claude/skills/site-page/SKILL.md`: exemplar (§2), voice card (§2), voice
  pass (§5 item 3), "lead with the point" (§3).
- `conexus:substantive-critic`: the structural reviewer; unchanged here.
- T2 memory (`nx memory`, `mcp__plugin_conexus_nexus__memory_*`): the store
  for everything the editor remembers.
- Sam reads drafts in Typora.

## Research Findings

### Investigation

Three rounds. Round 1 surveyed existing editing tools and agent skills,
technical-editing practice, and editorial craft with the literature on LLMs as
editors (T3 `writing-craft`). Round 2 re-fetched every external source cited,
and censused which kinds of prose in this repo get any editing today. Round 3
was a five-critic review of an earlier, larger design, with a verification
pass that sampled real `substantive-critic` output on prose work. Two spikes
checked what the design relies on: the T2 record shapes, and the marked-up
copy in Typora. Each finding is a T2 record, `nexus_rdr/221-research-N`.

### Key Discoveries

1. **Verified (source search).** `site-page`'s voice pass is a
   `substantive-critic` dispatch with a voice card, a word budget and a
   cut-only brief. On the ci-board page it produced 19 line-level cuts and one
   paragraph move. The skill records that Sam's exemplar paragraph moved the
   voice further than any review round.
2. **Verified (source search).** Seven critic runs on page and essay work held
   26 serious findings, 23 structural and 3 sentence-level: structure is not
   being crowded out. The critique of the coordination page records seven
   fact-check rounds adding qualifiers and none cutting.
3. **Verified (source search).** Sam's memory notes and instructions record
   more than twenty distinct prose corrections between 2026-07-11 and
   2026-09-27. Two became tools; the em-dash lint was reverted the same day.
   The "don't over-atomise" correction recurred within days on another page.
4. **Verified (source search).** Editing coverage today: `web/` and
   `docs/exploration/` get `site-page`'s three passes; RDRs get a readability
   check that only warns (`rdr-gate-checklist`); skill files get a structural
   lint (`writing-nx-skills`); reference docs, CHANGELOG and commit messages
   get nothing. Unedited samples include a 121-word sentence
   (`docs/storage-tiers.md` line 7) and 150-250-word CHANGELOG bullets; other
   unedited docs (`workflows.md`, `querying-guide.md`) are clean. The
   docstring-to-page incident is recorded in `site-page` §5.
5. **Documented.** The authorities behind common style rules state them as
   diagnostics that need judgement: "There can be no fixed algorithm for good
   writing" (Gopen & Swan); "plenty of nominalizations are fine" (Williams &
   Bizup); Pinker defends the passive where it directs attention correctly.
   Applied mechanically in this repo, a 721-edit sweep read 19% worse and
   altered a quoted citation (nexus-ptwm2).
6. **Documented.** Experts editing AI-drafted text add hedges more often than
   they remove them (62,811 paired clinical notes, arXiv:2606.00018), and
   native academic writers hedge more than non-native ones. AI prose is
   under-hedged; the failure worth cutting is a qualifier nothing justifies.
7. **Documented.** Handing a whole document to an LLM to edit silently
   corrupts content: an average of 25% over long delegated workflows
   (arXiv:2604.15597). An LLM also rates its own output above equal-quality
   output (arXiv:2404.13076). Both argue for targeted proposals a human
   accepts.
8. **Documented.** Repeated LLM writing assistance narrows expression across
   populations of writers (*Nature Human Behaviour* 2026; arXiv:2508.01491).
   Drift of one author's voice under repeated editing is an inference from
   this, not a measurement.
9. **Verified (source search).** Sentence rules misfire on this corpus's
   voice: in `xanadu-in-nexus.md` and `linda-in-nexus.md`, actor-as-subject
   would rewrite the section-closing refrain ("This is the role Xanadu fills
   in Nexus."), a single stress position would break closing tricolons, and
   a curse-of-knowledge check would gloss deliberately unexplained SQL.
10. **Documented.** Editors deliver more than marked sentences: an editorial
    letter on global issues, queries to the author, and a style sheet of
    recurring decisions.
11. **Verified (spike).** T2 memory holds the record shapes below: titles
    with slashes store and read back, `nx memory get` returns a JSON body
    unchanged, a 90-day TTL is stored, and `nx memory put -` reads stdin. A
    partial title given to `get` returns the entry when only one matches, so
    the session log is enumerated with `nx memory list` and filtered.
    `nx memory delete` asks for confirmation unless given `-y`.

### Critical Assumptions

- [ ] **Worth accepting.** At least half of the editor's proposals on real
  work are ones Sam accepts. — **Status**: Unverified — **Method**: Phase 2
- [ ] **Voice kept.** Sam judges edited essays still in his voice. —
  **Status**: Unverified — **Method**: Phase 2
- [ ] **Memory helps.** A rejected edit is not proposed again, and a
  style-sheet entry is applied on the next document. — **Status**: Unverified
  — **Method**: Phase 1 test, Phase 2 observation
- [x] **Readable in Typora.** Typora renders inline `<del>` and `<ins>` in a
  Markdown file, and Sam finds the marked-up copy workable. — **Status**:
  Verified — **Method**: Spike (2026-09-28)

## Proposed Solution

### Approach

One editor, invoked by the author on one document at a time. Its brief is
built from examples and the author's own recorded decisions, not from a rule
list. It proposes; the author decides; what the author decides is kept in T2
and used next time. It lives as a project skill and agent in `.claude/`,
beside `site-page`.

### Technical Design

**The editor (`.claude/agents/line-editor.md`, `.claude/skills/prose-edit/`).**
Its brief for each run:

- the genre's exemplar passages;
- a voice card for the document, built from the document and the exemplars
  before any edit;
- the style sheet (user level, then repo level, then this document);
- the "not a defect" entries from past rejections;
- a change budget;
- an instruction to prefer cutting.

Guidance is written as diagnostic questions with the exception stated, for
example: "Does this nominalisation hide who acts? Leave it if it names a
stable concept." Among the questions: does the paragraph open with its claim;
is the actor the subject; does the sentence end on what matters; would a
reader new to the project follow this step; is this qualifier justified by a
real uncertainty. Unjustified qualifiers are proposed for cutting; justified
ones stay; unclear ones become queries. A construction that matches a device
on the voice card (a refrain, a tricolon, deliberate unexplained density) is
not a finding.

Output, in this order:

- an editor's note, at most a paragraph, on global issues;
- paragraph proposals: cut, move, merge or split, naming the paragraphs;
- sentence edits, each an exact old string, a new string and a one-line
  reason, numbered;
- queries to the author, numbered.

The editor never rewrites a whole section and changes nothing until the
author accepts.

**Review loop.** The skill writes a marked-up copy of the document outside
the repo (so it cannot be committed by accident): each proposed sentence edit
inline as `<del>old</del><ins>new</ins>` with its number, paragraph proposals
and queries as numbered notes where they apply, the editor's note at the top.
It opens the copy in the author's viewer (a user preference; Typora for Sam).
The author answers with the numbers to accept and any corrections. The skill
applies accepted edits to the real file with exact-match replacement, then
records the session.

**Memory in T2.** All of it is T2 memory entries:

| Record | T2 project | Title |
| --- | --- | --- |
| User style sheet, viewer preference | `prose` | `stylesheet`, `viewer` |
| Repo style sheet | `<repo>_prose` | `stylesheet` |
| Genre: exemplars and notes | `<repo>_prose` | `genre/<name>` |
| Document voice card and style notes | `<repo>_prose` | `doc/<path>` |
| Session log (proposals, accepted, rejected) | `<repo>_prose` | `log/<path>/<utc timestamp>`, 90-day TTL |

Rejected edits are summarised into the document's and, when the author says
so, the user's "not a defect" entries. When the author makes a correction in
conversation ("lead with the point here"), the skill offers to add it to the
style sheet at the level the author chooses.

**Genres.** Six genres: `rdr`, `reference-doc`, `how-to`,
`exploration-essay`, `changelog`, `commit-message`. A genre is its exemplars
(one or two passages Sam picks) plus short notes (for `rdr`, a pointer to
`docs/rdr/REGISTER.md`). The skill infers the genre from the path when the
repo style sheet maps it, and otherwise asks.

**Invocation.** `/prose-edit <path> [--genre <name>] [--budget <n>]`. Adding
an exemplar: `/prose-edit exemplar <genre> <path>:<lines>`.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Voice card and exemplar | `site-page` §2 | Reuse the technique; store in T2. |
| Voice pass | `site-page` §5 item 3 | Switch to the line editor after Phase 2 passes. |
| Structural review | `conexus:substantive-critic` | Unchanged. |
| RDR register | `docs/rdr/REGISTER.md` | Cited by the `rdr` genre notes. |
| Persistence | T2 memory | New records, no new store. |

### Decision Rationale

The one editing practice with evidence of working here is `site-page`'s voice
pass: an exemplar, a voice card, a budget and a brief to cut (Discovery 1).
This design keeps that and adds the part missing today, memory, so Sam's
corrections and rejections reach the next document (Gap 2). It avoids the
forms that failed: no lint and no sweep (Discovery 5), and no rule the model
applies without the author deciding each case (Discoveries 5, 7). It starts
as a project skill because that can be changed the same day Sam reacts to it;
a plugin can come later if it earns one.

## Alternatives Considered

### Alternative 1: Brief the critic as an editor, per skill, as today

**Description**: each skill that wants a line edit writes its own brief for
`substantive-critic`, as `site-page` §5 item 3 does now.

**Pros**:

- Already works: 19 cuts and a paragraph move on one page (Discovery 1).
- Nothing new to build.

**Cons**:

- Nothing is remembered between documents, so corrections do not carry
  forward (Gap 2).
- Covers only the prose a skill happens to brief for (Gap 1).

**Reason for rejection**: it leaves Gaps 1 and 2 open.

### Alternative 2: Mechanical checks (a prose linter)

**Description**: encode the style rules as lint checks run over prose.

**Pros**:

- Deterministic and cheap.

**Cons**:

- A 721-edit mechanical sweep here read 19% worse (Discovery 5).

**Reason for rejection**: most prose guidance needs knowing when not to apply
it, which a linter cannot know (Discovery 5).

### Alternative 3: Ship as a plugin now

**Description**: package the editor as an installable plugin from the start.

**Pros**:

- Usable in other repos.

**Cons**:

- Release work before there is evidence the editor is worth using.

**Reason for rejection**: deferred until Phase 2 shows the editor is worth
using; a project skill iterates faster and carries no release work.

## Trade-offs

### Consequences

- Positive: corrections persist; every kind of prose can get a line edit.
- Positive: nothing changes without the author accepting it.
- Negative: editing costs the author's attention on every proposal.
- Negative: nexus-only until moved to a plugin.

### Risks and Mitigations

- **Risk**: the editor flattens the essays' voice. **Mitigation**: exemplars
  and voice card, change budget, flagging instead of changing; Sam judges in
  Phase 2.
- **Risk**: proposals are mostly noise. **Mitigation**: Phase 2 measures the
  accept rate; below half, stop or revise.
- **Risk**: the style sheet grows into a rule list. **Mitigation**: entries
  are written as diagnostics with exceptions; the author reviews the sheet
  when adding to it.

### Failure Modes

- Wrong genre: visible in the marked-up copy's header; the author overrides.
- Stale exact-match string (file changed since the proposal): the edit is
  skipped and reported, never forced.
- T2 unavailable: the skill stops and says so.

## Implementation Plan

### Phase 1: Build

#### Step 1.1: Marked-up copy format

The format verified with Sam: the editor's note as a blockquote at the top;
sentence edits inline as `<del>old</del><ins>new</ins>` followed by a
superscript number; reasons as numbered footnotes; paragraph proposals
(`[P1]`) and queries (`[Q1]`) as numbered blockquotes where they apply.

#### Step 1.2: T2 records

The record shapes above. A small stdlib script,
`.claude/skills/prose-edit/scripts/memory.py`, reads the layers in order
(user, repo, document) and writes sessions, through the `nx memory` CLI, so
the merge order is the same every time. It lists log entries with
`nx memory list` rather than a partial-title `get`, and deletes with `-y`
(Discovery 11).

#### Step 1.3: Genres and exemplars

Sam picks one or two exemplar passages for each of the four Phase 2 genres
(`rdr`, `reference-doc`, `exploration-essay`, `changelog`) and seeds the repo
style sheet from his existing corrections.

#### Step 1.4: The editor

Agent and skill: brief assembly, the diagnostic questions, the output format.

#### Step 1.5: The review loop

Marked-up copy, viewer, accept by number, exact-match apply, session record,
the offer to add a correction to the style sheet.

**Exit**: the Test Plan scenarios below pass.

### Phase 2: Validate on real work

#### Step 2.1: Four real edits

Use the editor on four pieces of Sam's current work: an RDR, a reference doc
(for example `docs/storage-tiers.md`), an essay and a CHANGELOG entry. Record
for each: proposals made, accepted, rejected, queries answered, and Sam's
answer to "would you reach for this again?".

#### Step 2.2: Decide

Pass: at least half accepted
across the four, the essay judged still in Sam's voice, and a yes. On a fail,
stop and revise this RDR.

### Phase 3: Adopt

#### Step 3.1: `site-page` voice pass

`site-page` §5 item 3 dispatches the line editor with the page's voice card.

#### Step 3.2: Remaining genres

Exemplars for `how-to` and `commit-message`.

#### Step 3.3: Plugin decision

Whether to move the editor into a plugin, decided on Phase 2's evidence.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Style sheets, genres, document notes (T2) | `nx memory list` | `nx memory get` | `nx memory delete` | Phase 1 tests | T2 backup |
| Session log (T2) | same | same | 90-day TTL | Phase 1 tests | T2 backup |
| Marked-up copies | N/A | N/A | Temporary directory, cleared after apply | Phase 1 tests | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: A document with a quote, a code block, a table and frontmatter. — **Verify**: no proposal inside any of them.
- **Scenario**: `xanadu-in-nexus.md` and `linda-in-nexus.md`. — **Verify**: no proposal touches the refrains, tricolons or unexplained SQL.
- **Scenario**: Budget set to N. — **Verify**: at most N sentence edits.
- **Scenario**: A sentence with one justified and one unjustified qualifier. — **Verify**: only the unjustified one is proposed for cutting.
- **Scenario**: Reject an edit, run again on the same document. — **Verify**: not proposed again.
- **Scenario**: Add a style-sheet entry at user level, edit a document in another genre. — **Verify**: the entry is applied.
- **Scenario**: Accept edits after the file changed under one of them. — **Verify**: that edit is skipped and reported; the others apply.
- **Scenario**: Document path with no genre mapping. — **Verify**: the skill asks.
- **Scenario**: Marked-up copy location. — **Verify**: outside the repo; `git status` unchanged.

## Validation

### Testing Strategy

The memory script gets automated tests against the real engine substrate.
The editor's behaviour is checked by the scenarios above as recorded
transcripts, then by Phase 2 on real work.

### Performance Expectations

N/A.

## Finalization Gate

### Contradiction Check

The research warns about LLM editing (Discoveries 7, 8) and this builds an
LLM editor. The answer is the review loop: targeted proposals, each accepted
by the author, never a delegated rewrite. Phase 2 tests whether that is
enough.

### Assumption Verification

Readable in Typora is verified. Memory helps is verified in Phase 1;
Worth accepting and Voice kept in Phase 2.

### Scope Verification

Phase 2 is in scope and decides whether Phase 3 happens.

### Cross-Cutting Concerns

- **Versioning**: N/A (project skill).
- **Licensing**: repo licence.
- **Deployment model**: project skill in this repo.
- **Incremental adoption**: one document at a time, only when invoked.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: session-log TTL.
- All others: N/A.

### Proportionality

Sized to one editor, its memory and a review loop. Anything larger waits for
Phase 2.

## References

- T3 `writing-craft`: `research-rdr-221-gap1-prose-surface-census-2026-09-27`,
  `research-editorial-craft-and-llm-editing-2026-09-27`,
  `research-technical-editing-practice-2026-09-27`.
- T3 `research-style-rule-prescriptivism-2026-08-23`,
  `research-ai-slop-prose-removal-2026-08-21`.
- T2 `nexus_rdr/221-research-*`, `221-deepdive-*`.
- Gopen & Swan: https://www.usenix.org/sites/default/files/gopen_and_swan_science_of_scientific_writing.pdf
- Hedging in AI documentation: https://arxiv.org/abs/2606.00018
- Delegated editing: https://arxiv.org/abs/2604.15597
- Self-preference: https://arxiv.org/abs/2404.13076
- Homogenization: https://www.nature.com/articles/s41562-026-02550-0 ; https://arxiv.org/abs/2508.01491

## Revision History
