#!/usr/bin/env python3
"""RDR-111 CA-6 spike (nexus-1h26): logging-only hook payload capture.

Reads the hook's stdin JSON and appends it to a per-type capture file at
``$NX_SPIKE_CAPTURE_DIR/<hook_type>.jsonl``. Writes nothing to stdout so
the hook is a true no-op for the session.

The capture-dir default is ``~/.config/nexus/spike-ca6/``. Set
``NX_SPIKE_CAPTURE_DIR`` to redirect (cc-validation harness uses its
TEST_HOME). Hook type comes from the script's first argv (registered
per-type in settings.json), falling back to the script name.

Usage in settings.json (per hook type):

    "hooks": {
      "SubagentStop": [
        {"hooks": [{"type": "command",
          "command": "python3 .../spike_ca6/log_payload.py SubagentStop"}]}
      ],
      ...
    }
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else Path(sys.argv[0]).stem
    capture_dir = Path(
        os.environ.get(
            "NX_SPIKE_CAPTURE_DIR",
            str(Path.home() / ".config" / "nexus" / "spike-ca6"),
        )
    )
    capture_dir.mkdir(parents=True, exist_ok=True)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw, "_parse_error": True}

    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "hook_type": hook_type,
        "payload": payload,
    }
    out = capture_dir / f"{hook_type}.jsonl"
    # Intentional: an IOError here (disk full, capture_dir unwritable)
    # is a spike-fatal condition; the user wants to know capture is
    # broken rather than silently lose payloads. Production hook scripts
    # would swallow; this is a throwaway diagnostic and fail-loud wins.
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
