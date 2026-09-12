---
name: site-page
description: Use when adding, rewriting, reviewing, or publishing a page under web/ on the GitHub Pages site (hellblazer.github.io/nexus), or when writing a docs/exploration essay about a subsystem. Carries the two page genres, the house template, the review gate, and the publish steps.
---

# Site page

Authority: this file. Origin: the five pages shipped 2026-09-10/11 (install, getting started, RDRs, research, tuple space) and the reviewer feedback on the tuple-space page.

## 1. Pick the genre first

Two genres exist. Never mix them in one document.

| Genre | Lives at | Shape | Reader wants |
|---|---|---|---|
| How-to | `web/<name>.html` | Numbered lessons with action titles. Each lesson: `You might say` block, `What happens` / `What you see`, rendered output in a dropdown. Mechanism in a collapsed appendix at the end. | To use it now. |
| Exploration essay | `docs/exploration/<idea>-in-nexus.md` | Lineage named in sentence one, the practical problem, how the suite leverages it, what we borrowed, what we left out, what it changes for you, further reading. Model: `docs/exploration/xanadu-in-nexus.md`. | To understand the thinking and what it means for them. |

History, earlier attempts, and design rationale belong in the essay, once. A how-to that narrates them was rejected by a reader ("a project development history lesson"); the same material in the essay genre was praised.

## 2. Register (both genres)

- Plain technical English for non-native readers. One idea per sentence, active voice, no semicolons, no idioms or phrasal verbs, one term per concept. Join related ideas with so / because / and. Do not atomise every sentence.
- Every paragraph and every section opener starts with its claim. Never two setup sentences and then the point.
- Headings are plain nouns or actions. No tropes ("The shape of the problem").
- Motivate a new thing at the edge of what already exists and works. Name the existing mechanism, what it does well, where it stops, what the new thing adds there. Never "none of these can". Check every claimed gap against the tools Sam uses: harness messaging and teammate panes, hooks, T1/T2/T3, files.
- When a word is both a verb and a noun, name the noun fully ("take operation").
- No corpus counts, incident dates, or self-reference to this project's incidents on a `web/` page. Exploration essays may carry dates and RDR numbers.
- Placeholders as `<span class="ph">&lt;ORANGE CAPITALS&gt;</span>`. Prompts are examples, not scripts.
- Rendered output blocks come from real tool or CLI output captured in the session, trimmed. Never invented.

## 3. House template (`web/`)

- Head block: copy lines 1-9 of `web/rdr.html` (doctype, lang, charset, viewport, description, color-scheme, favicon), then `<title>`, the Google Fonts link, the shared `<style>` copied byte-for-byte from `web/rdr.html`, then a page-local `<style>` that only adds, then the GoatCounter tag before `</head>` (see memory `project_site_analytics_goatcounter`).
- Body: `<div class="wrap">` with `nav.rail` (eyebrow, `ol#toc` whose entries match section ids and h2 text exactly, `.meta` links to the sibling pages and `./`), then `<main>` with `.topnav`, `h1`, `p.lede`, notes, the terms dropdown (`details.gloss`), numbered `section.lesson` blocks, `.footer`.
- Dropdowns: terms table, every `pre.out` block (`details.gloss.out` with the label as `summary`), and the appendix blocks. Instruction blocks (`div.cmd` with the Copy button) stay open.
- Figures: hand-drawn inline SVG only, `currentColor` strokes, brass for the reader's own step, `figure`+`figcaption`, labels off the lines. Render once with headless Chrome at 760 wide before publishing; below 500 wide Chrome clamps and crops, which is not overflow.
- CSS invariants: `pre{white-space:pre-wrap;overflow-wrap:anywhere}` after the base `pre` rule, `min-width:0` on every grid/flex wrapper, `minmax(0,1fr)` tracks, tables inside `.tbl`.
- Heading order never skips a level. A box title inside a section is `p.ttl`, not `h4`.

## 4. Review gate before publish

Dispatch both in parallel, then read the findings files yourself:

1. `conexus:substantive-critic`: every claim checked against **code**, not only docs (docs were stale on two claims on 2026-09-10); register for non-native readers; the how-to test in §1 (which paragraphs a user would skip, any history, proportion per section). Ask for a proposed outline if the shape is wrong.
2. `conexus:code-review-expert`: stylesheet byte-identical to `web/rdr.html`, rail vs ids, heading order, SVG label collisions, dark theme tokens, 400 px width, every href.

Do not edit the file while a reviewer is reading it. Apply findings in one pass. Then:

```bash
python3 .claude/skills/site-page/link_audit.py            # every href on every web/ page and README site links
```

Sentence scan (flag > 25 words that are not lists):

```bash
awk '/<main>/{m=1} /<script>/{m=0} m' web/<page>.html | sed 's/<[^>]*>//g' | tr -s ' \n' ' ' | grep -oE '[^.!?]+[.!?]' | awk 'NF>25{print NF": "$0}'
```

## 5. Publish

1. Fast-forward `develop` first; a peer pushes to the shared checkout all day.
2. New page: add the nav row to the topnav of `web/index.html` and the `.meta` rails of every other page, and a row in `README.md`'s site list.
3. Commit by explicit path. Put the commit in its own tool call, never after a `;` in a chain that edits (a failed edit step let a commit run on 2026-09-11).
4. `git fetch && git rebase --autostash origin/develop` (autostash carries a peer's stray uncommitted file), then `scripts/git-push-develop.sh <your shas>`.
5. Pages deploys from `develop` on any push touching `web/**`. Wait for the run, then `curl -sI` the page and grep for a new id.
6. Artifact copy for review: same file with sibling hrefs made absolute (`https://hellblazer.github.io/nexus/...`) and the GoatCounter tag removed, published with the same `url` each time.
