---
type: tool_used
tool: Agent
input_match: '"subagent_type"\s*:\s*"(conexus:)?test-validator"'
min: 0
max: 0
---

"The real test of a good module split" is figurative; it must not
dispatch the test-validator agent. This replaces the guard on the retired
`test-validation` skill.
