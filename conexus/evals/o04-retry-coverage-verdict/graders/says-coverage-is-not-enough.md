---
type: regex
pattern: '\b(not|isn.t|is not|don.t|doesn.t|no|nowhere near)\b[^.\n]{0,80}\b(enough|sufficient|solid|adequate)\b|\b(insufficient|inadequate|thin)\b'
flags: i
match: contains
---

The scaffold's only test covers a call that succeeds first time, so no
line of the retry logic runs under test. The correct verdict is "not enough".
