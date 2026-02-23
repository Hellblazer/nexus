#!/usr/bin/env python3
"""
PostToolUse hook: Validate bead creation includes context pointer.
Runs after bd create commands.
"""

import json
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = data.get('tool_input', {})
    command = tool_input.get('command', '')

    # Only care about bd create
    if 'bd create' not in command:
        sys.exit(0)

    # Check if context pointer was included
    if 'Context:' not in command and 'nx' not in command:
        result = {
            "message": "Tip: Include 'Context: nx' in bead description."
        }
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
