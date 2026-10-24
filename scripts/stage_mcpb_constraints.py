#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Stage the release's dependency lock into the .mcpb bundle (nexus-2k66t.4.6).

The Claude Desktop bundle installs with ``uv sync``, which resolves fresh unless
it is given constraints. The lock already ships as
``src/nexus/_install/constraints.txt`` (generated from ``uv.lock`` by
``scripts/regen-install-constraints.sh``, pinned equal to it by
``tests/test_install_constraints_pin.py``); this copies that file into the
bundle directory as ``constraints.txt`` immediately before ``mcpb pack``, so
there is one lock and no second hand-kept copy, and appends one line,
``conexus==<release>``: the lock leaves the project itself out, and a bundle
whose floor is ``conexus>=N`` would otherwise be free to take a newer conexus
whose own floors the lock does not satisfy. ``mcpb/src/bootstrap.py`` reads the
result at install time. The staged copy is gitignored.

The source is read with the launcher's own reader (``bootstrap._read_constraints``,
so ``_PIN_LINE``): a file with any non-comment line the launcher would reject is
refused here, where the launcher would instead install unpinned. A source with
fewer than ``MIN_PINS`` pins is refused too: a near-empty file would pin nothing
and look like success.

    python3 scripts/stage_mcpb_constraints.py [--dest mcpb] [--source FILE] [--conexus-version X.Y.Z]

``--conexus-version`` defaults to the bundle manifest's version (release.yml
checks that equals the tag and passes it explicitly).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "src" / "nexus" / "_install" / "constraints.txt"
DEFAULT_DEST = REPO / "mcpb"
STAGED_NAME = "constraints.txt"

#: The lock has 219 pins (uv.lock's package count less the project). A floor close
#: enough that an export which lost a tenth of them fails, far above "came back empty".
MIN_PINS = 200

#: A release version: no marker, no wildcard, no whitespace.
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.!+_-]*$")


def _launcher():
    """The bundle launcher's module: the one definition of what a pin line is."""
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap_stage", REPO / "mcpb" / "src" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest_version(dest: Path) -> str:
    for candidate in (dest / "manifest.json", REPO / "mcpb" / "manifest.json"):
        if candidate.is_file():
            return str(json.loads(candidate.read_text(encoding="utf-8")).get("version", ""))
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="bundle directory (default: mcpb/)")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="constraints file to stage")
    ap.add_argument("--conexus-version", default=None, help="the release to pin (default: the manifest's version)")
    args = ap.parse_args(argv)

    if not args.source.is_file():
        print(f"stage_mcpb_constraints: source {args.source} does not exist", file=sys.stderr)
        return 1
    if not args.dest.is_dir():
        print(f"stage_mcpb_constraints: bundle directory {args.dest} does not exist", file=sys.stderr)
        return 1
    launcher = _launcher()
    pins, reason = launcher._read_constraints(str(args.source))
    if pins is None:
        print(f"stage_mcpb_constraints: {args.source}: {reason}; refusing to stage it", file=sys.stderr)
        return 1
    if len(pins) < MIN_PINS:
        print(
            f"stage_mcpb_constraints: {args.source} holds only {len(pins)} pins (floor {MIN_PINS}); "
            "refusing to ship a bundle that would pin almost nothing",
            file=sys.stderr,
        )
        return 1
    version = args.conexus_version if args.conexus_version is not None else _manifest_version(args.dest)
    own = f"conexus=={version}"
    if not _VERSION.match(version) or not launcher._PIN_LINE.match(own):
        print(f"stage_mcpb_constraints: {version!r} is not a version to pin conexus to", file=sys.stderr)
        return 1
    present = [p for p in pins if p.lower().startswith("conexus==")]
    if present and present != [own]:
        print(
            f"stage_mcpb_constraints: {args.source} already pins {present[0]}, not {own}; refusing",
            file=sys.stderr,
        )
        return 1
    text = args.source.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    if not present:
        text += f"# the release this bundle was built from (stage_mcpb_constraints.py)\n{own}\n"
    target = args.dest / STAGED_NAME
    target.write_text(text, encoding="utf-8", newline="\n")
    total = len(launcher._read_constraints(str(target))[0] or ())
    print(f"staged {total} pins ({own}): {args.source} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
