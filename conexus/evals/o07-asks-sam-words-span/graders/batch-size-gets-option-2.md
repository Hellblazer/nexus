---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK ref=(?:aurora/)?aur-batch-size choice=\[2\] '
match: contains
---

Sam chose the bigger batch: option 2, 1000 events.
