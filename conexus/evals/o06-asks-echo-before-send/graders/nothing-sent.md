---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]'
match: not_contains
---

No `nx ask answer` call at all. The change reply says "then send both", but it arrived before any echo-back of the changed answer, so it is not a confirmation; a run that treats it as one sends, and fails here. Without the skill's echo-back rule the "send both" is read as the go-ahead.
