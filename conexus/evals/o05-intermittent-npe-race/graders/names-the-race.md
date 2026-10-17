---
type: regex
pattern: '\brace\b|race condition|thread[- ]?safe|not thread|concurren|synchroni[sz]|atomic|\block(s|ed|ing)?\b|interleav|data race|visibility|BlockingQueue'
flags: i
match: contains
---

`ConnectionPool.borrow()` checks a slot and then reads it again, and
`giveBack()` writes slots, from eight worker threads with no
synchronisation: between the check and the read another thread can empty the
slot, and `borrow()` returns null. Run on one thread the pool is correct, so
the race is the only cause (the first version of this fixture also had a
cursor bug that returned null on one thread; answers rightly reported both). The history shows the two non-fixes the
prompt mentions (a second borrow on null, a bigger pool).
