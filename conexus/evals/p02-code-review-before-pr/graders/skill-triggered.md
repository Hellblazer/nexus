---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"((conexus:)?code-review|security-review)"'
---

Direct match to the `code-review` skill description: "before committing or
creating a pull request."

WIDENED 2026-08-23 (nexus-dkotg) to accept the BUILT-IN `security-review`
as well. This is not a weakened assertion — both skills' documented scopes
cover this prompt, established by reading their descriptions, not by
observing what a run happened to do:

- `conexus:code-review`: "Use when code changes are ready for quality,
  SECURITY, or best practices review, before committing or creating a pull
  request." The prompt is close to a verbatim restatement of it.
- `security-review` (Claude Code built-in, not a conexus skill):
  "Complete a security review of the pending changes on the current
  branch." The prompt says "changes on this branch" and asks about
  security.

Observed across 6 runs: 1 fired `conexus:code-review`, 1 fired
`security-review`, 4 fired NO skill at all. Only the last is a defect, and
it is the `using-nx-skills` engagement problem, not this case's.

THE GENERAL POINT, which this corpus had never modelled: conexus skills do
not compete only with each other. The built-in skill surface
(`security-review`, `code-review`, `debug`, `run`, ...) is present in every
session and shares vocabulary with plugin skills. Every disambiguation
case here reasons about conexus-vs-conexus collisions only. A negative
grader that matches `"(conexus:)?X"` cannot see a built-in winning, so
absorption by a built-in reads as a clean pass.
