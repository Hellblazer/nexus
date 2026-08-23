---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names the
  "debugging" skill (bare "debugging" or "conexus:debugging"). This
  prompt describes a completed, working implementation ("every manual
  run ... looks correct") awaiting coverage sign-off, not a failing test,
  exception, or non-deterministic behavior — so "debugging" is the wrong
  sibling skill and must not fire. Any other skill invocation (including
  "test-validation" itself) is fine and should not affect the verdict.
---

Disambiguation half of the debugging / test-validation pair (see
p09-debugging-intermittent-npe for the mirror case).

OPEN QUESTION (enablement day): same caveat as
p09-debugging-intermittent-npe/graders/test-validation-not-triggered.md.
