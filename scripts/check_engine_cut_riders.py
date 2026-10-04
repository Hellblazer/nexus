#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Published engine binaries must be under their size ceilings (nexus-ujbz8).

``sizes <engine-service-vX.Y.Z>``
    Reads the release's asset sizes (``gh release view --json assets``, or
    ``--assets-json FILE`` for a saved copy) and fails when a binary is back
    at its pre-fix size. Exit 0 ok, 1 an asset is over its ceiling or absent,
    2 the release could not be read.

Ceilings are in MiB and sit between the old and the fixed size so a regression
trips them and normal growth does not: v0.1.142 shipped linux-amd64 at 231.8,
linux-arm64 at 227.0 and mac-arm64 at 193.3; nexus-lhr6a measured the fixed
build at 150 (amd64) and 154 (mac). It never measured linux-arm64; its
ceiling uses the amd64 margin (see ``SIZE_CEILINGS_MIB``). ``--max NAME=MIB``
overrides one ceiling.

Placement. ``sizes`` is BLOCKING in ``scripts/promote_engine_release.sh``, the
draft-to-published gate (engine-service-release.yml's promote-release job): an
oversized binary leaves the release a DRAFT instead of publishing an immutable
tag whose only remedy is a re-cut. The skill's Step 5c re-runs it against the
published release as a second read.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_MIB = 1024 * 1024

#: Release asset name -> ceiling in MiB. The pre-fix sizes were 231.8, 227.0, 193.3.
#: amd64 (measured 150) and mac (measured 154) carry 21 to 25 MiB of headroom.
#: linux-arm64 was NEVER measured by nexus-lhr6a. Its pre-fix size was 2% under
#: amd64's, so the fixed build is expected at about 147 (150 x 227.0 / 231.8, an
#: estimate, not a measurement); with the same 25 MiB margin that is 172, and
#: 175 is used so the two linux binaries share one ceiling. The old 190 sat 40
#: MiB over the expectation and let a partial regression pass. Replace the
#: estimate with the first published linux-arm64 size.
SIZE_CEILINGS_MIB: dict[str, int] = {
    "nexus-service-linux-amd64": 175,
    "nexus-service-linux-arm64": 175,
    "nexus-service-mac-arm64": 175,
}


def check_sizes(
    assets: list[dict],
    ceilings_mib: dict[str, int] | None = None,
) -> tuple[int, list[str]]:
    """Return ``(exit_code, report_lines)`` for ``sizes`` from release asset dicts."""
    ceilings = SIZE_CEILINGS_MIB if ceilings_mib is None else ceilings_mib
    by_name = {a.get("name"): a.get("size") for a in assets}
    lines: list[str] = []
    bad = 0
    for name, ceiling in sorted(ceilings.items()):
        size = by_name.get(name)
        if not isinstance(size, int) or size <= 0:
            bad += 1
            lines.append(f"MISSING  {name}: no such asset (or size {size!r}) on the release")
            continue
        mib = size / _MIB
        if mib > ceiling:
            bad += 1
            lines.append(
                f"TOO BIG  {name}: {mib:.1f} MiB > {ceiling} MiB ceiling "
                "(the dedup did not take effect in the release build, nexus-ujbz8)"
            )
        else:
            lines.append(f"ok       {name}: {mib:.1f} MiB <= {ceiling} MiB")
    if bad:
        lines.append(f"FAILED: {bad} asset check(s) failed")
        return 1, lines
    lines.append(f"PASSED: {len(ceilings)} binary size(s) under their ceilings")
    return 0, lines


def _release_assets(tag: str, assets_json: str | None, repo: str | None = None) -> list[dict]:
    if assets_json:
        data = json.loads(Path(assets_json).read_text())
    else:
        proc = subprocess.run(
            ["gh", "release", "view", tag, *(["--repo", repo] if repo else []), "--json", "assets"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh release view {tag} failed: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
    assets = data.get("assets") if isinstance(data, dict) else data
    if not isinstance(assets, list):
        raise RuntimeError("no 'assets' list in the release JSON")
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sizes", help="published release assets must be under their size ceilings")
    s.add_argument("tag", help="engine-service-vX.Y.Z")
    s.add_argument("--assets-json", default=None, help="read a saved `gh release view --json assets` instead of calling gh")
    s.add_argument("--repo", default=None, help="owner/repo for gh (default: the current checkout's)")
    s.add_argument("--max", action="append", default=[], metavar="NAME=MIB", help="override one ceiling")
    args = parser.parse_args(argv)

    ceilings = dict(SIZE_CEILINGS_MIB)
    for item in args.max:
        name, _, val = item.partition("=")
        if not val.isdigit():
            print(f"UNVERIFIABLE: --max {item!r} is not NAME=MIB", file=sys.stderr)
            return 2
        ceilings[name] = int(val)
    try:
        assets = _release_assets(args.tag, args.assets_json, args.repo)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"UNVERIFIABLE: {exc}", file=sys.stderr)
        return 2
    rc, lines = check_sizes(assets, ceilings)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
