---
type: llm
focus: trace
criteria: >-
  Judge the code the agent wrote through Write, Edit or MultiEdit tool calls,
  in any file. Pass if it retries the Voyage request only on transient
  failures (HTTP 429 or 5xx, or a timeout or connection error), with a
  bounded number of attempts and a delay between them, and lets a
  non-transient failure such as HTTP 400 or 401 fail without retrying. Fail
  if it retries every exception, retries without a bound, or has no delay.
---

The judgement the regexes cannot make: which failures are retried. The judge
reads the trace so it sees a retry helper in another module too (nexus-pztjj).
