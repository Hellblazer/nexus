// workflow_harness.mjs — run a `.claude/workflows/*.js` script against stub
// primitives that implement the documented Workflow contract, and print the
// result as JSON.
//
// WHAT THIS PROVES AND WHAT IT DOES NOT. The stubs encode the contract as the
// workflow-authoring reference states it: pipeline(items, ...stages) runs each
// ITEM through the stages and a throwing stage drops that item to null;
// parallel(thunks) calls THUNKS and turns a rejection into a null element;
// agent() has TWO documented failure modes and the stub models both: it
// RESOLVES null when a dispatch is skipped or dies terminally, and it THROWS
// once the token budget is exhausted. A scenario makes a label throw with
// {"__throw": "<message>"}; budget
// is {total, spent(), remaining()} with remaining as a METHOD. So this catches
// a script that misuses a primitive's shape, mishandles a null, or lets a hole
// read as a clean result. It does NOT prove the real runtime agrees with the
// reference — only a real Workflow invocation does that.
//
// Usage: node workflow_harness.mjs <script.js> <scenario.json>
// scenario.json: {args, budget: {total}, agents: {"<label>": <result|null>},
//                 agentDefault: <result|null>}
// An `agents` entry (or agentDefault) of {"__throw": "<message>"} makes that
// dispatch REJECT instead of resolve. Without it the budget-exhaustion path is
// structurally unreachable by any scenario, which is how two unprotected
// `agent()` call sites shipped past a green suite (bead nexus-xeoa0).

import { readFileSync } from 'node:fs';

const [, , scriptPath, scenarioPath] = process.argv;
if (!scriptPath || !scenarioPath) {
  console.error('usage: workflow_harness.mjs <script.js> <scenario.json>');
  process.exit(2);
}

const scenario = JSON.parse(readFileSync(scenarioPath, 'utf8'));
const dispatched = [];
const logs = [];

const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || '<unlabelled>';
  dispatched.push({ label, phase: opts && opts.phase, prompt });
  const result = Object.prototype.hasOwnProperty.call(
    scenario.agents ?? {},
    label
  )
    ? scenario.agents[label]
    : scenario.agentDefault ?? null;
  if (result && typeof result === 'object' && '__throw' in result) {
    throw new Error(result.__throw);
  }
  return result;
};

// Documented: run each ITEM through all stages, no barrier between stages.
// Stage N receives (prevResult, originalItem, index). A stage that throws
// drops that item to null and skips its remaining stages, so the result array
// stays positionally aligned with items.
const pipeline = async (items, ...stages) => {
  if (!Array.isArray(items)) {
    throw new TypeError(
      `pipeline(): first argument must be the ITEMS array, got ${typeof items}`
    );
  }
  for (const stage of stages) {
    if (typeof stage !== 'function') {
      throw new TypeError(
        `pipeline(): every stage must be callable, got ${
          stage === null ? 'null' : typeof stage
        }`
      );
    }
  }
  return Promise.all(
    items.map(async (item, index) => {
      let prev = item;
      for (const stage of stages) {
        try {
          prev = await stage(prev, item, index);
        } catch {
          return null;
        }
      }
      return prev;
    })
  );
};

// Documented: a BARRIER over THUNKS. A thunk that throws resolves to null in
// the result array; the call itself never rejects.
const parallel = async (thunks) => {
  if (!Array.isArray(thunks)) {
    throw new TypeError('parallel(): argument must be an array of thunks');
  }
  return Promise.all(
    thunks.map(async (thunk) => {
      if (typeof thunk !== 'function') {
        throw new TypeError(
          `parallel(): every element must be a thunk (() => Promise), got ${typeof thunk}`
        );
      }
      try {
        return await thunk();
      } catch {
        return null;
      }
    })
  );
};

const log = (message) => {
  logs.push(String(message));
};

const total = scenario.budget && scenario.budget.total ? scenario.budget.total : null;
const budget = {
  total,
  spent: () => 0,
  remaining: () => (total === null ? Infinity : total),
};

const source = readFileSync(scriptPath, 'utf8').replace(
  /^export const meta =/m,
  'globalThis.__meta ='
);

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction(
  'agent',
  'pipeline',
  'parallel',
  'log',
  'budget',
  'args',
  source
);

let result = null;
let error = null;
try {
  result = await run(agent, pipeline, parallel, log, budget, scenario.args);
} catch (e) {
  error = { name: e.name, message: e.message };
}

console.log(
  JSON.stringify({
    meta: globalThis.__meta ?? null,
    result,
    error,
    logs,
    dispatched: dispatched.map((d) => ({ label: d.label, phase: d.phase })),
  })
);
