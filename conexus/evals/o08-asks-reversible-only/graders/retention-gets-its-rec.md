---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]\[(?:aurora/)?aur-retention\]\[2\]\['
match: contains
---

aur-retention is reversible: yes, rec 2.
