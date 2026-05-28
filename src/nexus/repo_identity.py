# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worktree-stable repo identity + conformant collection naming.

RDR-137 Phase 5.2b (nexus-tts0d.21): the five pure helpers
(``_repo_identity``, ``_repo_identity_with_main``, ``_safe_collection``,
``_resolve_repo_collection``, ``_sanitise_owner_segment``, plus
``list_sibling_collections``) lived in :mod:`nexus.registry` before
RDR-137 because they were colocated with the legacy ``RepoRegistry``
class. They are not registry-coupled: they implement git-worktree-
stable repo identity and the RDR-103 conformant collection-naming
rules, both of which outlive the registry's deletion.

Relocating them here lets Phase 5.3 (``nexus-tts0d.20``) delete
``RepoRegistry`` + ``repos.json`` without breaking the 15+ unrelated
call sites that depend on these helpers.

``nexus.registry`` re-exports every helper for one release-cycle of
import-path backwards-compat; new code imports from
``nexus.repo_identity`` directly.
"""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()


# RDR-137 followup IMP-24 (nexus-43qgm.24): LRU-memoize the subprocess
# call. _repo_identity_with_main intentionally invokes
# _resolve_main_repo twice per call (once via _repo_identity for the
# monkeypatch contract, once directly to expose main_repo). The
# memoization halves the subprocess cost without changing semantics
# — git-worktree status is stable for the process lifetime, so the
# cache is safe. Cache keyed by str(repo) (Path objects with equal
# string form hash identically).
@lru_cache(maxsize=128)
def _resolve_main_repo_cached(repo_str: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_str,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            git_common = Path(result.stdout.strip())
            if not git_common.is_absolute():
                git_common = (Path(repo_str) / git_common).resolve()
            return str(git_common.parent)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("git rev-parse failed, using repo path directly", error=str(exc))
    return repo_str


def _resolve_main_repo(repo: Path) -> Path:
    """Return the canonical main-repo Path for *repo*.

    Uses ``git rev-parse --git-common-dir`` to resolve the main repository
    root even when *repo* is a worktree path.  Falls back to the given
    *repo* path when git is unavailable (not installed, not a git repo,
    etc.).

    LRU-memoized via :func:`_resolve_main_repo_cached` so repeated
    calls within the same process do not re-spawn subprocesses
    (RDR-137 followup IMP-24).
    """
    return Path(_resolve_main_repo_cached(str(repo)))


def _repo_identity(repo: Path) -> tuple[str, str]:
    """Return ``(basename, hash8)`` for collection naming, stable across worktrees.

    The hash is the first 8 hex characters of the SHA-256 digest of the
    resolved main repo path.  Two worktrees of the same repo produce
    identical collection names.

    Test-mock surface
    (``monkeypatch.setattr("nexus.repo_identity._repo_identity", ...)``)
    is the same 2-tuple signature it had before relocation, so existing
    tests continue to work after their imports retarget. The legacy
    ``nexus.registry._repo_identity`` re-export keeps untouched test
    code green for one release cycle.
    """
    main_repo = _resolve_main_repo(repo)
    path_hash = hashlib.sha256(str(main_repo).encode()).hexdigest()[:8]
    return main_repo.name, path_hash


def _repo_identity_with_main(repo: Path) -> tuple[str, str, Path]:
    """Return ``(basename, hash8, main_repo_path)`` for *repo*.

    nexus-zr2ie / RDR-137 gate critique 2026-05-28: callers that need to
    persist the canonical main-repo path (e.g. catalog owner ``repo_root``)
    should use this 3-tuple variant instead of writing ``str(repo)``.

    Delegates the ``(name, hash)`` pair to :func:`_repo_identity` so the
    widely-used ``monkeypatch.setattr("nexus.repo_identity._repo_identity", ...)``
    test-mock pattern continues to control the lookup key for callers
    that now route through this 3-tuple variant.
    """
    name, path_hash = _repo_identity(repo)
    main_repo = _resolve_main_repo(repo)
    return name, path_hash, main_repo


def _safe_collection(
    prefix: str, name: str, path_hash: str, *, suffix: str = "",
) -> str:
    """Build ``{prefix}{name}-{hash8}{suffix}``, truncating *name* to
    stay within 63 chars.

    ChromaDB enforces a 63-character limit on collection names.  The
    fixed overhead is ``len(prefix) + 1 (hyphen) + 8 (hash) + len(suffix)``,
    leaving the remainder for the basename.  When truncation occurs the
    full name is still recoverable via the hash.
    """
    max_name = 63 - len(prefix) - 1 - len(path_hash) - len(suffix)
    truncated = name[:max_name]
    return f"{prefix}{truncated}-{path_hash}{suffix}"


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


def _resolve_repo_collection(
    repo: Path, content_type: str, *, cat: Any = None,
) -> str:
    """Return the conformant collection name for ``(repo, content_type)``.

    Catalog-aware path: when ``cat`` is supplied AND has an owner
    registered for ``repo``, returns the catalog-minted
    ``<ct>__<owner>__<model>__v<n>`` name from
    :meth:`Catalog.collection_for_repo`.

    No-catalog / unregistered-owner path: synthesizes a conformant
    name from the path-derived ``<basename>-<hash8>`` identity.
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
    sanitised = _sanitise_owner_segment(name)
    model = effective_embedding_model_for_writes(content_type)
    return _safe_collection(
        f"{content_type}__", sanitised, path_hash, suffix=f"__{model}__v1",
    )


def list_sibling_collections(
    collection_name: str,
    t3_client: Any,
) -> list[str]:
    """Return all T3 collections sharing the same repo identity.

    Handles both forms:

    * **Legacy 2-segment** (``docs__art-architecture-8c2e74c0``):
      siblings are collections whose name ends with ``-8c2e74c0``
      (the 8-char hash suffix).
    * **RDR-103 conformant 4-segment**
      (``code__owner-1-2__voyage-code-3__v1``):
      siblings are collections that share the same ``__<owner_id>__``
      segment regardless of content_type, embedding_model, or
      version. RDR-137 followup IMP-27 (nexus-43qgm.27): pre-fix the
      function silently returned ``[]`` for ALL conformant names
      because ``rsplit('-', 1)`` produced ``("...__v", "1")`` and the
      8-char length check failed.

    Always excludes the input + ``taxonomy__*``.
    """
    from nexus.corpus import (  # noqa: PLC0415
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )

    matcher: Any = None
    if is_conformant_collection_name(collection_name):
        # Conformant path — share by owner_id segment.
        try:
            owner_id = parse_conformant_collection_name(collection_name)["owner_id"]
        except (KeyError, ValueError):
            return []
        owner_segment = f"__{owner_id}__"
        matcher = lambda n: owner_segment in n  # noqa: E731
    else:
        # Legacy 2-segment path — share by 8-char hash suffix.
        parts = collection_name.rsplit("-", 1)
        if len(parts) != 2 or len(parts[1]) != 8:
            return []
        hash8 = parts[1]
        matcher = lambda n: n.endswith(f"-{hash8}")  # noqa: E731

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
        if matcher(name):
            siblings.append(name)

    return sorted(siblings)


__all__ = (
    "_repo_identity",
    "_repo_identity_with_main",
    "_resolve_main_repo",
    "_resolve_repo_collection",
    "_safe_collection",
    "_sanitise_owner_segment",
    "list_sibling_collections",
)
