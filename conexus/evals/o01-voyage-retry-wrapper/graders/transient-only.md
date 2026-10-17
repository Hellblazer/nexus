---
type: llm
focus: { source: file, path: src/embed.py }
criteria: >-
  Pass if the code retries the Voyage request only on transient failures
  (HTTP 429 or 5xx, or a timeout or connection error), with a bounded number
  of attempts and a delay between them, and lets a non-transient failure such
  as HTTP 400 or 401 fail without retrying. Fail if it retries every
  exception, retries without a bound, or has no delay.
---

The judgement the regexes cannot make: which failures are retried.
