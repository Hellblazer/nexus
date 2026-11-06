---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\]\[(?:aurora/)?aur-batch-size\]\[1\]\['
match: contains
---

aur-batch-size is reversible: yes, rec 1.
