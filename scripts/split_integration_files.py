# SPDX-License-Identifier: AGPL-3.0-or-later
"""Split an integration file selection into N shards, balanced on recorded time.

ci.yml's affected-integration job runs every integration file on a pull request
into main (nexus-jh1f2 cut 4) and splits them three ways. It reads the selected
files on stdin, one per line, and prints the files of shard ``--shard`` of
``--of``.

A file weighs its seconds in ``--durations`` (``tests/integration_durations.json``,
per-file wall seconds recorded from a whole-family run with ``--record``). A file
with no recorded time weighs its test count in ``--collected`` (the job's
``pytest --collect-only -q`` output, ``path::test`` lines) times the median
seconds per test of the recorded files; with nothing recorded, its test count;
with no count either, 1. Test count alone does not balance this family: one file,
tests/test_indexer_e2e.py, took 1321 of the 3281 seconds measured on chas on
2026-11-11. ``.test_durations.json`` records 12 of the 143 integration files.

Files go heaviest first to the lightest shard, ties broken by path, so every
shard computes the same split. A shard can come out empty when there are fewer
files than shards. A stale time table costs balance, never coverage: every file
still lands in exactly one shard. Stdlib only: the job runs it on the runner's
bare python3.

``--record OUT JUNIT...`` writes the time table from JUnit files instead.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def test_counts(collect_output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in collect_output.splitlines():
        if "::" in line and not line.startswith(" "):
            path = line.split("::", 1)[0]
            counts[path] = counts.get(path, 0) + 1
    return counts


def weights(files: list[str], counts: dict[str, int], durations: dict[str, float]) -> dict[str, float]:
    per_test = [durations[f] / counts[f] for f in files if durations.get(f, 0) > 0 and counts.get(f, 0) > 0]
    unit = statistics.median(per_test) if per_test else 1.0
    return {f: durations.get(f) or (counts.get(f, 0) or 1) * unit for f in files}


def split(files: list[str], of: int, counts: dict[str, int], durations: dict[str, float] | None = None) -> list[list[str]]:
    unique = sorted(dict.fromkeys(files))
    weight = weights(unique, counts, durations or {})
    shards: list[list[str]] = [[] for _ in range(of)]
    load = [0.0] * of
    for f in sorted(unique, key=lambda p: (-weight[p], p)):
        k = min(range(of), key=lambda i: (load[i], i))
        shards[k].append(f)
        load[k] += weight[f]
    return [sorted(s) for s in shards]


def _module_to_path(classname: str, known: set[str]) -> str | None:
    """``tests.integration.test_x.TestY`` -> ``tests/integration/test_x.py``: the longest known prefix."""
    parts = classname.split(".")
    for n in range(len(parts), 0, -1):
        cand = "/".join(parts[:n]) + ".py"
        if cand in known:
            return cand
    return None


def record(junits: list[Path], known: set[str]) -> dict[str, float]:
    seconds: dict[str, float] = {}
    for j in junits:
        for case in ET.parse(j).getroot().iter("testcase"):
            path = _module_to_path(case.get("classname") or "", known)
            if path:
                seconds[path] = seconds.get(path, 0.0) + float(case.get("time") or 0)
    return {k: round(v, 1) for k, v in sorted(seconds.items())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Split an integration file selection into shards.")
    ap.add_argument("--shard", type=int, help="1-based shard number")
    ap.add_argument("--of", type=int, help="shard count")
    ap.add_argument("--collected", help="pytest --collect-only -q output over the whole selection")
    ap.add_argument("--durations", help="per-file seconds, JSON (tests/integration_durations.json)")
    ap.add_argument("--record", nargs="+", metavar=("OUT", "JUNIT"),
                    help="write per-file seconds from JUnit files to OUT; files on stdin name the known paths")
    args = ap.parse_args(argv)
    if args.record:
        if len(args.record) < 2:
            sys.stderr.write("split_integration_files: --record needs OUT and at least one JUnit file\n")
            return 2
        known = {line.strip() for line in sys.stdin if line.strip()}
        table = record([Path(p) for p in args.record[1:]], known)
        Path(args.record[0]).write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
        return 0
    if args.of is None or args.shard is None or args.of < 1 or not 1 <= args.shard <= args.of:
        sys.stderr.write(f"split_integration_files: shard {args.shard} of {args.of} is not a shard\n")
        return 2
    counts = test_counts(Path(args.collected).read_text(encoding="utf-8")) if args.collected else {}
    durations: dict[str, float] = {}
    if args.durations and Path(args.durations).is_file():
        durations = json.loads(Path(args.durations).read_text(encoding="utf-8"))
    files = [line.strip() for line in sys.stdin if line.strip()]
    sys.stdout.writelines(f"{f}\n" for f in split(files, args.of, counts, durations)[args.shard - 1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
