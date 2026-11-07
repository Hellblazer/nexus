---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK ref=(?:aurora/)?aur-retention choice=\[2\] '
match: contains
---

Ninety days is option 2.
