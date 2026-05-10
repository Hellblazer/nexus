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


def _safe_collection(
    prefix: str, name: str, path_hash: str, *, suffix: str = "",
) -> str:
    """Build ``{prefix}{name}-{hash8}{suffix}``, truncating *name* to
    stay within 63 chars.

    ChromaDB enforces a 63-character limit on collection names.  The
    fixed overhead is ``len(prefix) + 1 (hyphen) + 8 (hash) + len(suffix)``,
    leaving the remainder for the basename.  When truncation occurs the
    full name is still recoverable via the hash.

    The optional *suffix* is appended verbatim so callers that need to
    emit conformant ``__<model>__v<n>`` trailers (RDR-103) can share
    this truncation logic instead of recomputing the budget themselves.
    """
    max_name = 63 - len(prefix) - 1 - len(path_hash) - len(suffix)
    truncated = name[:max_name]
    return f"{prefix}{truncated}-{path_hash}{suffix}"


def _resolve_repo_collection(
    repo: Path, content_type: str, *, cat: Any = None,
) -> str:
    """Return the conformant collection name for ``(repo, content_type)``.

    Catalog-aware path: when ``cat`` is supplied AND has an owner
    registered for ``repo``, returns the catalog-minted
    ``<ct>__<owner>__<model>__v<n>`` name from
    :meth:`Catalog.collection_for_repo`.

    No-catalog / unregistered-owner path: synthesizes a conformant
    name from the path-derived ``<basename>-<hash8>`` identity. This
    keeps tests and ad-hoc CLI runs working post Phase-5 strict-flip
    while still emitting a 4-segment conformant shape that satisfies
    :meth:`T3Database.get_or_create_collection`'s strict-naming guard.
    """
    if cat is not None:
        try:
            return cat.collection_for_repo(repo, content_type).render()
        except LookupError:
            # Owner not registered; fall through to synthesis.
            pass
        except Exception as exc:
            _log.debug(
                "registry_resolve_catalog_failed",
                repo=str(repo),
                content_type=content_type,
                error=str(exc),
            )
    from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415

    if content_type not in ("code", "docs", "rdr"):
        raise ValueError(
            f"_resolve_repo_collection: unknown content_type {content_type!r}"
        )
    name, path_hash = _repo_identity(repo)
    # The conformant collection-name grammar accepts only alphanumerics
    # and hyphens inside the owner segment. Sanitise the basename so
    # repos with ``_`` (segment separator) AND repos like
    # ``com.conductor.sys.monitoring`` (Java reverse-domain naming) or
    # other non-alnum characters in the basename still produce a name
    # ``validate_collection_name`` accepts.
    #
    # GH #551: pre-fix, only ``_`` was replaced; dots and other chars
    # passed through verbatim, so the registry persisted a name like
    # ``code__com.conductor.sys.monitoring-b25083f0`` that subsequent
    # ``get_or_create_collection`` calls then failed to validate -- and
    # since the registry entry was already written, the failure looped
    # forever in the index log on every git hook fire.
    sanitised = _sanitise_owner_segment(name)
    model = effective_embedding_model_for_writes(content_type)
    return _safe_collection(
        f"{content_type}__", sanitised, path_hash, suffix=f"__{model}__v1",
    )


def _sanitise_owner_segment(name: str) -> str:
    """Return *name* with any character that ``validate_collection_name``
    would reject collapsed to ``-``.

    Conformant grammar (RDR-103): owner segment must contain only
    alphanumerics and hyphens. ``_`` is the segment separator and must
    not appear inside the segment. Dots, slashes, spaces, and any other
    glyph map to ``-``. Repeated hyphens collapse to a single hyphen
    and leading / trailing hyphens are stripped so the resulting
    segment also satisfies the start-and-end-with-alphanumeric guard
    in ``validate_collection_name``.
    """
    out_chars: list[str] = []
    for ch in name:
        if ch.isalnum():
            out_chars.append(ch)
        else:
            out_chars.append("-")
    collapsed = "".join(out_chars)
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-")


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

        Collection names always come back conformant. When ``cat`` is
        supplied AND has an owner row for ``repo``, the catalog-minted
        ``<ct>__<owner>__<model>__v1`` name is used. Otherwise
        :func:`_resolve_repo_collection` synthesises the conformant
        4-segment name from the path-derived ``<basename>-<hash8>``
        identity (RDR-103 Phase 5).
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
