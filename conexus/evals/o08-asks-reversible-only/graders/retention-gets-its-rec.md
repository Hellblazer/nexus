---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK ref=(?:aurora/)?aur-retention choice=\[2\] '
match: contains
---

aur-retention is reversible: yes, rec 2.
