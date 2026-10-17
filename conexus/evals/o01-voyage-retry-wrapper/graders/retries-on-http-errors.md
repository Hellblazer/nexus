---
type: regex
target: trace
pattern: '"name":"(?:Write|Edit|MultiEdit)","input":\{[^\n]*?(?:HTTPError|\.code\b|status|\b5\d\d\b|503)'
match: contains
---

The code the model wrote handles HTTP failures of the Voyage request (an
`HTTPError`, a status code, or the 5xx/503 it names). The scaffold's
`src/embed.py` has none of these, so a match in an Edit's `old_string`
cannot happen and a match means the change was made.

Graded on the trace, anchored to a Write, Edit or MultiEdit input, so a
retry in a new helper module counts as much as one in `src/embed.py`
(nexus-pztjj).
