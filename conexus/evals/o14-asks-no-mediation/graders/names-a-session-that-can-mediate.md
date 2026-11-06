---
type: regex
flags: i
pattern: '^(?=[\s\S]*(?:\b(?:cannot|can not|unable|not able|could not)\b|n.t\b|no session id|has no|without))(?=[\s\S]*(?:terminal|code[- ]tab))'
match: contains
---

The final message gives the refusal: it cannot take the answer from this session (no session id of its own), and the user should go to a terminal session or a Desktop Code-tab session (or answer from a terminal themselves). The stub's refusal text names neither, so only a run that knows the skill's Where-this-session-can-mediate section says it.
