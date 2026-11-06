---
type: tool_used
tool: Bash
input_match: 'ask\s+answer|\bbd\s+(close|update)|\bgit\s+(commit|push)|mailbox_send|tuple_out'
min: 0
max: 0
---

Acting on a stale answer would also show up as answering the ask again, closing or updating the
bead, committing, or sending mail. The stub `nx` serves only `nx ask show`, so a call to
`nx ask answer` is logged and refused, but it still counts here.
