# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo registry: JSON persistence with atomic write and thread safety."""
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any


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
            except (json.JSONDecodeError, ValueError):
                # Corrupt or truncated JSON — start with an empty registry
                # rather than crashing.  The file will be overwritten on the
                # next successful write.
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

    def update(self, repo: Path, **kwargs: Any) -> None:
        """Update fields for *repo* (e.g. head_hash, status)."""
        key = str(repo)
        with self._lock:
            if key in self._data["repos"]:
                self._data["repos"][key].update(kwargs)
                self._save()

    # ── internal ──────────────────────────────────────────────────────────────

    def _save(self) -> None:
        """Atomic write: write to .tmp then os.replace()."""
        tmp = Path(str(self._path) + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self._path)
