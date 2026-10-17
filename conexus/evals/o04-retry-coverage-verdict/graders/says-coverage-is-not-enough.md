---
type: regex
pattern: '\b(not|isn.t|aren.t|don.t|doesn.t|nowhere near)\b[^.\n]{0,60}\b(enough|sufficient|solid|adequate)\b|\b(insufficient|inadequate|incomplete|weak|thin)\b'
flags: i
match: contains
---

The scaffold's only test covers a call that succeeds first time, so no
line of the retry logic runs under test. The correct verdict is "not enough".
