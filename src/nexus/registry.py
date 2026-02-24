# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo registry: JSON persistence with atomic write and thread safety."""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()


def _repo_identity(repo: Path) -> tuple[str, str]:
    """Return ``(basename, hash8)`` for collection naming, stable across worktrees.

    Uses ``git rev-parse --git-common-dir`` to resolve the main repository root
    even when called from a worktree.  Falls back to the given *repo* path when
    git is unavailable (not installed, not a git repo, etc.).

    The hash is the first 8 hex characters of the SHA-256 digest of the
    resolved main repo path.  Two worktrees of the same repo produce identical
    collection names.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            git_common = Path(result.stdout.strip())
            if not git_common.is_absolute():
                git_common = (repo / git_common).resolve()
            main_repo = git_common.parent
        else:
            main_repo = repo
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("git rev-parse failed, using repo path directly", error=str(exc))
        main_repo = repo

    path_hash = hashlib.sha256(str(main_repo).encode()).hexdigest()[:8]
    return main_repo.name, path_hash


def _collection_name(repo: Path) -> str:
    """Return a unique ChromaDB collection name for *repo*.

    The collection name is ``code__{basename}-{hash8}`` where *hash8* is the
    first 8 hex characters of the SHA-256 digest of the main repository path
    (resolved via git, stable across worktrees).
    """
    name, path_hash = _repo_identity(repo)
    return f"code__{name}-{path_hash}"


def _docs_collection_name(repo: Path) -> str:
    """Return the docs__ ChromaDB collection name for *repo*.

    Uses the same identity scheme as _collection_name() for consistency.
    """
    name, path_hash = _repo_identity(repo)
    return f"docs__{name}-{path_hash}"


class RepoRegistry:
    """Thread-safe registry of indexed repositories stored as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {"repos": {}}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Failed to load registry; starting empty", path=str(path), error=str(exc))
                self._data = {"repos": {}}
            if not isinstance(self._data.get("repos"), dict):
                _log.warning("Registry has invalid structure; starting empty", path=str(path))
                self._data = {"repos": {}}

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, repo: Path) -> None:
        """Register *repo*, initialising collection names and head_hash."""
        key = str(repo)
        name = repo.name
        code_col = _collection_name(repo)
        docs_col = _docs_collection_name(repo)
        with self._lock:
            self._data["repos"][key] = {
                "name": name,
                "collection": code_col,  # backward compat alias
                "code_collection": code_col,
                "docs_collection": docs_col,
                "head_hash": "",
                "status": "registered",
            }
            self._save()

    def remove(self, repo: Path) -> None:
        """Remove *repo* from the registry."""
        key = str(repo)
        with self._lock:
            self._data["repos"].pop(key, None)
            self._save()

    def get(self, repo: Path) -> dict[str, Any] | None:
        """Return registry entry for *repo*, or None if not registered."""
        with self._lock:
            entry = self._data["repos"].get(str(repo))
            return dict(entry) if entry is not None else None

    def all(self) -> list[str]:
        """Return list of all registered repo paths."""
        with self._lock:
            return list(self._data["repos"].keys())

    def all_info(self) -> dict[str, dict[str, Any]]:
        """Return dict of all registered repos: path -> full entry dict."""
        with self._lock:
            return {k: dict(v) for k, v in self._data["repos"].items()}

    def update(self, repo: Path, **kwargs: Any) -> None:
        """Update fields for *repo* (e.g. head_hash, status)."""
        key = str(repo)
        with self._lock:
            if key in self._data["repos"]:
                self._data["repos"][key].update(kwargs)
                self._save()

    # ── internal ──────────────────────────────────────────────────────────────

    def _save(self) -> None:
        """Atomic write via mkstemp + os.replace(), safe against concurrent processes.

        Using a fixed .tmp name would allow two concurrent nx processes to collide:
        both write to the same temp file and one silently loses its update.
        mkstemp creates a uniquely-named temp file so each process writes independently.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=self._path.parent, prefix=".repos_")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                fh.write(json.dumps(self._data, indent=2))
            os.replace(tmp_path_str, self._path)
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise
