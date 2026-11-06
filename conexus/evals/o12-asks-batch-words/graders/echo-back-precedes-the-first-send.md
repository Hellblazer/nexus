---
type: regex
target: trace
pattern: '"type":"text","text":"(?=(?:[^"\\]|\\.)*?(?:send|confirm|go ahead|proceed)(?:[^"\\]|\\.)*?\?)(?=(?:[^"\\]|\\.)*?(?:500 is fine|ninety days|gap on k9kr9))(?:[^"\\]|\\.)*"[\s\S]*?"name":"Bash","input":\{"command":"(?:[^"\\]|\\.)*?ask answer'
flags: i
match: contains
---

A single text message sits in the trace before the first `ask answer` Bash call that (1) quotes words from the user's reply for an ask being answered (500 is fine, ninety days, gap on k9kr9) and (2) asks the user to confirm or send, ending in a question mark. The echo-back is a text message, so only the trace shows the order. A send with no such message ahead of it fails, and so does a bare "which should I send?" or a queue presentation that never quotes the user.
