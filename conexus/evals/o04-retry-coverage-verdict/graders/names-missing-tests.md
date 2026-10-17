---
type: llm
criteria: >-
  Pass if the answer says the coverage is not enough and names at least two
  missing tests among: a call that raises TransientError and then succeeds;
  giving up and re-raising after the last attempt; a non-transient exception
  propagating at once without a retry; the backoff delays passed to sleep
  (growth, cap, jitter). Fail if it calls the coverage sufficient.
---

The specific gaps, which the regex above cannot see.
