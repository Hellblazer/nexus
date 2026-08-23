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
// ASSUMED PRIMITIVE SIGNATURES — see pressure-test.js's header for the full
// rationale; the short version, unchanged here:
//   - `args` is an ambient global carrying this invocation's arguments.
//   - `agent(prompt, opts)` dispatches one subagent call, schema-validated.
//   - `pipeline(items, ...stages)` runs each ITEM through the stages
//     independently, with no barrier between stages — the default shape
//     for per-item work. (An earlier draft misread this as a sequential
//     reduce-with-accumulator and serialized the Trace phase; wave code
//     review [23363] caught it.)
//   - `parallel(thunks)` runs an array of async thunks concurrently and
//     waits for all of them (a TRUE BARRIER) — used for Trace (independent
//     items, results collected once) and Verify (Census needs every
//     verified "dead" candidate's outcome at once).
//   - `budget` is a global exposing remaining agent-dispatch capacity, read
//     defensively since its exact shape is unconfirmed.
//   - `log(message)` is a global that writes a note into the workflow's run
//     log/observability trail, independent of the returned value — used
//     here to satisfy "no silent caps: log dropped items."
//   - This script ends with `export default result` rather than a bare
//     top-level `return`, because `export const meta = {...}` already makes
//     this file an ES module. If the real Workflow tool expects the result
//     some other way, only the last line needs to change.
//   - No `Date.now()` / `Math.random()` anywhere in this file.

export const meta = {
  name: 'dead-wire-census',
  description:
    'Enumerate a surface (MCP tools, CLI verbs, skills, or HTTP routes), trace each item to its consumers, adversarially re-check every "dead" verdict, and return an evidence-backed census table.',
  whenToUse:
    'When checking a surface for built-but-disconnected work: after a refactor that may have orphaned call sites, before a deletion pass, or on the recurring cadence this project already runs by hand (see T2 nexus/engine-dead-wire-census-2026-08-19 and nexus/dead-wire-census-dispositions-2026-08-19 for a real run and its outcome). Every "dead" row here is evidence for a human decision, not a delete order — the same census produced a KEEP, a DELETE, and a WIRE-AS-A-FEATURE ruling on three different rows.',
  phases: ['enumerate', 'trace', 'verify', 'census'],
};

// args:
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

function budgetRemaining() {
  // `budget` is a documented global whose exact shape is not pinned down
  // anywhere in this repo. Read it defensively rather than assume a shape.
  if (typeof budget === 'number') return budget;
  if (
    typeof budget === 'object' &&
    budget !== null &&
    typeof budget.remaining === 'number'
  ) {
    return budget.remaining;
  }
  return Infinity;
}

if (!args.surface) {
  throw new Error('dead-wire-census requires args.surface');
}
const surfaceDescription = SURFACE_PROMPTS[args.surface] ?? args.surface;

const enumerateStage = async (ctx) => {
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
  return { ...ctx, items: enumeration.items };
};

const traceStage = async (ctx) => {
  // parallel() thunks: each item's trace is independent, so all traces run
  // concurrently; the census phase later needs the collected array anyway,
  // so the barrier costs nothing extra.
  const traceFns = ctx.items.map((item) => async () => {
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
    return traced;
  });
  const traced = (await parallel(traceFns)).filter(Boolean);
  return { ...ctx, traced };
};

const verifyStage = async (ctx) => {
  const candidateDead = ctx.traced.filter(
    (t) => t.classification === 'dead' || t.classification === 'suspected'
  );

  if (candidateDead.length === 0) {
    return { ...ctx, verified: [] };
  }

  const remaining = budgetRemaining();
  if (candidateDead.length > remaining) {
    // No silent caps: if the budget cannot cover a second look at every
    // candidate, say so loudly rather than quietly verifying a subset and
    // presenting it as complete.
    log(
      `dead-wire-census: ${candidateDead.length} candidate-dead item(s) need adversarial verification but only ${remaining} agent dispatch(es) remain in budget. Verifying the first ${remaining} by trace order; the rest keep their unverified trace-stage classification in the census, flagged as such.`
    );
  }
  const toVerify =
    candidateDead.length > remaining
      ? candidateDead.slice(0, Math.max(remaining, 0))
      : candidateDead;
  const skipped = candidateDead.slice(toVerify.length);
  if (skipped.length > 0) {
    log(
      `dead-wire-census: skipped verification for (budget-limited): ${skipped
        .map((s) => s.id)
        .join(', ')}`
    );
  }

  // True barrier: every "dead"/"suspected" candidate gets one adversarial
  // second look, dispatched together, because Census needs the whole
  // overturned/upheld set at once. This is the project's own
  // unused-is-not-useless rule applied mechanically: "no caller found" is a
  // starting hypothesis, not a verdict, until someone has tried to break it.
  const verified = await parallel(
    toVerify.map((c) =>
      agent(
        `A trace concluded this item is "${c.classification}": ${c.id}\n\nEvidence given: ${c.evidence}\n\nTry to prove this wrong: look for indirect callers (reflection, config-driven dispatch, string-built call sites, a caller in a different repo, ops tooling, a deploy script), and check whether "unused" here actually means "useful but not yet wired" rather than "safe to delete." State your verdict and whether the original classification stands.`,
        {
          label: `verify:${c.id}`,
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
      )
    )
  );
  return { ...ctx, verified, skippedVerification: skipped.map((s) => s.id) };
};

const censusStage = async (ctx) => {
  const verifiedById = new Map(ctx.verified.map((v) => [v.id, v]));
  const skippedIds = new Set(ctx.skippedVerification ?? []);
  const droppedNoVerification = [];

  const rows = ctx.traced.map((t) => {
    const verification = verifiedById.get(t.id);
    const wasCandidate =
      t.classification === 'dead' || t.classification === 'suspected';
    if (wasCandidate && !verification && !skippedIds.has(t.id)) {
      // Should not happen if verifyStage covered every candidate not
      // explicitly skipped for budget reasons — log rather than silently
      // drop, per this project's vacuous-gate doctrine: a sweep that finds
      // nothing to check is a failure to surface, not a quiet pass.
      droppedNoVerification.push(t.id);
    }
    return {
      id: t.id,
      location: ctx.items.find((i) => i.id === t.id)?.location ?? 'unknown',
      classification:
        verification && !verification.upheld
          ? 'live (overturned on verify)'
          : t.classification,
      evidence: t.evidence,
      verification: verification?.reasoning ?? null,
      verificationSkipped: skippedIds.has(t.id),
    };
  });

  if (droppedNoVerification.length > 0) {
    log(
      `dead-wire-census: ${droppedNoVerification.length} candidate-dead item(s) had no verification row and no recorded skip reason — kept at their pre-verify classification, flagged for follow-up: ${droppedNoVerification.join(', ')}`
    );
  }

  return { ...ctx, rows, droppedNoVerification };
};

const finalCtx = await pipeline(
  [enumerateStage, traceStage, verifyStage, censusStage],
  {}
);

const result = {
  surface: args.surface,
  itemCount: finalCtx.items.length,
  rows: finalCtx.rows,
  skippedVerificationCount: (finalCtx.skippedVerification ?? []).length,
  droppedCount: finalCtx.droppedNoVerification.length,
};

export default result;
