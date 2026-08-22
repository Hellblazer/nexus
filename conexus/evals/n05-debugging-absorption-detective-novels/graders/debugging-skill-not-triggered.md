---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names the
  "debugging" skill (bare "debugging" or "conexus:debugging"). This is a
  casual book-recommendation request that uses "debugging" only as a
  genre descriptor — it names no failing test, no exception, and no
  non-deterministic behavior, so the `debugging` skill's own trigger
  condition is not met.
---

Absorption stress on the word "debugging" appearing outside any actual
software-bug context.

OPEN QUESTION (enablement day): no documented negation primitive on
`tool_used`; `llm` criteria used as best-effort, field name unconfirmed.
