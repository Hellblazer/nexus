---
type: llm
criteria: >-
  Pass if the review reports all three of these as problems to fix before the
  PR: the API key written to the log at INFO in embed(); the --cache-path
  value reaching a shell through os.system (command injection); the cache
  file made world-writable (chmod 666) in /tmp. Fail if any of the three is
  missing, or is mentioned only to say it is fine.
---

The semantic check behind the three regexes, which cannot tell a sentence
that reports a defect from one that clears it in every phrasing.
