#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the privacy-policy section 2 table from the LLM-surface registry (RDR-243 Phase 1 Step 5c, nexus-rfvyy.6.18).

One source: ``src/nexus/tables/llm-surfaces.toml``, read through ``nexus.tables.llm_surfaces``. The text between the
BEGIN and END markers in ``docs/privacy-policy.md`` is ``render_policy_table(RELEASE_ENFORCED_PHASE)``: the rows
enforced in the release being built, plus one line about the rows that are not (nexus-rfvyy.5). ``docs/sensitivity.md``
carries the same table without the line, and the line in a region of its own. The same module renders the
``--sensitive`` help. ``tests/test_privacy_policy_generated.py`` pins the files to the render.

Regenerate after a table edit or a change of the release phase:

    uv run python scripts/gen_privacy_policy_table.py --write

Verify without writing (what the Python pin does):

    uv run python scripts/gen_privacy_policy_table.py --check
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY = REPO_ROOT / "docs" / "privacy-policy.md"
SENSITIVITY = REPO_ROOT / "docs" / "sensitivity.md"


def begin_marker(name: str) -> str:
    return f"<!-- BEGIN GENERATED: {name} (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->"


def end_marker(name: str) -> str:
    return f"<!-- END GENERATED: {name} -->"


BEGIN = begin_marker("llm-surfaces")
END = end_marker("llm-surfaces")


def regions() -> list[tuple[pathlib.Path, str, str]]:
    """Every generated region: (file, region name, rendered text). The files are read at call time."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nexus.tables import llm_surfaces as ls  # noqa: PLC0415 — path set just above

    phase = ls.RELEASE_ENFORCED_PHASE
    return [
        (POLICY, "llm-surfaces", ls.render_policy_table(phase)),
        (SENSITIVITY, "llm-surfaces", ls.render_policy_table(phase, include_pending_line=False)),
        (
            SENSITIVITY, "llm-surfaces-pending",
            ls.not_yet_enforced_line(phase) or "Every registry row is enforced in this release.",
        ),
    ]


def splice(text: str, region: str, name: str = "llm-surfaces") -> str | None:
    """*text* with the region *name* replaced by *region*; ``None`` when its markers are missing or out of order."""
    begin, end = begin_marker(name), end_marker(name)
    start, stop = text.find(begin), text.find(end)
    if start < 0 or stop < start:
        return None
    return text[: start + len(begin)] + "\n" + region + "\n" + text[stop:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the generated regions")
    mode.add_argument("--check", action="store_true", help="exit 1 when a region is not current")
    args = ap.parse_args(argv)
    current: dict[pathlib.Path, str] = {}
    wanted: dict[pathlib.Path, str] = {}
    for path, name, region in regions():
        if path not in current:
            current[path] = path.read_bytes().decode("utf-8") if path.exists() else ""
            wanted[path] = current[path]
        spliced = splice(wanted[path], region, name)
        if spliced is None:
            sys.stderr.write(f"{path} has no BEGIN/END generated markers for {name}\n")
            return 1
        wanted[path] = spliced
    stale = [p for p in current if current[p] != wanted[p]]
    if args.write:
        for p in stale:
            p.write_bytes(wanted[p].encode("utf-8"))  # LF endings on every platform
            sys.stdout.write(f"wrote {p}\n")
        return 0
    for p in stale:
        sys.stderr.write(f"{p} is not current; run `uv run python scripts/gen_privacy_policy_table.py --write`\n")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
