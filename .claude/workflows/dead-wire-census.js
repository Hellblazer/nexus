// dead-wire-census.js
//
// Named workflow: the recurring "built but disconnected" sweep this project
// runs by hand — enumerate a surface, trace each item to its consumers,
// adversarially re-check every "dead" verdict, and return an evidence-backed
// census table. Extracted from a real run recorded in nx T2 memory (project
// "nexus"):
//   - engine-dead-wire-census-2026-08-19        [22850] the census itself
//     (293 engine routes vs 276 client path literals; CONFIRMED and
//     SUSPECTED buckets, each row backed by a grep or a probe)
//   - dead-wire-census-dispositions-2026-08-19  [22854] what happened next:
//     some "dead" rows were ruled KEEP-AND-COMPLETE, one was ruled DELETE,
//     one was ruled WIRE-AS-A-FEATURE — proof that a census is evidence for
//     a human decision, never a delete order by itself.
// Also draws on this project's own hot rule (unused-is-not-useless): "dead
// things have ONE live dependent" and a missing caller in one search is a
// hypothesis, not a verdict, until someone tries to break it.
// Bead: nexus-hhqli (epic nexus-qkbo7, "ship named Workflows in the plugin").
//
// OPEN QUESTION (plugin distribution): this file lives in the repo's own
// `.claude/workflows/` directory, which works for anyone with this repo
// checked out. Whether a Claude Code plugin can ship named workflows for
// distribution to OTHER repos is undocumented as of 2026-08-22 — see
// docs/workflows.md. Until that is confirmed, this workflow is repo-local
// only.
//
// OPEN QUESTION (file convention): no existing file or doc in this repo
// names a convention for workflow scripts, so this uses a plain `.js`
// extension (no TypeScript, per the Workflow tool's own contract: "plain JS
// (no TS)"). If a different convention is established later, migrate this
// file rather than assuming `.js` is settled.
//
// PRIMITIVE SIGNATURES — read from the workflow-authoring reference on
// 2026-09-21 (bead nexus-xeoa0), which supersedes the ASSUMED block that
// stood here. This file had NEVER EXECUTED: it called
// `pipeline([stageFns], {})`, passing its four stage functions as the ITEMS
// array and `{}` as its only stage. `{}` is not callable, so nothing after
// that line had ever run. The header's own note that an earlier draft
// misread `pipeline` the same way, and that wave code review [23363] caught
// it, survived into the file while the correction did not.
//   - `args` is an ambient global carrying this invocation's arguments, or
//     `undefined` when none were passed.
//   - `agent(prompt, opts)` dispatches one subagent call. With `opts.schema`
//     it resolves to the validated object. It resolves to `null` — it does
//     NOT throw — when the user skips the agent or the subagent dies on a
//     terminal API error, so every result is null-checked here.
//   - `pipeline(items, ...stages)` runs each ITEM through the stages with NO
//     barrier between stages: item A can be in stage 2 while item B is still
//     in stage 1. Stage N receives `(prevResult, originalItem, index)`. A
//     stage that throws drops that item to `null` and skips its remaining
//     stages, so the result array stays positionally aligned with `items`.
//     This is the shape the Trace -> Verify chain wants: verifying item A
//     needs item A's trace and nothing else.
//   - `parallel(thunks)` is a TRUE BARRIER. Nothing here needs one: the only
//     step that needs every item at once is the census reduction, which is
//     plain code, not a dispatch.
//   - `budget` is `{total: number|null, spent(), remaining()}` in TOKENS —
//     `remaining` is a METHOD, and `total` is null when the user set no
//     target. The retired `budgetRemaining()` here tested
//     `typeof budget.remaining === 'number'`, which a method fails, so it
//     always returned Infinity and the "no silent caps" cap it guarded never
//     once fired. It also compared a COUNT OF ITEMS against a TOKEN figure.
//     Both are gone; drops are now detected after the fact from the pipeline
//     result, which is something this file can actually observe.
//   - `log(message)` writes a narrator line into the run's progress display,
//     independent of the returned value — used here for "no silent caps".
//   - The script body runs inside an async function, so the result is a bare
//     top-level `return`. The earlier `export default result` was the other
//     half of the same unverified guess.
//   - No `Date.now()` / `Math.random()` anywhere in this file; the runtime
//     throws on them because they would break resume.

export const meta = {
  name: 'dead-wire-census',
  description:
    'Enumerate a surface (MCP tools, CLI verbs, skills, or HTTP routes), trace each item to its consumers, adversarially re-check every "dead" verdict, and return an evidence-backed census table.',
  whenToUse:
    'When checking a surface for built-but-disconnected work: after a refactor that may have orphaned call sites, before a deletion pass, or on the recurring cadence this project already runs by hand (see T2 nexus/engine-dead-wire-census-2026-08-19 and nexus/dead-wire-census-dispositions-2026-08-19 for a real run and its outcome). Every "dead" row here is evidence for a human decision, not a delete order — the same census produced a KEEP, a DELETE, and a WIRE-AS-A-FEATURE ruling on three different rows.',
  // One entry per progress group, titles matched exactly against the `phase`
  // passed in each agent() call. The census reduction has no entry because it
  // dispatches no agent.
  phases: [
    { title: 'enumerate', detail: 'list every item on the surface' },
    { title: 'trace', detail: 'one agent per item, find its consumers' },
    { title: 'verify', detail: 'adversarial second look at every dead verdict' },
  ],
};

// args: an object with the fields below, OR a single STRING -- either JSON
//   text for the object form, or "key: value" lines (surface/scopeHints) --
//   see parseWorkflowStringArgs below, which the top of this file's body
//   normalizes into the object form before anything else runs (nexus-kk4ut:
//   the Skill tool's own `args` parameter is typed as a string, so a
//   natural-language invocation like "Run the dead-wire-census workflow
//   over the MCP tool surface" (docs/workflows.md's own example) lands here
//   as text, not an object).
//   surface: 'mcp-tools' | 'cli-verbs' | 'skills' | 'routes' | string
//     A known surface name (see SURFACE_PROMPTS below), or a free-text
//     description/glob of a custom surface (e.g. "engine HTTP handlers under
//     service/src/main/java/.../handlers").
//   scopeHints: string (optional) - extra context narrowing where to look
//     (a directory, a package prefix, a naming convention) so the enumerate
//     agent does not have to guess at repo layout.

const SURFACE_PROMPTS = {
  'mcp-tools':
    'every MCP tool this project registers (grep for tool registrations/decorators across its MCP server modules)',
  'cli-verbs':
    'every `nx` CLI command and subcommand (src/nexus/commands/**, the add_command calls in src/nexus/cli.py)',
  skills: "every skill file under this project's plugin(s) skills/ directories",
  routes:
    'every engine HTTP route (service/.../*Handler.java route registrations)',
};

const CLASSIFICATION_SCHEMA = {
  enum: ['live', 'dead', 'inert-in-mode', 'suspected'],
};

const isCandidateDead = (classification) =>
  classification === 'dead' || classification === 'suspected';

// >>> SHARED: parseWorkflowStringArgs (nexus-kk4ut) >>>
//
// Kept BYTE-IDENTICAL in pressure-test.js and dead-wire-census.js -- a test
// in tests/scripts/test_claude_workflows.py extracts this block from both
// files (by these >>> / <<< markers) and asserts they match exactly. Why
// duplicated instead of imported from one shared module: the reference
// workflow runtime (tests/scripts/fixtures/workflow_harness.mjs, built from
// the workflow-authoring reference) treats a script's ENTIRE source as the
// BODY of one AsyncFunction (`new AsyncFunction('agent', ..., 'args',
// source)`) -- a top-level `import` there is a SyntaxError, and whether the
// real runtime resolves a dynamic `import()` against a sibling file under
// .claude/workflows/ is undocumented. Guessing at an unverified runtime
// primitive is exactly what cost this pair of files two rounds already
// (2026-09-21, nexus-xeoa0: both called `pipeline([stageFns], {})`,
// mistaking their own stage functions for `pipeline`'s items array, and
// neither had ever executed). A duplicated, pinned-identical function is a
// known-good primitive; an unverified shared module is not.
//
// Normalizes a workflow's `args` when it arrives as a single STRING rather
// than the documented object -- the Skill tool's own `args` parameter is
// typed as a string in its JSON schema, so any natural-language invocation
// forwarded through it (docs/workflows.md's own "Use the pressure-test
// workflow on..." example) lands here as text. Two failure-avoiding
// decisions:
//
//  1. Try JSON.parse FIRST. A caller who needs an exact value containing
//     "key:"-shaped text (a YAML/JSON diff, literally) passes a JSON object
//     string and gets it back verbatim -- no parsing ambiguity at all.
//  2. In the key:value fallback, a key is recognized ONLY when its line
//     starts with it at COLUMN 0 (`^key\s*:`). An indented line ("  spec:"),
//     a diff-prefixed line ("+spec:", "-spec:"), or any other non-flush-left
//     line is ALWAYS a continuation of the current value, never a new key.
//     This is what makes a pasted diff safe as a target: diff output is
//     never flush left except for its own +/-/space markers, and those do
//     not spell a recognized key either. (Reviewer-reproduced failure,
//     nexus-kk4ut: a YAML diff target containing an indented "  spec:" line
//     used to truncate the target there.)
//
// `keys` orders the recognized field names; `keys[0]` is also what a string
// with NO recognized key at all is treated as (the whole string verbatim --
// "pressure-test this diff" / "census the CLI verbs" are the common
// one-line calls). `numericKeys` lists which parsed fields get coerced with
// `Number(...)`.
function parseWorkflowStringArgs(raw, keys, numericKeys) {
  const trimmed = raw.trim();
  if (trimmed.startsWith('{')) {
    try {
      const asJson = JSON.parse(trimmed);
      if (asJson && typeof asJson === 'object' && !Array.isArray(asJson)) {
        return asJson;
      }
    } catch {
      // Not valid JSON -- fall through to the key: value form.
    }
  }
  const keyLineRe = new RegExp(`^(${keys.join('|')})\\s*:\\s*(.*)$`);
  const valueLines = {};
  let currentKey = null;
  let sawAnyKey = false;
  for (const line of raw.split('\n')) {
    const m = keyLineRe.exec(line);
    if (m) {
      sawAnyKey = true;
      currentKey = m[1];
      valueLines[currentKey] = [m[2]];
    } else if (currentKey) {
      valueLines[currentKey].push(line);
    }
  }
  if (!sawAnyKey) {
    return { [keys[0]]: raw.trim() };
  }
  const parsed = {};
  for (const key of Object.keys(valueLines)) {
    parsed[key] = valueLines[key].join('\n').trim();
  }
  for (const key of numericKeys ?? []) {
    if (parsed[key] !== undefined) {
      const n = Number(parsed[key]);
      if (!Number.isNaN(n)) {
        parsed[key] = n;
      }
    }
  }
  return parsed;
}
// <<< SHARED: parseWorkflowStringArgs <<<

if (typeof args === 'string') {
  args = parseWorkflowStringArgs(args, ['surface', 'scopeHints'], []);
}

if (!args || !args.surface) {
  throw new Error('dead-wire-census requires args.surface');
}
const surfaceDescription = SURFACE_PROMPTS[args.surface] ?? args.surface;

// --- Enumerate -------------------------------------------------------------
// One dispatch, no fan-out, so it runs before the pipeline rather than as a
// stage of it: the pipeline's items ARE this step's output.

const enumeration = await agent(
  `Enumerate every item on this surface: ${surfaceDescription}${
    args.scopeHints ? `\n\nScope hints: ${args.scopeHints}` : ''
  }\n\nList every item you find, even ones you suspect are already dead or test-only. Do not filter at this stage — filtering happens later, with evidence.`,
  {
    label: 'enumerate',
    phase: 'enumerate',
    // The only sanctioned effort override in this file: this step is
    // mechanical (listing what exists), not judgment work.
    effort: 'low',
    schema: {
      type: 'object',
      required: ['items'],
      properties: {
        items: {
          type: 'array',
          items: {
            type: 'object',
            required: ['id', 'location'],
            properties: {
              id: { type: 'string' },
              location: { type: 'string' },
            },
          },
        },
      },
    },
  }
);

if (!enumeration) {
  throw new Error(
    'dead-wire-census: the enumerate agent returned no result (skipped, or a terminal error). Nothing to census.'
  );
}
const items = enumeration.items ?? [];
if (items.length === 0) {
  // The vacuous-gate doctrine: a sweep that found nothing to check is a
  // failure to surface, not a quiet pass.
  log(
    `dead-wire-census: the enumerate agent found ZERO items on surface "${args.surface}". This is a failed enumeration, not an empty surface — treat the run as inconclusive.`
  );
}
log(`dead-wire-census: enumerated ${items.length} item(s) on "${args.surface}".`);
if (budget.total) {
  log(
    `dead-wire-census: ${Math.round(
      budget.remaining() / 1000
    )}k tokens remain of a ${Math.round(
      budget.total / 1000
    )}k target. Agent dispatches throw once the target is reached; any item whose chain is cut off that way is reported in droppedIds below rather than silently omitted.`
  );
}

// --- Trace -> Verify -------------------------------------------------------
// pipeline(), not a barrier: verifying item A needs item A's trace and
// nothing else, so item A can be under adversarial verification while item B
// is still being traced.

const traceStage = async (item) => {
  const traced = await agent(
    `Find every consumer of this item: ${item.id} (defined at ${item.location}).\n\nSearch the whole client surface, not just the immediate directory. Classify as one of: live (has a real caller), dead (zero callers anywhere), inert-in-mode (has callers, but only reachable in a mode/config this install does not run), or suspected (you found no caller but did not exhaustively search every mode). Cite the grep/search evidence for the classification.`,
    {
      label: `trace:${item.id}`,
      phase: 'trace',
      schema: {
        type: 'object',
        required: ['id', 'classification', 'evidence'],
        properties: {
          id: { type: 'string' },
          classification: CLASSIFICATION_SCHEMA,
          evidence: { type: 'string' },
        },
      },
    }
  );
  return { item, traced };
};

const verifyStage = async (prev) => {
  const { item, traced } = prev;
  if (!traced || !isCandidateDead(traced.classification)) {
    return { item, traced, verification: null };
  }

  // This project's own unused-is-not-useless rule applied mechanically: "no
  // caller found" is a starting hypothesis, not a verdict, until someone has
  // tried to break it.
  //
  // The catch is not defensive padding. `agent()` has TWO failure modes and
  // they are not interchangeable: it RESOLVES null when a dispatch is skipped
  // or dies terminally, and it THROWS when the token budget is exhausted.
  // A throw here would propagate out of this stage, and the reference is
  // explicit that a stage which throws drops its item to null and skips the
  // rest of its chain — so an exhausted budget during verify would discard
  // `traced`, which already succeeded, and the item would be reported as
  // "trace did not complete". That is a false statement about what happened.
  // Swallowing the throw into `verification: null` routes the item into the
  // unverified-candidate path below, which is the accurate description: the
  // trace stands, the adversarial second look did not happen.
  let verification = null;
  try {
    verification = await agent(
      `A trace concluded this item is "${traced.classification}": ${traced.id}\n\nEvidence given: ${traced.evidence}\n\nTry to prove this wrong: look for indirect callers (reflection, config-driven dispatch, string-built call sites, a caller in a different repo, ops tooling, a deploy script), and check whether "unused" here actually means "useful but not yet wired" rather than "safe to delete." State your verdict and whether the original classification stands.`,
      {
        label: `verify:${item.id}`,
        phase: 'verify',
        schema: {
          type: 'object',
          required: ['id', 'upheld', 'reasoning'],
          properties: {
            id: { type: 'string' },
            upheld: { type: 'boolean' },
            reasoning: { type: 'string' },
          },
        },
      }
    );
  } catch (err) {
    log(
      `dead-wire-census: verify dispatch for ${item.id} threw (${
        err && err.message ? err.message : err
      }) — the trace stands and the item is reported as an unverified candidate, not as a dropped trace.`
    );
  }
  return { item, traced, verification };
};

const chains = await pipeline(items, traceStage, verifyStage);

// --- Census ----------------------------------------------------------------
// Plain reduction over the whole result set. `chains` is positionally aligned
// with `items`, so an item whose chain was dropped is still identifiable by
// index — which is what lets a drop be reported by id instead of vanishing.

const droppedIds = [];
const unverifiedCandidateIds = [];

const rows = items.map((item, index) => {
  const chain = chains[index];
  if (!chain || !chain.traced) {
    droppedIds.push(item.id);
    return {
      id: item.id,
      location: item.location,
      classification: 'unknown (trace did not complete)',
      evidence: null,
      verification: null,
      traceDropped: true,
      verificationMissing: false,
    };
  }

  const { traced, verification } = chain;
  const wasCandidate = isCandidateDead(traced.classification);
  if (wasCandidate && !verification) {
    unverifiedCandidateIds.push(item.id);
  }

  return {
    // `item.id`, never `traced.id`: the enumerated id is ground truth, and
    // the trace agent's echoed one is a value a model retyped. They are the
    // same string until they are not, and `droppedIds` /
    // `unverifiedCandidateIds` in this same payload are keyed on `item.id` —
    // so keying rows off the echo is what would break the cross-reference.
    id: item.id,
    location: item.location,
    classification:
      verification && !verification.upheld
        ? 'live (overturned on verify)'
        : traced.classification,
    evidence: traced.evidence,
    verification: verification ? verification.reasoning : null,
    traceDropped: false,
    verificationMissing: wasCandidate && !verification,
  };
});

if (droppedIds.length > 0) {
  log(
    `dead-wire-census: ${droppedIds.length} item(s) never got a trace result — the dispatch was skipped, errored terminally, or ran out of token budget. These are NOT clean rows; they are holes in the census: ${droppedIds.join(', ')}`
  );
}
if (unverifiedCandidateIds.length > 0) {
  log(
    `dead-wire-census: ${unverifiedCandidateIds.length} candidate-dead item(s) got no adversarial second look — kept at their pre-verify classification and flagged for follow-up: ${unverifiedCandidateIds.join(', ')}`
  );
}

return {
  surface: args.surface,
  itemCount: items.length,
  rows,
  droppedIds,
  unverifiedCandidateIds,
  // A census with holes is not a complete census. The caller should read this
  // before treating any "dead" row as settled.
  complete:
    items.length > 0 &&
    droppedIds.length === 0 &&
    unverifiedCandidateIds.length === 0,
};
