---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]\[(?:aurora/)?aur-batch-size\]\[2\]\['
match: contains
---

Sam chose the bigger batch: option 2, 1000 events.
