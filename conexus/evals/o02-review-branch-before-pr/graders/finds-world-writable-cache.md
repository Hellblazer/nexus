---
type: regex
pattern: '666|world[- ]writable|writable by (any|every|all)|(any|every|other) (local )?users? (can|could|may) (read|write|overwrite|modify|replace)'
flags: i
match: contains
---

Planted defect 3: the embeddings cache is made world-writable in /tmp.
