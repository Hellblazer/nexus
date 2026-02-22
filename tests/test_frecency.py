"""AC4: Git frecency scoring — sum(exp(-0.01 * days_passed)) per file."""
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from nexus.frecency import compute_frecency


def test_frecency_single_recent_commit() -> None:
    """A file committed today has frecency ≈ 1.0."""
    now = datetime.now(UTC)
    with patch("nexus.frecency._git_commit_timestamps", return_value=[now.timestamp()]):
        score = compute_frecency(Path("/repo"), Path("/repo/file.py"))
    assert abs(score - 1.0) < 0.01


def test_frecency_multiple_commits() -> None:
    """Frecency is the sum of per-commit exponential decay."""
    now = datetime.now(UTC)
    timestamps = [
        now.timestamp(),                              # today:     exp(-0.01 * 0)   = 1.0
        (now - timedelta(days=100)).timestamp(),      # 100d ago:  exp(-0.01 * 100) ≈ 0.368
    ]
    with patch("nexus.frecency._git_commit_timestamps", return_value=timestamps):
        score = compute_frecency(Path("/repo"), Path("/repo/file.py"))
    expected = 1.0 + math.exp(-1.0)
    assert abs(score - expected) < 0.01


def test_frecency_no_commits_returns_zero() -> None:
    """File with no git history → frecency 0.0."""
    with patch("nexus.frecency._git_commit_timestamps", return_value=[]):
        score = compute_frecency(Path("/repo"), Path("/repo/file.py"))
    assert score == 0.0


def test_frecency_old_commit_near_zero() -> None:
    """A commit from 10 years ago contributes almost nothing."""
    ts = (datetime.now(UTC) - timedelta(days=3650)).timestamp()
    with patch("nexus.frecency._git_commit_timestamps", return_value=[ts]):
        score = compute_frecency(Path("/repo"), Path("/repo/file.py"))
    # exp(-0.01 * 3650) ≈ 2e-16 — effectively 0
    assert score < 0.001
