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
complete. A run that lost dispatches comes back with `complete: false` and
the loss named, never as a clean table with quiet holes in it. The fields are
per script and they are not interchangeable:

- `dead-wire-census` emits `droppedIds` (an item with no trace result) and
  `unverifiedCandidateIds` (a candidate-dead item whose adversarial second
  look never landed).
- `pressure-test` emits `unverifiedFindings` (a finding no verification vote
  landed on) and `synthesisDropped` with `unrankedSurvivors` (findings that
  survived refutation when the ranking dispatch itself was lost).

`agent()` fails in two ways and they need different handling: it RESOLVES
null when a dispatch is skipped or dies terminally, and it THROWS once the
budget is spent. `parallel()` converts a thrown thunk into a null element, so
every dispatch inside one is already covered; a pipeline stage that throws
instead drops its whole item, and a bare top-level `await agent(...)` throws
out of the script entirely. Both scripts catch at the two sites where that
applied — `dead-wire-census`'s verify stage and `pressure-test`'s synthesis —
so a budget spent late in a run costs the work that had not happened yet and
not the work already paid for.

The throw path is exercised: `workflow_harness.mjs` takes an agent result of
`{"__throw": "..."}`, and both catches carry a test that was proved
non-vacuous by removing the catch and watching it fail. Until 2026-09-21 the
stub could only resolve, which is how both unprotected sites passed a green
suite.

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
- **Both scripts were broken from their first commit until 2026-09-21, and
  only one of them has since been run for real.** They were authored in
  2026-08 against a description of the contract rather than the contract
  itself, and both failed the same way: each passed its stage functions as
  `pipeline`'s ITEMS array and a seed object as its only stage, so nothing
  downstream of that call had ever executed. Nothing in this repo loaded the
  files, so nothing could see it. Repaired against the workflow-authoring
  reference under bead nexus-xeoa0.

  `dead-wire-census.js` has now run under the real Workflow tool, over the
  nexus-catalog link-graph tool surface: 11 agents, no errors, 7 items
  enumerated and traced, 3 candidate-dead rows adversarially verified,
  `complete: true`. That run is what confirms the result channel is a bare
  top-level `return` (both files had guessed `export default result`), that
  `pipeline(items, ...stages)` carries each item through both stages with the
  conditional verify stage dispatching only for candidates, and that `log()`
  and agent()'s label/phase/schema/effort opts behave as documented.

  **`pressure-test.js` has NOT been run** (bead nexus-wvquh). It shares the
  repaired idioms, and
  `tests/scripts/test_claude_workflows.py` exercises both bodies against stub
  primitives, but a stub can only confirm the reading of the reference the
  scripts were written from. Treat its first invocation as its first real
  evidence, and fix its header's signature block and its call sites together
  if anything differs.
- **A "dead" verdict is not a delete order.** Both the source pattern and
  `dead-wire-census.js` end with an evidence table, not an action. The real
  2026-08-19 census that this workflow is built from produced three
  different rulings on three different "dead" rows (keep-and-complete,
  delete, wire-as-a-feature) — a human still decides.
