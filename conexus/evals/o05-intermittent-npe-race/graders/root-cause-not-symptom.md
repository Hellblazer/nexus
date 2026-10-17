---
type: llm
criteria: >-
  Pass if the answer identifies unsynchronised shared state in
  ConnectionPool (borrow checks a slot and reads it again while giveBack and
  other borrowers write the same slots from the worker threads) as the root cause, explains why the null check and the
  larger pool do not fix it, and proposes a fix that makes borrow and
  giveBack thread-safe (synchronized methods, a lock, or a blocking queue).
  Fail if it proposes another null check, retry or a larger pool as the
  fix.
---

The diagnosis, not just the word "race".
