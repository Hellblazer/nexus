---
type: regex
target: trace
pattern: '"name":"(?:Write|Edit|MultiEdit)","input":\{[^\n]*?(?:(attempts?|retries|max_tries|tries)\s*[=:]|range\(\s*(\d+|\w*(attempt|retr|tries)\w*)|stop_after_attempt|max_retries)'
flags: i
match: contains
---

The retry is bounded: an attempts/retries count is set or looped over.
An unbounded `while True` retry would turn one outage into a hung batch.

Graded on the trace, anchored to the input of a Write, Edit or MultiEdit
call, so the retry counts in whatever file the model puts it (`src/embed.py`
or a new helper module). Each trace line is one JSON message, so `[^\n]*?`
keeps the match inside that call's input; hook output in the trace escapes
its quotes and cannot match the anchor (nexus-pztjj).
