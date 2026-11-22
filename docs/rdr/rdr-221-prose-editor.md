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
- Sam reviews proposed edits as suggestions in the MarkupEditor app (Typora
  until the MarkupEditor review, nexus-scmcs).

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
   `docs/storage-tiers.md` (line 173 then, line 194 now, 78 words by a later count), and CHANGELOG bullets across releases 7.63.0 to
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
    and `site-page` §3 (starts at line 36 of `.claude/skills/site-page/SKILL.md`) lists register rules for both of its
    genres. The voice pass in §5 item 3 is
    cut-only, runs in parallel with two other passes, and its findings are
    applied in one pass (item 3 of §5 is at line 69).
13. **Verified (source search).** Every worktree of this repo shares one git
    common directory: `git rev-parse --path-format=absolute --git-common-dir`
    prints the primary's `.git` from the primary and from every worktree (the
    form without the flag prints a relative `.git` in the primary), while each
    worktree's own directory name differs per session.
14. **Documented (Amendment A1).** Nine frontier models, including Claude
    Opus 5, Opus 5.5, Sonnet 5 and Fable 5, use features that make text harder
    to parse more often than paraphrases of their own text do (Clark, Martin
    and Mahowald, arXiv:2610.29162, catalog 1.12.241). The paraphrases are
    made by translating each sentence into Japanese and back. The features are
    stacked nouns, verbs coined from nouns, inanimate subjects with verbs for
    people or animals, object relatives without "that", word pairs no
    reference corpus has, and longer dependencies.
    - Against human writing, all nine use more inanimate subjects and rarer
      word pairs, and most also use longer dependencies, more stacked nouns
      and more object relatives without "that" in conversation. The human
      baselines are film subtitles, StackExchange posts and student essays,
      not technical documentation.
    - In single-prompt essays, the setting closest to a document, the effects
      are smaller: dependency length is "unchanged or slightly longer after
      the round trip", and stacked nouns change significantly for only two
      models. The paper's explanation for the larger effects in chat and code
      (many turns) is a hypothesis.
    - Opus 5.5 is lower than Opus 5 on 19 of 21 of these measures.
    - The paper's proposed cause is post-training that rewards short text. As
      a proof of concept, a 1.5B model trained with a per-token length penalty
      came to prefer the shorter member of an alternation ("the book the
      professor wrote" over "the book that the professor wrote"), and the
      preference carried over to untrained "that" contexts ("so that", "sure
      that"). Of four alternations further from training, only the genitive
      ("the house's roof" over "the roof of the house") shifted, and only
      without KL regularisation (Appendix C, Table 3).
    - The detector counts "it", "this" and code spans as inanimate subjects
      (Appendix B.1), so a check for that feature misfires on ordinary
      technical English ("the route refuses", "it returns") unless it states
      those exceptions.

    Quotes: `nexus_rdr/221-research-29`.

### Critical Assumptions

- [x] **Worth accepting.** At least half of the editor's sentence edits on real
  work are ones Sam accepts. — **Status**: Verified, Phase 2 passed
  2026-10-25: 10 of 10 edits accepted across four documents (T2
  `nexus_rdr/221-phase2-results`). Limits: n=10, measured on the editor before
  Amendment A1, and the existing brief's comparison tied at 100% (14 of 14) with
  more accepted changes (14 against 6). — **Method**: Phase 2
- [x] **Voice kept.** Sam judges edited essays still in his voice. —
  **Status**: Verified, Sam 2026-10-25 judged the edited essay still in his
  voice (`nexus_rdr/221-phase2-results`). One essay. — **Method**: Phase 2
- [x] **Memory helps, first half: a rejected edit is not proposed again.**
  — **Status**: Measured, narrowly (nexus-ger02.16): the same change at the
  same place is not shown again after one rerun in which the author rejected
  every shown edit: 0 of 57 rejected edits in 13 measurable runs on two
  documents (a no-memory editor repeats 92.6% on the small one and 38.3% on the
  storage one), and 0 of 20 in 10 runs on the small one with two edits rejected
  and the rest held. The pre-registered floor is 10 measurable runs per
  document; the small document had 4 and the storage one 9 in the first batch
  (`tests/prose_edit/acceptance/README.md`, lines 174-175), so the storage document
  missed the floor by one; the second batch added 10 on the small one. Not
  measured: a class of change (another em dash is proposed again, by design), a
  different change at the same spot, several reruns, and a document changed
  between runs (scripted test only). — **Method**: Phase 1 test, Phase 2
  observation
- [waived: not measured] **Memory helps, second half: a style-sheet entry is
  applied on the next document.** — **Status**: Not measured in any run
  (nexus-ger02.16 measured the first half only). — **Method**: none
- [x] **Readable in Typora.** Typora renders inline `<del>` and `<ins>` in a
  Markdown file, and Sam finds the marked-up copy workable. — **Status**:
  Verified — **Method**: Spike (2026-09-28). No longer load-bearing: the
  MarkupEditor review (nexus-scmcs) replaced the marked-up copy and the viewer.
- [waived: Sam closed it unverified on 2026-11-22] **Unpacking is accepted
  (Amendment A1).** The editor finds the
  compression features in a seeded document without touching the decoys, and
  on Sam's published docs, for each reason prefix, at least half of the
  lengthening edits shown to him are accepted (Step 1a.4). — **Status**: Closed
  unverified. The seeded half held (3 runs at 9 of 10 against the bar of 7;
  `nexus_rdr/221-a1-acceptance-2026-10-24`). The published-docs half was never
  measured: Sam dropped the post-merge tally (nexus-zhdxx) on 2026-11-22, so
  acceptance of the unpack feature rests on the seeded fixture alone. — **Method**: Phase 1a acceptance runs; the
  post-merge tally was dropped

## Proposed Solution

### Approach

One editor, invoked by the author on one document at a time. Its brief is
built from examples and the author's own recorded decisions, not from a rule
list. It proposes; the author decides; what the author decides is kept in T2
and used next time. The accepted text planned a project skill and agent in
`.claude/`, beside `site-page`. It shipped as the opt-in plugin
`conexus-prose-edit` (first commit `cda821a61a`, 2026-10-27), registered in
`.claude-plugin/marketplace.json`; `site-page` stays a project skill.

### Technical Design

**The editor (`prose-edit/agents/line-editor.md`, `prose-edit/skills/prose-edit/`,
`prose-edit/skills/prose-cut/`).**
Its brief for each run:

- the genre's exemplar passages;
- a voice card for the document, built from the document and the exemplars
  before any edit;
- the style sheet (user level, then repo level, then this document);
- the "not a defect" entries from past rejections, and this document's own
  stored rejections;
- a change budget;
- an instruction to prefer cutting (Amendment A1 replaces it with "cut
  redundancy, unpack compression", below).

Guidance is written as diagnostic questions with the exception stated, for
example: "Does this nominalisation hide who acts? Leave it if it names a
stable concept." Among the questions: does the paragraph open with its claim;
is the actor the subject; does the sentence end on what matters; would a
reader new to the project follow this step; is this qualifier justified by a
real uncertainty. As shipped, only filler words are proposed for cutting
("really" is one); every other qualifier or intensifier becomes a query to the
author, justified or not. This is Sam's ruling of 2026-09-30 (bd comment on
nexus-ger02.3) and replaces the accepted rule that unjustified qualifiers are
cut and justified ones stay. The brief carries it in section 6
(`prose-edit/skills/prose-edit/scripts/brief.py`): "Cut only filler words;
every other qualifier or intensifier is a query." A construction that matches a device
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

**Amendment A1: unpack compression (nexus-r9ze7).** The editor also proposes
edits that make a sentence longer when the longer form parses on the first
reading (Discovery 14). Sam approved the design on 2026-10-22 (T2
`nexus/design-prose-edit-compression.md`): it applies to published documents,
and it changes the line editor itself, not only the style sheet, with no
separate parse pass (Alternative 4). How often these features occur in this
repo's documents has not been measured: the Step 1a.4 tally that was to give
the first measurement was dropped (Sam, 2026-11-22). Three texts change.

Brief section 6 (`brief.py`), now headed "Prefer cutting", becomes "Cut
redundancy, unpack compression":

> Cut what repeats: when a word, clause or sentence repeats what the text
> already says, propose the cut (an empty new string). Cut only filler words;
> every other qualifier or intensifier is a query. An edit may add words when
> that makes a sentence parse on the first reading; its reason starts with the
> feature it fixes. When the plainer wording needs a meaning the text does not
> give, ask a query instead. Never cut a voice-card device, however this
> section or a diagnostic reads. Never put in content or claims.

`line-editor.md` gains a diagnostic row, "Can a reader parse this on the first
pass?". The row applies only when the brief's `Genre:` line is `rdr`,
`reference-doc`, `how-to` or `exploration-essay`; on a `changelog` or
`commit-message` run it does not apply, because there the problem is excess
length (Gap 1). In cut-only mode (Step 3.1) neither the row nor the
lengthening clause of section 6 applies.

- Propose when the sentence has:
  1. three or more nouns stacked as modifiers;
  2. a verb coined from a noun;
  3. an inanimate subject with a verb for people or animals;
  4. a long insertion between subject and verb;
  5. a long clause missing its optional "that".
- Leave it when the construction is:
  - a term the document defines, or a house term on the voice card;
  - an identifier or code;
  - a verb technical English uses for things (fails, returns, refuses, reads,
    writes, accepts);
  - density the voice card lists as deliberate.

`line-editor.md` rules change in two places. The standing rule (line 62 when this was written; its replacement is line 68 of
`prose-edit/agents/line-editor.md`)
"Prefer cutting. A cut has an empty new string. Never put in a claim, number,
name, source or example the text does not contain." becomes:

> Cut what repeats. A cut has an empty new string. An edit may add words when
> that makes the sentence parse on the first reading (the parse row). Never put
> in a claim, number, name, source or example the text does not contain.

No other line in the file may forbid adding words. Three rules are added:

- Never cut an optional "that" or a genitive "of"; restoring an optional
  "that" is an edit.
- A lengthening edit's reason starts with one of `noun stack:`,
  `coined verb:`, `inanimate subject:`, `dependency:` or `optional word:`.
- A query raised by the parse row starts its text with the same prefix.

A rejected `optional word:` edit is a pure insertion, and the rejection filter
matches a pure insertion anywhere in the document (Memory in T2, below), so one
rejected restored "that" drops every later one in that document. This is
accepted: the drop is listed in the chat with its cause. (The Step 1a.4 tally
was to count dropped edits; it was dropped.)

Out of scope: prose written at generation time (messages, commits, beads), any
lint or mechanical check, any sweep of existing documents, and a separate parse
pass. The accepted text demoted a prefix that failed the Step 1a.4 tally to
query-only by a repo or document style-sheet entry that Sam approves. The
tally was dropped on 2026-11-22, so the demotion mechanism (nexus-b0b40,
closed as moot) is not active, and no demotion is planned. The
`site-page` section 3 query-only list was never that mechanism: it is keyed on
section 3 bullets.

**Review loop.** A path run opens the document in the MarkupEditor app
(0.9.11 or later, the first release with suggestions) and sends each proposed
sentence edit there as a suggestion, its reason as the suggestion's note. The
editor's note, paragraph proposals, queries and any edit the app refuses are
shown in the chat. The author accepts or rejects each suggestion in the app and
says done; the skill reads the decisions back from the app (a suggestion left
pending is held), returns the app to a clean buffer, and never saves the
document there. A stdin run shows its edits in the chat and the author answers
with the numbers to accept. Either way the skill applies accepted sentence
edits to the real file with exact-match replacement, in file order. An edit is applied only when its old string occurs
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
| User style sheet | `prose` | `stylesheet` |
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
edit is listed in the chat, with its cause), and the document's rejections are
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
that level; the dropped edit is listed in the chat with the cause `not-a-defect`. `/prose-edit rejections <path>` lists a
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
This design keeps that (Amendment A1 widens the brief to cut redundancy and
unpack compression, Discovery 14) and adds the part missing today: Sam's corrections
applied as an edit pass on any draft (Gap 2), with his rejections remembered. It avoids the
forms that failed: no lint and no sweep (Discovery 5), and no rule the model
applies without the author deciding each case (Discoveries 5, 7). It was
planned as a project skill because that can be changed the same day Sam reacts
to it, with a plugin to come if it earned one; it shipped as a plugin after
Phase 2 passed (Alternative 3).

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

**Outcome**: taken after Phase 2. Phase 2 passed on 2026-10-25, and the
plugin `conexus-prose-edit` landed in `cda821a61a` on 2026-10-27. The records
read for this amendment do not say who decided the move or on what evidence
(inferred, not read).

### Alternative 4: Unpack compression without changing the editor (Amendment A1)

**Description**: carry the compression diagnostics as style-sheet entries
only, or run them as a separate parse pass after the line edit.

**Pros**:

- No change to the line editor's text or its pins.

**Cons**:

- A style-sheet entry cannot lift the editor's own "Prefer cutting" rule,
  which forbids the lengthening edits the diagnostics ask for (inferred, not
  read).
- A separate pass doubles the runs and the author's review per document
  (inferred, not read).

**Reason for rejection**: Sam chose to change the editor (T2
`nexus/design-prose-edit-compression.md`, "Approach B"); the record does not
state his reasons, and the two cons above are inferred.

## Trade-offs

### Consequences

- Positive: corrections persist; every kind of prose can get a line edit.
- Positive: nothing changes without the author accepting it, or, in
  cut-only mode, the `site-page` gate reviewing it.
- Negative: editing costs the author's attention on every proposal.
- Negative: the editor is an opt-in plugin, so another repo gets it only by
  installing `conexus-prose-edit`, and it needs Nexus (`nx`) for its memory.

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
  and the brief carries them to the editor. A filter drop is listed in the
  chat; a fix the brief held back does not (see the limits above), and
  the key's missing position can drop a stored cut of a word where the author
  would have accepted it.
- **Risk** (Amendment A1): the line editor runs on a model of the kind
  Discovery 14 measures, so it may miss the features it is asked to find, or
  propose a lengthening edit that changes the meaning. **Mitigation**: the
  seeded acceptance bar in Phase 1a, fixed before the runs; a plainer wording
  that needs a meaning the text does not give becomes a query; the author
  accepts each edit.

### Failure Modes

- Wrong genre: a path that no genre map reaches makes the run ask which genre
  it is; `--genre` overrides a mapped one. The marked-up copy header that once
  showed the genre is gone.
- Stale, repeated or overlapping exact-match string: the edit is skipped and
  reported, never forced.
- T2 unavailable: the skill stops and says so.

## Implementation Plan

### Phase 1: Build

#### Step 1.1: Marked-up copy format (replaced)

The accepted text specified a marked-up copy verified with Sam: the editor's
note as a blockquote at the top, sentence edits inline as
`<del>old</del><ins>new</ins>` with a superscript number, reasons as numbered
footnotes, paragraph proposals (`[P1]`) and queries (`[Q1]`) as numbered
blockquotes. The MarkupEditor review (nexus-scmcs, conexus 8.3.0) replaced it:
no marked-up copy is made. Sentence edits are suggestions in the MarkupEditor
app with their reasons as notes; the editor's note, paragraph proposals,
queries and filter drops are shown in the chat (see Review loop).

#### Step 1.2: T2 records

The record shapes above. A small stdlib script,
`prose-edit/skills/prose-edit/scripts/memory.py`, reads the layers in order
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

Suggestions in the MarkupEditor app (the chat for a stdin run), the rejection
filter before display, accept in the app or by number, exact-match apply, session record, the offer to add a correction to
the style sheet.

**Exit**: the Test Plan scenarios below pass. What that exit does not claim is
listed in `tests/prose_edit/acceptance/README.md`, "What Phase 1 exit does not
claim": no batch ran at the final tree; the review loop was not re-run on the
MarkupEditor route; scenarios 7, 8, 12 and 14 are covered by unit tests only;
and the stand-in harness (nexus-scmcs.3) is still in progress.

### Phase 1a: Unpack compression (Amendment A1, nexus-r9ze7)

Implementation starts only after this amendment passes its gate and Sam says
go. Phase 1a is built and its acceptance runs are made on a branch; it lands on
`develop` only after Phase 2's remaining runs (nexus-ger02.8) are done, so
Phase 2's accept rate is measured on one editor.

#### Step 1a.1: Text and unit pins

Failing pins first, in `tests/prose_edit/test_brief.py` and
`tests/prose_edit/test_brief_review.py`: the new section 6 heading and text,
the parse row and its genre and cut-only limits, the five reason prefixes, the
query-prefix rule, the optional-word rule, the new rule (now line 68 of `line-editor.md`) with its guard
against invented content, and the absence of the old "Prefer cutting." rule.
Then the text in `brief.py` and `line-editor.md`, and the skill description in
`SKILL.md`.

#### Step 1a.2: Acceptance fixture

`tests/prose_edit/fixtures/compression.md`, in the `reference-doc` genre,
seeds 10 sentences, two for each feature, written for this fixture. Each
seeded sentence shows one feature, and the scorer's table
gives it exactly one expected prefix. It also holds decoys: an identifier, a
term the document defines, three of the computing verbs, and one deliberately
dense passage. Every decoy is always scored, whatever the editor's voice card
says. A `compression` kind is added to `verdicts.py` (scorer pins first) and to
the README's kind table (`run-scenario.sh` has no kind registry). The scenario runs with
`--budget 12`, so the budget cannot be what stops the tenth seeded edit.

#### Step 1a.3: Acceptance runs

Three fresh runs, one runner at a time, no retry. The bar, fixed before the
runs: in each run, at least 7 of the 10 seeded sentences get a lengthening edit
whose reason starts with that sentence's prefix, or a query anchored in that
sentence whose text starts with it; and no decoy is touched. A decoy is touched
by an edit overlapping it, or by a query anchored in it whose text starts with
a compression prefix. A run that misses the bar is reported to Sam with its
transcript; any change to the text or the fixture after a miss is followed by a
fresh set of three runs, never a retry of one. Then rerun the canaries and the
`xanadu`, `linda`, `refrain`, `qa`, `qb`, `qc`, `protected`, `budget` and
`range` scenarios on the changed tree; they pass as they did before the
amendment.

Sam's ruling of 2026-10-24 (nexus-4s8sb), made after the three runs: under the
`dependency:` prefix an edit that changes the sentence and keeps at least as
many words is a hit, because a split or a reorder fixes a dependency without
lengthening it. The other prefixes keep the lengthening rule. The A1 runs stand
as judged under the bar above (3 of 3 at 9 of 10; T2
`nexus_rdr/221-a1-acceptance-2026-10-24` records that runs 1 and 3 fixed D2 by
an 18-word split under `dependency:`). Runs after the ruling use the amended
rule; none of the three is rescored.

#### Step 1a.4: Tally after merge (dropped)

Dropped by Sam on 2026-11-22 (nexus-zhdxx closed; "RDR-221 closes without
it"). The accepted text counted, over Sam's next five `/prose-edit` runs on
published docs, the lengthening edits shown for each reason prefix (accepted,
rejected and held, from the session log) and those the filter dropped, and
demoted a prefix with at least 5 such edits and fewer than half accepted to
query-only (nexus-b0b40, closed as moot, not active). No tally was run. The seeded
fixture is the only measured evidence for the unpack feature (see Critical
Assumptions).

**Exit**: Step 1a.3's bar holds in all three runs, and the regression runs pass.
The tally is not part of the exit.

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

Add a cut-only mode as a second skill in the plugin,
`conexus-prose-edit:prose-cut <path>[:range] --mode cut-only --voice-card
<file> --word-budget <n>`. It is a separate skill because `/prose-edit` is
explicit-only (`disable-model-invocation`) and the gate that calls this mode is
a model; `/prose-edit --mode cut-only` points to it. It proposes only sentence
edits that remove words and paragraph cuts, uses the given voice card instead
of building one, applies the style sheet and the rejection filter, logs the
session, and returns its proposals to the caller with no marked-up copy and no
question. The word budget is a target: the filter reports the count against it
and drops nothing for exceeding it. `prose-cut count <page>` prints the page's
prose words, the unit of the budget.

`site-page` §5 item 3 dispatches it with the page's §2 card and word budget.
The gate makes the fact and style passes' fixes first, then decides the cuts
with `prose-cut decide <work> --accept S --reject S --hold S`, which applies the
accepted ones through the same apply as an author run. A cut whose text a fix
changed is skipped and stored nowhere, so a fact fix wins over a cut. A
rejected cut is stored for the page; a held one (named in no set) is not.
Paragraph cuts are advice only and never stored. This is the one place the
author does not accept each edit, because the `site-page` gate already owns that
review. In this mode the parse row and the lengthening clause of section 6 do
not apply (Amendment A1).

#### Step 3.2: Remaining genres

Exemplars for `how-to` and `commit-message`. Dropped, not done: Sam decided
on 2026-11-22 ("Drop them"). The T2 project `nexus_prose` holds genre records
for `rdr`, `reference-doc`, `changelog` and `exploration-essay` only (read
2026-11-22). Both names exist in the code's genre list (`memory.py`, `GENRES`),
and a genre with no exemplars runs on the style sheet alone (plugin README).
Bead nexus-ger02.13's notes carry the correction to its earlier "Done".

#### Step 3.3: Plugin decision

Whether to move the editor into a plugin, decided on Phase 2's evidence.
Done: the editor shipped as `conexus-prose-edit` (Alternative 3, Approach).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Style sheets, genres, document notes (T2) | `nx memory list` | `nx memory get` | `nx memory delete` | Phase 1 tests | T2 backup |
| Session log (T2) | same | same | 90-day TTL | Phase 1 tests | T2 backup |
| Run work files (no marked-up copy since conexus 8.3.0) | N/A | N/A | Temporary directory, deleted at the end of the run (`rmtmp`) | Phase 1 tests | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: A document with a quote, a code block, a table and frontmatter. — **Verify**: no proposal inside any of them.
- **Scenario**: `xanadu-in-nexus.md` and `linda-in-nexus.md`. — **Verify**: no proposal touches the refrains, tricolons or unexplained SQL.
- **Scenario**: Budget set to N. — **Verify**: at most N sentence edits.
- **Scenario**: A sentence with a filler word and a qualifier that is not filler. — **Verify**: only the filler word is proposed for cutting; the other qualifier comes back as a query (Sam, 2026-09-30; the accepted scenario asked for the unjustified qualifier to be cut).
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
- **Scenario**: Run work files location (the marked-up copy is gone). — **Verify**: outside the repo; `git status` unchanged.
- **Scenario** (Amendment A1): `tests/prose_edit/fixtures/compression.md`, run as kind `compression` with `--budget 12`. — **Verify**: at least 7 of the 10 seeded sentences get a lengthening edit whose reason, or a query whose text, starts with that sentence's prefix; no decoy is touched (Step 1a.3).

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

Amendment A1 lets an editor briefed to cut also add words. The two do not
conflict: a cut removes what repeats, a lengthening edit restores what a reader
needs to parse the sentence, and the reason prefix says which one an edit is.
The editor runs on a model of the kind Discovery 14 measures; the seeded bar in
Phase 1a tests whether it finds the features anyway.

### Assumption Verification

Readable in Typora is verified, and no longer load-bearing since the MarkupEditor review. Memory helps is measured in Phase 1 (nexus-ger02.16:
the rate at which a rejected fix is shown again, against a threshold fixed before the
runs; the counts are in `tests/prose_edit/acceptance/README.md`); Worth accepting and
Voice kept were verified in Phase 2 (2026-10-25). Unpacking is accepted
(Amendment A1) was tested by the Phase 1a acceptance runs, against a bar fixed
before them; the post-merge tally that was to follow them was dropped by Sam on
2026-11-22, and the assumption is closed unverified (`nexus_rdr/221-research-28`
records the source of the tally design).

### Gap disposition (close, 2026-11-22)

Sam's ruling, T2 `nexus/sam-ruling-rdr221-close-2026-11-22`, after the close
critique `nexus/critique-3616b9868f-none-2026-11-22`.

- **Gap 1, no standing line editor: partly solved.** Genres with exemplars
  exist for `rdr`, `reference-doc`, `changelog` and `exploration-essay`. The
  `how-to` and `commit-message` genres were dropped (Sam, 2026-11-22, "Drop
  them"; Step 3.2), and no stdin or commit-message run was made. The editor ran
  on `docs/storage-tiers.md` in Phase 2 on 2026-10-05, before Amendment A1
  (2 edits, 5 queries, 1 paragraph cut), and left the 78-word "Domain split
  (RDR-063...)" sentence untouched (now `docs/storage-tiers.md:194`); no A1 run
  has been made on it. The changelog trial was one short entry (nexus-2k66t.3, then under
  `[Unreleased]`, now `CHANGELOG.md:1396`),
  and the long CHANGELOG entries were not exercised (T2
  `nexus/critique-3616b9868f-none-2026-11-22`, `nexus_rdr/221-phase2-results`).
- **Gap 2, corrections are guidance, never an edit pass: mechanism solved,
  coverage partial.** The style-sheet mechanism exists and runs: the T2 style
  sheet `nexus_prose/stylesheet` holds 8 diagnostics (em dash in new prose,
  "load-bearing", aphorism or restating closing line, bold-label paragraphs,
  argument in bullets, unsourced claims, runs of same-length sentences, filler
  words), and `site-page` §3 adds its register rules for the `how-to` and
  `exploration-essay` genres. Discovery 3 counts more than twenty corrections.
  Of the four Gap 2 names, the em dash rule is in the style sheet and "lead
  with the point" is in `site-page` §3; "no trope headings" and "one term per
  concept" are in `site-page` §3 only, so they reach `how-to` and
  `exploration-essay` runs and no `rdr`, `reference-doc` or `changelog` run.
- **Gap 3, review adds and nothing cuts: accepted as reduced** (Sam,
  2026-11-22). The editor cuts filler words only and turns every other
  qualifier into a query. The `site-page` voice pass, now `prose-cut`, remains
  the only cutter applied without a per-edit accept, and it covers `web/` pages
  and essays only.
- **Gap 4, voice has no anchor outside one skill: solved for four genres.**
  Exemplars exist for 4 of the 6 genres the brief names (`rdr`,
  `reference-doc`, `changelog`, `exploration-essay`; T2 `nexus_prose`
  `genre/*`). Voice cards are stored per document on the author's yes, so far
  for essay and web documents only. The how-to and commit-message genres have
  no exemplars, by Sam's drop (Step 3.2).

### Scope Verification

Phase 2 is in scope and decides whether Phase 3 happens. Phase 1a is in scope;
its out-of-scope list is in the Amendment A1 paragraph of the Technical Design.

### Cross-Cutting Concerns

- **Versioning**: the plugin `conexus-prose-edit` is versioned with conexus (`prose-edit/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` both read 8.5.5 on develop `3616b9868f`).
- **Licensing**: repo licence.
- **Deployment model**: opt-in plugin `conexus-prose-edit`, registered in `.claude-plugin/marketplace.json` (agent `prose-edit/agents/line-editor.md`, skills `prose-edit/skills/prose-edit` and `prose-cut`). `site-page` stays a project skill.
- **Incremental adoption**: one document at a time, only when invoked.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: session-log TTL.
- All others: N/A.

### Proportionality

Sized to one editor, its memory and a review loop. Anything larger waits for
Phase 2. Amendment A1 changes three texts and adds one fixture and one scorer
kind; it adds no lint, store or pass. The demotion mechanism (nexus-b0b40) is
closed as moot and not active, since the tally it served was dropped.

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
- Compression in AI-generated text (Amendment A1): https://arxiv.org/abs/2610.29162

## Revision History

- 2026-09-29: Gate round 1 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); commit `d31715dda`; critique `nexus_rdr/221-gate-critique-2026-09-29-r1`.
- 2026-09-29: Accepted with fix-check residuals dispositioned by bead (epic nexus-ger02): R1 nexus-ger02.7, nexus-ger02.10; R2 nexus-ger02.10; R3 nexus-ger02.2; R4 nexus-ger02.1, nexus-ger02.3, nexus-ger02.4; R5 nexus-ger02.8; R6 nexus-ger02.10.
- 2026-10-03: Phase 1 (Steps 1.2 to 1.5) changed these things relative to the text accepted on 2026-09-29, found in the Phase 1 review (nexus-ger02.5, nexus-ger02.6):
  - Rejection memory (`1d94d2a28`, nexus-ger02.16) amended the body: a rejection matches a later proposal by its minimal change, the document's rejections go into the editor's brief, an edit the author does not name is held, and "Memory helps" is measured narrowly in Phase 1.
  - Sam's ruling of 2026-09-30 on qualifiers (bd comment on nexus-ger02.3: only filler words are cut by default, every other qualifier or intensifier is a query) is in effect, and it deviates from the Technical Design sentence and Test Plan scenario 4 as accepted, which this entry leaves as they were; the deviation will be recorded in the post-mortem at close.
  - The genre map for a path lives in `memory.py`'s default map (`_default_genre`), not in the repo style sheet that Step 1.3 item 3 names. The repo style sheet can still carry `genre_map` entries.
  - Additions the accepted text does not describe: the stdin channel (flags on the first line, the text after it, written to the work directory); `--hold` and `--reject` on apply, so only a named `--reject` stores a rejection; a mandatory dry run before a real apply, enforced by `dryrun.json` hashes; a flat-text marked-up copy for an HTML page; and the brief as a file (`WORK/brief.md`, whose last line is an id the editor echoes as `brief_sha`; the id is in no prompt).
  - Sam's three decisions of 2026-10-03 on the Phase 1 review (nexus-ger02.5, nexus-ger02.6; T2 `nexus_rdr/221-decision-phase1-review-2026-10-03`): precedence labels (every style-sheet bullet in the brief carries its layer and the brief states that the narrower layer wins; a document's own record can also ignore or make query-only a site-page section 3 rule), a stored voice card (an author-approved card kept in `doc/<path>`, offered after a run and used as the anchor for later runs), and the promote filter plus dry-run gate (the filter drops edits matching a promoted not-a-defect entry, and a real promote needs a matching dry run first).
- 2026-10-22: Amendment A1 drafted (nexus-rrkm7, epic nexus-r9ze7): brief section 6 becomes "Cut redundancy, unpack compression", the line editor gains a parse row, two rules and a rewrite of its "Prefer cutting" rule, with Discovery 14, the "Unpacking is accepted" assumption, a risk, Phase 1a with its pre-registered bar, and a Test Plan scenario. To be gated (nexus-c7fim); any lifecycle move is Sam's. Design: T2 `nexus/design-prose-edit-compression.md`.
- 2026-10-23: Gate round 2 — PASSED (0 Critical, 12 Significant, 0 ship-blocker(s)); commit `dc6cdfae2`; critique `nexus_rdr/221-gate-critique-2026-10-23-r2`.
- 2026-10-23: round 2 Significants S1-S12 dispositioned by Sam's decisions of 2026-10-23 (S6, S7, S11, S12) and by the disposition edit (all twelve), fix check `nexus_rdr/221-fix-check-6159bc54e` (consensus PASS); its counted defects carried by nexus-zhdxx, nexus-b0b40, nexus-x7o5e and nexus-0hkkj.
- 2026-10-25: Step 1a.2 text aligned with the fixture as built: no seeded sentence comes from the paper's Table 2, and `run-scenario.sh` has no kind registry (stacked review B4/B5). The section 6 heading stays unconditional; the brief drops the unpack clause for changelog and commit-message.
- 2026-10-25: Phase 2 PASSED (Sam): 10 of 10 editor edits accepted across the four documents, the essay still in Sam's voice, yes to reaching for it again on all four, comparison accept rates tied at 100% (editor 6/6, brief 14/14). Record: T2 `nexus_rdr/221-phase2-results`. Phase 1a lands on `develop` after this.
- 2026-11-01: Step 3.1 aligned with cut-only as shipped in conexus 7.91.0 (nexus-ger02.10, amended on Sam's go, nexus-scmcs.5): a second skill, `prose-cut`, in place of a `/prose-edit` mode; the word budget a target, not a cap; the gate fixes first and then applies the cuts with `prose-cut decide`, in place of applying them alongside the other findings. The stale `site-page` line cites are gone from the step. Discovery 12's line cites are left as they read when verified. The review-loop text (marked-up copy, viewer) still matches `develop`; the MarkupEditor review is amended with its branch (nexus-scmcs.4).
- 2026-11-16: Review loop aligned with the MarkupEditor review as shipped in conexus 8.3.0 (nexus-scmcs, landed at `f2542acfa`; amended under grant 3, nexus-scmcs.2): a path run sends its sentence edits to the MarkupEditor app as suggestions and reads the author's decisions back from it; a stdin run answers in the chat; the Typora viewer and the marked-up copy are gone (the `prose/viewer` T2 record still exists). "Readable in Typora" stays verified and is no longer load-bearing. Step 1.5 and the Technical Environment follow.
- 2026-11-22: Text amended to match what shipped, before the close (nexus-ger02; Sam's ruling, T2 `nexus/sam-ruling-rdr221-close-2026-11-22`, after the close critique `nexus/critique-3616b9868f-none-2026-11-22`, which found the body describing behaviour that did not ship). Status unchanged; the close waits for a re-critique.
  - Step 1a.4 tally dropped (nexus-zhdxx closed, Sam); the demotion mechanism (nexus-b0b40) is deferred and not active; "Unpacking is accepted" is closed unverified, resting on the seeded fixture alone.
  - The qualifier rule and Test Plan scenario 4 follow Sam's 2026-09-30 ruling as shipped (only filler words are cut; every other qualifier is a query); this records the deviation the 2026-10-03 entry deferred to a post-mortem.
  - Deployment: the editor shipped as the opt-in plugin `conexus-prose-edit` (`prose-edit/`), not a project skill under `.claude/`; Approach, Technical Design, Step 1.2, Step 3.3, Alternative 3, Trade-offs and Cross-Cutting follow.
  - The marked-up copy and viewer are gone (conexus 8.3.0); Step 1.1, Failure Modes, Day 2, the Test Plan and the Risks mitigation follow. Filter drops are listed in the chat.
  - Critical Assumptions "Worth accepting" and "Voice kept" ticked on the Phase 2 pass; "Memory helps" ticked on its narrow measurement; all four boxes dispositioned.
  - A "Gap disposition" block in the Finalization Gate: Gap 2 solved, Gap 4 solved, Gap 3 accepted as reduced, Gap 1 partly solved (Gap 2 later restated as "mechanism solved, coverage partial" and Gap 4 as "solved for four genres").
- 2026-11-22 (second commit, after the re-critique `nexus/critique-85a121250a-none-2026-11-22-r2`: 0 Critical, 5 Significant, all evidence overclaims): the Gap 1 disposition says the storage-tiers sentence was exercised pre-A1 and untouched, and that the how-to and commit-message genres were dropped by Sam ("Drop them"); Gap 2 reads "mechanism solved, coverage partial" with the corrections not carried; "Memory helps" is split into two boxes (first half ticked with its missed 10-run floor, second half waived as not measured); the Phase 1 exit points at what it does not claim; the stale cites (`line-editor.md` rule now line 68, Discovery 12's `site-page` lines 36 and 69) and the `prose/viewer` claim are corrected; nexus-b0b40 is closed as moot; "the only automated cutter" now reads "the only cutter applied without a per-edit accept".
