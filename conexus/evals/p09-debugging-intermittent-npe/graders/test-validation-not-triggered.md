---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names the
  "test-validation" skill (bare "test-validation" or
  "conexus:test-validation"). This prompt describes an unsolved,
  in-progress bug investigation, not a completed implementation awaiting
  coverage sign-off, so "test-validation" is the wrong sibling skill and
  must not fire. Any other skill invocation (including "debugging"
  itself) is fine and should not affect the verdict.
---

Disambiguation half of the debugging / test-validation pair: both skills
key on the word "test" or its neighborhood ("tests fail" vs. "test
coverage"), and both can plausibly follow a development turn, so this is
the confusion class the pair is meant to catch.

OPEN QUESTION (enablement day): same caveat as the release/engine-release
pair — no documented negation primitive on `tool_used`; `llm` criteria
used as best-effort, field name unconfirmed.
