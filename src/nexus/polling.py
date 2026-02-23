# SPDX-License-Identifier: AGPL-3.0-or-later
"""HEAD polling: detect hash changes and trigger re-indexing."""
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

_log = structlog.get_logger()

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry


def _current_head(repo: Path) -> str:
    """Return the current HEAD commit hash for *repo*, or '' on error."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        _log.warning("git rev-parse timed out", repo=str(repo))
        return ""
    if result.returncode != 0:
        _log.warning("git rev-parse failed", repo=str(repo), returncode=result.returncode)
        return ""
    return result.stdout.strip()


def index_repo(repo: Path, registry: "RepoRegistry") -> None:
    """Trigger a full re-index of *repo*. Imported by serve to avoid circular imports."""
    from nexus.indexer import index_repository

    index_repository(repo, registry)


def check_and_reindex(repo: Path, registry: "RepoRegistry") -> None:
    """Check if HEAD has changed; if so, trigger re-indexing.

    Skips if:
    - repo not in registry
    - repo status is 'indexing' (already in progress)
    - HEAD hash unchanged

    Repos in 'error' status are retried on the next poll.

    Always records the new head_hash even if indexing fails, to prevent
    an infinite re-index loop on persistent failures.
    """
    info = registry.get(repo)
    if info is None:
        return

    if info.get("status") == "indexing":
        return  # skip repos currently being indexed
    # "error" status repos are retried on next poll

    current = _current_head(repo)
    if current == info.get("head_hash", ""):
        return

    from nexus.errors import CredentialsMissingError
    try:
        index_repo(repo, registry)
        registry.update(repo, head_hash=current)
    except CredentialsMissingError:
        # Don't record head_hash — allow retry on next poll when credentials are added
        _log.warning("Credentials missing — will retry on next poll", repo=str(repo))
    except Exception:
        # Record head_hash even on other errors to prevent infinite reindex loops
        registry.update(repo, head_hash=current)
        raise
