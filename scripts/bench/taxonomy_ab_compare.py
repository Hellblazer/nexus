# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print a saved ``taxonomy_ab.py`` leg-1 summary against a gated re-run (``--gate-from``), per set and arm.

Reads two output files; touches no engine.

    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_ab_compare.py \\
        bench/results/rdr134-taxonomy-ab-leg1-2026-11-16.json \\
        bench/results/rdr134-taxonomy-ab-leg1-gated-2026-11-16.json

The OFF arm is searched again in the gated run, so its row shows how far the same arm drifts between
two runs on its own; read each ON arm's change against that.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FIELDS = ("ndcg3", "recall10", "wins", "losses", "d_ndcg3")


def _cell(saved: dict[str, Any], gated: dict[str, Any], field: str) -> str:
    s, g = saved.get(field), gated.get(field)
    if s is None or g is None:  # OFF has no wins, losses or deltas
        return "-"
    if field in ("wins", "losses"):
        return f"{s}->{g}"
    return f"{s:.4f}->{g:.4f} ({g - s:+.4f})"


def compare(saved: dict[str, Any], gated: dict[str, Any]) -> str:
    """One block per set; one line per arm present in both summaries. An arm or set in only one
    summary is named, so a mismatch of shape is not read as a match."""
    ssum, gsum = saved["summary"], gated["summary"]
    lines = [f"n: saved {ssum['n']}, gated {gsum['n']}"]
    info = gated.get("gate")
    if info:
        lines.append(f"unrouted by the gate ({len(info['unrouted'])}): " + ", ".join(info["unrouted"]))
        if info["gate_not_in_sets"]:
            lines.append("gated but not in the sets: " + ", ".join(info["gate_not_in_sets"]))
    for sname in sorted(set(ssum) | set(gsum)):
        if sname in ("n", "S2_collections"):
            continue
        if sname not in ssum or sname not in gsum:
            lines.append(f"{sname}: only in {'saved' if sname in ssum else 'gated'}")
            continue
        lines.append(f"{sname}  (saved->gated)")
        for arm in ssum[sname]:
            if arm not in gsum[sname]:
                lines.append(f"  {arm}: only in saved")
                continue
            lines.append(f"  {arm:<10} " + "  ".join(
                f"{f}={_cell(ssum[sname][arm], gsum[sname][arm], f)}" for f in FIELDS))
        lines += [f"  {arm}: only in gated" for arm in gsum[sname] if arm not in ssum[sname]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write("usage: taxonomy_ab_compare.py SAVED.json GATED.json\n")
        return 2
    saved, gated = (json.loads(Path(p).read_text()) for p in args)
    sys.stdout.write(compare(saved, gated) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
