---
type: tool_used
tool: Agent
input_match: '(?=[\s\S]*"subagent_type"\s*:\s*"(conexus:)?(code-review-expert|substantive-critic)")(?=[\s\S]*Ship when)'
---

A reviewer is dispatched with a stated end ("Ship when <X>"), which both
reviewer descriptions ask for; without it they open with NO ACCEPTANCE BAR.

This case is also the corpus's positive control for agent dispatch: the
`tool_used` on `Agent` with `subagent_type` that the n-case guards assert
absent is shown here to be observable when it does happen.
