# SPDX-License-Identifier: AGPL-3.0-or-later
"""Split an integration file selection into N shards, balanced on test count.

ci.yml's affected-integration job runs every integration file on a pull request
into main (nexus-jh1f2 cut 4) and splits them three ways. It reads the selected
files on stdin, one per line, and prints the files of shard ``--shard`` of
``--of``. Each file weighs the number of its tests in ``--collected``, the job's
``pytest --collect-only -q`` output over the whole selection (``path::test``
lines); a file with none listed weighs 1. ``.test_durations.json`` is no help
here: it records 12 of the 143 integration files. Files go heaviest first to the
lightest shard, ties broken by path, so every shard computes the same split. A
shard can come out empty when there are fewer files than shards. Stdlib only:
the job runs it on the runner's bare python3.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def test_counts(collect_output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in collect_output.splitlines():
        if "::" in line and not line.startswith(" "):
            path = line.split("::", 1)[0]
            counts[path] = counts.get(path, 0) + 1
    return counts


def split(files: list[str], of: int, counts: dict[str, int]) -> list[list[str]]:
    unique = sorted(dict.fromkeys(files))
    weight = {f: counts.get(f, 0) or 1 for f in unique}
    shards: list[list[str]] = [[] for _ in range(of)]
    load = [0] * of
    for f in sorted(unique, key=lambda p: (-weight[p], p)):
        k = min(range(of), key=lambda i: (load[i], i))
        shards[k].append(f)
        load[k] += weight[f]
    return [sorted(s) for s in shards]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Split an integration file selection into shards.")
    ap.add_argument("--shard", type=int, required=True, help="1-based shard number")
    ap.add_argument("--of", type=int, required=True, help="shard count")
    ap.add_argument("--collected", help="pytest --collect-only -q output over the whole selection")
    args = ap.parse_args(argv)
    if args.of < 1 or not 1 <= args.shard <= args.of:
        sys.stderr.write(f"split_integration_files: shard {args.shard} of {args.of} is not a shard\n")
        return 2
    counts = test_counts(Path(args.collected).read_text(encoding="utf-8")) if args.collected else {}
    files = [line.strip() for line in sys.stdin if line.strip()]
    sys.stdout.writelines(f"{f}\n" for f in split(files, args.of, counts)[args.shard - 1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
