---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names the
  "query" skill (bare "query" or "conexus:query"). This is a single-fact,
  file:line-shaped lookup answerable by reading one function signature —
  the `query` skill's own description says explicitly it is "Not for
  file:line, single-fact, or already-answered questions". A direct file
  read, Grep, or a plain search()/query() MCP tool call is the expected
  path, not the Skill tool.
---

Absorption stress on `query`: the skill's description already carves
this shape out explicitly, so this case checks that the carve-out is
honored in practice, not just documented.

OPEN QUESTION (enablement day): no documented negation primitive on
`tool_used`; `llm` criteria used as best-effort, field name unconfirmed.
