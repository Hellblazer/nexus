# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recording stand-in for ``conexus/launch/nx_setup.py``.

The setup tools (the real ``nx-mcp``'s and the stand-in server's) start
``nx_setup.py``, which on ``start`` begins a real build. A test that needs the
tools to start "the script" without a build puts this file in its place: it
appends each command line it is given to ``calls.jsonl`` beside it, and prints
the view the test wrote for that command in ``views.json``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
views = json.load(open(os.path.join(here, "views.json"), encoding="utf-8"))
command = sys.argv[1]
if views.get("_exit"):
    sys.exit(int(views["_exit"]))
if views.get("_raw") is not None:
    sys.stdout.write(views["_raw"])
else:
    sys.stdout.write(json.dumps(views[command]) + "\\n")
"""


class Recorder:
    """The recording script at ``<folder>/nx_setup.py``."""

    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self.path = folder / "nx_setup.py"
        self.path.write_text(SOURCE, encoding="utf-8")

    def views(self, **views: Any) -> None:
        """What each command (``status``, ``start``, ``not-now``) prints; or
        ``_exit`` / ``_raw`` to misbehave."""
        (self.folder / "views.json").write_text(json.dumps(views), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        log = self.folder / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
