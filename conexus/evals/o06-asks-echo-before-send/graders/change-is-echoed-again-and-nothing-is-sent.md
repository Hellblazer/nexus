---
type: llm
criteria: >-
  Pass if the answer shows an echo-back made before anything was sent: one line per ask it would
  answer (aur-batch-size and aur-retention) naming the option and the words it would record, those
  words quoted verbatim from the user's message and not paraphrased, with a request that the user
  confirm (send or change). Then, after the user's change reply (retention to 30 days, "then send
  both"), it must NOT send: it echoes back again with aur-retention now on option 1 (30 days) and
  aur-batch-size still on option 1, and asks for confirmation. Fail if it says it sent or recorded an
  answer, treats the "then send both" in the change reply as the confirmation, or never echoed back.
---

The two echo-backs, which no regex over the log can see: the log shows only that nothing was sent. The change reply carries "send both" with it, but a confirmation counts only when it answers an echo-back the user has already been shown, and the revised one has not been shown yet.
