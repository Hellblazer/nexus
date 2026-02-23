"""AC4: Git frecency scoring — sum(exp(-0.01 * days_passed)) per file."""
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ── nexus-1eq: batch_frecency runs a single git subprocess ───────────────────

def test_batch_frecency_single_subprocess_call() -> None:
    """batch_frecency issues a single git log call regardless of file count."""
    from nexus.frecency import batch_frecency

    git_output = (
        "COMMIT 1700000000\n"
        "\n"
        "src/foo.py\n"
        "src/bar.py\n"
        "\n"
        "COMMIT 1699000000\n"
        "\n"
        "src/foo.py\n"
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = git_output

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        scores = batch_frecency(Path("/repo"))

    # Only one subprocess call
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "git"
    assert "--name-only" in cmd

    # Both files scored; foo.py has two commits (higher score)
    assert Path("/repo/src/foo.py") in scores
    assert Path("/repo/src/bar.py") in scores
    assert scores[Path("/repo/src/foo.py")] > scores[Path("/repo/src/bar.py")]


def test_batch_frecency_timeout_returns_empty() -> None:
    """batch_frecency returns {} on git timeout."""
    from nexus.frecency import batch_frecency

    with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("git", 60)):
        scores = batch_frecency(Path("/repo"))

    assert scores == {}


def test_batch_frecency_empty_repo_returns_empty() -> None:
    """batch_frecency returns {} when git log has no output."""
    from nexus.frecency import batch_frecency

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        scores = batch_frecency(Path("/repo"))

    assert scores == {}


# ── Gap 3: frecency.py edge cases (timeout, non-zero returncode, bad ts) ─────

def test_compute_frecency_git_timeout_returns_zero() -> None:
    """When subprocess.run raises TimeoutExpired, compute_frecency returns 0.0."""
    import subprocess as _subprocess

    with patch("subprocess.run", side_effect=_subprocess.TimeoutExpired("git", 30)):
        score = compute_frecency(Path("/repo"), Path("/repo/file.py"))

    assert score == 0.0


def test_git_commit_timestamps_nonzero_returncode() -> None:
    """When git returns non-zero, _git_commit_timestamps returns empty list."""
    from nexus.frecency import _git_commit_timestamps

    mock_result = MagicMock()
    mock_result.returncode = 128  # e.g., not a git repo
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        timestamps = _git_commit_timestamps(Path("/repo"), Path("/repo/file.py"))

    assert timestamps == []


def test_batch_frecency_invalid_timestamp_skipped() -> None:
    """When COMMIT line has non-numeric value, it is skipped gracefully."""
    from nexus.frecency import batch_frecency

    git_output = (
        "COMMIT not-a-number\n"
        "\n"
        "src/foo.py\n"
        "\n"
        "COMMIT 1700000000\n"
        "\n"
        "src/bar.py\n"
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = git_output

    with patch("subprocess.run", return_value=mock_result):
        scores = batch_frecency(Path("/repo"))

    # foo.py should NOT be scored (its COMMIT timestamp was invalid → current_ts=None)
    assert Path("/repo/src/foo.py") not in scores
    # bar.py should be scored normally
    assert Path("/repo/src/bar.py") in scores
    assert scores[Path("/repo/src/bar.py")] > 0.0
