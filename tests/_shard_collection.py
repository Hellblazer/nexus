# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every CI test shard must split the SAME collection (nexus-bo5lv).

pytest-split's least_duration (nexus-zkr7q) sorts the ids and hands each test to the
lightest group, so one test missing from one leg's collection moves most of the split:
measured on the real ids and durations, 20k to 26k of 31k tests change group. The legs
would then run overlapping groups with a gap, and every shard would still pass. Nothing
else sees it: the executed floor counts one shard, and the fan-ins sum whatever ran.

Each shard writes ``nx-shard-collection-<worker>.json`` beside its DDL report (one per xdist worker,
which is where collection happens): how many tests the split was given and an XOR of their
sha256 digests, and the same two numbers for the group it kept. pytest-gate then checks:

* every report of every shard names the same split input (count and digest);
* the groups' counts add up to that count;
* the groups' digests XOR to the input's digest, so a test in two groups and one in none
  cannot cancel out as they would in the counts alone.

An unverified report (rerunning the split on the recorded input kept other tests) is printed as a
note, not a failure: the partition above is the gate, and the rerun leans on pytest-split internals.

    python -m tests._shard_collection <dir> --shards 6

``--write-durations PATH`` then refreshes the split's durations file from the same run (nexus-zkr7q):
each shard's ``nx-shard-durations.json`` holds the seconds its tests took there, and the six are
merged over PATH's existing entries. It writes nothing unless the check passed and every group
left one, so the refresh comes from one whole, partitioned run.

STANDARD LIBRARY ONLY: pytest-gate runs it with no pytest installed. The pytest half that
writes the report is ``tests/_shard_collection_plugin.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path

REPORT_GLOB = "nx-shard-collection*.json"
DURATIONS_NAME = "nx-shard-durations.json"


def digest(nodeids: Iterable[str]) -> tuple[int, str]:
    """``(count, xor of the sha256 of each id)``, order-free; the XOR of two disjoint sets'
    digests is the digest of their union."""
    n = 0
    acc = 0
    for nodeid in nodeids:
        n += 1
        acc ^= int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), "big")
    return n, f"{acc:064x}"


def report(full: Iterable[str], selected: Iterable[str], group: int, splits: int) -> dict[str, object]:
    full_n, full_x = digest(full)
    sel_n, sel_x = digest(selected)
    return {
        "group": group, "splits": splits,
        "collected": full_n, "collected_xor": full_x,
        "selected": sel_n, "selected_xor": sel_x,
    }


def check(directory: Path, shards: int) -> tuple[list[str], str]:
    """``(problems, summary)`` for the reports under *directory* (searched recursively)."""
    problems: list[str] = []
    notes: list[str] = []
    by_group: dict[int, dict[str, object]] = {}
    inputs: set[tuple[object, object]] = set()
    paths = sorted(directory.rglob(REPORT_GLOB))
    for path in paths:
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: unreadable ({exc})")
            continue
        if rec.get("splits") != shards:
            problems.append(f"{path}: written for {rec.get('splits')} splits, not {shards}")
            continue
        if rec.get("verified") is not True:
            notes.append(f"{path}: the split was not reproduced from the recorded input (advice only)")
        inputs.add((rec.get("collected"), rec.get("collected_xor")))
        group = rec.get("group")
        seen = by_group.get(group)  # type: ignore[arg-type]
        if seen is None:
            by_group[group] = rec  # type: ignore[index]
        elif (seen.get("selected"), seen.get("selected_xor")) != (rec.get("selected"), rec.get("selected_xor")):
            problems.append(f"group {group}: two of its workers kept different tests ({path})")
    missing = sorted(set(range(1, shards + 1)) - set(by_group))
    if missing:
        problems.append(f"no report from group(s) {missing}: a shard that wrote none cannot be checked")
    if len(inputs) > 1:
        problems.append(
            "the shards split different collections (count, digest): "
            + ", ".join(f"{c} {str(x)[:12]}" for c, x in sorted(inputs, key=str))
        )
    summary = f"{len(paths)} report(s), {len(by_group)} of {shards} groups"
    if not problems and inputs:
        (collected, collected_xor), = inputs
        total = sum(int(r["selected"]) for r in by_group.values())  # type: ignore[arg-type]
        acc = 0
        for r in by_group.values():
            acc ^= int(str(r["selected_xor"]), 16)
        if total != collected:
            problems.append(f"the groups hold {total} tests between them, the split was given {collected}")
        if f"{acc:064x}" != collected_xor:
            problems.append("the groups are not a partition of the split input: some test is in two groups or none")
        summary += f", each given the same {collected} tests, groups holding {total}"
    return problems, summary + "".join(f"\n  note: {n}" for n in notes)


def merge_durations(directory: Path, shards: int) -> tuple[list[str], dict[str, float]]:
    """``(problems, seconds by test id)`` from every shard's durations file under *directory*; a file's
    group is the one its sibling collection report names."""
    problems: list[str] = []
    merged: dict[str, float] = {}
    groups: set[int] = set()
    for path in sorted(directory.rglob(DURATIONS_NAME)):
        siblings = sorted(path.parent.glob(REPORT_GLOB))
        try:
            sibling = json.loads(siblings[0].read_text()) if siblings else {}
            seconds = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: unreadable ({exc})")
            continue
        group = sibling.get("group")
        if group is None:
            problems.append(f"{path}: no collection report beside it names its group")
        elif len(seconds) != sibling.get("selected"):
            # A green shard times every test it kept; fewer means the file is not that run's.
            problems.append(f"group {group}: {len(seconds)} durations for {sibling.get('selected')} tests kept ({path})")
        elif group in groups:
            problems.append(f"group {group}: more than one durations file ({path})")
        else:
            groups.add(group)
            merged.update(seconds)
    missing = sorted(set(range(1, shards + 1)) - groups)
    if missing:
        problems.append(f"no durations from group(s) {missing}")
    return problems, merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--write-durations", type=Path, metavar="PATH",
                        help="merge the shards' durations into PATH (only when the check passes)")
    args = parser.parse_args(argv)
    problems, summary = check(args.directory, args.shards)
    print(f"shard collection check: {summary}: {'ok' if not problems else 'FAILED'}")
    for p in problems:
        print(f"  {p}")
    if problems or args.write_durations is None:
        return 1 if problems else 0
    problems, merged = merge_durations(args.directory, args.shards)
    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"durations: {args.write_durations} left unchanged")
        return 1
    target: Path = args.write_durations
    current = json.loads(target.read_text()) if target.exists() else {}
    current.update(merged)
    target.write_text(json.dumps(current, sort_keys=True, indent=4))
    print(f"durations: {len(merged)} measured tests merged into {target} ({len(current)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
