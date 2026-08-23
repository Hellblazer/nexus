---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"engine-release"'
max: 0
---

Disambiguation half of the release / engine-release pair: the two
skills' descriptions differ only in which release lifecycle (PyPI package
vs. Java engine binary) they cover, and both are keyed on the word
"release" in casual phrasing, so this is exactly the confusion class the
pair is meant to catch.

GRADER TYPE CORRECTED 2026-08-23. This was `type: llm` with criteria that
began "Fail if the transcript contains a Skill tool call...". An `llm`
grader cannot see tool calls: its `focus` field defaults to
`last_message`, and the runtime feeds the judge only
`run.lastAssistantText`. Verified against the binary's own schema
(`focus: c2h().default("last_message")`, where `c2h` is
`enum(["trace","last_message","files"])`). Proven in a real run: this
class of grader voted FAIL 3/3 on a trace where a paired
`tool_used max: 0` counter reported the skill was never invoked.

`tool_used` with `max: 0` is the negation primitive. The earlier note here
claiming none was documented was wrong -- the schema is
`{type:"tool_used", tool, input_match?, min?, max?}` with both bounds
`int >= 0`. `max: 0` asserts the matching call never happened, which is
exactly what a not-triggered case means, and it reads the trace instead of
a summary sentence.
