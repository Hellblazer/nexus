---
type: regex
pattern: '\b(stale|out of date|superseded)\b|\b(version|ask|entry)\b[^.\n]{0,60}\b(differs|changed|was edited|has been edited|no longer (matches|the same))\b|\b(differs|different|changed)\b[^.\n]{0,40}\bversion\b'
flags: i
match: contains
---

The final message calls the answer stale or says the ask's version moved.
