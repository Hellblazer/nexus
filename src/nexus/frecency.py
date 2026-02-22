# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git frecency scoring: sum(exp(-0.01 * days_since_commit)) per file."""
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def _git_commit_timestamps(repo: Path, file: Path) -> list[float]:
    """Return Unix timestamps for every commit that touched *file*."""
    result = subprocess.run(
        ["git", "log", "--follow", "--format=%ct", "--", str(file)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [float(ts) for ts in result.stdout.strip().splitlines() if ts.strip()]


def compute_frecency(repo: Path, file: Path) -> float:
    """Return frecency score: sum of exp(-0.01 * days_since_commit) over all commits."""
    now = datetime.now(UTC).timestamp()
    timestamps = _git_commit_timestamps(repo, file)
    if not timestamps:
        return 0.0
    total = 0.0
    for ts in timestamps:
        days = (now - ts) / 86400.0
        total += math.exp(-0.01 * days)
    return total
