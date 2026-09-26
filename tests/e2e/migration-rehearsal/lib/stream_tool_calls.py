# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read a ``claude -p --output-format stream-json --verbose`` transcript on stdin.

RDR-219 Phase 3b (nexus-wauo1.40): the grant-mode proofs must show the MCP tool
was really CALLED and really RETURNED, not that the model wrote a marker line.

    stream_tool_calls.py --result OUT TOOL [TOOL ...]

Writes the run's final result text to OUT, then prints one line per TOOL:
``<tool> ok``, ``<tool> error`` (a tool_result with is_error, an auth failure
in its text, or an empty body) or ``<tool> missing`` (no tool_use for it).
Prints tool NAMES and statuses only, never tool inputs or result bodies.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

AUTH_FAILURE = re.compile(r"not logged in|invalid api key|authentication_error", re.I)


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("tools", nargs="+")
    args = ap.parse_args(argv)

    use_ids: dict[str, str] = {}  # tool_use id -> tool name
    status: dict[str, str] = {t: "missing" for t in args.tools}
    result_text = ""
    for line in sys.stdin:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result_text = str(event.get("result", ""))
            continue
        for item in (event.get("message") or {}).get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") in status:
                use_ids[str(item.get("id"))] = item["name"]
                if status[item["name"]] == "missing":
                    status[item["name"]] = "error"  # until a good result arrives
            elif item.get("type") == "tool_result":
                name = use_ids.get(str(item.get("tool_use_id")))
                if name is None:
                    continue
                body = _text(item.get("content"))
                if not item.get("is_error") and body.strip() and not AUTH_FAILURE.search(body):
                    status[name] = "ok"
    with open(args.result, "w", encoding="utf-8") as fh:
        fh.write(result_text)
    for tool in args.tools:
        print(f"{tool} {status[tool]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
