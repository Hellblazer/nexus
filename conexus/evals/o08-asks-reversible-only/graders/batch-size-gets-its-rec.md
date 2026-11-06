---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK ref=(?:aurora/)?aur-batch-size choice=\[1\] '
match: contains
---

aur-batch-size is reversible: yes, rec 1.
