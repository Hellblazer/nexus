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
// PRIMITIVE SIGNATURES — read from the workflow-authoring reference on
// 2026-09-21 (bead nexus-xeoa0), which supersedes the ASSUMED block that
// stood here. This file had NEVER EXECUTED, for the same reason its sibling
// dead-wire-census.js had not: it called `pipeline([stageFns], {...})`,
// passing its three stage functions as the ITEMS array and a seed object as
// its only stage. A seed object is not callable, so nothing after that line
// had ever run.
//   - `args` is an ambient global carrying this invocation's arguments, or
//     `undefined` when none were passed.
//   - `agent(prompt, opts)` dispatches one subagent call. With `opts.schema`
//     it resolves to the validated object. It resolves to `null` — it does
//     NOT throw — when the user skips the agent or the subagent dies on a
//     terminal API error, so every result is null-checked here.
//   - `pipeline(items, ...stages)` runs each ITEM through the stages with no
//     barrier between stages. It is the default shape for per-item work, and
//     it is NOT what this file's three phases want: Verify needs every
//     lens's findings at once and Synthesize needs the whole surviving set,
//     so the phases here are plain sequential awaits and the fan-out inside
//     each one is a `parallel()` barrier that earns its latency.
//   - `parallel(thunks)` is a TRUE BARRIER: it awaits every thunk. A thunk
//     that throws resolves to `null` in the result array rather than
//     rejecting the call, so every array it returns is filtered here.
//   - `budget` is `{total: number|null, spent(), remaining()}` in TOKENS —
//     `remaining` is a METHOD, and `total` is null when the user set no
//     target. The retired `budgetRemaining()` here tested
//     `typeof budget.remaining === 'number'`, which a method fails, so it
//     always returned Infinity and the vote scale-down it guarded never once
//     fired. It also compared a COUNT OF AGENTS against a TOKEN figure.
//   - The script body runs inside an async function, so the result is a bare
//     top-level `return`. The earlier `export default result` was the other
//     half of the same unverified guess. Editors that parse this file as a
//     plain ES module flag that `return` and mark the returned bindings
//     unused; the workflow runtime is the authority, not the editor.
//   - No `Date.now()` / `Math.random()` anywhere in this file; the runtime
//     throws on them because they would break resume.

export const meta = {
  name: 'pressure-test',
  description:
    'Run several reviewers with distinct lenses against a target and a spec, adversarially verify every finding by majority vote, then synthesize a ranked, deduplicated verdict.',
  whenToUse:
    'Before committing a change that carries real risk of a silent revert or a spec-fidelity gap: a diff implementing a verbatim directive, a design decision with more than one defensible reading, or any change under a "no more reverts" directive. Not for routine, low-risk edits — dispatch the two standing reviewers (code-review-expert, substantive-critic) for those instead; this workflow is heavier by design.',
  // One entry per progress group, titles matched exactly against the `phase`
  // passed in each agent() call.
  phases: [
    { title: 'review', detail: 'one agent per lens, plus the optional probe' },
    { title: 'verify', detail: 'adversarial refute votes on every finding' },
    { title: 'synthesize', detail: 'rank and dedupe what survived' },
  ],
};

// args: an object with the fields below, OR a single STRING -- either JSON
//   text for the object form, or "key: value" lines (target/spec/probe/
//   votesPerFinding) -- see parseWorkflowStringArgs below, which the top of
//   this file's body normalizes into the object form before anything else
//   runs (nexus-kk4ut). `lenses` cannot be expressed in the string form; a
//   caller that needs it passes the object form (or JSON text).
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
  args = parseWorkflowStringArgs(args, ['target', 'spec', 'probe', 'votesPerFinding'], ['votesPerFinding']);
}

if (!args || !args.target) {
  throw new Error('pressure-test requires args.target');
}
if (!args.spec) {
  // The spec-fidelity lens is what makes this a pressure test rather than an
  // ordinary review, and it has nothing to check the target against without
  // this. Refuse rather than run a three-lens review under a four-lens name.
  throw new Error(
    'pressure-test requires args.spec — the verbatim directive or design decision the target is checked against'
  );
}

const lenses = args.lenses ?? DEFAULT_LENSES;


// --- Review ----------------------------------------------------------------
// A true barrier: the verify phase needs every lens's findings at once, so
// this phase collects all of them before anything downstream starts.
// parallel() takes THUNKS, not promises — an `agent(...)` call built eagerly
// into an array would dispatch immediately and escape the concurrency cap.

const reviewThunks = lenses.map((lens) => () =>
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
  reviewThunks.push(() =>
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

const reviewsRaw = await parallel(reviewThunks);
const reviews = reviewsRaw.filter(Boolean);
if (reviews.length < reviewThunks.length) {
  // No silent caps: a lens that never reported is a lens that did not run,
  // and a verdict assembled from the rest is narrower than its name claims.
  log(
    `pressure-test: ${reviewThunks.length - reviews.length} of ${
      reviewThunks.length
    } review agent(s) returned nothing (skipped, or a terminal error). The verdict below covers ${
      reviews.length
    } lens(es), not the full battery.`
  );
}
if (reviews.length === 0) {
  throw new Error(
    'pressure-test: every review agent returned nothing. There is no verdict to synthesize.'
  );
}

// --- Verify ----------------------------------------------------------------
// A true barrier: a majority cannot be taken until a finding's votes have all
// landed, and synthesis needs the whole surviving set at once.

const allFindings = reviews.flatMap((r) =>
  (r.findings ?? []).map((f) => ({ ...f, lens: r.lens }))
);

const requestedVotes = args.votesPerFinding ?? 3;
const votesFor = (finding) => (finding.severity === 'minor' ? 1 : requestedVotes);

let survivingFindings = [];
let unverifiedFindings = [];
let voteResults = [];

if (allFindings.length > 0) {
  if (budget.total) {
    const plannedAgents = allFindings.reduce((sum, f) => sum + votesFor(f), 0);
    log(
      `pressure-test: ${allFindings.length} finding(s) -> ${plannedAgents} verify dispatch(es); ${Math.round(
        budget.remaining() / 1000
      )}k tokens remain of a ${Math.round(
        budget.total / 1000
      )}k target. Dispatches throw once the target is reached; any finding left without votes is reported as unverified rather than counted as surviving.`
    );
  }

  // Round-major, not finding-major: every finding's FIRST vote is queued
  // before any finding's second. This file used to claim a uniform
  // degradation under a tight budget -- drop every finding to one vote --
  // via a cap that could never fire. Removing that cap without this would
  // have replaced it with an order-dependent one, where a budget that runs
  // out mid-fan-out gives the first findings three votes and the last ones
  // none. Ordering the queue this way degrades gracefully instead.
  //
  // Honest bound: the reference documents that excess calls queue and run as
  // slots free up, but does NOT document the queue's ordering discipline. So
  // this is better than finding-major under a FIFO queue and no worse under
  // any other -- it is not a guarantee. The deterministic lever for cheap
  // verification is `args.votesPerFinding`, which the caller controls.
  const maxVotes = allFindings.reduce(
    (most, finding) => Math.max(most, votesFor(finding)),
    0
  );
  const voteThunks = [];
  for (let voteIndex = 0; voteIndex < maxVotes; voteIndex += 1) {
    allFindings.forEach((finding, findingIndex) => {
      if (voteIndex >= votesFor(finding)) return;
      voteThunks.push(
        () =>
          agent(
            `A reviewer (lens: ${finding.lens}) claims:\n\n${finding.claim}\n\nEvidence given: ${finding.evidence}\n\nCheck the evidence yourself and argue against the claim as strongly as you honestly can. State whether the claim is refuted.`,
            {
              label: `verify:${finding.lens}:${findingIndex}:${voteIndex}`,
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
          ).then((result) =>
            result ? { findingIndex, ...result } : { findingIndex, landed: false }
          )
      );
    });
  }

  voteResults = (await parallel(voteThunks)).filter(Boolean);

  allFindings.forEach((finding, findingIndex) => {
    const landed = voteResults.filter(
      (v) => v.findingIndex === findingIndex && typeof v.refuted === 'boolean'
    );
    if (landed.length === 0) {
      // A finding nobody managed to vote on has NOT survived an adversarial
      // pass. Counting it as a survivor would make the synthesis prompt's own
      // premise false, so it goes out separately instead.
      unverifiedFindings.push(finding);
      return;
    }
    const refutedCount = landed.filter((v) => v.refuted).length;
    // Majority vote: a finding survives unless MORE THAN HALF of its landed
    // votes refute it. With one vote (minor findings) that single vote decides.
    if (refutedCount * 2 <= landed.length) {
      survivingFindings.push(finding);
    }
  });

  if (unverifiedFindings.length > 0) {
    log(
      `pressure-test: ${unverifiedFindings.length} finding(s) got ZERO landed verification votes and are reported as unverified, not as survivors: ${unverifiedFindings
        .map((f) => f.claim)
        .join(' | ')}`
    );
  }
}

// --- Synthesize ------------------------------------------------------------

let synthesis = null;
let synthesisDropped = false;
if (survivingFindings.length > 0) {
  // This is the only dispatch in the file not inside a `parallel()` thunk,
  // and `parallel()` is what converts a throw into a null element. Here
  // there is no such conversion, so an `agent()` throw — the documented
  // budget-exhaustion case — would propagate out of the script and discard
  // everything: every review, every vote, every upheld finding. Losing the
  // ranking is acceptable; losing the findings that survived refutation
  // because the ranking could not be paid for is not. The catch keeps the
  // findings and records that the verdict is missing for a reason, which is
  // different from missing because nothing survived.
  try {
    synthesis = await agent(
      `Synthesize these surviving findings into a ranked, deduplicated verdict. Every finding here already survived an adversarial majority-vote refutation attempt, so do not re-litigate them — dedupe findings that restate the same defect from different lenses, then rank and give an overall verdict.\n\n${JSON.stringify(
        survivingFindings,
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
  } catch (err) {
    synthesisDropped = true;
    log(
      `pressure-test: the synthesis dispatch threw (${
        err && err.message ? err.message : err
      }). ${survivingFindings.length} finding(s) DID survive refutation and are returned unranked; the null verdict here means the ranking was never paid for, not that nothing survived.`
    );
  }
} else {
  log(
    `pressure-test: nothing survived verification (${allFindings.length} finding(s) raised, ${unverifiedFindings.length} unverified). No synthesis agent dispatched; the verdict is reported as null rather than as "ship".`
  );
}

return {
  // null when no finding survived, or when the synthesis agent itself
  // returned nothing. It is NOT a "ship" verdict either way — a caller that
  // reads a missing verdict as approval is reading a hole as a pass.
  verdict: synthesis ? synthesis.verdict : null,
  ranked: synthesis ? synthesis.ranked : [],
  reviewerCount: reviews.length,
  reviewerRequested: reviewThunks.length,
  findingCount: allFindings.length,
  survivingCount: survivingFindings.length,
  unverifiedFindings,
  // Only populated when the synthesis dispatch threw. It carries the findings
  // that survived refutation but never got ranked, so the work they cost is
  // not lost with the ranking. Empty on every other path, where `ranked`
  // already carries them.
  unrankedSurvivors: synthesisDropped ? survivingFindings : [],
  // True when the ranking was lost to a throw. A caller MUST distinguish this
  // from a null verdict caused by nothing surviving: same `verdict: null`,
  // opposite meaning.
  synthesisDropped,
  complete:
    reviews.length === reviewThunks.length &&
    unverifiedFindings.length === 0 &&
    !synthesisDropped,
};
