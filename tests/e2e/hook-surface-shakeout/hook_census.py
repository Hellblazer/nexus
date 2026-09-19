# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census every declared hook entry against what actually fired in a session.

Reads the shipped ``hooks.json`` for the DENOMINATOR -- the handlers the
plugin declares -- and a Claude Code debug log for the NUMERATOR, then reports
which declared handlers were never seen.

THE FAILURE THIS IS BUILT FOR IS SILENCE. Claude Code treats an unavailable
`mcp_tool` hook as a non-blocking error: it logs a skip and proceeds. A
hooks.json naming a tool the wheel does not register therefore does not fail
-- the guard just stops running. Twelve of RDR-215's entries are `mcp_tool`,
including the bd-close gate, the RDR-184 EXPECT writer and the orchestrator
guard, and that whole class is invisible to a green test suite.

NON-VACUITY IS THE POINT, so this file refuses to be reassuring:

* If the log yields NO recognisable hook activity at all, that is MISSING and
  exits non-zero. A census that examined nothing is not a clean census -- it
  is the exact shape RDR-215's own tally spent fifteen entries on (a check
  whose domain stopped containing its subject, passing because it could no
  longer see anything to fail on).
* A declared handler that never appears is reported by NAME, not summarised
  as a count, because the interesting question is always WHICH one.
* Skip and error lines fail the run even when every handler was also seen:
  firing once and being skipped once is not success.

Usage: hook_census.py HOOKS_JSON DEBUG_LOG [DEBUG_LOG ...]
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

#: Lines that mean a hook did not run. Claude Code's exact wording has moved
#: between versions, so these are matched loosely and ANY hit fails the run --
#: a false positive here costs one investigation, a false negative costs a
#: silent fail-open on twelve guards.
_TROUBLE = re.compile(
    r"hook\s+skipped|skipping\s+hook|mcp_tool\s+hook|hook\s+(?:failed|error|timed?\s*out)"
    r"|failed\s+to\s+(?:run|execute)\s+hook|not\s+connected",
    re.IGNORECASE,
)


def declared(hooks_json: pathlib.Path) -> dict[str, str]:
    """``handler name -> the events it is declared on``.

    Accumulates rather than assigns: ``hook_auto_approve`` is declared on BOTH
    ``PreToolUse`` and ``PermissionRequest``, and a plain ``out[name] = event``
    kept only the last, reporting a denominator of 24 against a manifest of 25
    entries and losing one event association. Caught by running this file
    against the real manifest before the container run that would have shipped
    the wrong denominator -- the same defect class the shakeout is built for,
    one layer up.
    """
    data = json.loads(hooks_json.read_text())
    events: dict[str, list[str]] = {}
    for event, groups in data["hooks"].items():
        for group in groups:
            for entry in group.get("hooks", []):
                if entry.get("type") == "mcp_tool":
                    name = entry.get("tool")
                elif entry.get("command") == "nx-hook":
                    args = entry.get("args") or []
                    name = f"nx-hook {args[0]}" if args else "nx-hook"
                elif entry.get("command") == "python3":
                    args = entry.get("args") or []
                    name = pathlib.Path(args[0]).name if args else "python3"
                else:
                    name = entry.get("command") or "?"
                if name:
                    events.setdefault(name, []).append(event)
    return {n: ",".join(sorted(set(e))) for n, e in events.items()}


def seen(names: list[str], blobs: list[str]) -> set[str]:
    hay = "\n".join(blobs)
    return {n for n in names if (n.split()[-1] if n.startswith("nx-hook") else n) in hay}


def main(argv: list[str]) -> int:
    hooks_json = pathlib.Path(argv[1])
    blobs = []
    for p in argv[2:]:
        path = pathlib.Path(p)
        if path.is_file():
            blobs.append(path.read_text(errors="replace"))
    decl = declared(hooks_json)
    if not decl:
        print("CENSUS MISSING: hooks.json declared no handlers -- the "
              "denominator is empty, so this census could not fail.")
        return 2
    if not blobs:
        print(f"CENSUS MISSING: none of {argv[2:]} exist, so nothing was examined.")
        return 2

    fired = seen(list(decl), blobs)
    trouble = sorted({
        line.strip()
        for blob in blobs
        for line in blob.splitlines()
        if _TROUBLE.search(line)
    })

    print(f"declared handlers : {len(decl)}")
    print(f"observed firing   : {len(fired)}")
    for name in sorted(decl):
        mark = "FIRED  " if name in fired else "  --   "
        print(f"  {mark} {decl[name]:<18} {name}")

    if not fired:
        print()
        print("CENSUS MISSING: not one declared handler was observed. That is a "
              "harness result, not a clean bill of health -- the log shape "
              "probably changed, or the plugin never loaded. Do NOT read this "
              "as a pass.")
        return 2

    rc = 0
    never = sorted(set(decl) - fired)
    if never:
        print()
        print(f"NEVER FIRED ({len(never)}), by name:")
        for name in never:
            print(f"  {decl[name]:<18} {name}")
        print("A declared handler that never fires is the fail-open this "
              "shakeout exists to catch, OR an event this run did not "
              "trigger. Decide which before treating it as either.")
        rc = 1

    if trouble:
        print()
        print(f"TROUBLE LINES ({len(trouble)}):")
        for line in trouble[:20]:
            print(f"  {line[:200]}")
        rc = 1

    if rc == 0:
        print()
        print("Every declared handler was observed, with no skip or error line.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
