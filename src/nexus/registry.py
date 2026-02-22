# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo registry: JSON persistence with atomic write and thread safety."""
import json
import os
import threading
from pathlib import Path
from typing import Any


class RepoRegistry:
    """Thread-safe registry of indexed repositories stored as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {"repos": {}}
        if path.exists():
            self._data = json.loads(path.read_text())

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, repo: Path) -> None:
        """Register *repo*, initialising collection name and head_hash."""
        key = str(repo)
        name = repo.name
        with self._lock:
            self._data["repos"][key] = {
                "name": name,
                "collection": f"code__{name}",
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
            return self._data["repos"].get(str(repo))

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
