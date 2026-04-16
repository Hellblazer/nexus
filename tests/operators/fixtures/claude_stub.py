#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stub ``claude`` process for RDR-079 operator-pool tests.

Reads one-JSON-per-line user turns from stdin and emits a canned
streaming response per turn to stdout. Mimics the shape that the real
``claude -p --input-format stream-json --output-format stream-json
--verbose`` pipeline produces (RDR-079 Empirical Finding 1 + 3).

Each turn → these emit events in order:
  1. ``assistant`` message containing a ``StructuredOutput`` tool_use
     whose ``input`` echoes the ``{extractions: [...]}`` content from
     the user turn (or a default payload when absent).
  2. ``result`` record with ``subtype: success``, fake duration/cost,
     and synthetic token usage.

Behavior is configurable via env vars read at startup:
  * ``STUB_INPUT_TOKENS_PER_TURN`` — value written as usage.input_tokens
    (default 100).
  * ``STUB_OUTPUT_TOKENS_PER_TURN`` — usage.output_tokens (default 50).
  * ``STUB_HANG`` — if set to "1", the stub reads the user turn and
    never replies (tests timeout handling).
  * ``STUB_CRASH_ON_TURN`` — integer N; the stub exits(1) BEFORE
    emitting the Nth turn's response (tests worker-crash recovery).

Usable anywhere ``claude`` would be invoked: the test passes this
script's path as ``argv[0]`` to ``build_worker_cmdline`` via a
monkeypatch on ``shutil.which``, or passes it directly to
``asyncio.create_subprocess_exec``.
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    input_tokens = int(os.environ.get("STUB_INPUT_TOKENS_PER_TURN", "100"))
    output_tokens = int(os.environ.get("STUB_OUTPUT_TOKENS_PER_TURN", "50"))
    hang = os.environ.get("STUB_HANG") == "1"
    crash_on_turn = int(os.environ.get("STUB_CRASH_ON_TURN", "0"))

    turn_no = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        if payload.get("type") != "user":
            continue

        turn_no += 1

        if crash_on_turn and turn_no == crash_on_turn:
            return 1

        if hang:
            # Wait forever. Tests assert worker-timeout handling fires.
            time.sleep(3600)
            return 0

        # Parse the user content for echo if structured
        user_content = payload.get("message", {}).get("content", "")
        if isinstance(user_content, list):
            text_parts = [c.get("text", "") for c in user_content if isinstance(c, dict)]
            user_text = " ".join(text_parts)
        else:
            user_text = str(user_content)

        tool_use_input: dict = {
            "extractions": [{"echo": user_text[:120]}],
        }
        # 1. assistant message with StructuredOutput tool_use
        sys.stdout.write(json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_stub_{turn_no}",
                        "name": "StructuredOutput",
                        "input": tool_use_input,
                    },
                ],
            },
        }) + "\n")
        sys.stdout.flush()

        # 2. user tool_result event (simulates claude's internal loop)
        sys.stdout.write(json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": f"toolu_stub_{turn_no}",
                    "content": "Structured output provided successfully",
                }],
            },
        }) + "\n")
        sys.stdout.flush()

        # 3. final result record per-turn
        sys.stdout.write(json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "",
            "num_turns": 1,
            "duration_ms": 42,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "service_tier": "standard",
            },
        }) + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
