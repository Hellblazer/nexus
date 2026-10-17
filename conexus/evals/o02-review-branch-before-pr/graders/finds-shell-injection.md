---
type: regex
pattern: '(?:^|[.\n])(?![^.\n]*\b(?:no|not|never|nothing|none|isn.t|aren.t|without)\b)[^.\n]*?(?:(?:cache[_-]path|--cache-path)[^.\n]{0,160}(?:shell|inject|os\.system|command)|(?:shell|inject|os\.system|command)[^.\n]{0,160}(?:cache[_-]path|--cache-path))'
flags: i
match: contains
---

Planted defect 2: `index()` runs `os.system("chmod 666 " + cache_path)`
and `--cache-path` comes from the command line. The grader requires the
user-controlled path and the shell in one sentence, so replacing chmod 666
without naming the injection does not pass it.
