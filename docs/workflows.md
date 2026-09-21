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

`budget` is `{total, spent(), remaining()}` and it counts TOKENS, not agent
dispatches. `total` is null unless the user set a target, and the target is a
hard ceiling: once `spent()` reaches it, further `agent()` calls throw.

Neither script tries to pre-cap its own fan-out against it, because a count
of items cannot be compared to a token figure. Both log the remaining budget
before their fan-out when a target is set, and — this is the part that
matters — both report, by id, every item or finding whose chain did not
complete. A run that lost dispatches comes back with `complete: false` and a
populated `droppedIds` / `unverifiedFindings`, never as a clean table with
quiet holes in it.

Until 2026-09-21 both files claimed to scale fan-out down under a tight
budget. They could not: each tested `typeof budget.remaining === 'number'`,
which a method fails, so every read fell through to an `Infinity` fallback
and the cap never fired once (bead nexus-xeoa0).

## Honest caveats

- **Plugin distribution is unverified.** These files live in this repo's own
  `.claude/workflows/` directory, from which Claude Code does discover and
  list them by name for anyone with this repo checked out — discovery is
  what is confirmed, not execution; see the next bullet. Whether a Claude
  Code plugin can ship named
  workflows for distribution to *other* repositories is undocumented as of
  2026-08-22. Until that is confirmed one way or the other, treat these as
  repo-local tools, not something the `conexus` plugin currently exports.
- **Neither script has ever been run by the real Workflow tool.** They were
  authored in 2026-08 against a description of the contract rather than the
  contract itself, and both were broken from the first commit in the same
  way: each passed its stage functions as `pipeline`'s ITEMS array and a
  seed object as its only stage, so nothing downstream of that call had ever
  executed. Nothing in this repo loaded the files, so nothing could see it.
  Repaired on 2026-09-21 against the workflow-authoring reference (bead
  nexus-xeoa0), and `tests/scripts/test_claude_workflows.py` now runs both
  bodies against stub primitives that implement that reference.

  That test catches a misused primitive shape, a mishandled `null` dispatch,
  and a hole reported as a clean result. It does NOT prove the real runtime
  agrees with the reference, because the stubs encode the same reading of it
  that the scripts do. The first real invocation is still the first real
  evidence — run it on a low-stakes target and fix the header's signature
  block and the call sites together if anything differs.
- **A "dead" verdict is not a delete order.** Both the source pattern and
  `dead-wire-census.js` end with an evidence table, not an action. The real
  2026-08-19 census that this workflow is built from produced three
  different rulings on three different "dead" rows (keep-and-complete,
  delete, wire-as-a-feature) — a human still decides.
