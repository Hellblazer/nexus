---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]'
match: not_contains
---

No `nx ask answer` call at all: the mediator sent nothing.
