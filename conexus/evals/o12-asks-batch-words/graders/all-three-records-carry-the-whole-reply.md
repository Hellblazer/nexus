---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'batch=OK[\s\S]*batch=OK[\s\S]*batch=OK'
match: contains
---

Three answers came from one reply, so each carries `--batch-words` holding the whole reply (the stub checks it is the reply, at most 2000 characters). Each record's `--sam-words` stays its own words (scope=OK in every CHECK line, enforced by every-send-is-clean).
