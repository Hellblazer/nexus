"""Recording shim for `nx`: logs argv to $PROSE_EDIT_NX_CALLS, then runs the real CLI.

memory.py calls whatever PROSE_EDIT_NX names; the tests point it here so they can
assert on how memory.py drives `nx memory` (stdin for content, ids for reads, -y on
delete) without wrapping T2 in a mock. The real CLI still executes every call.

PROSE_EDIT_NX_DELAY_PUT=<seconds> sleeps before every `memory put`. That widens the gap
between a writer's read and its write, so a missing lock loses updates every time
instead of some of the time.
"""
import json
import os
import sys
import time

with open(os.environ["PROSE_EDIT_NX_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["memory", "put"] and os.environ.get("PROSE_EDIT_NX_DELAY_PUT"):
    time.sleep(float(os.environ["PROSE_EDIT_NX_DELAY_PUT"]))
from nexus.cli import main  # noqa: E402

sys.argv = ["nx", *sys.argv[1:]]
main()
