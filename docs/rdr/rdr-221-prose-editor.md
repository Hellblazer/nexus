---
title: "Prose Editor: an Example-Anchored Line Editor with Memory in T2"
id: RDR-221
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-27
accepted_date: 2026-09-29
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
The other is Sam, correcting drafts by hand, one document at a time. His
corrections reach later writing as guidance, but nothing applies them as an
edit to a draft.

### Enumerated gaps to close

#### Gap 1: No standing line editor

Outside `web/` and the essays, no step edits a sentence. The RDR gate's
readability check warns and cannot fail; reference docs, CHANGELOG entries and
commit messages get no pass at all (Discovery 4). What ships unedited
includes an 80-word sentence in `docs/storage-tiers.md`, and CHANGELOG
entries of 150 words or more: 13 of the 65 in releases 7.63.0 to 7.66.0, the
longest 560. A code change pasted a docstring
into a published page as three paragraphs, each sentence true, when the
reader needed one table row and one sentence (Discovery 4).

#### Gap 2: Corrections are guidance, never an edit pass

Sam's memory notes record more than twenty distinct prose corrections since
July: lead with the point, no em dashes in new prose, no trope headings, one
term per concept, and others. They reach later sessions as writing guidance,
and `site-page` §3 carries several for `web/` pages and essays (Discovery 12).
Two became tools; one of those two was reverted. Nothing applies them as an
edit to a draft, and one correction recurred within days on another page in
the same series (Discovery 3).

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
| The `writing-clearly` guidance skill (withdrawn) | Precedent | A rules file a model executes without a human deciding each case could not be written safely. Here Sam accepts or rejects every edit (in cut-only mode the `site-page` gate reviews the cuts instead, Step 3.1), and the guidance is written as diagnostics with their exceptions stated. |

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
   get nothing. Unedited samples include an 80-word sentence in
   `docs/storage-tiers.md` (line 173), and CHANGELOG bullets across releases 7.63.0 to
   7.66.0 with a median of 90 words, 13 of 65 at 150 or more and the longest
   560 (measured 2026-09-29); other
   unedited docs (`workflows.md`, `querying-guide.md`) are clean. The
   docstring-to-page incident is recorded in `site-page` §5.
5. **Documented.** The authorities behind common style rules state them as
   diagnostics that need judgement: "There can be no fixed algorithm for good
   writing" (Gopen & Swan); "plenty of nominalizations are fine" (Williams &
   Bizup); Pinker defends the passive where it directs attention correctly.
   Applied mechanically in this repo, a 721-edit sweep made about 19% of its edits
   worse and altered a contract string and a title in accepted RDRs
   (nexus-ptwm2).
6. **Documented.** Experts editing AI-drafted text add hedges more often than
   they remove them (62,811 paired note sections, arXiv:2606.00018).
   Separately, corpus studies find native academic writers hedge more than
   non-native ones (T3 `research-style-rule-prescriptivism-2026-08-23`). In
   clinical notes, then, AI drafts were under-hedged; the failure worth
   cutting is a qualifier nothing justifies.
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
12. **Verified (source search).** Sam's corrections already reach later
    sessions as writing guidance: his global instructions and the auto-memory
    index load into every session and the feedback files are read on demand,
    and `site-page` §3 (lines 34-47) lists register rules for both of its
    genres. The voice pass in §5 item 3 is
    cut-only, runs in parallel with two other passes, and its findings are
    applied in one pass (lines 62-70).
13. **Verified (source search).** Every worktree of this repo shares one git
    common directory: `git rev-parse --path-format=absolute --git-common-dir`
    prints the primary's `.git` from the primary and from every worktree (the
    form without the flag prints a relative `.git` in the primary), while each
    worktree's own directory name differs per session.

### Critical Assumptions

- [ ] **Worth accepting.** At least half of the editor's sentence edits on real
  work are ones Sam accepts. — **Status**: Unverified — **Method**: Phase 2
- [ ] **Voice kept.** Sam judges edited essays still in his voice. —
  **Status**: Unverified — **Method**: Phase 2
- [ ] **Memory helps.** A rejected edit is not proposed again, and a
  style-sheet entry is applied on the next document. — **Status**: Measured,
  narrowly (nexus-ger02.16): the same change at the same place is not shown
  again after one rerun in which the author rejected every shown edit: 0 of 57
  rejected edits in 13 measurable runs on two documents (a no-memory editor
  repeats 92.6% on the small one and 38.3% on the storage one), and 0 of 20 in
  10 runs on the small one with two edits rejected and the rest held. Not
  measured: a class of change (another em dash is proposed again, by design), a
  different change at the same spot, several reruns, a document changed
  between runs (scripted test only), and the style-sheet half. In the first
  batch the small document had 4 measurable runs and the storage one 9, short
  of the 10 the decision asks for; the second batch added 10 on the small one.
  — **Method**:
  Phase 1 test, Phase 2 observation
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
- the "not a defect" entries from past rejections, and this document's own
  stored rejections;
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
  these are advice, applied by the author, never by the skill;
- sentence edits, each an exact old string, a new string and a one-line
  reason, numbered;
- queries to the author, numbered.

The editor never rewrites a whole section and changes nothing until the
author accepts; the one exception is cut-only mode (Step 3.1), whose cuts the
`site-page` gate reviews.

**Review loop.** The skill writes a marked-up copy of the document outside
the repo (so it cannot be committed by accident): each proposed sentence edit
inline as `<del>old</del><ins>new</ins>` with its number, paragraph proposals
and queries as numbered notes where they apply, the editor's note at the top.
It opens the copy in the author's viewer (a user preference; Typora for Sam).
The author answers with the numbers to accept and any corrections. The skill
applies accepted sentence edits to the real file with exact-match
replacement, in file order. An edit is applied only when its old string occurs
exactly once in the file (or in the range, for a range run) and overlaps no
other accepted edit; when two accepted edits overlap, both are skipped and
reported. Then it records the session.

**Memory in T2.** All of it is T2 memory entries, because Sam chose the nx
stores as the only home for the editor's state (T2
`nexus_rdr/221-decision-5-nx-integration`). `<repo>` is the basename of the
parent of `git rev-parse --path-format=absolute --git-common-dir`, so the
primary and every worktree of one repo
shares one style sheet (Discovery 13); `<path>` is always relative to the
repo root. An exemplar is stored as its passage text plus the path and line
range it came from, so a later edit to the source file does not change it.

| Record | T2 project | Title |
| --- | --- | --- |
| User style sheet, viewer preference | `prose` | `stylesheet`, `viewer` |
| Repo style sheet | `<repo>_prose` | `stylesheet` |
| Genre: exemplars and notes | `<repo>_prose` | `genre/<name>` |
| General "not a defect" entries | `prose` or `<repo>_prose` | `not-a-defect` |
| Document voice card (author-approved), style notes, stored rejections | `<repo>_prose` | `doc/<path>` |
| Session log (proposals, accepted, rejected) | `<repo>_prose` | `log/<path>/<utc timestamp>`, 90-day TTL |

The document record is `{"scalars", "lists", "rejections", "voice_card"}`. `voice_card` is optional and has the
shape `{"text", "at"}` (a non-empty string of at most 4000 characters, and the UTC time the author approved it).
It is absent until the author says yes, after a run, to saving the card the editor returned; the editor's card is
never stored by itself, and a stdin run stores none. Later runs put the saved card in the brief, labelled
author-approved, as the anchor the editor starts from and returns unchanged, in place of a card rebuilt from text
the editor has already edited (the drift Discovery 8 names). Without a saved card a run behaves as before.
Every bullet of the brief's style-sheet section ends with its layer (document, genre, repo or user), and the
brief states the rule: an entry from a narrower layer that contradicts a broader one wins (document > genre >
repo > user); entries that do not contradict each other all apply. The site-page section 3 rules are the genre
layer.

A sentence edit is rejected only when the author says so: `reject N` or
`reject the rest`, with an optional one-line reason (to tell a wrong edit from
a right one the author does not want). An edit the author does not name is held,
neither applied nor stored. A rejection is stored verbatim (its old and new
strings) in the document's record. It matches a later proposal by its minimal
change: the old and new strings with the words they share at the start and at
the end trimmed at word boundaries. `memory.py` drops a later proposal with the
same minimal change for that document before the author sees it (the dropped
edit stays visible in the marked-up copy), and the document's rejections are
also put in the editor's brief so it does not propose them. Overlap alone is not
a match. Two limits are deliberate. The key has no position, so a stored cut of a
word also drops that cut anywhere else in the document, and an insertion matches
the same insertion anywhere. And a fix the brief holds back is never proposed, so
there is nothing to drop and nothing to see: it is invisible except through the
editor's note, which nothing enforces. A rejection becomes a
general "not a defect" entry at user or repo level only when the author says
so and has read the entry's text, and only after a dry run that showed the author that entry (the real promote
is refused without a matching dry run, as apply is). Once stored, the entry goes into the editor's brief and the
filter drops any later edit that makes the same change, by the same key as a rejection, in every document at
that level; the dropped edit stays visible in the marked-up copy with the cause `not-a-defect`. `/prose-edit rejections <path>` lists a
document's stored rejections, numbered, and
`/prose-edit rejections <path> --remove <n>` removes one. The session log keeps
each session's detail for 90 days; the document record is the authority for
rejections. When the author makes a correction in
conversation ("lead with the point here"), the skill offers to add it to the
style sheet at the level the author chooses.

**Genres.** Six genres: `rdr`, `reference-doc`, `how-to`,
`exploration-essay`, `changelog`, `commit-message`. A genre is its exemplars
(one or two passages Sam picks) plus short notes (for `rdr`, a pointer to
`docs/rdr/REGISTER.md`). The skill infers the genre from the path when the
repo style sheet maps it, and otherwise asks.

**Invocation.** `/prose-edit <path>[:<start>-<end>] [--genre <name>]
[--budget <n>]`. A line range limits proposals to that passage, as for one
CHANGELOG entry; the voice card and the document record are still the whole
file's, and an old string must occur exactly once within the range.
`/prose-edit - --genre commit-message` reads text from stdin: it keeps no
document record, stores no rejections, and applies nothing, printing the
accepted text instead; its session is logged under `log/stdin/<utc timestamp>`.
Listing and removing rejections: `/prose-edit rejections <path> [--remove <n>]`.
Adding an exemplar: `/prose-edit exemplar <genre> <path>:<start>-<end>`.

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
This design keeps that and adds the part missing today: Sam's corrections
applied as an edit pass on any draft (Gap 2), with his rejections remembered. It avoids the
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

- Exemplars and rejections are not kept between documents, and corrections
  are applied only where a skill writes them into its brief (Gap 2).
- Covers only the prose a skill happens to brief for (Gap 1).

**Reason for rejection**: it leaves Gaps 1 and 2 open.

### Alternative 2: Mechanical checks (a prose linter)

**Description**: encode the style rules as lint checks run over prose.

**Pros**:

- Deterministic and cheap.

**Cons**:

- A 721-edit mechanical sweep here made 19% of its edits worse
  (Discovery 5).

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
- Positive: nothing changes without the author accepting it, or, in
  cut-only mode, the `site-page` gate reviewing it.
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
- **Risk**: stored rejections hide proposals the author would now accept.
  **Mitigation**: rejections are per document, listed on demand, and removable;
  only the author generalises one. They match by minimal change, not verbatim,
  and the brief carries them to the editor. A filter drop stays visible in the
  marked-up copy; a fix the brief held back does not (see the limits above), and
  the key's missing position can drop a stored cut of a word where the author
  would have accepted it.

### Failure Modes

- Wrong genre: visible in the marked-up copy's header; the author overrides.
- Stale, repeated or overlapping exact-match string: the edit is skipped and
  reported, never forced.
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
the merge order is the same every time. It also resolves `<repo>`, drops
proposals whose minimal change matches a stored rejection for the document, and
lists and removes stored rejections. It lists log entries with
`nx memory list` rather than a partial-title `get`, and deletes with `-y`
(Discovery 11).

#### Step 1.3: Genres and exemplars

Sam picks one or two exemplar passages for each of the four Phase 2 genres
(`rdr`, `reference-doc`, `exploration-essay`, `changelog`) and seeds the repo
style sheet with his existing corrections that `site-page` §3 does not
already carry. `site-page` §3 stays in the tracked skill as its own authority
(SKILL.md line 8); for the `how-to` and `exploration-essay` genres the editor
reads §3 from that file at run time instead of holding a copy, so the two
cannot drift. For other genres the T2 style sheet is the editor's authority;
the memory files and Sam's instructions remain writing guidance.

#### Step 1.4: The editor

Agent and skill: brief assembly, the diagnostic questions, the output format.

#### Step 1.5: The review loop

Marked-up copy, viewer, the rejection filter before display, accept by
number, exact-match apply, session record, the offer to add a correction to
the style sheet.

**Exit**: the Test Plan scenarios below pass.

### Phase 2: Validate on real work

#### Step 2.1: Four real edits

Use the editor on four pieces of Sam's current work: an RDR, a reference doc
(for example `docs/storage-tiers.md`), an essay and a CHANGELOG entry, with the
budget fixed at 10 sentence edits per document before the run. Record for
each: sentence edits proposed, accepted and rejected, with each rejection's
reason (wrong, or right but unwanted); paragraph proposals and queries,
counted separately; and Sam's answer to "would you reach for this again?".
As a comparison, run the existing `site-page` voice-pass brief on the original
text of the essay and the reference doc, with the voice card the editor built
and Sam approved for that document and a cap of 10 cuts, each inside one
sentence; present its cuts in the same marked-up form, and Sam accepts or
rejects each. The cap replaced a word budget of 10% of the document (Sam,
2026-10-04) so that both arms' accept rates count the same unit. The full
comparison settings (denominators, pooling, no exemplar for the brief, blind
A/B presentation) are the 2026-10-04 comment on nexus-ger02.8.

#### Step 2.2: Decide

Pass: at least half of the proposed sentence edits accepted across the four,
the essay judged still in Sam's voice, a yes, and on the two comparison
documents the editor's accept rate at least the existing brief's. On
a fail, stop and revise this RDR.

### Phase 3: Adopt

#### Step 3.1: `site-page` voice pass

Add a cut-only mode: `/prose-edit <path> --mode cut-only --voice-card <file>
--word-budget <n>`. It proposes only sentence edits that remove words and
paragraph cuts, uses the given voice card instead of building one, applies the
style sheet and the rejection filter, logs the session, and returns its
proposals to the caller with no marked-up copy. `site-page` §5 item 3
dispatches it with the page's §2 card and word budget; the gate session applies
the cuts with the other passes' findings as it does today, where a fact fix
wins over a cut (SKILL.md line 70). This is the one place the author does not
accept each edit, because the `site-page` gate already owns that review.

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
- **Scenario**: Accept an edit whose old string occurs twice, and two edits that overlap. — **Verify**: all three are skipped and reported.
- **Scenario**: A range run on one CHANGELOG entry. — **Verify**: proposals only inside the range.
- **Scenario**: A stdin run with `--genre commit-message`. — **Verify**: no document record, nothing applied, session logged.
- **Scenario**: List stored rejections, remove one, run again. — **Verify**: the removed edit can be proposed again.
- **Scenario**: `<repo>` from the primary, a worktree and a subdirectory. — **Verify**: the same project name each time.
- **Scenario**: Cut-only mode with a voice card and word budget. — **Verify**: only word-removing proposals, returned without a marked-up copy.
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

Readable in Typora is verified. Memory helps is measured in Phase 1 (nexus-ger02.16:
the rate at which a rejected fix is shown again, against a threshold fixed before the
runs; the counts are in `tests/prose_edit/acceptance/README.md`); Worth accepting and
Voice kept in Phase 2.

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

- 2026-09-29: Gate round 1 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); commit `d31715dda`; critique `nexus_rdr/221-gate-critique-2026-09-29-r1`.
- 2026-09-29: Accepted with fix-check residuals dispositioned by bead (epic nexus-ger02): R1 nexus-ger02.7, nexus-ger02.10; R2 nexus-ger02.10; R3 nexus-ger02.2; R4 nexus-ger02.1, nexus-ger02.3, nexus-ger02.4; R5 nexus-ger02.8; R6 nexus-ger02.10.
- 2026-10-03: Phase 1 (Steps 1.2 to 1.5) changed these things relative to the text accepted on 2026-09-29, found in the Phase 1 review (nexus-ger02.5, nexus-ger02.6):
  - Rejection memory (`1d94d2a28`, nexus-ger02.16) amended the body: a rejection matches a later proposal by its minimal change, the document's rejections go into the editor's brief, an edit the author does not name is held, and "Memory helps" is measured narrowly in Phase 1.
  - Sam's ruling of 2026-09-30 on qualifiers (bd comment on nexus-ger02.3: only filler words are cut by default, every other qualifier or intensifier is a query) is in effect, and it deviates from the Technical Design sentence and Test Plan scenario 4 as accepted, which this entry leaves as they were; the deviation will be recorded in the post-mortem at close.
  - The genre map for a path lives in `memory.py`'s default map (`_default_genre`), not in the repo style sheet that Step 1.3 item 3 names. The repo style sheet can still carry `genre_map` entries.
  - Additions the accepted text does not describe: the stdin channel (flags on the first line, the text after it, written to the work directory); `--hold` and `--reject` on apply, so only a named `--reject` stores a rejection; a mandatory dry run before a real apply, enforced by `dryrun.json` hashes; a flat-text marked-up copy for an HTML page; and the brief as a file (`WORK/brief.md`, whose last line is an id the editor echoes as `brief_sha`; the id is in no prompt).
  - Sam's three decisions of 2026-10-03 on the Phase 1 review (nexus-ger02.5, nexus-ger02.6; T2 `nexus_rdr/221-decision-phase1-review-2026-10-03`): precedence labels (every style-sheet bullet in the brief carries its layer and the brief states that the narrower layer wins; a document's own record can also ignore or make query-only a site-page section 3 rule), a stored voice card (an author-approved card kept in `doc/<path>`, offered after a run and used as the anchor for later runs), and the promote filter plus dry-run gate (the filter drops edits matching a promoted not-a-defect entry, and a real promote needs a matching dry run first).
