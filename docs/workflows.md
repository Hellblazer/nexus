# Named workflows

`.claude/workflows/` holds JavaScript scripts for the Claude Code Workflow
tool: a script that orchestrates several subagents to do work that fans out
across many independent pieces (an audit over many files, a multi-lens
review, a migration) — written as code instead of delegated turn by turn.

## What is here

- `pressure-test.js` — run several reviewers with distinct lenses against a
  target and a spec, adversarially verify every finding by majority vote,
  then synthesize a ranked verdict. Built from two real review batteries
  this project ran by hand in one week (nexus-3fab5, nexus-ptwm2; see the
  file's header comment for the exact T2 records).
- `dead-wire-census.js` — enumerate a surface (MCP tools, CLI verbs, skills,
  HTTP routes), trace each item to its consumers, adversarially re-check
  every "dead" verdict, and return an evidence-backed census table. Built
  from the 2026-08-19 engine dead-wire census (T2 `nexus/engine-dead-wire-census-2026-08-19`,
  `nexus/dead-wire-census-dispositions-2026-08-19`).

Bead: nexus-hhqli (epic nexus-qkbo7, "ship named Workflows in the plugin").

## How to invoke

The Workflow tool takes a `meta` block (name, description, `whenToUse`,
phases) and a script body. To run one of these by hand, ask Claude to run it
by name and supply the arguments the file's own comment block documents,
for example:

> Use the pressure-test workflow on this diff against the directive in
> RDR-XXX §Approach.

or

> Run the dead-wire-census workflow over the MCP tool surface.

Each file documents its own `args` shape in a comment above the executable
body — read that before invoking. A workflow is opt-in: nothing here runs
automatically, and naming it in a request is what tells Claude to load and
execute the script rather than do the same work turn by turn.

## Budget

Both scripts read a `budget` global defensively (its exact shape is not
pinned down anywhere in this repo yet) and scale down agent fan-out — fewer
verification votes, a logged skip — rather than silently ignoring a tight
budget. If you are running one of these on a target with an unusually large
number of findings or surface items, expect the agent count to grow with
that count; the ~15-agent default guideline is a starting point, not a hard
ceiling enforced by these files.

## Honest caveats

- **Plugin distribution is unverified.** These files live in this repo's own
  `.claude/workflows/` directory, which is confirmed to work for anyone with
  this repo checked out. Whether a Claude Code plugin can ship named
  workflows for distribution to *other* repositories is undocumented as of
  2026-08-22. Until that is confirmed one way or the other, treat these as
  repo-local tools, not something the `conexus` plugin currently exports.
- **Primitive signatures are assumed, not confirmed.** The Workflow tool's
  contract names `agent()`, `pipeline()`, `parallel()`, `budget`, and a
  `meta` export, but the exact call signatures used in these two files were
  authored against that contract description, not verified by running them
  (out of scope for the bead that produced them — see nexus-hhqli). Each
  file's header comment states its own assumptions explicitly. Before
  relying on either script, run it once on a low-stakes target and confirm
  the primitives behave as assumed; fix the assumptions section and the
  call sites together if they do not.
- **A "dead" verdict is not a delete order.** Both the source pattern and
  `dead-wire-census.js` end with an evidence table, not an action. The real
  2026-08-19 census that this workflow is built from produced three
  different rulings on three different "dead" rows (keep-and-complete,
  delete, wire-as-a-feature) — a human still decides.
