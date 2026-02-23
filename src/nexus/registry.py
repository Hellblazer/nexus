# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo registry: JSON persistence with atomic write and thread safety."""
import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def _collection_name(repo: Path) -> str:
    """Return a unique ChromaDB collection name for *repo*.

    The collection name is ``code__{basename}-{hash8}`` where *hash8* is the
    first 8 hex characters of the SHA-256 digest of the full absolute path.
    This guarantees uniqueness even when two repos share the same leaf name
    (e.g. ``/work/a/repo`` and ``/work/b/repo`` both named ``repo``).
    """
    path_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    return f"code__{repo.name}-{path_hash}"


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
                _log.warning("Failed to load registry from %s (%s); starting empty.", path, exc)
                self._data = {"repos": {}}

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, repo: Path) -> None:
        """Register *repo*, initialising collection name and head_hash."""
        key = str(repo)
        name = repo.name
        with self._lock:
            self._data["repos"][key] = {
                "name": name,
                "collection": _collection_name(repo),
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
