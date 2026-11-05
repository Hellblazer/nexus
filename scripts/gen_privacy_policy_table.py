#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the privacy-policy section 2 table from the LLM-surface registry (RDR-243 Phase 1 Step 5c, nexus-rfvyy.6.18).

One source: ``src/nexus/tables/llm-surfaces.toml``, read through ``nexus.tables.llm_surfaces``. The text between the
BEGIN and END markers in ``docs/privacy-policy.md`` is ``render_policy_table(RELEASE_ENFORCED_PHASE)``: the rows
enforced in the release being built, plus one line about the rows that are not. The same module renders the
``--sensitive`` help. ``tests/test_privacy_policy_generated.py`` pins the file to the render.

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
BEGIN = "<!-- BEGIN GENERATED: llm-surfaces (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->"
END = "<!-- END GENERATED: llm-surfaces -->"


def render_region() -> str:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nexus.tables import llm_surfaces as ls  # noqa: PLC0415 — path set just above

    return ls.render_policy_table(ls.RELEASE_ENFORCED_PHASE)


def splice(text: str, region: str) -> str | None:
    """*text* with the generated region replaced by *region*; ``None`` when the markers are missing or out of order."""
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        return None
    return text[: start + len(BEGIN)] + "\n" + region + "\n" + text[end:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the generated region")
    mode.add_argument("--check", action="store_true", help="exit 1 when the region is not current")
    args = ap.parse_args(argv)
    current = POLICY.read_bytes().decode("utf-8") if POLICY.exists() else ""
    wanted = splice(current, render_region())
    if wanted is None:
        sys.stderr.write(f"{POLICY} has no BEGIN/END generated markers for llm-surfaces\n")
        return 1
    if args.write:
        POLICY.write_bytes(wanted.encode("utf-8"))  # LF endings on every platform
        sys.stdout.write(f"wrote {POLICY}\n")
        return 0
    if current != wanted:
        sys.stderr.write(f"{POLICY} is not current; run `uv run python scripts/gen_privacy_policy_table.py --write`\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
