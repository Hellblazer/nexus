# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git frecency scoring: sum(exp(-0.01 * days_since_commit)) per file."""
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger()


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
        _log.warning("git log timed out — skipping frecency", file=str(file))
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
            _log.debug("Unexpected git log output line", file=str(file), line=repr(ts))
    return timestamps


def compute_frecency(repo: Path, file: Path) -> float:
    """Return frecency score: sum of exp(-0.01 * days_since_commit) over all commits.

    Single-file API for computing frecency score. For indexing pipelines,
    use :func:`batch_frecency` which is more efficient (single git subprocess).
    """
    now = datetime.now(UTC).timestamp()
    timestamps = _git_commit_timestamps(repo, file)
    if not timestamps:
        return 0.0
    total = 0.0
    for ts in timestamps:
        days = max(0.0, (now - ts) / 86400.0)
        total += math.exp(-0.01 * days)
    return total


def batch_frecency(repo: Path) -> dict[Path, float]:
    """Return frecency scores for all committed files in *repo*.

    Batch API for indexing pipelines. See also :func:`compute_frecency`
    for single-file usage.

    Runs a single ``git log`` subprocess rather than one per file.
    Returns a mapping from absolute file path to score.
    Files with no commits map to 0.0 (callers should use `.get(path, 0.0)`).
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=COMMIT %ct", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        _log.warning("git log timed out — returning empty frecency map", repo=str(repo))
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}

    now = datetime.now(UTC).timestamp()
    scores: dict[Path, float] = {}
    current_ts: float | None = None

    # Format: "COMMIT <timestamp>\n<file1>\n<file2>\n\n"
    # The "COMMIT " prefix is 7 chars; blank lines between commits are skipped.
    # Fragile assumption: file paths never start with "COMMIT ".
    # If git output format changes, this parser may silently misassign timestamps.
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT "):
            try:
                current_ts = float(line[7:])
            except ValueError:
                current_ts = None  # intentional: corrupt git log line, skip
        elif current_ts is not None:
            file_path = repo / line
            days = max(0.0, (now - current_ts) / 86400.0)
            scores[file_path] = scores.get(file_path, 0.0) + math.exp(-0.01 * days)

    return scores
