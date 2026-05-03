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


def _safe_collection(prefix: str, name: str, path_hash: str) -> str:
    """Build ``{prefix}{name}-{hash8}``, truncating *name* to stay within 63 chars.

    ChromaDB enforces a 63-character limit on collection names.  The fixed
    overhead is ``len(prefix) + 1 (hyphen) + 8 (hash)``, leaving the remainder
    for the basename.  When truncation occurs the full name is still recoverable
    via the hash.
    """
    max_name = 63 - len(prefix) - 1 - len(path_hash)  # 1 for the hyphen
    truncated = name[:max_name]
    return f"{prefix}{truncated}-{path_hash}"


def _collection_name(repo: Path) -> str:
    """Return a unique ChromaDB collection name for *repo*.

    The collection name is ``code__{basename}-{hash8}`` where *hash8* is the
    first 8 hex characters of the SHA-256 digest of the main repository path
    (resolved via git, stable across worktrees).  Long basenames are truncated
    to stay within the 63-character ChromaDB limit.
    """
    name, path_hash = _repo_identity(repo)
    return _safe_collection("code__", name, path_hash)


def _docs_collection_name(repo: Path) -> str:
    """Return the docs__ ChromaDB collection name for *repo*.

    Uses the same identity scheme as _collection_name() for consistency.
    """
    name, path_hash = _repo_identity(repo)
    return _safe_collection("docs__", name, path_hash)


def _rdr_collection_name(repo: Path) -> str:
    """Return the rdr__ ChromaDB collection name for *repo*.

    Uses the same identity scheme as _collection_name() for consistency.
    """
    name, path_hash = _repo_identity(repo)
    return _safe_collection("rdr__", name, path_hash)


def _resolve_repo_collection(
    repo: Path, content_type: str, *, cat: Any = None,
) -> str:
    """Return the collection name for ``(repo, content_type)``.

    RDR-103 Phase 3a: when ``cat`` is supplied AND has an owner
    registered for ``repo``, returns the conformant
    ``<ct>__<owner>__<model>__v<n>`` name minted by
    :meth:`Catalog.collection_for_repo`. Otherwise falls back to the
    pre-RDR-103 ``<ct>__<basename>-<hash8>`` shape.

    Used by :meth:`RepoRegistry.add` (passed catalog explicitly) and is
    intentionally a thin façade that the indexer's
    ``_repo_collection_or_legacy`` mirrors. Phase 5 removes both the
    fallback branch here and the legacy helper definitions above.
    """
    if cat is not None:
        try:
            return cat.collection_for_repo(repo, content_type).render()
        except LookupError:
            # Owner not registered; fall through to legacy.
            pass
        except Exception as exc:
            _log.debug(
                "registry_resolve_catalog_failed",
                repo=str(repo),
                content_type=content_type,
                error=str(exc),
            )
    if content_type == "code":
        return _collection_name(repo)
    if content_type == "docs":
        return _docs_collection_name(repo)
    if content_type == "rdr":
        return _rdr_collection_name(repo)
    raise ValueError(
        f"_resolve_repo_collection: unknown content_type {content_type!r}"
    )


def list_sibling_collections(
    collection_name: str,
    t3_client: Any,
) -> list[str]:
    """Return all T3 collections sharing the same repo identity suffix.

    For ``docs__art-architecture-8c2e74c0``, returns all collections whose
    name ends with ``-8c2e74c0``, excluding the input and ``taxonomy__*``.

    Limitation: ``knowledge__*`` collections without a ``{hash8}`` suffix
    are not detected — use explicit ``--against`` for those.
    """
    # Extract hash8 suffix: last 8 chars after the final hyphen
    parts = collection_name.rsplit("-", 1)
    if len(parts) != 2 or len(parts[1]) != 8:
        return []
    hash8 = parts[1]

    try:
        all_colls = t3_client.list_collections()
    except Exception:
        return []

    siblings = []
    for coll in all_colls:
        name = coll.name if hasattr(coll, "name") else str(coll)
        if name == collection_name:
            continue
        if name.startswith("taxonomy__"):
            continue
        if name.endswith(f"-{hash8}"):
            siblings.append(name)

    return sorted(siblings)


class RepoRegistry:
    """Thread-safe registry of indexed repositories stored as JSON."""

    # Paths matching these prefixes are never persisted — they come from test
    # runs, worktrees, or accidental indexing of temp directories.
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

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, repo: Path, *, cat: Any = None) -> None:
        """Register *repo*, initialising collection names and head_hash.

        RDR-103 Phase 3a: when ``cat`` is supplied, collection names are
        sourced from the catalog as conformant
        ``<ct>__<owner>__<model>__v1`` shapes. When ``cat`` is None
        (default) or the lookup raises (owner not yet registered), the
        legacy ``_collection_name`` / ``_docs_collection_name`` helpers
        produce the pre-RDR-103 names. Phase 5 drops the legacy branch
        together with the helper definitions.
        """
        key = str(repo)
        name = repo.name
        code_col = _resolve_repo_collection(repo, "code", cat=cat)
        docs_col = _resolve_repo_collection(repo, "docs", cat=cat)
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

    @classmethod
    def _is_ephemeral(cls, path: str) -> bool:
        """Return True if *path* looks like a pytest temp dir or orphaned worktree."""
        if "/pytest-" in path:
            return True
        if "/worktrees/" in path and not Path(path).exists():
            return True
        return False

    def _prune_stale(self) -> None:
        """Remove entries whose paths no longer exist on disk."""
        repos = self._data.get("repos", {})
        before = len(repos)
        clean = {k: v for k, v in repos.items() if Path(k).exists()}
        pruned = before - len(clean)
        if pruned:
            self._data["repos"] = clean
            self._save()
            _log.info("registry_pruned_stale", removed=pruned, remaining=len(clean))

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
                pass  # intentional: cleanup after re-raise
            raise
