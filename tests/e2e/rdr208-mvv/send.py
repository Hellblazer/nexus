# SPDX-License-Identifier: AGPL-3.0-or-later
"""Call the mailbox_send MCP tool function directly and print its result as JSON.

Usage: send.py TO CORRELATION_ID BODY [FROM]. With no FROM the tool resolves
the sender from this process's claude ancestor's session marker, as it does
inside a real MCP server.
"""
import json
import sys

from nexus.mcp.core import mailbox_send

to, correlation_id, body = sys.argv[1:4]
from_address = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
sys.stdout.write(json.dumps(mailbox_send(
    to=to, body=body, kind="notice", correlation_id=correlation_id,
    from_address=from_address,
)) + "\n")
