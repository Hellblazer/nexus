---
type: regex
target: trace
pattern: '"name":"(?:Write|Edit|MultiEdit)","input":\{(?=(?:(?!"name":")[^\n])*?"file_path":"(?![^"]*(?:/tests?/|/testing/|/test_[^"/]*"|_test\.py"|/conftest\.py"|/tests\.py"))[^"]*")(?:(?!"name":")[^\n])*?"(?:content|new_string)":"(?:[^"\\]|\\.)*?(?:\b(?:\w*_)?(?:attempts?|retries|tries)\s*[=:]|range\(\s*(?:\d+|\w*(?:attempt|retr|tries)\w*)|stop_after_attempt|max_retries)'
flags: i
match: contains
---

The retry is bounded: an attempts/retries count is set or looped over.
An unbounded `while True` retry would turn one outage into a hung batch.

Graded on the trace, so the retry counts in whatever source file the model
writes it to (`src/embed.py` or a new helper module) through Write, Edit or
MultiEdit (nexus-pztjj). The token must sit inside that call's `content` or
`new_string` string, so an Edit's `old_string`, a file path, a later text
block or a batched second call cannot supply it, and hook output escapes its
quotes so it cannot match the anchor. Test files are skipped: every live run
also wrote `tests/test_embed.py`, whose 503s and attempt counts would pass on
their own. A write made through a Bash heredoc is not graded.
