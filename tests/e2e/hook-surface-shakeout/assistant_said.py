# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exit 0 iff an ASSISTANT message in a Claude Code transcript carries TOKEN.

Used by the RDR-208 MVV to tell a model's REPLY from the prompt's own echo:
the prompt names the token it asks for, so any check against the rendered
pane passes before the model has answered (measured 2026-09-18, one billed
run). The transcript's own role field is unambiguous and does not depend on
how a Claude Code version renders a turn.

Usage: assistant_said.py TRANSCRIPT.jsonl TOKEN
"""
import json
import sys

path, token = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    for line in fh:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        if entry.get("type") != "assistant" or message.get("role") != "assistant":
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        if token in text:
            raise SystemExit(0)
raise SystemExit(1)
