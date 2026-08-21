# Writing style

*Write the way you would explain it to a colleague across the desk, out loud, with the numbers in front of you.*

This applies to everything written under the project's name: docs, RDRs, CHANGELOG sections, blog posts, commit messages, PR bodies, bead descriptions, T2 write-backs, and replies posted on the maintainer's behalf. `nx prose lint` checks the part a regex can check; the rest is judgment, and the review questions at the end are how a reviewer applies it.

The rules are stated as targets, not bans. Converging practitioner evidence, graded B/C in the research record (T3 `research-ai-slop-prose-removal-2026-08-21` §3), says a bare "don't" under-performs a positive instruction; no single controlled study settles it. Each rule therefore says what to write, and the lint below is the mechanical backstop, not the mechanism.

## The register

- ***Lead with the outcome.*** First sentence answers "what happened" or "what is this." Detail follows for readers who want it. Never restate the question, the heading, or the input before answering.
- ***Verbs carry the action; actors are subjects.*** "We assessed the store" rather than "an assessment of the store was conducted." Find the verb buried in the -tion/-ment noun and make it the main verb (Williams & Bizup). Passive is fine when the actor is unknown or irrelevant: "the bridge was built in 1932."
- ***Short sentences carry conclusions; longer ones carry the evidence chain.*** Vary length. Uniform sentence length is a measured marker of generated text (research §1, Northeastern study), and it is part of what makes a wall of text.
- ***Numbers with their conditions.*** "p50 80s, p95 217s, n=142" rather than "significant latency." Never an estimate presented as a measurement.
- ***Name the source.*** "Williams (1990) argues" rather than `some argue`. If there is no one to name, drop the appeal.
- ***Say the plain word.*** Use, cover, careful, fits. Not `leverage`, `delve`, `meticulous`, `seamless` (measured LLM markers, research §1). Instead of `load-bearing`, rewrite the sentence to say what depends on what; a one-word swap usually says something different.
- ***Commit or cut.*** One hedge per sentence at most (the lint rule counts per sentence). "This may help" is a claim; `it could potentially be argued that this may sometimes help` is not.
- ***Connect with punctuation that has a job.*** Colon to introduce, parentheses for an aside, period to stop. Semicolons sparingly: GOV.UK's reader-tested guidance avoids them, and a period usually works. No em dashes, and do not launder a dash into a colon chain; a heading or sentence with two colons needs a rewrite, not a swap.
- ***State the fact, skip the maxim.*** No aphorisms, no chiasmus, no `it's not X, it's Y` unless there is a real tension and both halves are true. No closing line that restates the body.
- ***Start with the content.*** No `Great question`, no `Certainly`, no `I'd be happy to`.

## Prose or list

Use prose for argument and a list for parallel items.

An argument has dependencies: this caused that, which rules out the other. Bullets flatten those relationships into a row of equals (Tufte). If you strip the bullets and the paragraph still reads, it was prose wearing bullets. Write it as prose.

A list is for things that are genuinely parallel and have no internal argument: options, steps, a checklist, the files touched. GOV.UK's reader testing shows bulleted steps beat prose for procedures. Where the items are labeled facets of one thing, use `- ***Label*** because ...` inside a real list, not four paragraphs each opening with a bold term.

Headings exist for navigation. Cut a heading no reader would jump to.

## An exemplar

> The gate failed twice on the same tree, and for two different reasons. The first was a stale jar: `build-gate-jar.sh` had not run after the pull, so the freshness stamp was from the previous `service/` commit. The second was a lease race in `SharedCluster`, where two workers both read an empty lease file and both proceeded. We fixed the stamp by running the script in the gate's preflight. The race needed an `O_EXCL` create; 14 runs since, zero repeats.

Six sentences, 9 to 31 words each. Outcome first, causes second, fix third, evidence last. Every number carries its condition. The incident is constructed for the example; in real prose those numbers must be measurements.

## What the lint checks

`nx prose lint PATHS` runs the regex-checkable subset: em dashes, `load-bearing`, the marker lexicon, the contrast frame, formulaic closers, sycophantic openers, hedge stacks, vague attribution. `tests/test_prose_style_lint.py` (`uv run pytest -m lint`) holds the RDR templates, this spec, and the `[Unreleased]` CHANGELOG section at zero findings, and everything else (docs/ and blog/ trees, README, active RDRs) to the ratchet below. Closed RDRs and historical CHANGELOG sections are exempt: rewriting them would cost git-blame for no reader benefit (nexus-ibdl). Existing prose is never mechanically rewritten to satisfy the lint; that experiment was run, reviewed, and reverted (T2 `ptwm2-rdr-sweep-editorial-fable-2026-08-21`).

Existing files with findings are recorded in `docs/.prose-baseline.json`. The baseline is a ratchet: a file may never exceed its number, and when its count drops the baseline must be lowered to match (`uv run python -m tests.test_prose_style_lint` rewrites it from the gate's own file set). New files start at zero.

The RDR gate (`nx rdr preamble rdr-gate`) runs the same lint on the RDR being gated and blocks Layer 1 on any finding, in repos that ship this file. `--skip-prose` is the audited override, the same contract as `--skip-gaps`.

Readability scores are not a target. Flesch and Fog measure syllables and sentence length; chopping sentences raises the score without making the text clearer.

## Review questions

For a reviewer or the substantive critic, after the lint pass:

- Delete the first sentence of each paragraph. Was anything lost?
- Strip the bullets. Does the argument still read, or was structure hiding a gap?
- Read it aloud. Which sentence would nobody say to a colleague?
- Is every number carrying its condition (n, box, date, what was held fixed)?
- Is each `not X, it's Y` a real tension, or decoration?
- Does sentence length vary, or is the whole thing one cadence?
