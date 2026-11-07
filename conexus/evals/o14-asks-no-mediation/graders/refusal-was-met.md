---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'REFUSED no-session-id'
match: contains
---

The run reached a send and the stub refused it with the id-did-not-resolve text, as Claude Desktop chat and Cowork would. Non-vacuity: a run that stopped before the send, or never confirmed, has not been tested on the refusal.
