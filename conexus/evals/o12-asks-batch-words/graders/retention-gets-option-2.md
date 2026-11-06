---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]\[(?:aurora/)?aur-retention\]\[2\]\['
match: contains
---

Ninety days is option 2.
