---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names
  "test-authoring" or "test-validation". This prompt uses "test" purely
  figuratively ("the real test of X is whether...") to ask an
  architecture-opinion question about a module split — it names no test
  file, no pytest marker, no test suite, and no coverage check, so
  neither test skill's trigger condition is met.
---

Absorption stress on the bare word "test" appearing outside any actual
testing context.

OPEN QUESTION (enablement day): no documented negation primitive on
`tool_used`; `llm` criteria used as best-effort, field name unconfirmed.
