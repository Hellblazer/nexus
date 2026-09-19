# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count the CHANNEL-delivered wakes for one mailbox in a Claude Code transcript.

Prints the count and exits 0.

Claude Code records a channel notification as a `user` entry carrying
`origin: {"kind": "channel", "server": "<mcp server>"}`, while a typed prompt
carries `{"kind": "human"}` or no origin at all (measured 2026-09-18 in the
container). That field is what "the session woke on the channel, with no
prompt from this harness" MEANS, so the MVV counts it rather than grepping the
rendered pane: the pane truncates the wake line with an ellipsis mid-session-id
("subspace mailbox/6a70b05f-1e35-4d1f-...") so the id it is asked to match can
never appear there, which cost a billed run.

Usage: channel_wakes.py TRANSCRIPT.jsonl MAILBOX_ADDRESS [SERVER]
"""
import json
import sys

path, address = sys.argv[1], sys.argv[2]
server = sys.argv[3] if len(sys.argv) > 3 else "nexus"
needle = f"subspace mailbox/{address},"

count = 0
with open(path, encoding="utf-8") as fh:
    for line in fh:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "user":
            continue
        origin = entry.get("origin") or {}
        if not isinstance(origin, dict) or origin.get("kind") != "channel":
            continue
        if server and origin.get("server") not in (None, server):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        if needle in text:
            count += 1
print(count)
