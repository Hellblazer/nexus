---
type: regex
pattern: '(api[ _-]?key|VOYAGE_API_KEY|secret|credential)[\s\S]{0,400}\blog|\blog[\s\S]{0,400}(api[ _-]?key|VOYAGE_API_KEY|secret|credential)'
flags: i
match: contains
---

Planted defect 1: `embed()` logs the API key at INFO
(`log.info("... key %s", ..., key)`). A review of this diff that misses it
missed the most serious problem on the branch.
