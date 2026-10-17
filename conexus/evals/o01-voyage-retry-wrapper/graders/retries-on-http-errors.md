---
type: regex
target: { source: file, path: src/embed.py }
pattern: 'HTTPError|\.code\b|status|\b5\d\d\b|503'
match: contains
---

The edited `src/embed.py` handles HTTP failures of the Voyage request
(an `HTTPError`, a status code, or the 5xx/503 it names). The scaffold's file
has none of these, so a match means the change was made.
