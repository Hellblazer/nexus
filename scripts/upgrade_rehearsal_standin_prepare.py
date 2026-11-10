#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STAND-IN prepare for the upgrade rehearsal (nexus-f3yqd.5). Not the real seeder.

The real seeder is scripts/upgrade_boot_writes.py --prepare-only (nexus-f3yqd.1: ~86k rows).
This stand-in exists so the rehearsal's driver and wrapper can be developed and run end to end
before that lands, and as a small, fast fixture. It builds the old state with the OLD client
(the nx the driver installed as a uv tool in the throwaway HOME): ``nx init``, a few hundred
documents through ``nx store put`` and one ``nx index md``, one sentinel note, then stops the
engine and Postgres and prints one JSON object (also written to --json) with the keys the driver
reads: verdict, collections (name -> rows), prepare_s.

It runs under the driver's scrubbed environment (HOME, NEXUS_CONFIG_DIR, NX_LOCAL=1).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SENTINEL_TITLE = "rehearsal-standin-sentinel"
SENTINEL_TEXT = ("Sentinel note for the upgrade rehearsal stand-in: the kestrel counts copper "
                 "lanterns along the harbour wall before dawn.")
SUBJECT = "rehearsal-standin"
WORDS = ("amber basalt cinder delta ember fjord garnet harbor iris juniper kelp lichen marble "
         "nectar onyx pebble quartz reed slate tundra umber vellum willow xenon yarrow zephyr").split()


def _nx(nx: str, *args: str, stdin: str | None = None, timeout: float = 1800.0) -> subprocess.CompletedProcess:
    return subprocess.run([nx, *args], input=stdin, text=True, capture_output=True, timeout=timeout)


def _must(proc: subprocess.CompletedProcess, what: str) -> None:
    if proc.returncode != 0:
        sys.stderr.write(f"stand-in prepare: {what} failed (exit {proc.returncode})\n")
        sys.stderr.write((proc.stdout or "")[-1500:] + "\n")
        sys.stderr.write((proc.stderr or "")[-1500:] + "\n")
        raise SystemExit(2)


def parse_collection_rows(listing: str) -> dict[str, int]:
    """``nx collection list`` text -> {name: first integer on the row}. Tolerant of columns."""
    out: dict[str, int] = {}
    for line in listing.splitlines():
        m = re.match(r"\s*([a-z0-9_.-]+__\S+)\s+(.*)", line)
        if not m:
            continue
        ints = re.findall(r"(?<![\w.])\d[\d,]*(?![\w.])", m.group(2))
        if ints:
            out[m.group(1)] = int(ints[0].replace(",", ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", required=True)
    ap.add_argument("--prev-client", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--nx", required=True, help="the old client's nx")
    ap.add_argument("--docs", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261110)
    args = ap.parse_args()
    t0 = time.monotonic()
    nx = args.nx

    _must(_nx(nx, "init", "--no-autostart", "--no-beads-prime", "-y"), "nx init")
    rng = random.Random(args.seed)

    def put(i: int) -> subprocess.CompletedProcess:
        local = random.Random(args.seed + i)
        body = " ".join(local.choice(WORDS) + str(local.randrange(1000)) for _ in range(120))
        return _nx(nx, "store", "put", "-", "--title", f"rehearsal-doc-{i:04d}", "--collection", SUBJECT,
                   stdin=body)

    _must(_nx(nx, "store", "put", "-", "--title", SENTINEL_TITLE, "--collection", SUBJECT, stdin=SENTINEL_TEXT),
          "store put of the sentinel")
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, proc in enumerate(pool.map(put, range(args.docs))):
            _must(proc, f"store put of document {i}")

    md = Path(args.work) / "rehearsal-standin-note.md"
    md.write_text("# Rehearsal stand-in note\n\n" + "\n\n".join(
        " ".join(rng.choice(WORDS) for _ in range(60)) for _ in range(20)) + "\n")
    _must(_nx(nx, "index", "md", str(md), "--collection", SUBJECT), "nx index md")

    listing = _nx(nx, "collection", "list")
    _must(listing, "nx collection list")
    collections = parse_collection_rows(listing.stdout)
    _nx(nx, "daemon", "service", "stop", "--with-pg", timeout=300.0)

    result = {
        "verdict": "prepared" if collections else "error",
        "standin": True,
        "prev_client": args.prev_client,
        "docs": args.docs,
        "collections": collections,
        "collection_list": listing.stdout[-2000:],
        "prepare_s": round(time.monotonic() - t0, 1),
    }
    Path(args.json).write_text(json.dumps(result, indent=2) + "\n")
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0 if collections else 2


if __name__ == "__main__":
    sys.exit(main())
