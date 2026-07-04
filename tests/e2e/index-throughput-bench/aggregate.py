"""Aggregate index-throughput benchmark runs into a scaling table (nexus-duoak.2).

Consumes the per-worker artifacts run.sh leaves behind::

    <out>/w{N}.log    full nx-index output including the --debug-timing block
    <out>/w{N}.wall   wall-clock seconds for the run (one float)

and prints: workers, wall_s, s/file, speedup (vs 1-worker), hooks_s,
hooks_share. The hooks_share column is the decision-gate input for
nexus-duoak.5 (does the LockedHookRegistry flatten the curve?).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_STAGES = ("chunking_s", "embed_s", "upload_s", "hooks_s", "retry_s")
_HEADER_RE = re.compile(r"\[debug-timing\] per-stage totals across (\d+) files:")
_STAGE_RE = re.compile(r"^\s*(\w+_s|total)\s+([0-9.]+)s")
_RUN_LOG_RE = re.compile(r"^w(\d+)\.log$")


def parse_debug_timing(text: str) -> dict[str, float]:
    """Extract the per-stage totals block from an nx-index output capture."""
    if "[debug-timing] no per-stage samples recorded" in text:
        raise ValueError("no per-stage samples recorded in log")
    header = _HEADER_RE.search(text)
    if not header:
        raise ValueError("debug-timing block not found in log")
    stages: dict[str, float] = {"n_files": int(header.group(1))}
    for line in text[header.end():].splitlines():
        m = _STAGE_RE.match(line)
        if not m:
            continue
        key, seconds = m.group(1), float(m.group(2))
        stages["total_s" if key == "total" else key] = seconds
        if key == "total":
            break
    missing = [s for s in (*_STAGES, "total_s") if s not in stages]
    if missing:
        raise ValueError(f"debug-timing block incomplete, missing: {missing}")
    return stages


def parse_runs(out_dir: Path) -> list[dict[str, float]]:
    """One row per w{N}.log/.wall pair, sorted by worker count."""
    rows: list[dict[str, float]] = []
    for log_path in sorted(out_dir.iterdir()):
        m = _RUN_LOG_RE.match(log_path.name)
        if not m:
            continue
        workers = int(m.group(1))
        wall_path = out_dir / f"w{workers}.wall"
        if not wall_path.exists():
            raise ValueError(f"missing wall-clock file: {wall_path.name}")
        stages = parse_debug_timing(log_path.read_text())
        wall_s = float(wall_path.read_text().strip())
        rows.append({
            "workers": workers,
            "wall_s": wall_s,
            "s_per_file": wall_s / stages["n_files"],
            "hooks_s": stages["hooks_s"],
            "hooks_share": stages["hooks_s"] / stages["total_s"],
        })
    rows.sort(key=lambda r: r["workers"])
    if rows:
        baseline = rows[0]["wall_s"] if rows[0]["workers"] == 1 else None
        for r in rows:
            r["speedup"] = (baseline / r["wall_s"]) if baseline else float("nan")
    return rows


def format_table(rows: list[dict[str, float]]) -> str:
    header = f"{'workers':>7} {'wall_s':>8} {'s/file':>7} {'speedup':>7} {'hooks_s':>8} {'hooks_share':>11}"
    lines = [header]
    for r in rows:
        lines.append(
            f"{int(r['workers']):>7} {r['wall_s']:>8.1f} {r['s_per_file']:>7.2f} "
            f"{r['speedup']:>7.2f} {r['hooks_s']:>8.1f} {r['hooks_share']:>11.3f}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(format_table(parse_runs(out)))
