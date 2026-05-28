# SPDX-License-Identifier: AGPL-3.0-or-later
"""Legacy ``repos.json`` registry — back-compat shim + helper re-exports.

RDR-137 Phase 5.3 (nexus-tts0d.20): production code no longer reads
or writes the legacy registry; every consumer routes through
:mod:`nexus.repos` (catalog-backed) instead. The :class:`RepoRegistry`
class is preserved here for:

1. **Test fixtures** that need to materialise the legacy file shape on
   disk during the deprecation window (a substantial body of regression
   tests still seeds ``repos.json`` to verify the catalog cutover
   handles the legacy data).
2. **The lint guard** in
   ``tests/test_no_repo_registry_resurrection.py`` whitelists this one
   file as the *only* permitted location for ``class RepoRegistry``.

The five pure helpers (``_repo_identity`` and friends) moved to
:mod:`nexus.repo_identity` in nexus-tts0d.21; this module re-exports
them for one release cycle of import-path back-compat. New code
should import from ``nexus.repo_identity`` directly.

The whole module disappears in the release after the legacy ``repos.json``
deprecation window closes.
"""
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import structlog

from nexus.repo_identity import (  # noqa: F401
    _repo_identity,
    _repo_identity_with_main,
    _resolve_main_repo,
    _resolve_repo_collection,
    _safe_collection,
    _sanitise_owner_segment,
    list_sibling_collections,
)

_log = structlog.get_logger()


__all__ = (
    "RepoRegistry",
    "_repo_identity",
    "_repo_identity_with_main",
    "_resolve_main_repo",
    "_resolve_repo_collection",
    "_safe_collection",
    "_sanitise_owner_segment",
    "list_sibling_collections",
)


class RepoRegistry:
    """**Deprecated** — legacy ``repos.json`` registry.

    Preserved as a test-fixture helper during the deprecation window
    (RDR-137 Phase 5.3, ``nexus-tts0d.20``). Production code has cut
    over to :mod:`nexus.repos` (catalog-backed). The class disappears
    in the release after the deprecation window closes.

    The :mod:`nexus.commands.upgrade` migration verb reads the file via
    :func:`nexus.repos._read_repos_json` (stdlib json) instead of this
    class so the deletion does not block migration; the dual-read shim
    similarly uses the stdlib path.
    """

    _EPHEMERAL_PREFIXES = ("/private/tmp", "/private/var", "/tmp", "/var/folders")

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
            self._prune_stale()

    def add(self, repo: Path, *, cat: Any = None) -> None:
        key = str(repo)
        name = repo.name
        code_col = _resolve_repo_collection(repo, "code", cat=cat)
        docs_col = _resolve_repo_collection(repo, "docs", cat=cat)
        with self._lock:
            self._data["repos"][key] = {
                "name": name,
                "collection": code_col,
                "code_collection": code_col,
                "docs_collection": docs_col,
                "head_hash": "",
                "status": "registered",
            }
            self._save()

    def remove(self, repo: Path) -> None:
        key = str(repo)
        with self._lock:
            self._data["repos"].pop(key, None)
            self._save()

    def get(self, repo: Path) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data["repos"].get(str(repo))
            return dict(entry) if entry is not None else None

    def all(self) -> list[str]:
        with self._lock:
            return list(self._data["repos"].keys())

    def all_info(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data["repos"].items()}

    def update(self, repo: Path, **kwargs: Any) -> None:
        key = str(repo)
        with self._lock:
            if key in self._data["repos"]:
                self._data["repos"][key].update(kwargs)
                self._save()

    @classmethod
    def _is_ephemeral(cls, path: str) -> bool:
        if "/pytest-" in path:
            return True
        if "/worktrees/" in path and not Path(path).exists():
            return True
        return False

    def _prune_stale(self) -> None:
        repos = self._data.get("repos", {})
        before = len(repos)
        clean = {k: v for k, v in repos.items() if Path(k).exists()}
        pruned = before - len(clean)
        if pruned:
            self._data["repos"] = clean
            self._save()
            _log.info("registry_pruned_stale", removed=pruned, remaining=len(clean))

    def _save(self) -> None:
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
