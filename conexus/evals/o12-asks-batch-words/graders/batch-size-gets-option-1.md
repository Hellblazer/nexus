---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]\[(?:aurora/)?aur-batch-size\]\[1\]\['
match: contains
---

500 is option 1.
