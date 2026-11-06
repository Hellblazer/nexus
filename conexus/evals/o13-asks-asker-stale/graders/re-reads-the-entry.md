---
type: tool_used
tool: Bash
input_match: 'nx\s+ask\s+show\b'
min: 1
---

The frame says to re-read the board entry with `nx ask show` before acting. The checkout's `nx`
is `./bin/nx`, which logs each call; either spelling matches.
