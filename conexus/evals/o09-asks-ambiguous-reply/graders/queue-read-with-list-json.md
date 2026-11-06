---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[list\]\[--json\]'
match: contains
---

The mediator reads the queue with `nx ask list --json` (flow step 1). A run that never looked at the board cannot have mediated it; this is the non-vacuity check for the no-send cases.
