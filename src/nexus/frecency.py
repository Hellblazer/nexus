# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git frecency scoring: sum(exp(-0.01 * days_since_commit)) per file."""
import logging
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)


def _git_commit_timestamps(repo: Path, file: Path) -> list[float]:
    """Return Unix timestamps for every commit that touched *file*."""
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--format=%ct", "--", str(file)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        _log.warning("git log timed out for %s — skipping frecency", file)
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    timestamps: list[float] = []
    for ts in result.stdout.strip().splitlines():
        ts = ts.strip()
        if not ts:
            continue
        try:
            timestamps.append(float(ts))
        except ValueError:
            _log.debug("Unexpected git log output line for %s: %r", file, ts)
    return timestamps


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
