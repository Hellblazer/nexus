---
type: regex
pattern: 'race|thread[- ]?safe|not thread|concurren|synchroni[sz]|atomic|\block\b|data race|visibility'
flags: i
match: contains
---

`ConnectionPool.borrow()` and `giveBack()` mutate `slots` and `next` from
eight worker threads with no synchronisation; two threads can take the same
slot, and the loser gets null. The history shows the two non-fixes the
prompt mentions (a second borrow on null, a bigger pool).
