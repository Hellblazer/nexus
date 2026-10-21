---
type: llm
focus: { source: file, path: src/embed.py }
criteria: >-
  Pass if the code retries the Voyage request only on transient failures
  (HTTP 429 or 5xx, or a timeout or connection error), with a bounded number
  of attempts and a delay between them, and lets a non-transient failure such
  as HTTP 400 or 401 fail without retrying. If this file applies a retry
  helper defined in another module, judge what the call site shows of that
  policy and pass when it is consistent with these rules. Fail if it retries
  every exception, retries without a bound, has no delay, or has no retry.
---

The judgement the regexes cannot make: which failures are retried. The judge
reads the final `src/embed.py`, not the trace: `focus: trace` hands it only
the first and last messages of a run ("[…70 messages elided…]" in the
nexus-pztjj live run), which cut the retry edit itself. A helper module's
body is therefore checked by the trace-anchored regexes, not by the judge.
