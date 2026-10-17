---
type: regex
target: { source: file, path: src/embed.py }
pattern: '(attempts?|retries|max_tries|tries)\s*[=:]|range\(\s*\w*(attempt|retr|tries)'
flags: i
match: contains
---

The retry is bounded: an attempts/retries count is set or looped over.
An unbounded `while True` retry would turn one outage into a hung batch.
