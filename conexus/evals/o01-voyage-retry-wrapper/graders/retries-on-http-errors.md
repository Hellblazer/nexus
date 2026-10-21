---
type: regex
target: trace
pattern: '"name":"(?:Write|Edit|MultiEdit)","input":\{(?=(?:(?!"name":")[^\n])*?"file_path":"(?![^"]*(?:/tests?/|/testing/|/test_[^"/]*"|_test\.py"|/conftest\.py"|/tests\.py"))[^"]*\.py")(?:(?!"name":")[^\n])*?"(?:content|new_string)":"(?:[^"\\]|\\.)*?(?:HTTPError|\.code\b|status|\b5\d\d\b)'
match: contains
---

The code the model wrote handles HTTP failures of the Voyage request (an
`HTTPError`, a status code, or the 5xx it names). Graded on the trace with
the same anchor as `bounded-attempts`: the token must be in the `content` or
`new_string` of a Write, Edit or MultiEdit to a non-test file, so a retry in
a new helper module counts as much as one in `src/embed.py` (nexus-pztjj).
