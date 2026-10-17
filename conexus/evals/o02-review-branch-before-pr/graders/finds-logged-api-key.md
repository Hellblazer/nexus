---
type: regex
pattern: '(?:^|[.\n])(?![^.\n]*\b(?:no|not|never|nothing|none|isn.t|aren.t|without)\b)[^.\n]*?(?:(?:\blog(?:s|ged|ging)?\b|log\.info)[^.\n]{0,80}\b(?:api[ _-]?key|VOYAGE_API_KEY|key|secret|credential)|\b(?:api[ _-]?key|VOYAGE_API_KEY|secrets?|credentials?)\b[^.\n]{0,80}\b(?:logged|written to the logs?|to the logs?|in the logs?|leak))'
flags: i
match: contains
---

Planted defect 1: `embed()` logs the API key at INFO
(`log.info("... key %s", ..., key)`). The key word and the logging word must
sit in one sentence, and a sentence with a negation in it ("no secrets are
logged") does not count; the cost is that a finding phrased with "not"
is missed, which the llm grader covers. Crafted answers that must pass and fail are pinned
in tests/test_eval_corpus_lint.py.
