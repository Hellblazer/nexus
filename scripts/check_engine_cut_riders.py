#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine-cut riders: named commits that MUST be in the tagged commit, and the proof they shipped.

An engine tag is cut from whatever commit the releaser names, and a fix that is
on develop does not ride a tag placed on an older commit. nexus-ujbz8 (Sam,
2026-09-30): the native-image size fix must ride the next cut. Two commits carry
it, and a tag that omits either ships binaries at the old size with no failing
check, because the release job is not covered by the embedded-resources checker
(nexus-zz2w7).

Two subcommands, one before the tag and one after the publish:

``ancestry <tag-commit>``
    Every rider in ``RIDERS`` must be an ancestor of ``<tag-commit>`` (exit 0),
    else the missing ones are named (exit 1). A rider that does not resolve in
    this clone, or a ``<tag-commit>`` that does not, is exit 2 (UNVERIFIABLE),
    never a pass: ``git merge-base --is-ancestor`` returns the same nonzero for
    "not an ancestor" and "no such object" only by exit code 1 versus 128, and a
    shell loop on ``||`` would merge them. An empty rider list is exit 2 too.

``sizes <engine-service-vX.Y.Z>``
    Reads the published release's asset sizes (``gh release view --json assets``,
    or ``--assets-json FILE`` for a saved copy) and fails when a binary is back
    at its pre-fix size. Exit 0 ok, 1 an asset is over its ceiling or absent,
    2 the release could not be read.

Ceilings are in MiB and sit between the old and the fixed size so a regression
trips them and normal growth does not: v0.1.142 shipped linux-amd64 at 231,
linux-arm64 at 227 and mac-arm64 at 193; the fixed build is about 150, well
under 227 and about 154. ``--max NAME=MIB`` overrides one ceiling.

Wired in ``.claude/skills/engine-release/SKILL.md`` (Step 2b before the tag,
Step 5c after the publish).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (commit, bead, what it carries). Entries stay until the bead closes; an entry
#: that is already in the last engine tag costs nothing, the check passes it.
RIDERS: tuple[tuple[str, str, str], ...] = (
    ("32f6987b2", "nexus-lhr6a",
     "native build before shade, forceCreation, osx ORT globs out of the traced "
     "metadata, ORT include names loadable libs only"),
    ("29653410b", "nexus-vwfc0",
     "4 more foreign traced globs removed; pom tests"),
)

_MIB = 1024 * 1024

#: Release asset name -> ceiling in MiB. The pre-fix sizes were 231, 227, 193.
SIZE_CEILINGS_MIB: dict[str, int] = {
    "nexus-service-linux-amd64": 175,
    "nexus-service-linux-arm64": 190,
    "nexus-service-mac-arm64": 175,
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def check_ancestry(
    repo: Path,
    tag_commit: str,
    riders: tuple[tuple[str, str, str], ...] = RIDERS,
) -> tuple[int, list[str]]:
    """Return ``(exit_code, report_lines)`` for ``ancestry``."""
    if not riders:
        return 2, ["UNVERIFIABLE: the rider list is empty; there is nothing to check"]
    head = _git(repo, "rev-parse", "--verify", "--quiet", f"{tag_commit}^{{commit}}")
    if head.returncode != 0:
        return 2, [f"UNVERIFIABLE: {tag_commit!r} does not resolve to a commit in this clone"]
    full = head.stdout.strip()
    lines = [f"tag commit {tag_commit} = {full}"]
    missing: list[str] = []
    unresolved: list[str] = []
    for sha, bead, what in riders:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").returncode != 0:
            unresolved.append(f"{sha} ({bead})")
            lines.append(f"UNRESOLVED {sha} {bead}: not a commit in this clone (fetch, or the sha is wrong)")
            continue
        rc = _git(repo, "merge-base", "--is-ancestor", sha, full).returncode
        if rc == 0:
            lines.append(f"ok       {sha} {bead}: {what}")
        elif rc == 1:
            missing.append(f"{sha} ({bead})")
            lines.append(f"MISSING  {sha} {bead}: {what}")
        else:
            unresolved.append(f"{sha} ({bead})")
            lines.append(f"UNRESOLVED {sha} {bead}: git merge-base exited {rc}")
    if unresolved:
        lines.append("UNVERIFIABLE: " + ", ".join(unresolved))
        return 2, lines
    if missing:
        lines.append(
            "FAILED: the tag commit does not carry " + ", ".join(missing)
            + "; tag a commit that descends from them (the develop tip does)"
        )
        return 1, lines
    lines.append(f"PASSED: all {len(riders)} rider commit(s) are ancestors of {full[:12]}")
    return 0, lines


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


def _release_assets(tag: str, assets_json: str | None) -> list[dict]:
    if assets_json:
        data = json.loads(Path(assets_json).read_text())
    else:
        proc = subprocess.run(
            ["gh", "release", "view", tag, "--json", "assets"],
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
    a = sub.add_parser("ancestry", help="riders must be ancestors of the commit to tag")
    a.add_argument("tag_commit", help="the commit (or ref) the engine tag will be placed on")
    s = sub.add_parser("sizes", help="published release assets must be under their size ceilings")
    s.add_argument("tag", help="engine-service-vX.Y.Z")
    s.add_argument("--assets-json", default=None, help="read a saved `gh release view --json assets` instead of calling gh")
    s.add_argument("--max", action="append", default=[], metavar="NAME=MIB", help="override one ceiling")
    args = parser.parse_args(argv)

    if args.cmd == "ancestry":
        rc, lines = check_ancestry(REPO_ROOT, args.tag_commit)
    else:
        ceilings = dict(SIZE_CEILINGS_MIB)
        for item in args.max:
            name, _, val = item.partition("=")
            if not val.isdigit():
                print(f"UNVERIFIABLE: --max {item!r} is not NAME=MIB", file=sys.stderr)
                return 2
            ceilings[name] = int(val)
        try:
            assets = _release_assets(args.tag, args.assets_json)
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"UNVERIFIABLE: {exc}", file=sys.stderr)
            return 2
        rc, lines = check_sizes(assets, ceilings)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
