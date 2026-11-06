---
type: llm
criteria: >-
  Pass if the answer shows an echo-back made before anything was sent: one line per ask it would
  answer (aur-batch-size and aur-retention) naming the option and the words it would record, those
  words quoted verbatim from Sam's message and not paraphrased, with a request that Sam confirm (send
  or change), and then, after Sam's change, says nothing was sent and asks what to change. Fail if it
  says it sent or recorded an answer, or never echoed back.
---

The echo-back and the change reply, which no regex over the log can see: the log shows only that nothing was sent.
