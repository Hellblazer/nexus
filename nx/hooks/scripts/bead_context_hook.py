#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PostToolUse hook: Validate bead creation includes context pointer.
Also warns when beads reference RDRs that may not be accepted yet.
Runs after bd create commands.
"""

import json
import re
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

    messages = []

    # Check if context pointer was included
    if 'Context:' not in command and 'nx' not in command:
        messages.append("Tip: Include 'Context: nx' in bead description.")

    # Check for RDR references (RDR-024 guardrail)
    rdr_refs = re.findall(r'RDR-(\d+)', command, re.IGNORECASE)
    if rdr_refs:
        rdr_list = ', '.join(f'RDR-{r}' for r in rdr_refs)
        messages.append(
            f"Bead references {rdr_list}. "
            f"Verify RDR status before implementation: "
            f"/rdr-show {rdr_refs[0]}"
        )

    if messages:
        result = {"message": " | ".join(messages)}
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
