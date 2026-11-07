---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK ref=(?:aurora/)?aur-batch-size choice=\[1\] '
match: contains
---

500 is option 1.
