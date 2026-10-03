---
name: site-page
description: Use when adding, rewriting, reviewing, or publishing a page under web/ on the GitHub Pages site (hellblazer.github.io/nexus), or when writing a docs/exploration essay about a subsystem. Carries the two page genres, the house template, the review gate, and the publish steps.
---

# Site page

Authority: this file. Origin: the five pages shipped 2026-09-10/11 (install, getting started, RDRs, research, tuple space) and the reviewer feedback on the tuple-space page.

## 1. Pick the genre first

Three genres exist. Never mix them in one document.

| Genre | Lives at | Shape | Reader wants |
|---|---|---|---|
| How-to | `web/<name>.html` | Numbered lessons with action titles. Each lesson: `You might say` block, `What happens` / `What you see`, rendered output in a dropdown. Mechanism in a collapsed appendix at the end. | To use it now. |
| Overview | `web/<name>.html` | Present tense, third person, no second-person instructions, no history, no versions. The system's shape first (a figure and its participants), then one end-to-end use case as the spine, introducing each mechanism at the step where it acts, then who decides what. Reference detail collapsed. Model: `web/how-agents-ship.html`. | To understand how a system runs today. |
| Exploration essay | `docs/exploration/<idea>-in-nexus.md` | Lineage named in sentence one, the practical problem, how the suite leverages it, what we borrowed, what we left out, what it changes for you, further reading. Model: `docs/exploration/xanadu-in-nexus.md`. | To understand the thinking and what it means for them. |

History, earlier attempts, and design rationale belong in the essay, once. A how-to that narrates them was rejected by a reader ("a project development history lesson"); the same material in the essay genre was praised.

**A how-to's numbered lessons are things the reader does.** Every lesson before the appendix has a `You might say` block and an action the reader takes. A lesson that would carry `reading only` is mechanism, and mechanism goes in the collapsed appendix. Count the lessons before review: one reading-only lesson ahead of an actionable one sends the page back. The first ci-board page (2026-09-26) opened with two reading-only lessons (the data path, then template capacity) and put the one command a reader runs fourth; Sam found the whole page hard to read.

**One reader per page.** A `web/` page serves the person using the feature. Operator material (hosting, signature checks, tokens, duplicate handling, wire formats a reader never types) stays in the RDR or an operator page, and the how-to links to it in one sentence.

**When the subject has an RDR, start from what the reader does, never from the RDR's outline.** An RDR is ordered for a reviewer judging a design: data flow, capacity, protocol, trust. Copying that order produces a design document with lessons numbered on it. List the questions a reader brings (is my push green, tell me when it changes), make each a lesson, and take from the RDR only what those lessons need.

## 2. Before drafting

- Ask Sam for an exemplar paragraph in the voice he wants, before writing a full draft. In the 2026-09-15 coordination-page pilot this moved the voice further than any review round did.
- Write a short voice card for the page: point of view (ours, opinionated, ideology-free, confident use of prior art with no lineage case), audience, register. A few lines, not a brief.
- A rewrite that adds meaning (what each fact means for the reader) tends to repeat "so" and to invent causes. Vary the connective, and keep "so" for a real consequence.

## 3. Register (both genres)

- Plain technical English for non-native readers. Active voice, no semicolons, no idioms or phrasal verbs, one term per concept.
- Sentence boundaries follow the logic. When one fact follows from another, join them with because / so / and in one sentence; start a new sentence for a new idea. A paragraph of four-to-ten-word sentences parses but loses the connectives that say why, and reads as a list with the bullets removed. Review pushes only toward shorter sentences (the long-sentence scan has no opposite), so §5's scan measures both directions.
- Say each thing once. The lede, a figure caption and the body that narrate the same flow are three copies; keep the body, and let the caption name what the figure shows.
- Every paragraph and every section opener starts with its claim. Never two setup sentences and then the point.
- Headings are plain nouns or actions. No tropes ("The shape of the problem").
- Motivate a new thing at the edge of what already exists and works. Name the existing mechanism, what it does well, where it stops, what the new thing adds there. Never "none of these can". Check every claimed gap against the tools Sam uses: harness messaging and teammate panes, hooks, T1/T2/T3, files.
- When a word is both a verb and a noun, name the noun fully ("take operation").
- No corpus counts, incident dates, or self-reference to this project's incidents on a `web/` page. Exploration essays may carry dates and RDR numbers.
- No volume figures that drift with configuration: posts per push, jobs per run, pushes until full. They are wrong the next time a workflow changes. State the limit and its reason instead.
- The terms table holds only words the lessons use. A word only the appendix needs is explained where it appears. More than about six terms means the page is organised around internals.
- Placeholders as `<span class="ph">&lt;ORANGE CAPITALS&gt;</span>`. Prompts are examples, not scripts.
- Rendered output blocks come from real tool or CLI output captured in the session, trimmed. Never invented.

## 4. House template (`web/`)

- Head block: copy lines 1-9 of `web/rdr.html` (doctype, lang, charset, viewport, description, color-scheme, favicon), then `<title>`, the Google Fonts link, the shared `<style>` copied byte-for-byte from `web/rdr.html`, then a page-local `<style>` that only adds, then the GoatCounter tag before `</head>` (see memory `project_site_analytics_goatcounter`).
- Body: `<div class="wrap">` with `nav.rail` (eyebrow, `ol#toc` whose entries match section ids and h2 text exactly, `.meta` links to the sibling pages and `./`), then `<main>` with `.topnav`, `h1`, `p.lede`, notes, the terms dropdown (`details.gloss`), numbered `section.lesson` blocks, `.footer`.
- Dropdowns: terms table, every `pre.out` block (`details.gloss.out` with the label as `summary`), and the appendix blocks. Instruction blocks (`div.cmd` with the Copy button) stay open.
- Figures: hand-drawn inline SVG only, `currentColor` strokes, brass for the reader's own step, `figure`+`figcaption`, labels off the lines. Render once with headless Chrome at 760 wide before publishing; below 500 wide Chrome clamps and crops, which is not overflow.
- CSS invariants: `pre{white-space:pre-wrap;overflow-wrap:anywhere}` after the base `pre` rule, `min-width:0` on every grid/flex wrapper, `minmax(0,1fr)` tracks, tables inside `.tbl`.
- Heading order never skips a level. A box title inside a section is `p.ttl`, not `h4`.

## 5. Review gate before publish

The gate applies to every commit that changes prose under `web/**`, including a code commit that updates a page to match. A code change that edits page text runs at least the voice pass (3 below) on the changed section. On 2026-09-27 a CI status-script change pasted its docstring's precedence rules into the ci-board page as three paragraphs with no review; each sentence was true, and the reader needed one table row and one sentence.

Dispatch three passes in parallel, each with its own brief, then read the findings files yourself:

1. Fact pass, `conexus:substantive-critic`: every claim checked against **code**, not only docs (docs were stale on two claims on 2026-09-10); register for non-native readers; the how-to test in §1 (which paragraphs a user would skip, any history, proportion per section). Ask for a proposed outline if the shape is wrong.
2. Style pass, `conexus:code-review-expert`: stylesheet byte-identical to `web/rdr.html`, rail vs ids, heading order, SVG label collisions, dark theme tokens, 400 px width, every href.
3. Voice pass, `conexus:substantive-critic` with a separate brief: the voice card from §2, and a word budget stated in the brief. Its job is to cut, never to add. It judges which words are earned rather than forcing the number: in the 2026-09-15 pilot the voice reviewer called most of the page's growth earned and warned that forcing a 10% cap would cut real meaning.

The voice pass exists because of review accretion: a fact-checker sees an error but not a surplus, so each round adds a hedge and none removes one.

Do not edit the file while a reviewer is reading it. Apply all three passes' findings in one pass, and where a cut and a fact fix touch the same sentence, the fact fix wins. Report the final word count against the budget. Then:

```bash
python3 .claude/skills/site-page/link_audit.py            # every href on every web/ page and README site links
```

Sentence scan, both directions: sentences over 25 words, and runs of three or more sentences of 10 words or fewer, with a FRAGMENTED verdict above 5 runs per 100 sentences (calibration in the script's docstring). Both are prompts for judgement, not hard failures:

```bash
python3 .claude/skills/site-page/sentence_scan.py web/<page>.html
```

## 6. Publish

1. Fast-forward `develop` first; a peer pushes to the shared checkout all day.
2. New page: add the nav row to the topnav of `web/index.html` and the `.meta` rails of every other page, and a row in `README.md`'s site list.
3. Commit by explicit path. Put the commit in its own tool call, never after a `;` in a chain that edits (a failed edit step let a commit run on 2026-09-11).
4. `git fetch && git rebase --autostash origin/develop` (autostash carries a peer's stray uncommitted file), then `scripts/git-push-develop.sh <your shas>`.
5. Pages deploys from `develop` on any push touching `web/**`. Wait for the run, then `curl -sI` the page and grep for a new id.
6. Artifact copy for review: same file with sibling hrefs made absolute (`https://hellblazer.github.io/nexus/...`) and the GoatCounter tag removed, published with the same `url` each time.
