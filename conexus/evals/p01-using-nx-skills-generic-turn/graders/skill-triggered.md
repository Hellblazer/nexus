---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"(conexus:)?using-nx-skills"'
---

`using-nx-skills` describes itself as triggering "when starting any turn" —
it is the meta-skill that scans the rest of the conexus skill list before
any other response. This prompt carries no other skill-specific keyword
(no "review", "release", "RDR", etc.), so it isolates whether the
turn-start scan itself fires on a plain, unremarkable development request.
