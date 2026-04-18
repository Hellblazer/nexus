# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git frecency scoring: sum(exp(-0.01 * days_since_commit)) per file."""
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger()


_DEFAULT_DECAY_RATE: float = 0.01
_DEFAULT_GIT_LOG_TIMEOUT: int = 30


def _git_commit_timestamps(
    repo: Path,
    file: Path,
    timeout: int = _DEFAULT_GIT_LOG_TIMEOUT,
) -> list[float]:
    """Return Unix timestamps for every commit that touched *file*.

    *timeout* defaults to 30 s; override via TuningConfig.git_log_timeout.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--format=%ct", "--", str(file)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
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


def compute_frecency(
    repo: Path,
    file: Path,
    *,
    decay_rate: float = _DEFAULT_DECAY_RATE,
    timeout: int = _DEFAULT_GIT_LOG_TIMEOUT,
) -> float:
    """Return frecency score: sum of exp(-decay_rate * days_since_commit) over all commits.

    Single-file API for computing frecency score. For indexing pipelines,
    use :func:`batch_frecency` which is more efficient (single git subprocess).

    *decay_rate* defaults to 0.01; *timeout* defaults to 30 s.  Override via
    TuningConfig to honour per-repo configuration.
    """
    now = datetime.now(UTC).timestamp()
    timestamps = _git_commit_timestamps(repo, file, timeout=timeout)
    if not timestamps:
        return 0.0
    total = 0.0
    for ts in timestamps:
        days = max(0.0, (now - ts) / 86400.0)
        total += math.exp(-decay_rate * days)
    return total


def batch_frecency(
    repo: Path,
    *,
    decay_rate: float = _DEFAULT_DECAY_RATE,
    timeout: int = _DEFAULT_GIT_LOG_TIMEOUT,
) -> dict[Path, float]:
    """Return frecency scores for all committed files in *repo*.

    Batch API for indexing pipelines. See also :func:`compute_frecency`
    for single-file usage.

    Runs a single ``git log`` subprocess rather than one per file.
    Returns a mapping from absolute file path to score.
    Files with no commits map to 0.0 (callers should use `.get(path, 0.0)`).

    *decay_rate* defaults to 0.01; *timeout* defaults to 30 s (batch uses 2×
    that for the internal subprocess call).  Override via TuningConfig.
    """
    # Search review I-7: the previous sentinel ``COMMIT %ct`` relied on
    # no file path starting with "COMMIT " — technically possible and
    # would corrupt all subsequent file scores in that commit. Use a
    # delimiter git will never emit for paths (vertical bars surrounding
    # the timestamp) so we can split unambiguously on a substring that
    # never appears in a valid file path.
    _MARKER = "|||nxcommit|||"
    try:
        result = subprocess.run(
            ["git", "log", f"--format={_MARKER}%ct{_MARKER}", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout * 2,
        )
    except subprocess.TimeoutExpired:
        _log.warning("git log timed out — returning empty frecency map", repo=str(repo))
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}

    now = datetime.now(UTC).timestamp()
    scores: dict[Path, float] = {}
    current_ts: float | None = None

    # Format: "|||nxcommit|||<timestamp>|||nxcommit|||\n<file1>\n<file2>\n\n"
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_MARKER) and line.endswith(_MARKER):
            inner = line[len(_MARKER):-len(_MARKER)]
            try:
                current_ts = float(inner)
            except ValueError:
                current_ts = None  # corrupt git log line, skip
        elif current_ts is not None:
            file_path = repo / line
            days = max(0.0, (now - current_ts) / 86400.0)
            scores[file_path] = scores.get(file_path, 0.0) + math.exp(-decay_rate * days)

    return scores
