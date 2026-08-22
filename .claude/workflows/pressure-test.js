// pressure-test.js
//
// Named workflow: the review battery this project has run by hand twice in
// one week (2026-08-21), captured here so anyone in this repo can invoke it
// instead of re-deriving the same shape. Extracted from two real runs
// recorded in nx T2 memory (project "nexus"):
//   - review-3fab5-code-2026-08-21          [23307] code-mechanics review
//   - review-3fab5-critique-2026-08-21      [23308] spec-fidelity critique
//   - review-3fab5-fable-adversarial-2026-08-21 [23309] argue-the-revert-case
//   - review-3fab5-verify-2026-08-21        [23312] verification pass
//   - review-nexus-3fab5                    [23313] the completed battery
//   - ptwm2-critique-2026-08-21 / ptwm2-design-critique-fable-2026-08-21
//     (a second run of the same shape, same day)
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
// ASSUMED PRIMITIVE SIGNATURES (the model-facing contract names the
// primitives — agent(), pipeline(), parallel(), budget — but not their exact
// shapes; this file's exact use of them has not been run and should be
// checked against the real Workflow tool before first execution):
//   - `args` is an ambient global carrying this invocation's arguments.
//   - `agent(prompt, opts)` dispatches one subagent call and resolves to its
//     result, validated against `opts.schema` when given.
//   - `pipeline(fns)` runs an array of async functions as the workflow's
//     DEFAULT execution shape (sequential phases, or independent per-item
//     work with no barrier between items — this file uses it for sequential
//     phase composition, threading an accumulating context object through).
//   - `parallel(fns)` runs an array of async functions concurrently as a
//     TRUE BARRIER: every call must land before the caller proceeds. Used
//     only where the next phase genuinely needs every result at once.
//   - `budget` is a global exposing the run's remaining agent-dispatch
//     capacity. Its exact shape is unconfirmed, so `budgetRemaining()` below
//     reads it defensively and falls back to "no visible cap" rather than
//     guessing a shape that does not exist.
//   - This script ends with `export default result` rather than a bare
//     top-level `return`, because `export const meta = {...}` already makes
//     this file an ES module and a bare `return` is not legal at module top
//     level. If the real Workflow tool expects the result some other way
//     (a wrapping function, a `finish()`/`emit()` global), only the last
//     line needs to change.
//   - No `Date.now()` / `Math.random()` anywhere in this file, per the
//     determinism requirement.

export const meta = {
  name: 'pressure-test',
  description:
    'Run several reviewers with distinct lenses against a target and a spec, adversarially verify every finding by majority vote, then synthesize a ranked, deduplicated verdict.',
  whenToUse:
    'Before committing a change that carries real risk of a silent revert or a spec-fidelity gap: a diff implementing a verbatim directive, a design decision with more than one defensible reading, or any change under a "no more reverts" directive. Not for routine, low-risk edits — dispatch the two standing reviewers (code-review-expert, substantive-critic) for those instead; this workflow is heavier by design.',
  phases: ['review', 'verify', 'synthesize'],
};

// args:
//   target: string        - description of the change, or a diff/commit
//     range, that every lens reviews.
//   spec: string           - the verbatim directive or design decision text
//     the spec-fidelity lens checks the target against. Required: this is
//     what turns a generic code review into a pressure test rather than an
//     ordinary review.
//   lenses: Array<{name, instruction}> (optional) - override the three
//     default lenses below. Each entry becomes one review agent.
//   probe: string (optional) - an empirical question to test against a real
//     example from the target (e.g. "does the new gate question actually
//     change behavior on a real case?"), run as a fourth review arm. Omit to
//     skip it.
//   votesPerFinding: number (optional, default 3) - how many independent
//     adversarial-refute agents vote on each critical/significant finding.
//     Minor findings always get a single-pass check; scaling three-way
//     voting to every minor nit is not worth the agent count.

const DEFAULT_LENSES = [
  {
    name: 'code-mechanics',
    instruction:
      'Review the target for correctness bugs, missing edge cases, and security issues. Cite file:line for every finding.',
  },
  {
    name: 'spec-fidelity',
    instruction:
      'Compare the target against the verbatim directive given below, point by point. For each point, state whether the target implements it, partially implements it, or omits it, with file:line evidence. Do not credit an implementation you cannot point to in the diff.',
  },
  {
    name: 'adversarial-revert-case',
    instruction:
      'Argue the strongest case for reverting the target. Assume the change is wrong until the evidence says otherwise. Every finding must survive the question: would a defender of the change have a real answer for this, or does the claim just sound plausible?',
  },
];

const FINDING_SCHEMA = {
  type: 'object',
  required: ['severity', 'claim', 'evidence'],
  properties: {
    severity: { enum: ['critical', 'significant', 'minor'] },
    claim: { type: 'string' },
    evidence: { type: 'string' },
  },
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

const lenses = args.lenses ?? DEFAULT_LENSES;

const reviewStage = async (ctx) => {
  const reviewJobs = lenses.map((lens) =>
    agent(
      `${lens.instruction}\n\nTarget:\n${args.target}${
        lens.name === 'spec-fidelity' ? `\n\nVerbatim directive:\n${args.spec}` : ''
      }`,
      {
        label: `review:${lens.name}`,
        phase: 'review',
        schema: {
          type: 'object',
          required: ['lens', 'findings'],
          properties: {
            lens: { type: 'string' },
            findings: { type: 'array', items: FINDING_SCHEMA },
          },
        },
      }
    )
  );

  if (args.probe) {
    reviewJobs.push(
      agent(
        `Empirically test this claim against a real example pulled from the target — do not reason about it in the abstract, run or trace the actual case: ${args.probe}\n\nTarget:\n${args.target}`,
        {
          label: 'review:empirical-probe',
          phase: 'review',
          schema: {
            type: 'object',
            required: ['lens', 'findings'],
            properties: {
              lens: { type: 'string' },
              findings: { type: 'array', items: FINDING_SCHEMA },
            },
          },
        }
      )
    );
  }

  // True barrier: the verify stage needs every lens's findings at once, so
  // this is one of the few places parallel() (not pipeline()) is correct.
  const reviews = await parallel(reviewJobs);
  return { ...ctx, reviews };
};

const verifyStage = async (ctx) => {
  const allFindings = ctx.reviews.flatMap((r) =>
    (r.findings ?? []).map((f) => ({ ...f, lens: r.lens }))
  );

  if (allFindings.length === 0) {
    return { ...ctx, allFindings, survivingFindings: [] };
  }

  const requestedVotes = args.votesPerFinding ?? 3;
  const votesFor = (finding) =>
    finding.severity === 'minor' ? 1 : requestedVotes;

  const totalVoteAgents = allFindings.reduce(
    (sum, f) => sum + votesFor(f),
    0
  );
  const remaining = budgetRemaining();
  const scaleDown = totalVoteAgents > remaining && totalVoteAgents > 0;
  if (scaleDown) {
    log(
      `pressure-test: ${totalVoteAgents} verify agents would exceed the remaining budget (${remaining}); reducing every finding's vote count to 1. Verdicts on critical/significant findings are less robust this run.`
    );
  }

  // True barrier: every finding's votes must land before a majority can be
  // taken, and synthesis needs the whole surviving set at once.
  const voteResults = await parallel(
    allFindings.flatMap((finding, findingIndex) => {
      const votes = scaleDown ? 1 : votesFor(finding);
      return Array.from({ length: votes }, () =>
        agent(
          `A reviewer (lens: ${finding.lens}) claims:\n\n${finding.claim}\n\nEvidence given: ${finding.evidence}\n\nCheck the evidence yourself and argue against the claim as strongly as you honestly can. State whether the claim is refuted.`,
          {
            label: `verify:${finding.lens}:${findingIndex}`,
            phase: 'verify',
            schema: {
              type: 'object',
              required: ['refuted', 'reasoning'],
              properties: {
                refuted: { type: 'boolean' },
                reasoning: { type: 'string' },
              },
            },
          }
        ).then((result) => ({ findingIndex, ...result }))
      );
    })
  );

  const survivingFindings = allFindings.filter((_finding, findingIndex) => {
    const votesForThis = voteResults.filter(
      (v) => v.findingIndex === findingIndex
    );
    const refutedCount = votesForThis.filter((v) => v.refuted).length;
    // Majority vote: a finding survives unless MORE THAN HALF of its votes
    // refute it. With one vote (minor findings, or a budget-forced scale
    // down) that single vote decides.
    return refutedCount * 2 <= votesForThis.length;
  });

  return { ...ctx, allFindings, voteResults, survivingFindings };
};

const synthesizeStage = async (ctx) => {
  const verdict = await agent(
    `Synthesize these surviving findings into a ranked, deduplicated verdict. Every finding here already survived an adversarial majority-vote refutation attempt, so do not re-litigate them — dedupe findings that restate the same defect from different lenses, then rank and give an overall verdict.\n\n${JSON.stringify(
      ctx.survivingFindings,
      null,
      2
    )}`,
    {
      label: 'synthesize',
      phase: 'synthesize',
      schema: {
        type: 'object',
        required: ['verdict', 'ranked'],
        properties: {
          verdict: { enum: ['ship', 'fix-then-ship', 'not-justified'] },
          ranked: { type: 'array', items: FINDING_SCHEMA },
        },
      },
    }
  );
  return { ...ctx, ...verdict };
};

// pipeline() is the default here: each phase needs the previous phase's full
// output, runs once, in order. The fan-out is inside reviewStage/verifyStage,
// gated by parallel() only where a barrier is actually needed.
const finalCtx = await pipeline(
  [reviewStage, verifyStage, synthesizeStage],
  { reviews: [] }
);

const result = {
  verdict: finalCtx.verdict,
  ranked: finalCtx.ranked,
  reviewerCount: lenses.length + (args.probe ? 1 : 0),
  findingCount: finalCtx.allFindings.length,
  survivingCount: finalCtx.survivingFindings.length,
};

export default result;
