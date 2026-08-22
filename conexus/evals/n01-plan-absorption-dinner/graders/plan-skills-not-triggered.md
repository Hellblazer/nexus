---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names
  "strategic-planning", "plan-first", "architecture", or any other
  planning/plan-machinery skill. This prompt is a casual dinner-recipe
  question that happens to contain the word "plan" — it is not
  development work needing decomposition into tasks (strategic-planning)
  and not a retrieval question needing reduction from many documents
  (plan-first). No skill invocation is expected at all.
---

Targets the exact absorption defect class already found once in this
plugin: `nexus-77cct` records that the retired plan-meta skills "absorb
any question containing the word 'plan' and outrank the plan a caller
actually wanted" (`conexus/PENDING_RELEASE.md`). This case is the
regression guard for that class recurring in `strategic-planning` /
`plan-first`, the two live skills whose names or descriptions still key
on the bare word "plan".

OPEN QUESTION (enablement day): no documented negation primitive on
`tool_used`; `llm` criteria used as best-effort, field name unconfirmed.
