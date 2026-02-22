# SPDX-License-Identifier: AGPL-3.0-or-later
"""HEAD polling: detect hash changes and trigger re-indexing."""
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.registry import RepoRegistry


def _current_head(repo: Path) -> str:
    """Return the current HEAD commit hash for *repo*, or '' on error."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
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
    """
    info = registry.get(repo)
    if info is None:
        return

    if info.get("status") == "indexing":
        return

    current = _current_head(repo)
    if current == info.get("head_hash", ""):
        return

    index_repo(repo, registry)
    registry.update(repo, head_hash=current)
