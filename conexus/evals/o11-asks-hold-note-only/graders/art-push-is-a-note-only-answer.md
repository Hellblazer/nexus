---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK ref=(?:art-release/)?art-push-main choice=\[(?![0-9]+\])[^\]]*hold[^\]]*\]'
flags: i
match: contains
---

The ART ask has no Hold option, so 'hold' is a note-only answer: the reply argument is text, not an option number.
