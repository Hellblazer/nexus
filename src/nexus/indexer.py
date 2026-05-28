# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code repository indexing pipeline — orchestrator.

This module is responsible for:
- Repository file discovery and classification dispatch
- Constructing IndexContext and routing to per-type indexers
- RDR markdown indexing (via doc_indexer)
- Misclassification and deleted-file pruning
- Frecency-only update runs
- Pipeline version stamping
- Per-repo locking

Per-file indexing logic lives in focused sub-modules (RDR-032):
  nexus.code_indexer   — code files (AST chunking, context extraction)
  nexus.prose_indexer  — prose and markdown files (CCE embedding)
  nexus.indexer_utils  — staleness check, credential check, shared helpers
  nexus.index_context  — IndexContext dataclass
"""
import errno
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus._locking import lock_file, unlock_file
from nexus.corpus import index_model_for_collection
from nexus.retry import _chroma_with_retry, _voyage_with_retry  # noqa: F401 — re-exported for any existing imports
from nexus.errors import CredentialsMissingError  # re-exported for backward compatibility
from nexus.indexer_utils import (
    build_staleness_cache,
    check_credentials,
    check_local_path_writable,
    check_staleness,
    should_ignore as _should_ignore,  # shared implementation
    _DEFAULT_IGNORE,
)

# Re-exports from nexus.code_indexer for backward compatibility with tests that
# import these names directly from nexus.indexer.
from nexus.code_indexer import (  # noqa: F401
    _extract_context,
    _extract_name_from_node,
    _COMMENT_CHARS,
    DEFINITION_TYPES,
)

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog
    from nexus.catalog.tumbler import Tumbler
    from nexus.hook_registry import HookRegistry
    from nexus.indexer_utils import StalenessCache
    from nexus.registry import RepoRegistry
    from nexus.stage_timers import StageTimers

# Re-export from indexer_utils for backward compatibility (tests import from here).
DEFAULT_IGNORE: list[str] = _DEFAULT_IGNORE


# Pipeline version: bump when indexing changes invalidate existing embeddings.
# History:
#   v1-v3: pre-versioning (no version stamp in collection metadata)
#   v4:    RDR-028 language registry + RDR-014 CCE prefixes
PIPELINE_VERSION: str = "4"


def stamp_collection_version(col: object) -> None:
    """Write PIPELINE_VERSION to collection metadata, preserving existing keys."""
    existing = getattr(col, "metadata", None) or {}
    col.modify(metadata={**existing, "pipeline_version": PIPELINE_VERSION})  # type: ignore[attr-defined]


def get_collection_pipeline_version(col: object) -> str | None:
    """Return the pipeline_version from collection metadata, or None."""
    meta = getattr(col, "metadata", None) or {}
    return meta.get("pipeline_version")


def check_pipeline_staleness(col: object, collection_name: str) -> bool:
    """Check if collection has a stale pipeline version.

    Returns True if the stored version differs from PIPELINE_VERSION.
    Returns False for new collections (stored version is None) or matching versions.
    """
    stored = get_collection_pipeline_version(col)
    if stored is None:
        return False
    if stored != PIPELINE_VERSION:
        _log.warning(
            "collection_pipeline_stale",
            collection=collection_name,
            stored_version=stored,
            current_version=PIPELINE_VERSION,
            hint="Run with --force-stale to re-index stale collections, or --force to re-index all.",
        )
        return True
    return False


def _git_metadata(repo: Path) -> dict:
    """Collect git metadata for *repo*. Returns empty strings for missing values."""
    def run(args: list[str]) -> str:
        r = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""

    return {
        "git_project_name": repo.name,
        "git_branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit_hash": run(["git", "rev-parse", "HEAD"]),
        "git_remote_url": run(["git", "remote", "get-url", "origin"]),
    }


# _should_ignore is imported from indexer_utils (as `should_ignore`) above.
# The local name `_should_ignore` is preserved for backward compatibility.


def _git_ls_files(repo: Path, *, include_untracked: bool = False) -> list[Path]:
    """Return repository files using git ls-files, respecting .gitignore.

    By default returns only tracked (committed/staged) files.
    With *include_untracked*, also includes untracked files that are not
    ignored by .gitignore / .git/info/exclude / global gitignore.

    In a git repo (.git exists), failure raises RuntimeError — silent fallback
    to rglob would index .gitignored secrets like .env (nexus-3ov6).
    Non-git directories return [] so the caller can use rglob.
    """
    is_git_repo = (repo / ".git").is_dir()
    args = ["git", "ls-files", "--cached", "-z"]
    if include_untracked:
        args.extend(["--others", "--exclude-standard"])
    try:
        result = subprocess.run(
            args, cwd=repo, capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        if is_git_repo:
            raise RuntimeError(
                f"git ls-files failed in git repo {repo}: {exc}"
            ) from exc
        return []
    if result.returncode != 0:
        if is_git_repo:
            raise RuntimeError(
                f"git ls-files failed in git repo {repo}: {result.stderr.strip()}"
            )
        return []  # non-git directory — caller uses rglob
    # -z uses NUL separators (handles filenames with spaces/newlines)
    paths = []
    for rel_str in result.stdout.split("\0"):
        if rel_str:  # filter empty strings from trailing NUL
            paths.append(repo / rel_str)
    return paths


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
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("current_head_failed", repo=str(repo), error=str(exc))
        return ""


def _set_owner_head_hash(repo: Path, head_hash: str) -> None:
    """RDR-137 Phase 3.8 (nexus-tts0d.13): persist *head_hash* on the
    owner row for *repo*.

    Writes ``owners.head_hash`` (Phase 1.5b column from
    ``nexus-tts0d.2``). Silently degrades when the catalog is not
    initialised or the owner is not registered yet — both are
    legitimate states during a first-time index, and ``index_repository``
    will register the owner in its catalog hook.
    """
    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path
        from nexus.registry import _repo_identity

        cat_dir = catalog_path()
        if not (cat_dir / ".catalog.db").exists():
            return
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        _, repo_hash = _repo_identity(repo)
        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            return
        cat._db.execute(
            "UPDATE owners SET head_hash = ? WHERE tumbler_prefix = ?",
            (head_hash, str(owner)),
        )
        cat._db.commit()
    except Exception as exc:
        _log.warning(
            "set_owner_head_hash_failed",
            repo=str(repo), error=str(exc),
        )


def _repo_lock_path(repo: Path) -> Path:
    """Return the per-repo lock file path: ~/.config/nexus/locks/<hash8>.lock.

    Uses the same worktree-stable identity as the registry so two worktrees
    of the same repo map to a single lock.
    """
    from nexus.config import nexus_config_dir
    from nexus.registry import _repo_identity

    _, path_hash = _repo_identity(repo)
    return nexus_config_dir() / "locks" / f"{path_hash}.lock"


_LOCK_STALE_SECONDS = 5  # lock files older than this with no live PID are stale


def _clear_stale_lock(lock_path: Path) -> None:
    """Delete *lock_path* if it is stale (dead PID or old empty file).

    A lock file is stale when:
    - It contains a PID that no longer exists (ESRCH), OR
    - It has no parseable PID and is older than ``_LOCK_STALE_SECONDS``.

    The second case handles background processes (disown/&) that were killed
    before writing their PID, leaving empty 0-byte lock files forever.

    This is advisory cleanup — the platform lock from
    :mod:`nexus._locking` provides the real mutual exclusion.
    """
    if not lock_path.exists():
        return
    try:
        pid = int(lock_path.read_text().strip())
    except (ValueError, OSError):
        # No readable PID — check file age.  A just-opened lock will be
        # younger than _LOCK_STALE_SECONDS; an orphan from a crashed
        # process will be much older.
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return
        if age > _LOCK_STALE_SECONDS:
            _remove_stale(lock_path)
        return
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            _remove_stale(lock_path)


def _sweep_stale_locks(lock_dir: Path) -> None:
    """Remove all stale lock files in *lock_dir*."""
    if not lock_dir.is_dir():
        return
    for lock_file in lock_dir.glob("*.lock"):
        _clear_stale_lock(lock_file)


def _remove_stale(lock_path: Path) -> None:
    """Unlink *lock_path*, ignoring FileNotFoundError (concurrent cleanup)."""
    try:
        lock_path.unlink()
        _log.debug("stale_lock_removed", path=str(lock_path))
    except FileNotFoundError:
        pass


def _repo_collection_or_legacy(repo: Path, content_type: str) -> str:
    """Return the conformant collection name for ``(repo, content_type)``.

    Catalog-aware path: when the catalog is initialized and the repo
    owner is registered, returns the catalog-minted conformant name.

    No-catalog / unregistered-owner path: synthesizes a conformant
    name from the path-derived ``<basename>-<hash8>`` identity so the
    no-catalog ad-hoc workflow (tests, single-shot CLI runs on a fresh
    repo) continues to satisfy ``T3Database``'s strict-naming guard.
    """
    from nexus.catalog import Catalog  # noqa: PLC0415
    from nexus.config import catalog_path  # noqa: PLC0415

    try:
        cat_path = catalog_path()
        if Catalog.is_initialized(cat_path):
            cat = Catalog(cat_path, cat_path / ".catalog.db")
            try:
                return cat.collection_for_repo(repo, content_type).render()
            except LookupError:
                # Owner not yet registered; fall through to the
                # path-derived synthesis below. This happens for
                # callers that bypass the ``_catalog_hook`` upfront
                # flow (e.g. ad-hoc CLI invocations on a fresh repo).
                pass
    except Exception:
        _log.debug(
            "repo_collection_catalog_lookup_failed",
            repo=str(repo),
            content_type=content_type,
            exc_info=True,
        )
    return _conformant_name_for_repo(repo, content_type)


def _conformant_name_for_repo(repo: Path, content_type: str) -> str:
    """Synthesize a conformant collection name from path-derived identity.

    Owner segment uses the ``<basename>-<hash8>`` shape produced by
    :func:`nexus.registry._repo_identity` so two worktrees of the same
    repo collapse to the same collection. Embedding model is the
    canonical model for ``content_type``; version is always v1 for
    ad-hoc fallbacks.
    """
    from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415
    from nexus.registry import _repo_identity, _safe_collection  # noqa: PLC0415

    if content_type not in ("code", "docs", "rdr"):
        raise ValueError(
            f"_conformant_name_for_repo: unknown content_type {content_type!r}"
        )
    basename, repo_hash = _repo_identity(repo)
    # See ``_resolve_repo_collection``: the conformant grammar treats
    # ``_`` as the segment separator, so basenames containing
    # underscores must be sanitised to ``-`` before composing the owner
    # segment. The two synthesis points share the same rule so a repo
    # gets the same name from either entry point.
    sanitised = basename.replace("_", "-")
    model = effective_embedding_model_for_writes(content_type)
    return _safe_collection(
        prefix=f"{content_type}__",
        name=sanitised,
        path_hash=repo_hash,
        suffix=f"__{model}__v1",
    )


# RDR-103 Phase 4: legacy-to-conformant migration on first index ────────────


_MIGRATION_CONTENT_TYPES = ("code", "docs", "rdr")


def _legacy_collection_name(repo: "Path", content_type: str) -> str:
    """Return the pre-RDR-103 ``<ct>__<basename>-<hash8>`` shape for
    ``(repo, content_type)``. Used by :func:`_migrate_legacy_collections`
    to detect existing legacy collections in T3 so they can be renamed
    in place to the conformant 4-segment shape on the first index after
    the catalog upgrade.
    """
    from nexus.registry import _repo_identity, _safe_collection  # noqa: PLC0415

    if content_type not in ("code", "docs", "rdr"):
        raise ValueError(
            f"_legacy_collection_name: unknown content_type {content_type!r}"
        )
    basename, repo_hash = _repo_identity(repo)
    return _safe_collection(f"{content_type}__", basename, repo_hash)


def _migration_source_candidates(
    repo: "Path", content_type: str,
) -> list[str]:
    """Return ordered candidate "source" collection names that the
    migration helper should consider for rename to the catalog-derived
    target.

    Two shapes can carry pre-migration data for ``(repo, content_type)``:

    - The pre-RDR-103 legacy 2-segment ``<ct>__<basename>-<hash8>``
      (older installs that indexed before Phase 1 introduced
      ``CollectionName``).
    - The Phase-5 path-derived 4-segment synth
      ``<ct>__<basename>-<hash8>__<canonical_model>__v1``
      (post-Phase-5 installs that indexed before the catalog owner
      was registered, so ``_repo_collection_or_legacy`` fell through
      to :func:`_conformant_name_for_repo`).

    nexus-7vuw: the Phase-5 synth shape was missed by the original
    migration helper, leaving operator data orphaned at the
    path-derived collection while the indexer wrote fresh chunks to
    the catalog-derived ``<ct>__<owner-tumbler>__<canonical_model>__v1``
    target. Both shapes now flow through the same migration path.

    Order matters: the 2-segment legacy is checked first so existing
    Phase 4 migration semantics are preserved on pre-Phase-5 installs;
    the path-derived 4-segment is the fallback for installs that have
    already crossed the Phase-5 strict-flip and accumulated synth-shape
    collections.
    """
    from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415
    from nexus.registry import _repo_identity, _safe_collection  # noqa: PLC0415

    if content_type not in ("code", "docs", "rdr"):
        raise ValueError(
            f"_migration_source_candidates: unknown content_type {content_type!r}"
        )
    basename, repo_hash = _repo_identity(repo)
    sanitised = basename.replace("_", "-")
    model = effective_embedding_model_for_writes(content_type)
    return [
        # Pre-RDR-103 legacy 2-segment.
        _safe_collection(f"{content_type}__", basename, repo_hash),
        # Phase-5 path-derived 4-segment synth (mirrors the synthesis
        # in ``_conformant_name_for_repo``: underscores in the
        # basename are sanitised to hyphens for owner-segment grammar).
        _safe_collection(
            f"{content_type}__", sanitised, repo_hash,
            suffix=f"__{model}__v1",
        ),
    ]


def _migrate_legacy_collections(
    repo: "Path",
    *,
    cat: object | None,
    t3_db: object,
    registry: object,
    on_message: "Callable[[str], None] | None" = None,
) -> dict[str, str]:
    """RDR-103 Phase 4: rename legacy T3 collections to conformant
    names atomically on the first index after the catalog upgrade.

    Returns a ``{content_type: collection_name}`` map for the caller
    to use throughout the rest of the indexing run. The map values
    are conformant when the catalog is initialized and the owner is
    registered; otherwise they fall back to the legacy shape so the
    caller can still index against existing collections.

    Decision tree per pinned design (`nexus-yqnr.6` bead):

    1. legacy in T3, conformant absent → atomic rename via
       :func:`nexus.commands.collection.rename_collection_data_plane`
       (T3 native ``modify(name=)`` + T2 cascade + catalog re-point +
       collections projection update + ``CollectionSuperseded``
       event). Emits one ``Upgraded`` message via ``on_message``.
    2. conformant present, legacy absent → steady state. No message.
    3. both present → partial state from a prior interrupted run.
       Skip the rename to avoid the data-plane's
       ``collection already exists`` guard, return the conformant
       name, leave the legacy collection for operator cleanup.
       Emits one advisory message.
    4. neither present → greenfield, no migration needed.

    Catalog absent OR owner unregistered: returns the legacy-shape
    name for every content_type so the caller can still index.
    The next run (after :func:`_catalog_hook` registers the owner)
    will perform the migration.

    Pinned decision #1: the conformant target is computed using the
    indexer's CURRENT canonical model (via
    :meth:`Catalog.collection_for`), NOT parsed from the legacy
    collection name. Sidesteps unknown legacy models like ``voyage-3``
    that pre-date :data:`CANONICAL_EMBEDDING_MODELS`.
    """
    from typing import cast  # noqa: PLC0415

    from nexus.catalog.catalog import Catalog  # noqa: PLC0415
    # nexus-8g79.10 (V5): import from peer module instead of reaching
    # up into commands/. The CLI wrapper in commands/collection.py
    # adds the ``t3_db=_t3()`` default; we pass ``t3_db`` explicitly.
    from nexus.collection_rename import (  # noqa: PLC0415
        rename_collection_data_plane,
    )
    from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415
    from nexus.registry import _repo_identity  # noqa: PLC0415

    result: dict[str, str] = {}

    # Catalog-absent: nothing to migrate. Return an empty map so the
    # caller falls back to its own resolution (registry value or the
    # path-derived legacy helper).
    if cat is None:
        return result

    cat_obj = cast(Catalog, cat)

    # Owner-unregistered: nothing to migrate yet. The _catalog_hook
    # registers the owner later in this run; the migration happens on
    # the NEXT run. Return empty so the caller's existing fallback
    # handles this run.
    _, repo_hash = _repo_identity(repo)
    owner = cat_obj.owner_for_repo(repo_hash)
    if owner is None:
        return result

    for ct in _MIGRATION_CONTENT_TYPES:
        conformant = cat_obj.collection_for(
            content_type=ct,
            owner=owner,
            embedding_model=effective_embedding_model_for_writes(ct),
        ).render()

        # nexus-7vuw: pick the first source candidate that exists in T3.
        # Two shapes can carry pre-migration data:
        #   1. legacy 2-segment ``<ct>__<basename>-<hash8>`` (pre-RDR-103)
        #   2. Phase-5 path-derived 4-segment synth.
        # If both exist, prefer the legacy 2-segment (rename happens
        # first; the synth becomes the case-3 partial-state collision
        # message and is left for operator cleanup).
        legacy = None
        for candidate in _migration_source_candidates(repo, ct):
            if candidate == conformant:
                # The candidate IS the target; no migration needed
                # because the indexer is already writing to the right
                # place.
                continue
            if t3_db.collection_exists(candidate):  # type: ignore[attr-defined]
                legacy = candidate
                break
        if legacy is None:
            legacy = _legacy_collection_name(repo, ct)
            legacy_exists = False
        else:
            legacy_exists = True
        conformant_exists = bool(t3_db.collection_exists(conformant))  # type: ignore[attr-defined]

        if legacy_exists and not conformant_exists:
            # Decision tree case 1: rename legacy → conformant.
            #
            # The data-plane rename (T3 native modify) is the load-bearing
            # step: once it succeeds, the chunks live at ``conformant`` and
            # the catalog has been re-pointed. Subsequent
            # ``register_collection`` and ``supersede_collection`` calls
            # update the projections; if either raises, the data is still
            # at conformant and we MUST return ``conformant``, not legacy.
            # Returning legacy here would cause the caller to write fresh
            # chunks to an empty re-created legacy collection while the
            # actual data sits unreachable at conformant.
            data_plane_succeeded = False
            try:
                rename_collection_data_plane(
                    legacy, conformant, t3_db=t3_db, catalog=cat_obj,
                    on_warn=lambda msg: _log.warning(
                        "phase4_migration_cascade_warn", message=msg,
                    ),
                )
                data_plane_succeeded = True
            except Exception as exc:
                _log.warning(
                    "phase4_migration_data_plane_failed",
                    repo=str(repo), ct=ct, legacy=legacy,
                    conformant=conformant, error=str(exc),
                )
                # Data-plane failed; T3 still has the legacy collection.
                # Fall back so the caller indexes against existing data.
                result[ct] = legacy
                continue

            # Data plane succeeded; data now lives at conformant. From
            # this point onward any failure is non-fatal for the caller's
            # write path: ``conformant`` is the right name to use.
            try:
                from nexus.corpus import (  # noqa: PLC0415
                    is_conformant_collection_name,
                    parse_conformant_collection_name,
                )
                if is_conformant_collection_name(conformant):
                    segments = parse_conformant_collection_name(conformant)
                    cat_obj.register_collection(
                        conformant,
                        content_type=segments["content_type"],
                        owner_id=segments["owner_id"],
                        embedding_model=segments["embedding_model"],
                        model_version=segments["model_version"],
                    )
                else:
                    cat_obj.register_collection(conformant)
            except Exception:
                _log.warning(
                    "phase4_register_collection_failed_after_rename",
                    old=legacy, new=conformant, exc_info=True,
                )
            try:
                cat_obj.supersede_collection(
                    legacy, conformant, reason="rdr-103-phase4-migration",
                )
            except Exception:
                # Old name may not be in the collections projection
                # for legacy-shape collections that pre-date Phase 6
                # backfill. Non-fatal: the data-plane rename has
                # already moved everything; the supersede event is
                # a graph-completeness add-on.
                _log.debug(
                    "phase4_supersede_failed_nonfatal",
                    old=legacy, new=conformant, exc_info=True,
                )
            if on_message is not None:
                on_message(
                    f"Upgraded legacy collection {legacy} to {conformant}."
                )
            # Update the registry so subsequent runs read the
            # conformant name directly.
            key = f"{ct}_collection"
            try:
                registry.update(repo, **{key: conformant})  # type: ignore[attr-defined]
            except Exception:
                _log.debug(
                    "phase4_registry_update_failed",
                    repo=str(repo), ct=ct, exc_info=True,
                )
            result[ct] = conformant
        elif conformant_exists and legacy_exists:
            # Decision tree case 3: both exist (partial state).
            if on_message is not None:
                on_message(
                    f"Both legacy {legacy} and conformant {conformant} "
                    f"exist; skipping rename. Operator cleanup of the "
                    f"legacy collection is recommended via "
                    f"'nx collection delete {legacy}' once verified empty."
                )
            result[ct] = conformant
        else:
            # Decision tree cases 2 + 4: conformant present (steady
            # state) or neither present (greenfield). No message,
            # caller proceeds with conformant name.
            result[ct] = conformant

    return result


def _catalog_hook(
    repo: Path,
    repo_name: str,
    repo_hash: str,
    head_hash: str,
    indexed_files: list[tuple[Path, str, str]],
) -> dict[Path, str]:
    """Register/update indexed files in catalog. Silently skipped if catalog absent.

    Returns a ``{abs_path: doc_id}`` map (empty when the catalog is absent
    or cannot be opened) so the orchestrator can build a
    ``doc_id_resolver`` closure and inject it into ``IndexContext``
    before per-file indexing runs (RDR-101 Phase 3 PR δ Stage B). The
    return type is additive — existing test call sites that ignore the
    return value continue to work unchanged.
    """
    file_to_doc_id: dict[Path, str] = {}
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            _log.debug("catalog_hook_skipped", reason="catalog not initialized")
            return file_to_doc_id

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        # ``_catalog_hook`` accepts an explicit ``repo_hash`` (and
        # ``repo_name``) so callers and tests can override the
        # path-derived identity. ``ensure_owner_for_repo`` would
        # recompute the hash from ``repo`` and ignore the explicit
        # arg, so the inline lookup-or-register is preserved. The
        # idempotency guarantee matches ``ensure_owner_for_repo``:
        # existing owners short-circuit without a re-register.
        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            # nexus-zr2ie (RDR-137 gate critique 2026-05-28): derive the
            # canonical main-repo path so ``repo_root`` is worktree-
            # stable. Pre-fix this wrote ``str(repo)`` and contaminated
            # the catalog when the caller's ``repo`` argument was a
            # worktree path; ``resolve_path`` then produced broken
            # paths for every relative-path document under this owner
            # once the worktree was deleted. ``_repo_identity_with_main``
            # uses ``git rev-parse --git-common-dir`` to resolve.
            from nexus.registry import _repo_identity_with_main  # noqa: PLC0415
            _name, _hash, main_repo = _repo_identity_with_main(repo)
            owner = cat.register_owner(
                name=repo_name,
                owner_type="repo",
                repo_hash=repo_hash,
                repo_root=str(main_repo),
                description=f"Git repository: {repo_name}",
            )
            _log.info("catalog_owner_created", owner=str(owner), repo=repo_name)

        import sys
        _progress = sys.stderr.write
        _progress(f"  Catalog: registering {len(indexed_files)} files…\r")
        new_tumblers = []
        # nexus-o6aa.10.4 follow-up: track per-file failures so the
        # catalog hook stops failing silently. Pre-fix, a single
        # cat.register() exception inside the loop tripped the outer
        # except at line ~362 which logs at DEBUG, suppressing the
        # rest of the registrations and leaving file_to_doc_id empty
        # for every subsequent file in this run. Found via Hal's
        # ghost-chunk class on 2026-05-02.
        skipped_files: list[tuple[Path, str]] = []
        for abs_path, content_type, collection_name in indexed_files:
            try:
                rel_path = str(abs_path.relative_to(repo))
            except ValueError:
                rel_path = abs_path.name

            # nexus-8luh: capture mtime at index time so stale-source
            # detection (RDR-087 Phase 3.4) can compare stored vs
            # current. Missing file falls back to 0 (treated as
            # "unknown" downstream).
            #
            # Review remediation (Reviewer B/I-3): stat BEFORE read_bytes.
            # If the file is modified between stat and read, the stored
            # mtime will be *older* than the indexed content — the safe
            # direction, because a future ``st_mtime > stored`` comparison
            # correctly flags the file as stale relative to what we
            # indexed. The reverse order (hash first, stat later) would
            # record a stored mtime NEWER than the content we actually
            # hashed, suppressing a staleness warning that should fire.
            try:
                source_mtime = abs_path.stat().st_mtime
            except OSError:
                source_mtime = 0.0

            # Per-file content hash for rename detection (RDR-060 E7)
            import hashlib as _hl
            try:
                file_hash = _hl.sha256(abs_path.read_bytes()).hexdigest()
            except OSError:
                file_hash = ""

            try:
                existing = cat.by_file_path(owner, rel_path)
                if existing is None:
                    tumbler = cat.register(
                        owner=owner,
                        title=abs_path.name,
                        content_type=content_type,
                        file_path=rel_path,
                        physical_collection=collection_name,
                        head_hash=head_hash,
                        meta={"content_hash": file_hash} if file_hash else None,
                        source_mtime=source_mtime,
                    )
                    new_tumblers.append(tumbler)
                    file_to_doc_id[abs_path] = str(tumbler)
                else:
                    cat.update(
                        existing.tumbler,
                        head_hash=head_hash,
                        physical_collection=collection_name,
                        meta={"content_hash": file_hash} if file_hash else None,
                        source_mtime=source_mtime,
                    )
                    file_to_doc_id[abs_path] = str(existing.tumbler)
            except Exception as exc:
                # Per-file failure must NOT abort the rest of the loop.
                # The previous behaviour swallowed every subsequent
                # registration too, leaving the entire repo's chunks
                # without doc_id metadata (the ghost class).
                skipped_files.append((abs_path, str(exc)))
                _log.warning(
                    "catalog_hook_register_failed",
                    rel_path=rel_path,
                    abs_path=str(abs_path),
                    error=str(exc),
                    exc_info=True,
                )
        if skipped_files:
            _progress(
                f"  Catalog: {len(new_tumblers)} new, "
                f"{len(indexed_files) - len(new_tumblers) - len(skipped_files)} updated, "
                f"{len(skipped_files)} skipped (see structlog warnings)\n"
            )
        else:
            _progress(
                f"  Catalog: {len(new_tumblers)} new, "
                f"{len(indexed_files) - len(new_tumblers)} updated\n"
            )

        # Auto-generate links after registration (incremental: only new entries)
        links_created = 0
        if new_tumblers:
            _progress(f"  Catalog: linking {len(new_tumblers)} new entries…\r")
        try:
            from nexus.catalog.link_generator import (
                generate_pdf_corpus_links,
                generate_prose_filepath_links,
                generate_rdr_filepath_links,
            )
            fp_count = generate_rdr_filepath_links(cat, new_tumblers=new_tumblers)
            # nexus-sob9: prose + pdf coverage. Run with the same
            # incremental scope so a single bulk-index pass closes
            # the prose/pdf 0% gap from the 2026-05-08 prod shakeout.
            prose_count = generate_prose_filepath_links(
                cat, new_tumblers=new_tumblers,
            )
            pdf_count = generate_pdf_corpus_links(
                cat, new_tumblers=new_tumblers,
            )
            links_created = fp_count + prose_count + pdf_count
            if links_created:
                _log.info(
                    "catalog_links_generated",
                    filepath=fp_count, prose=prose_count, pdf=pdf_count,
                    repo=repo_name,
                )
        except Exception:
            _log.debug("catalog_link_generation_failed", exc_info=True)

        # Housekeeping: detect and evict orphaned catalog entries
        _progress(f"  Catalog: housekeeping…\r")
        indexed_set = _indexed_relpaths(indexed_files, repo)
        _run_housekeeping(cat, owner, indexed_set)
        _progress(f"  Catalog: done ({len(new_tumblers)} new, {links_created} links)\n")
    except Exception:
        _log.debug("catalog_hook_failed", exc_info=True)
    return file_to_doc_id


def _indexed_relpaths(indexed_files: list, repo: "Path") -> set[str]:
    """Repo-relative paths of the indexed files, tolerant of symlink mismatch.

    nexus-f3tyz: a single ``abs_path.relative_to(repo)`` ValueError (macOS
    symlinks ``/tmp`` -> ``/private/tmp`` and ``/var`` -> ``/private/var`` make
    one path not literally under ``repo``) previously aborted the whole
    housekeeping set-comprehension, so ``_run_housekeeping`` was silently
    skipped and orphaned catalog rows never got evicted. Fall back to comparing
    resolved paths (the relative suffix is identical, so the key stays
    consistent with registration); skip a genuinely-outside-repo path rather
    than abort the whole pass.
    """
    repo_resolved = repo.resolve()
    out: set[str] = set()
    for abs_path, _c1, _c2 in indexed_files:
        try:
            out.add(str(abs_path.relative_to(repo)))
        except ValueError:
            try:
                out.add(str(abs_path.resolve().relative_to(repo_resolved)))
            except ValueError:
                _log.debug(
                    "housekeeping_rel_path_skip",
                    path=str(abs_path), repo=str(repo),
                )
    return out


def _run_housekeeping(
    cat: "Catalog",
    owner: "Tumbler",
    indexed_set: set[str],
) -> None:
    """Orphan detection with miss_count tracking and rename detection.

    For each catalog entry owned by *owner*:
    - If the file is present in *indexed_set*, reset miss_count to 0.
    - If absent and a content_hash match exists at a new path, treat as rename:
      transfer links to the new entry and delete the old one.
    - If absent with no rename match, increment miss_count. Delete at threshold >= 2.
    """
    owner_entries = cat.by_owner(owner)

    # Build content_hash → entry map for rename detection
    hash_to_entry: dict[str, object] = {}
    for e in owner_entries:
        ch = (e.meta or {}).get("content_hash", "")
        if ch and e.file_path in indexed_set:
            hash_to_entry[ch] = e

    for entry in owner_entries:
        if entry.file_path in indexed_set:
            meta = entry.meta or {}
            if int(meta.get("miss_count", 0)) > 0:
                meta = dict(meta)
                meta["miss_count"] = 0
                cat.update(entry.tumbler, meta=meta)
            continue

        # Check for rename: orphan's content_hash matches a newly-indexed entry
        orphan_hash = (entry.meta or {}).get("content_hash", "")
        if orphan_hash and orphan_hash in hash_to_entry:
            new_entry = hash_to_entry[orphan_hash]
            # Transfer links from old entry to new entry
            old_links = cat.links_from(entry.tumbler)
            for lnk in old_links:
                cat.link_if_absent(
                    new_entry.tumbler, lnk.to_tumbler, lnk.link_type,
                    created_by=lnk.created_by,
                )
            # Also transfer incoming links
            incoming = cat.links_to(entry.tumbler)
            for lnk in incoming:
                cat.link_if_absent(
                    lnk.from_tumbler, new_entry.tumbler, lnk.link_type,
                    created_by=lnk.created_by,
                )
            cat.delete_document(entry.tumbler)
            _log.info(
                "housekeeping_rename_detected",
                old_path=entry.file_path,
                new_path=new_entry.file_path,
                old_tumbler=str(entry.tumbler),
                new_tumbler=str(new_entry.tumbler),
            )
            continue

        # File not in this index run — increment miss_count
        meta = dict(entry.meta or {})
        miss_count = int(meta.get("miss_count", 0)) + 1

        if miss_count >= 2:
            cat.delete_document(entry.tumbler)
            _log.info(
                "housekeeping_orphan_deleted",
                tumbler=str(entry.tumbler),
                file_path=entry.file_path,
            )
        else:
            meta["miss_count"] = miss_count
            cat.update(entry.tumbler, meta=meta)
            _log.debug(
                "housekeeping_miss_count_incremented",
                tumbler=str(entry.tumbler),
                miss_count=miss_count,
            )


def index_repository(
    repo: Path,
    registry: "RepoRegistry",
    *,
    frecency_only: bool = False,
    chunk_lines: int | None = None,
    force: bool = False,
    force_stale: bool = False,
    on_locked: str = "wait",
    on_start: Callable[[int], None] | None = None,
    on_file: Callable[[Path, int, float], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_stage_timers: Callable[[Path, "StageTimers"], None] | None = None,
    hooks: "HookRegistry | None" = None,
) -> dict[str, int]:
    """Index all files in *repo* into T3 code__ and docs__ collections.

    Files are classified and routed:
    - Code → code__ collection (voyage-code-3, AST chunking)
    - Prose → docs__ collection (voyage-context-3, semantic chunking)
    - PDF → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → rdr__ collection

    Marks status as 'indexing' while running, 'ready' on success,
    'pending_credentials' when T3 credentials are absent.

    *frecency_only* skips re-chunking and re-embedding; only updates the
    ``frecency_score`` metadata field on existing T3 chunks.  Frecency-only
    runs bypass the per-repo lock and do not update ``head_hash``.

    *chunk_lines* overrides the default chunk size (150 lines) for code files.
    When None, the module default is used.

    *on_locked* controls behaviour when another process holds the repo lock:
    ``'wait'`` (default) blocks until the lock is released; ``'skip'`` returns
    ``{}`` immediately without indexing.  Frecency-only runs bypass the lock.

    Returns a stats dict (empty for frecency_only runs) with keys:
    ``rdr_indexed``, ``rdr_current``, ``rdr_failed``.
    """
    lock_fd = None
    lock_path: Path | None = None
    if not frecency_only:
        lock_path = _repo_lock_path(repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _sweep_stale_locks(lock_path.parent)
        _clear_stale_lock(lock_path)
        lock_fd = open(lock_path, "w")  # noqa: SIM115  (must stay open while locked)
        try:
            lock_fd.write(str(os.getpid()))
            lock_fd.flush()
        except OSError:
            pass  # PID write is best-effort; lock still works without it
        try:
            lock_file(lock_fd, blocking=(on_locked == "wait"))
        except BlockingIOError:
            # on_locked == "skip" and another process holds the lock
            lock_fd.close()
            return {}

    if hooks is None:
        from nexus.hook_registry import HookRegistry, install_default_hooks
        hooks = HookRegistry()
        install_default_hooks(hooks)

    try:
        # RDR-137 Phase 3.8 (nexus-tts0d.13): registry.update(status=...)
        # writes dropped per A2 verdict — status is write-only with no
        # consumers. head_hash now writes to owners.head_hash on the
        # catalog (Phase 1.5b column) via _set_owner_head_hash.
        try:
            if frecency_only:
                _run_index_frecency_only(repo, registry)
                stats: dict[str, int] = {}
            else:
                stats = _run_index(repo, registry, chunk_lines=chunk_lines, force=force, force_stale=force_stale, on_start=on_start, on_file=on_file, on_phase=on_phase, on_stage_timers=on_stage_timers, hooks=hooks)
                _set_owner_head_hash(repo, _current_head(repo))
            return stats
        except CredentialsMissingError:
            raise
        except Exception:
            raise
    finally:
        if lock_fd is not None:
            unlock_file(lock_fd)
            lock_fd.close()
        if lock_path is not None:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass  # already gone — harmless


def _build_frecency_doc_id_map(
    repo: Path, files: list[Path],
) -> dict[Path, str]:
    """nexus-f4z9: resolve each file's catalog ``doc_id`` so the
    frecency-only update can key chunk lookups on ``doc_id`` instead
    of ``source_path``. Returns a ``{abs_path: doc_id}`` mapping;
    files without a catalog entry are absent from the map (the caller
    falls back to the legacy ``source_path`` filter for those).

    Best-effort: catalog absent / owner missing / lookup failure all
    return an empty map so the caller's legacy path keeps working.
    """
    file_to_doc_id: dict[Path, str] = {}
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path
        from nexus.registry import _repo_identity

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return file_to_doc_id
        cat = Catalog(cat_path, cat_path / ".catalog.db")
        _, repo_hash = _repo_identity(repo)
        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            return file_to_doc_id
        for abs_path in files:
            try:
                rel_path = str(abs_path.relative_to(repo))
            except ValueError:
                rel_path = abs_path.name
            try:
                entry = cat.by_file_path(owner, rel_path)
            except Exception:
                continue
            if entry is not None:
                file_to_doc_id[abs_path] = str(entry.tumbler)
    except Exception:
        _log.debug("frecency_doc_id_map_failed", exc_info=True)
    return file_to_doc_id


def _run_index_frecency_only(repo: Path, registry: "RepoRegistry") -> None:
    """Update frecency_score metadata on all indexed chunks without re-embedding.

    Handles both code__ and docs__ collections.
    """
    from nexus.config import get_credential
    from nexus.frecency import batch_frecency
    from nexus.db import make_t3

    info = registry.get(repo)
    if info is None:
        return

    # RDR-103 Phase 3a: registry value preserves the legacy name when the
    # repo was added before the migration; fallback queries the catalog
    # for a conformant name (Phase 5 drops the legacy branch entirely).
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _repo_collection_or_legacy(repo, "docs")

    from nexus.config import is_local_mode
    if not is_local_mode():
        voyage_key = get_credential("voyage_api_key")
        chroma_key = get_credential("chroma_api_key")
        check_credentials(voyage_key, chroma_key)
    else:
        check_local_path_writable()

    frecency_map = batch_frecency(repo)
    db = make_t3()

    # nexus-f4z9: pre-resolve doc_ids once for all files so the chunk
    # lookup can key on ``doc_id`` when the catalog has the entry.
    # Files predating the catalog backfill fall through to the legacy
    # ``source_path``-keyed filter.
    file_to_doc_id = _build_frecency_doc_id_map(repo, list(frecency_map.keys()))

    # Update frecency in both collections
    collection_names = [code_collection]
    if docs_collection:
        collection_names.append(docs_collection)

    # nexus-ks40: frecency_only is a read-update flow; if the
    # collection has not yet been written, skip rather than mint an
    # empty zombie via get_or_create_collection.
    from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: PLC0415

    # nexus-7zcv (RDR-108 Phase 4 review D-H4): the legacy
    # ``where={"doc_id": <id>}`` lookup matches nothing for Phase-3
    # chunks (the doc_id metadata field is gone). Use the catalog
    # document_chunks manifest to map doc_id -> chashes, then fetch
    # by ``chash[:32]`` IDs. Falls back to the legacy where-filter
    # when the catalog is unavailable (correct only for pre-Phase-3
    # chunks).
    _cat = None
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path
        _cp = catalog_path()
        if Catalog.is_initialized(_cp):
            _cat = Catalog(_cp, _cp / ".catalog.db")
    except Exception:
        _log.debug("frecency_only_catalog_lookup_failed", exc_info=True)

    for collection_name in collection_names:
        try:
            col = db.get_collection(collection_name)
        except _ChromaNotFoundError:
            continue
        for file, score in frecency_map.items():
            doc_id = file_to_doc_id.get(file, "")

            existing: dict | None = None
            if _cat is not None and doc_id:
                # Manifest-based path: resolve chashes for this doc.
                try:
                    manifest = _cat.get_manifest(doc_id)
                except Exception:
                    manifest = []
                natural_ids = [r.chash[:32] for r in manifest if r.chash]
                if natural_ids:
                    try:
                        present = col.get(
                            ids=natural_ids, include=["metadatas"],
                        )
                    except Exception:
                        present = None
                    if present and present.get("ids"):
                        existing = present

            if existing is None:
                # Legacy where-filter fallback. Returns nothing for
                # post-Phase-3 chunks; correct for pre-Phase-3 only.
                where = (
                    {"doc_id": doc_id} if doc_id
                    else {"source_path": str(file)}
                )
                existing = _paginated_get(
                    col, include=["metadatas"], where=where,
                )

            if not existing["ids"]:
                continue  # not yet indexed — needs full nx index repo

            updated_metadatas = [
                {**m, "frecency_score": float(score)}
                for m in existing["metadatas"]
            ]
            db.update_chunks(collection=collection_name, ids=existing["ids"], metadatas=updated_metadatas)


# ── Backward-compatible per-file wrappers ────────────────────────────────────
# These wrappers preserve the old 12-parameter signatures so that:
# 1. Existing tests that import and call _index_code_file/_index_prose_file
#    directly continue to work unchanged.
# 2. Tests that patch nexus.indexer._index_code_file or _index_prose_file
#    still intercept the calls from _run_index.
#
# Implementation delegates to the clean IndexContext-based API in the
# extracted sub-modules (nexus.code_indexer, nexus.prose_indexer).
# The wrappers are intentionally thin — no logic here.


def _index_code_file(
    file: Path,
    repo: Path,
    collection_name: str,
    target_model: str,
    col: object,
    db: object,
    voyage_client: object,
    git_meta: dict,
    now_iso: str,
    score: float,
    chunk_lines: int | None = None,
    force: bool = False,
    *,
    embed_fn: Callable | None = None,
    stage_timers: "StageTimers | None" = None,
    doc_id_resolver: Callable[[Path], str] | None = None,
    staleness_cache: "StalenessCache | None" = None,
    hooks: "HookRegistry | None" = None,
) -> int:
    """Index a single code file.  Delegates to nexus.code_indexer.index_code_file.

    Backward-compatible wrapper preserving the old 12-parameter signature.
    See nexus.code_indexer.index_code_file for the canonical implementation.

    ``stage_timers`` (nexus-7niu) is an optional :class:`StageTimers` the
    per-file indexer writes chunking / embed / upload / retry times into.
    When ``None`` the instrumented blocks are no-ops; no overhead.

    ``doc_id_resolver`` (RDR-101 Phase 3 PR δ Stage B.2) returns the
    catalog ``Document.doc_id`` for *file* so code chunks land in T3
    with a back-reference to the catalog. ``None`` is the legacy /
    no-catalog path.

    ``staleness_cache`` is the orchestrator-built collection-wide
    staleness map. When supplied, the per-file ``check_staleness`` is
    a dict lookup instead of a Chroma roundtrip.
    """
    from nexus.code_indexer import index_code_file
    from nexus.index_context import IndexContext

    ctx = IndexContext(
        col=col,
        db=db,
        voyage_key="",  # code path uses voyage_client directly
        voyage_client=voyage_client,
        repo_path=repo,
        corpus=collection_name,
        embedding_model=target_model,
        git_meta=git_meta,
        now_iso=now_iso,
        score=score,
        chunk_lines=chunk_lines,
        force=force,
        embed_fn=embed_fn,
        stage_timers=stage_timers,
        doc_id_resolver=doc_id_resolver,
        staleness_cache=staleness_cache,
        hooks=hooks,
    )
    return index_code_file(ctx, file)


def _index_prose_file(
    file: Path,
    repo: Path,
    collection_name: str,
    target_model: str,
    col: object,
    db: object,
    voyage_key: str,
    git_meta: dict,
    now_iso: str,
    score: float,
    force: bool = False,
    timeout: float = 120.0,
    *,
    embed_fn: Callable | None = None,
    stage_timers: "StageTimers | None" = None,
    doc_id_resolver: Callable[[Path], str] | None = None,
    staleness_cache: "StalenessCache | None" = None,
    hooks: "HookRegistry | None" = None,
) -> int:
    """Index a single prose file.  Delegates to nexus.prose_indexer.index_prose_file.

    Backward-compatible wrapper preserving the old 12-parameter signature.
    See nexus.prose_indexer.index_prose_file for the canonical implementation.

    ``stage_timers`` (nexus-7niu) is an optional :class:`StageTimers` the
    per-file indexer writes chunking / embed / upload / retry times into.
    ``None`` is the fast path: no overhead, no output.

    ``doc_id_resolver`` (RDR-101 Phase 3 PR δ Stage B.1) returns the
    catalog ``Document.doc_id`` for *file* so prose chunks land in T3
    with a back-reference to the catalog. ``None`` is the legacy /
    no-catalog path.
    """
    from nexus.prose_indexer import index_prose_file
    from nexus.index_context import IndexContext

    ctx = IndexContext(
        col=col,
        db=db,
        voyage_key=voyage_key,
        voyage_client=None,  # prose path uses voyage_key
        repo_path=repo,
        corpus=collection_name,
        embedding_model=target_model,
        git_meta=git_meta,
        now_iso=now_iso,
        score=score,
        force=force,
        timeout=timeout,
        embed_fn=embed_fn,
        stage_timers=stage_timers,
        doc_id_resolver=doc_id_resolver,
        staleness_cache=staleness_cache,
        hooks=hooks,
    )
    return index_prose_file(ctx, file)


def _index_pdf_file(
    file: Path,
    repo: Path,
    collection_name: str,
    target_model: str,
    col: object,
    db: object,
    voyage_key: str,
    git_meta: dict,
    now_iso: str,
    score: float,
    force: bool = False,
    timeout: float = 120.0,
    chunk_chars: int | None = None,
    *,
    embed_fn: Callable | None = None,
    stage_timers: "StageTimers | None" = None,
    doc_id_resolver: Callable[[Path], str] | None = None,
    staleness_cache: "StalenessCache | None" = None,
    hooks: "HookRegistry | None" = None,
) -> int:
    """Index a single PDF file into the docs__ collection.

    Uses PDF extraction + chunking from doc_indexer, embeds via _embed_with_fallback.
    Returns the post-filter chunk count (chunks upserted), or 0 if skipped/failed.

    *chunk_chars* overrides the PDF chunk size (default 1500 chars).  Pass
    ``tuning.pdf_chunk_chars`` from TuningConfig to honour per-repo config.

    ``stage_timers`` (nexus-7niu) is an optional :class:`StageTimers` that
    accumulates chunking / embed / upload / retry time for this file.
    ``None`` is the fast path — the instrumented blocks are no-ops.

    ``doc_id_resolver`` (RDR-101 Phase 3 PR δ Stage B.3) returns the
    catalog ``Document.doc_id`` for *file* so PDF chunks land in T3 with
    a back-reference to the catalog. ``None`` is the legacy /
    no-catalog path.
    """
    import hashlib as _hl
    from nexus.doc_indexer import _embed_with_fallback, _pdf_chunks

    content_hash = _hl.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            content_hash.update(block)
    content_hash_hex = content_hash.hexdigest()

    # Staleness check.
    # nexus-dcym: prefer doc_id-keyed lookup when the catalog hook
    # supplied a resolver; falls back to source_path for legacy chunks.
    catalog_doc_id_for_staleness = (
        doc_id_resolver(file) if doc_id_resolver is not None else ""
    )
    if not force and check_staleness(
        col, file, content_hash_hex, target_model,
        doc_id=catalog_doc_id_for_staleness,
        cache=staleness_cache,
    ):
        return 0

    # nexus-7niu: per-stage timer instrumentation. Silent when
    # ``stage_timers is None`` — no overhead, no output.
    if stage_timers is not None:
        _stage = stage_timers.stage
    else:
        from contextlib import nullcontext
        def _stage(_name: str):  # type: ignore[misc]
            return nullcontext()

    with _stage("chunking"):
        prepared = _pdf_chunks(file, content_hash_hex, target_model, now_iso, collection_name, chunk_chars=chunk_chars)
    if not prepared:
        _log.debug("skipped PDF with no chunks", path=str(file))
        return 0

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas_raw = [p[2] for p in prepared]

    # Build embed_texts with context prefix BEFORE augmentation overwrites 'title'.
    # PDF title is now stored under `title` (the source_title→title
    # collapse). Read from metadatas_raw and use it in the embed prefix.
    embed_texts_pdf: list[str] = []
    for doc, m in zip(documents, metadatas_raw):
        title = m.get("title", "")
        page_number = m.get("page_number", 0)
        prefix_parts: list[str] = []
        if title:
            prefix_parts.append(f"Document: {title}")
        prefix_parts.append(f"Page: {page_number}")
        prefix = "## " + "  ".join(prefix_parts)
        embed_texts_pdf.append(f"{prefix}\n\n{doc}")

    # Catalog Document.doc_id (RDR-101 Phase 3 PR δ Stage B.3): resolved
    # once per file. Empty string when no catalog handle exists;
    # ``normalize`` Step 4c drops the field on the way to T3.
    catalog_doc_id = doc_id_resolver(file) if doc_id_resolver is not None else ""

    # Augment metadata with repo-indexer fields.
    # Filter empty/zero values from _pdf_chunks to stay under ChromaDB's
    # 32-key metadata limit. PDF chunks produce ~31 raw keys; augmentation
    # adds ~12 more. Without filtering, the _write_batch trimmer silently
    # drops keys by insertion order, losing git metadata.
    _EMPTY_VALUES = ("", 0, False, None)
    # Keys where empty/zero IS meaningful (TTL guard, required fields)
    _KEEP_ALWAYS = {"ttl_days", "chunk_index", "page_number"}
    metadatas: list[dict] = []
    for m in metadatas_raw:
        # Drop empty raw metadata values
        cleaned = {
            k: v for k, v in m.items()
            if v not in _EMPTY_VALUES or k in _KEEP_ALWAYS
        }
        augmented = {
            **cleaned,
            "title": f"{file.relative_to(repo)}:page-{m.get('page_number', 0)}",
            "tags": "pdf",
            "category": "prose",
            "source_agent": "nexus-indexer",
            "ttl_days": 0,
            "frecency_score": float(score),
            "doc_id": catalog_doc_id,
            **{k: v for k, v in git_meta.items() if v},
        }
        metadatas.append(augmented)

    with _stage("embed"):
        if embed_fn is not None:
            embeddings = embed_fn(embed_texts_pdf)
            actual_model = target_model
        else:
            embeddings, actual_model = _embed_with_fallback(embed_texts_pdf, target_model, voyage_key, timeout=timeout)
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model

    with _stage("upload"):
        db.upsert_chunks_with_embeddings(
            collection_name=collection_name,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        # Post-store hook chains (RDR-095). Both single-doc and batch
        # chains fire from every storage event; consumers register in
        # whichever shape fits their work. Single-doc fire iterates the
        # batch one document at a time so per-doc hooks (e.g. RDR-089
        # aspect extraction) cover CLI ingest the same way they cover
        # MCP store_put.
        if hooks is None:
            from nexus.hook_registry import HookRegistry, install_default_hooks
            hooks = HookRegistry()
            install_default_hooks(hooks)
        hooks.fire_batch(
            ids, collection_name, documents, embeddings, metadatas,
            catalog_doc_id=catalog_doc_id,
        )
        for _did, _doc in zip(ids, documents):
            hooks.fire_single(_did, collection_name, _doc)
        # RDR-089 document-grain chain — once per PDF file boundary in
        # the `nx index repo` PDF path. content="" (chunk-level scope
        # only); the hook reads source_path itself per the P0.1
        # content-sourcing contract.
        # nexus-tdgc: forward catalog doc_id when available.
        hooks.fire_document(
            str(file), collection_name, "",
            doc_id=catalog_doc_id,
        )

    return len(prepared)


def _discover_and_index_rdrs(
    repo: Path,
    rdr_abs_paths: set[Path],
    db: object,
    voyage_key: str,
    now_iso: str,
    *,
    force: bool = False,
    embed_fn: Callable | None = None,
    hooks: "HookRegistry | None" = None,
) -> tuple[int, int, int]:
    """Find .md files under RDR paths and index them via batch_index_markdowns.

    M2: passes t3=db to avoid creating a redundant T3 client.

    Returns (indexed, skipped, failed) counts.

    Note: ``on_file`` progress callbacks are intentionally NOT wired here.
    RDR files are excluded from the main ``_run_index`` file loop and their
    count is not known up front (discovered inside this function).  For
    standalone RDR progress reporting, call ``batch_index_markdowns`` directly
    with an ``on_file`` callback (Path B in the progress reporting design).
    """
    from nexus.doc_indexer import batch_index_markdowns

    if not rdr_abs_paths:
        _log.debug("RDR indexing skipped — no rdr_paths configured")
        return 0, 0, 0

    md_paths: list[Path] = []
    for rdr_dir in rdr_abs_paths:
        if not rdr_dir.is_dir():
            continue
        for path in sorted(rdr_dir.rglob("*.md")):
            if path.is_file() and not path.is_symlink():
                md_paths.append(path)

    if not md_paths:
        _log.debug("no RDR files found", rdr_paths=[str(p) for p in rdr_abs_paths])
        return 0, 0, 0

    # RDR-103 Phase 3a: ``_repo_collection_or_legacy`` queries the
    # catalog for a conformant ``rdr__<owner>__voyage-context-3__v<n>``,
    # falling back to the legacy ``rdr__<basename>-<hash8>`` shape when
    # the catalog is not initialized or the owner is not yet registered.
    from nexus.registry import _repo_identity
    basename, _ = _repo_identity(repo)
    collection = _repo_collection_or_legacy(repo, "rdr")

    _log.info("indexing RDR files", count=len(md_paths), collection=collection)
    results = batch_index_markdowns(md_paths, corpus=basename, t3=db,
                                    collection_name=collection, force=force,
                                    embed_fn=embed_fn, hooks=hooks)
    indexed = sum(1 for s in results.values() if s == "indexed")
    skipped = sum(1 for s in results.values() if s == "skipped")
    failed = sum(1 for s in results.values() if s == "failed")
    log_kwargs: dict = {"indexed": indexed, "current": skipped, "failed": failed}
    if failed:
        log_kwargs["failed_paths"] = sorted(
            p for p, s in results.items() if s == "failed"
        )
    _log.info("RDR indexing complete", **log_kwargs)
    return indexed, skipped, failed


_CHROMA_PAGE_SIZE: int = 300
"""ChromaDB Cloud hard cap per get() call — paginate above this."""


def _paginated_get(col: object, include: list[str], where: dict | None = None) -> dict:
    """Fetch all matching chunks from *col* by paginating in _CHROMA_PAGE_SIZE batches.

    ChromaDB Cloud silently truncates unbounded col.get() calls at 300 records.
    This helper accumulates pages until a short page signals the end.

    Returns a dict with ``"ids"`` and, when ``"metadatas"`` is in *include*,
    a ``"metadatas"`` key — matching the shape returned by col.get().
    """
    offset = 0
    all_ids: list[str] = []
    all_metas: list[dict] = []
    has_metas = "metadatas" in include

    while True:
        kwargs: dict = {"include": include, "limit": _CHROMA_PAGE_SIZE, "offset": offset}
        if where is not None:
            kwargs["where"] = where
        batch = _chroma_with_retry(col.get, **kwargs)
        batch_ids: list[str] = batch["ids"] or []
        all_ids.extend(batch_ids)
        if has_metas:
            all_metas.extend(batch.get("metadatas") or [])
        if len(batch_ids) < _CHROMA_PAGE_SIZE:
            break
        offset += _CHROMA_PAGE_SIZE

    result: dict = {"ids": all_ids}
    if has_metas:
        result["metadatas"] = all_metas
    return result


def _batched_delete(col: object, ids: list[str]) -> int:
    """Delete *ids* from *col* in batches of _CHROMA_PAGE_SIZE (Cloud quota: 300)."""
    deleted = 0
    for i in range(0, len(ids), _CHROMA_PAGE_SIZE):
        batch = ids[i : i + _CHROMA_PAGE_SIZE]
        _chroma_with_retry(col.delete, ids=batch)
        deleted += len(batch)
    return deleted


def _prune_misclassified_in_collection(
    col: object,
    target_paths: set[Path],
    file_to_doc_id: dict[Path, str],
    *,
    kind: str,
    catalog: object | None = None,
) -> int:
    """Find and delete chunks in *col* whose document matches any
    file in *target_paths*. Returns the number of chunks pruned.

    nexus-7zcv (RDR-108 Phase 4 review D-H4): the legacy path used
    ``where={"doc_id": {"$in": [batch]}}`` against chunk metadata.
    RDR-108 Phase 3 removed ``doc_id`` from chunk metadata; the
    where-filter matches nothing for Phase-3 chunks and the prune
    silently no-ops. Post-fix: when a catalog is provided, resolve
    each doc_id's chashes via the document_chunks manifest and
    delete by ``chash[:32]`` (the RDR-108 D1 natural id). Legacy
    where-filter retained as fallback for catalog-absent callers.

    Files not present in *file_to_doc_id* (legacy / pre-RDR-102 D2
    rows) fall back to per-path ``source_path`` lookup so collections
    that pre-date the catalog backfill keep getting cleaned up. The
    legacy set is typically small post-backfill.
    """
    doc_ids: list[str] = []
    legacy_paths: list[str] = []
    for path in target_paths:
        d = file_to_doc_id.get(path, "")
        if d:
            doc_ids.append(d)
        else:
            legacy_paths.append(str(path))

    pruned = 0

    if catalog is not None and doc_ids:
        # Manifest-based path: per-doc, fetch the chashes that document
        # owns and delete the ones present in this (wrong) collection
        # using ``col.get(ids=...)`` on chash[:32]. Chroma returns only
        # the IDs that exist in this collection, so the cross-direction
        # check (a code file's chunks living in docs__) works without
        # any per-chunk metadata.
        # nexus-qj1q: dedupe via set so col.get(ids=batch_ids) sees
        # unique IDs. Two docs may share a chunk (same file content
        # vendored to two paths, shared boilerplate header, etc.); the
        # same chash[:32] would otherwise appear multiple times in the
        # batch and Chroma rejects with DuplicateIDError.
        natural_id_set: set[str] = set()
        # nexus-8g79.4: bare ``continue`` on get_manifest failure silently
        # skipped doc_ids whose manifest lookup raised (catalog miss,
        # transient SQLite error). Those chunks were then never pruned
        # from T3 — over successive runs T3 grew with unreachable orphan
        # chunks. Log at WARNING with the doc_id so operators see the
        # affected scope; count for the post-run summary.
        skipped_doc_ids: dict[str, str] = {}
        for did in doc_ids:
            try:
                manifest = catalog.get_manifest(did)
            except Exception as exc:
                skipped_doc_ids[did] = f"{type(exc).__name__}: {exc}"
                _log.warning(
                    "prune_misclassified_manifest_lookup_failed",
                    doc_id=did,
                    collection=getattr(col, "name", "?"),
                    kind=kind,
                    exc_info=True,
                )
                continue
            for row in manifest:
                if row.chash:
                    natural_id_set.add(row.chash[:32])
        all_natural_ids: list[str] = list(natural_id_set)
        # Batched ``col.get`` to fetch the present subset, then batched
        # delete. _CHROMA_PAGE_SIZE caps the ids list per call.
        for i in range(0, len(all_natural_ids), _CHROMA_PAGE_SIZE):
            batch_ids = all_natural_ids[i : i + _CHROMA_PAGE_SIZE]
            if not batch_ids:
                continue
            try:
                present = col.get(ids=batch_ids, include=[])
            except Exception:
                # nexus-8g79.4: same class — log so a recurring chroma
                # outage during prune doesn't hide silently behind a
                # bare ``continue``.
                _log.warning(
                    "prune_misclassified_chroma_get_failed",
                    collection=getattr(col, "name", "?"),
                    kind=kind,
                    batch_size=len(batch_ids),
                    exc_info=True,
                )
                continue
            present_ids = present.get("ids") or []
            if present_ids:
                _batched_delete(col, present_ids)
                pruned += len(present_ids)
                _log.debug(
                    f"pruned misclassified chunks from {kind} collection (manifest)",
                    count=len(present_ids),
                    doc_id_batch_size=len(batch_ids),
                )
    # Legacy where-filter path: complements the manifest-chash path
    # above. The manifest path catches Phase-3+ chunks (chroma natural-id
    # == chash[:32], no doc_id in metadata). The where={"doc_id"} path
    # catches Phase-2 chunks (doc_id in metadata, arbitrary chroma id)
    # AND any chunk seeded with explicit doc_id metadata (e.g. the
    # migration test that injects a misclassified chunk with the README
    # tumbler as doc_id but an arbitrary string id). Catalog-absent
    # callers (catalog is None) reach this via the doc_ids list still
    # being populated. For Phase-3 chunks with no doc_id metadata the
    # where-filter is a no-op (returns nothing), so running it alongside
    # the manifest path is safe.
    if doc_ids:
        for i in range(0, len(doc_ids), _CHROMA_PAGE_SIZE):
            batch = doc_ids[i : i + _CHROMA_PAGE_SIZE]
            if not batch:
                continue
            try:
                existing = _paginated_get(
                    col, include=[], where={"doc_id": {"$in": batch}},
                )
            except Exception:
                # Some Chroma deployments reject ``$in`` on absent keys;
                # treat as empty result.
                continue
            if existing["ids"]:
                _batched_delete(col, existing["ids"])
                pruned += len(existing["ids"])
                _log.debug(
                    f"pruned misclassified chunks from {kind} collection (doc_id-keyed)",
                    count=len(existing["ids"]),
                    doc_id_batch_size=len(batch),
                )

    # Legacy source_path fallback for unmapped files. Cardinality is
    # bounded by the number of files indexed before catalog backfill,
    # which on a repo that has been on a recent nexus is typically zero.
    for src in legacy_paths:
        existing = _paginated_get(col, include=[], where={"source_path": src})
        if existing["ids"]:
            _batched_delete(col, existing["ids"])
            pruned += len(existing["ids"])
            _log.debug(
                f"pruned misclassified chunks from {kind} collection (legacy)",
                count=len(existing["ids"]),
                source_path=src,
            )

    return pruned


def _prune_misclassified(
    repo: Path,
    code_collection: str,
    docs_collection: str,
    code_files: list[Path],
    prose_files: list[Path],
    pdf_files: list[Path],
    db: object,
    *,
    file_to_doc_id: dict[Path, str] | None = None,
    catalog: object | None = None,
) -> None:
    """Remove chunks from the wrong collection after reclassification.

    If a file was previously classified as code but is now prose (or vice versa),
    its chunks in the old collection must be removed.

    nexus-dcym: when ``file_to_doc_id`` is supplied (always populated by
    the catalog hook), the chunk-prune lookup keys on ``doc_id``. Files
    not in the map fall back to the legacy ``source_path`` lookup so
    chunks indexed before catalog backfill keep getting cleaned up.

    Pre-batching history: this function used to do one
    ``col.get(where={"doc_id": <id>})`` per file. For ART (~4,800 files)
    that meant ~9,600 sequential ChromaDB Cloud roundtrips at
    50-200 ms each — 8 to 30 minutes of pure round-trip cost where the
    actual work (chunks to delete) was almost always zero. The batched
    ``where={"doc_id": {"$in": [batch]}}`` form collapses that to
    ~ceil(N / _CHROMA_PAGE_SIZE) queries per direction (~34 total for
    ART) — about a 300x reduction in roundtrips.
    """
    from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: PLC0415
    from tqdm import tqdm

    # nexus-ks40: read-only sweeps must NOT speculatively create T3
    # collections. ``get_or_create_collection`` would mint an empty
    # zombie T3 collection whenever this prune ran on a repo whose
    # ``code__`` or ``docs__`` collection had not been written yet
    # (fresh corpus, type-skewed corpus, post-housekeeping cleanup).
    # Use ``get_collection`` and skip the matching sweep when the
    # collection genuinely does not exist (there is nothing to
    # misclassify against an absent target).
    def _read_collection_or_none(name: str):
        try:
            return db.get_collection(name)
        except _ChromaNotFoundError:
            return None

    code_col = _read_collection_or_none(code_collection)
    docs_col = _read_collection_or_none(docs_collection)
    file_to_doc_id = file_to_doc_id or {}

    # Prose + PDF files should NOT have chunks in the code__ collection.
    # Code files should NOT have chunks in the docs__ collection. Each
    # sweep runs as one batched operation against the relevant collection.
    code_targets = set(prose_files) | set(pdf_files)
    docs_targets = set(code_files)
    pruned_chunks = 0

    bar = tqdm(
        total=2,
        disable=None,  # auto-disable on non-tty
        desc="Pruning misclassified",
        unit="sweep",
    )
    try:
        if code_col is not None:
            pruned_chunks += _prune_misclassified_in_collection(
                code_col, code_targets, file_to_doc_id, kind="code",
                catalog=catalog,
            )
        bar.set_postfix_str(f"chunks={pruned_chunks}", refresh=False)
        bar.update(1)

        if docs_col is not None:
            pruned_chunks += _prune_misclassified_in_collection(
                docs_col, docs_targets, file_to_doc_id, kind="docs",
                catalog=catalog,
            )
        bar.set_postfix_str(f"chunks={pruned_chunks}", refresh=False)
        bar.update(1)
    finally:
        bar.close()


def _prune_deleted_files(
    code_collection: str,
    docs_collection: str,
    db: object,
    *,
    catalog: object | None,
) -> None:
    """Remove orphan T3 chunks via the catalog manifest (RDR-108 Phase 4 /
    nexus-dyxe).

    The catalog's ``document_chunks`` table is the authoritative source
    of truth for which chunk content (chash) belongs to which document.
    Membership is tested by the chunk's ``chunk_text_hash`` metadata
    field (content hash, always present in the post-Phase-A schema)
    rather than the chunk's natural ID. The two coincide once a chunk
    has been migrated by ``nx t3 reidentify``, but until then live
    indexer writes still produce synthetic IDs (``sha256(corpus:title:
    chunk{i})[:32]``). Comparing by content hash preserves live data
    regardless of which scheme the chunk was written under.

    Orphans (chunk_text_hash not referenced by any manifest row in this
    collection) are produced by:

      - deleted documents (FK CASCADE drops their manifest rows),
      - re-indexing that supersedes content (UPSERT-on-(doc_id, position)
        replaces the chash at that slot, so the old chash is no longer
        referenced).

    Timing for deleted files: ``_run_housekeeping`` defers
    ``cat.delete_document`` until ``miss_count >= 2`` (rename-detection
    grace window). The document and its manifest rows therefore survive
    the first index run after a file is removed; this GC only sweeps
    the orphaned T3 chunks on the second run, when housekeeping
    actually deletes the document and FK CASCADE drops the manifest
    rows. One-run latency on cleanup, never on correctness.

    Pre-D1 [:16] cleanup (RDR-108 re-gate O1 mixed-state) is delegated
    to ``nx t3 reidentify``, whose Pass 2 batch-deletes the old IDs
    after re-upsert. GC's job is doc-level orphan removal; same-content
    duplicates are reidentify's job.

    Catalog-absent is a safe no-op: GC requires the manifest as the
    source of truth and cannot infer orphans without it.

    Note (operator runbook): the ``nx t3 gc`` CLI verb still uses the
    legacy ``meta.doc_id``-keyed path (``commands/t3.py:gc_cmd``) and
    therefore reports zero candidates for post-Phase-3 chunks. The two
    paths will be reconciled in nexus-e5aw; until then this function is
    the authoritative GC for content-addressed chunks and ``nx t3 gc``
    handles only legacy pre-Phase-3 orphans.
    """
    if catalog is None:
        return
    # nexus-ks40: read-only GC must use get_collection so an absent
    # T3 collection is a clean skip, not a speculative empty creation
    # (the latter is the leak that fed the doctor's "T3 collections
    # without projection rows" zombie list).
    from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: PLC0415
    for collection_name in (code_collection, docs_collection):
        referenced = catalog.chashes_for_collection(collection_name)
        try:
            col = db.get_collection(collection_name)
        except _ChromaNotFoundError:
            continue
        all_chunks = _paginated_get(col, include=["metadatas"])
        if not all_chunks["ids"]:
            continue
        # nexus-oqku: when the catalog manifest has zero referenced
        # chashes for a collection that DOES have T3 chunks, every
        # chunk would be classified as orphan and the loop below
        # would delete the entire collection. This fires on the
        # first ``nx index repo`` after the RDR-108 schema migration
        # lands on a system that has not yet run manifest backfill.
        # An empty manifest cannot prove ANY chunk is live or dead;
        # treat it the same way as the per-chunk missing-chash case
        # (skip and log) instead of silent full-collection wipe.
        if not referenced:
            _log.warning(
                "manifest_empty_skipping_gc",
                collection=collection_name,
                t3_chunks=len(all_chunks["ids"]),
                note=(
                    "catalog manifest has zero referenced chashes for this "
                    "collection but T3 has chunks. GC cannot safely decide "
                    "orphans without a manifest. Run a fresh `nx index repo` "
                    "(populates manifest via post-store hook) or backfill "
                    "manually before retrying GC."
                ),
            )
            continue
        orphan_ids: list[str] = []
        unsafe_skipped = 0
        for chunk_id, meta in zip(all_chunks["ids"], all_chunks["metadatas"]):
            chash = (meta.get("chunk_text_hash") or "")[:32]
            if not chash:
                # Pre-RDR-053 relics carry no ``chunk_text_hash`` in
                # metadata, so the manifest cannot prove them live or
                # dead. Silently sweeping them would be data loss for
                # the documented carve-out collection
                # ``docs__scheme-evolution-research-b7de0b63`` (~690
                # chunks per RDR-108 RF-1) and any other unmigrated
                # corpus. Skip and log; the operator cleans them up by
                # re-indexing the source or running ``nx t3
                # reidentify``, which adds the field.
                unsafe_skipped += 1
                continue
            if chash not in referenced:
                orphan_ids.append(chunk_id)
        if unsafe_skipped:
            _log.warning("skipped chunks without chunk_text_hash",
                         collection=collection_name,
                         count=unsafe_skipped,
                         note=("re-index source or run `nx t3 reidentify` "
                               "to populate chunk_text_hash; until then GC "
                               "cannot decide these chunks safely"))
        if orphan_ids:
            _batched_delete(col, orphan_ids)
            _log.info("pruned orphan chunks",
                      collection=collection_name, count=len(orphan_ids))


# ── Main indexing pipeline ───────────────────────────────────────────────────


def _run_index(
    repo: Path,
    registry: "RepoRegistry",
    chunk_lines: int | None = None,
    *,
    force: bool = False,
    force_stale: bool = False,
    on_start: Callable[[int], None] | None = None,
    on_file: Callable[[Path, int, float], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_stage_timers: Callable[[Path, "StageTimers"], None] | None = None,
    hooks: "HookRegistry | None" = None,
) -> dict[str, int]:
    """Full indexing pipeline: classify → route → embed → upsert → prune.

    Routes files to the appropriate collection based on content classification:
    - Code files → code__ collection (voyage-code-3, AST chunking)
    - Prose files → docs__ collection (voyage-context-3 via CCE)
    - PDF files → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → rdr__ collection (via batch_index_markdowns)

    Returns a stats dict with ``rdr_indexed``, ``rdr_current``, ``rdr_failed``.
    """
    from nexus.classifier import ContentClass, classify_file
    from nexus.config import get_credential, load_config
    from nexus.frecency import batch_frecency
    from nexus.ripgrep_cache import build_cache

    info = registry.get(repo)
    if info is None:
        return {}

    # RDR-103 Phase 3a: registry value preserves the legacy name when
    # the repo was added before the migration; fallback queries the
    # catalog for a conformant name. Phase 4 migration runs further
    # down (after credentials check + T3 connect) and may overwrite
    # these with conformant names before the indexer reads from T3.
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _repo_collection_or_legacy(repo, "docs")
    _migrated_names: dict[str, str] = {}

    # Load config (picks up per-repo .nexus.yml if present)
    cfg = load_config(repo_root=repo)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    ignore_patterns: list[str] = list(dict.fromkeys(DEFAULT_IGNORE + cfg_patterns))
    indexing_config: dict = cfg.get("indexing", {})
    rdr_paths: list[str] = indexing_config.get("rdr_paths", ["docs/rdr"])
    read_timeout_seconds: float = cfg.get("voyageai", {}).get("read_timeout_seconds", 120.0)

    # Load tuning config and use its chunk_lines if not overridden by caller
    from nexus.config import _tuning_from_dict
    tuning = _tuning_from_dict(cfg.get("tuning", {}))
    effective_chunk_lines: int | None = chunk_lines if chunk_lines is not None else tuning.code_chunk_lines

    # Collect git metadata once for all chunks
    git_meta = _git_metadata(repo)

    # Compute frecency scores in a single git log pass
    frecency_map = batch_frecency(repo, decay_rate=tuning.decay_rate, timeout=tuning.git_log_timeout)

    # Build absolute RDR path set for exclusion
    rdr_abs_paths: set[Path] = set()
    for rdr_rel in rdr_paths:
        rdr_abs = (repo / rdr_rel).resolve()
        rdr_abs_paths.add(rdr_abs)

    # Walk repo and classify files into code, prose, and PDF lists
    code_files: list[tuple[float, Path]] = []
    prose_files: list[tuple[float, Path]] = []
    pdf_files: list[tuple[float, Path]] = []
    all_text_scored: list[tuple[float, Path]] = []  # code + prose for ripgrep cache

    # Use git ls-files to respect .gitignore (security + efficiency)
    include_untracked = indexing_config.get("include_untracked", False)
    git_files = _git_ls_files(repo, include_untracked=include_untracked)

    if git_files:
        candidate_files = git_files
    else:
        # Fallback to rglob if git ls-files fails (not a git repo, etc.)
        _log.warning("falling back to rglob file walk", repo=str(repo))
        candidate_files = sorted(p for p in repo.rglob("*") if p.is_file() and not p.is_symlink())

    # GH #371 + #436: skip files larger than MAX_INDEXABLE_FILE_BYTES
    # before classification. Pre-fix, a single huge file (vendored
    # minified JS, generated bundle, large JSON config) would be
    # classified as CODE / PROSE on extension alone and then passed
    # to ``read_text(encoding="utf-8")`` in the per-file indexer,
    # which loads the entire content into memory before any check.
    # Observed symptoms:
    # - #371: parent process OOM-killed at 3GB+ RSS on a repo with a
    #   single 2GB+ vendored bundle.
    # - #436: progress bar stalls at file N where file N+1 is the
    #   huge file (read_text blocks while allocating the full buffer
    #   under ext4 + low memory pressure; CPU at 0%).
    # Cap at 5 MiB by default; configurable per repo via
    # ``[indexing] max_file_bytes`` so monorepos with legitimately
    # large indexable files can opt up. Emit a structured warning per
    # skipped file so the operator can see what got dropped.
    max_file_bytes = int(indexing_config.get("max_file_bytes", 5 * 1024 * 1024))
    skipped_oversize: list[tuple[Path, int]] = []

    for path in candidate_files:
        if not path.is_file():
            continue  # git ls-files may list deleted files not yet committed
        rel = path.relative_to(repo)
        # Defense-in-depth: still filter hidden dirs and ignore patterns
        if any(part.startswith(".") for part in rel.parts):
            continue  # Skip hidden dirs/files
        if _should_ignore(rel, ignore_patterns):
            continue  # Skip ignored patterns

        # Skip files under RDR paths — they go to rdr__ separately
        resolved = path.resolve()
        if any(resolved == rdr or _is_under(resolved, rdr) for rdr in rdr_abs_paths):
            continue

        # GH #371 + #436: oversize guard. Cheap stat() before any read.
        try:
            file_size = path.stat().st_size
        except OSError:
            continue  # broken symlink / permissions — defer to per-file path
        if file_size > max_file_bytes:
            skipped_oversize.append((path, file_size))
            continue

        score = frecency_map.get(path, 0.0)
        classification = classify_file(path, indexing_config=indexing_config)

        match classification:
            case ContentClass.CODE:
                code_files.append((score, path))
                all_text_scored.append((score, path))
            case ContentClass.PROSE:
                prose_files.append((score, path))
                all_text_scored.append((score, path))
            case ContentClass.PDF:
                pdf_files.append((score, path))
                # PDF files not included in ripgrep text cache
            case ContentClass.SKIP:
                pass  # known-noise file; silently ignore

    # Sort all lists descending by frecency
    code_files.sort(key=lambda x: x[0], reverse=True)
    prose_files.sort(key=lambda x: x[0], reverse=True)
    pdf_files.sort(key=lambda x: x[0], reverse=True)
    all_text_scored.sort(key=lambda x: x[0], reverse=True)

    # GH #371 + #436: surface the oversize-skip set so the operator
    # knows what got dropped. Single structured log + on_phase line
    # so it's visible in non-TTY runs (CI, hooks, nohup).
    if skipped_oversize:
        _log.warning(
            "indexer_oversize_skip",
            count=len(skipped_oversize),
            max_file_bytes=max_file_bytes,
            sample=[
                {"path": str(p.relative_to(repo)), "size": s}
                for p, s in skipped_oversize[:5]
            ],
        )
        if on_phase is not None:
            largest = max(skipped_oversize, key=lambda x: x[1])
            on_phase(
                f"Skipped {len(skipped_oversize)} oversized file(s) "
                f"(> {max_file_bytes // (1024 * 1024)} MiB). Largest: "
                f"{largest[0].relative_to(repo)} at {largest[1] // (1024 * 1024)} MiB. "
                f"Configure indexing.max_file_bytes to opt up."
            )

    # Fire on_start with total non-RDR file count.
    # Note: this fires before the credential check below.  Phase 2 (CLI) must
    # handle CredentialsMissingError by closing the tqdm bar before re-raising.
    if on_start:
        on_start(len(code_files) + len(prose_files) + len(pdf_files))

    # Update ripgrep cache (code + prose text files, not PDFs)
    from nexus.config import nexus_config_dir
    from nexus.registry import _repo_identity
    _repo_basename, _repo_hash = _repo_identity(repo)
    cache_path = nexus_config_dir() / f"{_repo_basename}-{_repo_hash}.cache"
    build_cache(repo, cache_path, all_text_scored)

    # Credential check and T3 setup
    from nexus.config import is_local_mode as _is_local
    from datetime import UTC, datetime as _dt
    from nexus.db import make_t3

    _local_mode = _is_local()
    _embed_fn = None

    if _local_mode:
        check_local_path_writable()
        from nexus.db.local_ef import LocalEmbeddingFunction
        _local_ef = LocalEmbeddingFunction()
        local_model = _local_ef.model_name

        # Two incompatible EmbedFn shapes live in the codebase:
        #   1. Code / prose / PDF path (code_indexer.py, prose_indexer.py,
        #      indexer.py:_index_pdf_file): ``embed_fn(texts) -> embeddings``.
        #   2. doc_indexer.py (RDR + markdown + large PDF incremental):
        #      ``EmbedFn = Callable[[list[str], str], tuple[list, str]]``
        #      returning ``(embeddings, actual_model)``.
        # LocalEmbeddingFunction implements shape #1 natively. For
        # shape #2 callers (the RDR discovery path below), wrap with an
        # adapter that returns the (embeddings, model) tuple. Without
        # this, local-mode RDR indexing fails with "takes 2 positional
        # arguments but 3 were given".
        _embed_fn = _local_ef  # shape #1 for code / prose / PDF

        def _local_embed_fn_tuple(
            texts: list[str], target_model: str = "",
        ) -> tuple[list[list[float]], str]:
            return _local_ef(texts), local_model

        _embed_fn_doc = _local_embed_fn_tuple  # shape #2 for doc_indexer

        code_model = local_model
        docs_model = local_model
        voyage_key = ""
        voyage_client = None
        # First-run tier notice
        if _local_ef.model_name == "all-MiniLM-L6-v2":
            _log.info(
                "local_mode_tier0",
                msg="Using basic embeddings (tier 0). For better code search quality: pip install conexus[local]",
            )
    else:
        _embed_fn_doc = None
        voyage_key = get_credential("voyage_api_key")
        chroma_key = get_credential("chroma_api_key")
        check_credentials(voyage_key, chroma_key)
        import voyageai
        code_model = index_model_for_collection(code_collection)
        docs_model = index_model_for_collection(docs_collection)
        voyage_client = voyageai.Client(api_key=voyage_key, timeout=read_timeout_seconds, max_retries=0)

    _log.debug("connecting to ChromaDB")
    db = make_t3()
    _log.debug("ChromaDB connected")
    now_iso = _dt.now(UTC).isoformat()

    # RDR-103 Phase 4: legacy-to-conformant migration on first index
    # after the catalog upgrade. Runs BEFORE the conformant collection
    # creation below so the rest of the pipeline only ever sees the
    # conformant name. Idempotent: subsequent runs are a no-op (legacy
    # absent from T3). Catalog-absent is a safe no-op (returns legacy).
    _cat: object | None = None
    try:
        from nexus.catalog.catalog import Catalog as _Catalog
        from nexus.config import catalog_path as _catalog_path

        _cat_path = _catalog_path()
        if _Catalog.is_initialized(_cat_path):
            _cat = _Catalog(_cat_path, _cat_path / ".catalog.db")

        def _emit_migration_msg(msg: str) -> None:
            _log.info("phase4_migration", message=msg)
            if on_phase is not None:
                on_phase(msg)

        _migrated_names = _migrate_legacy_collections(
            repo, cat=_cat, t3_db=db, registry=registry,
            on_message=_emit_migration_msg,
        )
        # Migration may have promoted legacy names to conformant. Pick
        # up the new names so the rest of the pipeline (collection
        # creation, indexing, catalog hook) sees the post-migration
        # state.
        if _migrated_names.get("code"):
            code_collection = _migrated_names["code"]
        if _migrated_names.get("docs"):
            docs_collection = _migrated_names["docs"]
    except Exception:
        # Unexpected failure outside _migrate_legacy_collections's own
        # exception handling (which catches per-content-type rename
        # errors and returns legacy names). This catch is the safety
        # net for catalog-open / import / unforeseen failures; warn
        # so the operator can act, but proceed with pre-migration
        # collection names so the indexing run still completes.
        _log.warning("phase4_migration_failed", exc_info=True)

    # nexus-27u7: defer T3 collection creation to the per-content-type
    # branches that actually have files. Pre-fix the indexer
    # unconditionally created both ``code__`` and ``docs__`` collections
    # at the start of every ``nx index repo`` run; a code-only repo
    # (no .md / .rst) accumulated an empty ``docs__`` zombie that
    # ``nx catalog collection-gc`` had to sweep later. Symmetric for
    # docs-only corpora.
    #
    # Downstream call sites already handle ``code_col is None`` /
    # ``docs_col is None`` (frecency reads at indexer.py:1676,
    # build_staleness_cache below, stamp_collection_version below);
    # the missing piece was the gate here.
    have_code_files = bool(code_files)
    have_docs_files = bool(prose_files or pdf_files)
    _log.debug(
        "creating collections",
        code=code_collection if have_code_files else "(deferred; no files)",
        docs=docs_collection if have_docs_files else "(deferred; no files)",
    )
    code_col = (
        db.get_or_create_collection(code_collection)
        if have_code_files else None
    )
    docs_col = (
        db.get_or_create_collection(docs_collection)
        if have_docs_files else None
    )
    _log.debug("collections ready")

    # Check pipeline version staleness (informational warning only)
    if code_col is not None:
        check_pipeline_staleness(code_col, code_collection)
    if docs_col is not None:
        check_pipeline_staleness(docs_col, docs_collection)

    # --force-stale: escalate to force if any collection is stale
    if force_stale:
        any_stale = (
            (code_col is not None and get_collection_pipeline_version(code_col) not in (None, PIPELINE_VERSION))
            or (docs_col is not None and get_collection_pipeline_version(docs_col) not in (None, PIPELINE_VERSION))
        )
        if any_stale:
            _log.info("force_stale_escalating", reason="stale collection detected")
            force = True
        else:
            _log.info("force_stale_skipped", reason="all collections current")

    # ── Pre-index catalog registration (RDR-101 Phase 3 PR δ Stage B) ───────
    # Register catalog entries BEFORE per-file indexing so the prose
    # (and forthcoming code / PDF / RDR) indexers can write the catalog
    # ``Document.doc_id`` into T3 chunk metadata at chunk-write time.
    # ``_catalog_hook`` is a graceful no-op when the catalog is absent
    # (returns an empty map), so the resolver becomes a noop closure
    # and indexers fall back to the legacy / no-catalog code path.
    #
    # The hook also runs link generation and orphan housekeeping. Both
    # are determined by the file LIST not by indexing OUTCOME, so
    # running them up front is safe; on a CTRL-C mid-index, the next
    # run re-derives them idempotently.
    indexed_for_catalog: list[tuple[Path, str, str]] = []
    for _, f in code_files:
        indexed_for_catalog.append((f, "code", code_collection))
    for _, f in prose_files:
        indexed_for_catalog.append((f, "prose", docs_collection))
    # RDR-101 Phase 3 PR δ Stage B.3: PDFs were missing from
    # ``indexed_for_catalog`` pre-Stage-B.3 (B.1 surfaced this gap during
    # verification). Register them upfront so the resolver returns a
    # tumbler for PDF chunks too.
    for _, f in pdf_files:
        indexed_for_catalog.append((f, "pdf", docs_collection))
    if rdr_abs_paths:
        # RDR-103 Phase 3a + Phase 4: prefer the migrated-name dict
        # populated above; fall through to the catalog-first resolver
        # when migration did not run (catalog absent or owner missing).
        rdr_col_name = (
            _migrated_names.get("rdr")
            or _repo_collection_or_legacy(repo, "rdr")
        )
        for rdr_dir in rdr_abs_paths:
            if rdr_dir.is_dir():
                for md_file in sorted(rdr_dir.rglob("*.md")):
                    if md_file.is_file():
                        indexed_for_catalog.append((md_file, "rdr", rdr_col_name))

    if on_phase is not None:
        on_phase(f"Registering {len(indexed_for_catalog)} catalog entries…")
    _catalog_t0 = time.monotonic()
    file_to_doc_id = _catalog_hook(
        repo=repo,
        repo_name=_repo_basename,
        repo_hash=_repo_hash,
        head_hash=_current_head(repo),
        indexed_files=indexed_for_catalog,
    )
    if on_phase is not None:
        on_phase(
            f"Catalog registration done ({time.monotonic() - _catalog_t0:.1f}s)"
        )

    def _doc_id_resolver(path: Path) -> str:
        return file_to_doc_id.get(path, "")

    # Pre-build the per-collection staleness cache (nexus-rr0u follow-up).
    # One paginated sweep per collection up front replaces N per-file
    # ``col.get(where={doc_id})`` round-trips inside the indexing loop.
    # The orchestrator builds the caches AFTER catalog registration so a
    # fresh ``doc_id`` for a just-registered file is already present in
    # the metadata sweep below — no race against the registration write.
    # On a healthy repo (most files current) this turns ``nx index repo``
    # from O(N) Chroma round-trips into O(total_chunks / 300) — minutes
    # become seconds.
    if on_phase is not None:
        on_phase("Building staleness caches…")
    _staleness_t0 = time.monotonic()
    # nexus-27u7: empty StalenessCache when the collection wasn't
    # created (no files of that content_type). Caller-side
    # ``check_staleness(cache=…)`` falls through to the per-file path,
    # which itself short-circuits on a missing collection.
    from nexus.indexer_utils import StalenessCache  # noqa: PLC0415
    code_staleness = (
        build_staleness_cache(code_col) if code_col is not None
        else StalenessCache()
    )
    docs_staleness = (
        build_staleness_cache(docs_col) if docs_col is not None
        else StalenessCache()
    )
    _log.info(
        "staleness_caches_built",
        code_doc_ids=len(code_staleness.by_doc_id),
        code_source_paths=len(code_staleness.by_source_path),
        docs_doc_ids=len(docs_staleness.by_doc_id),
        docs_source_paths=len(docs_staleness.by_source_path),
        elapsed_seconds=time.monotonic() - _staleness_t0,
    )
    if on_phase is not None:
        on_phase(
            f"Staleness caches built — "
            f"code: {len(code_staleness.by_doc_id):,} docs, "
            f"docs: {len(docs_staleness.by_doc_id):,} docs "
            f"({time.monotonic() - _staleness_t0:.1f}s)"
        )

    # Index code files → code__ (voyage-code-3, AST chunking)
    # NOTE: calls _index_code_file (the module-level wrapper) so that tests
    # patching nexus.indexer._index_code_file continue to intercept correctly.
    _log.debug("indexing code files", count=len(code_files))
    for score, file in code_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        # nexus-7niu: build a per-file StageTimers only when the caller
        # subscribed via ``on_stage_timers``. ``None`` short-circuits
        # every instrumented block inside the indexer to a no-op.
        timers = None
        if on_stage_timers is not None:
            from nexus.stage_timers import StageTimers
            timers = StageTimers()
        chunks = _index_code_file(
            file, repo, code_collection, code_model, code_col, db,
            voyage_client, git_meta, now_iso, score,
            chunk_lines=effective_chunk_lines,
            force=force,
            embed_fn=_embed_fn,
            stage_timers=timers,
            doc_id_resolver=_doc_id_resolver,
            staleness_cache=code_staleness,
            hooks=hooks,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)
        if on_stage_timers is not None and timers is not None:
            on_stage_timers(file, timers)

    # Index prose files → docs__ (voyage-context-3 via CCE)
    # NOTE: calls _index_prose_file (the module-level wrapper) — same reason.
    _log.debug("indexing prose files", count=len(prose_files))
    for score, file in prose_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        # nexus-7niu: per-file StageTimers when the caller subscribed.
        timers = None
        if on_stage_timers is not None:
            from nexus.stage_timers import StageTimers
            timers = StageTimers()
        chunks = _index_prose_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
            timeout=read_timeout_seconds,
            embed_fn=_embed_fn,
            stage_timers=timers,
            doc_id_resolver=_doc_id_resolver,
            staleness_cache=docs_staleness,
            hooks=hooks,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)
        if on_stage_timers is not None and timers is not None:
            on_stage_timers(file, timers)

    # Index PDF files → docs__ (PDF extraction + voyage-context-3)
    _log.debug("indexing PDF files", count=len(pdf_files))
    for score, file in pdf_files:
        _log.debug("indexing", file=str(file))
        t0 = time.monotonic()
        # nexus-7niu: per-file StageTimers when the caller subscribed.
        timers = None
        if on_stage_timers is not None:
            from nexus.stage_timers import StageTimers
            timers = StageTimers()
        chunks = _index_pdf_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
            timeout=read_timeout_seconds,
            chunk_chars=tuning.pdf_chunk_chars,
            embed_fn=_embed_fn,
            stage_timers=timers,
            doc_id_resolver=_doc_id_resolver,
            staleness_cache=docs_staleness,
            hooks=hooks,
        )
        if on_file:
            on_file(file, chunks, time.monotonic() - t0)
        if on_stage_timers is not None and timers is not None:
            on_stage_timers(file, timers)

    # Post-processing phase markers (nexus-vatx Gap 2): the per-file
    # progress bar ends at "[N/N]" but the pipeline keeps running for
    # pruning, stamping, and catalog registration. Without markers the
    # operator sees silence and cannot tell hung from busy.
    #
    # Review remediation (Reviewer A/I-1): wrap the block in try/finally
    # so the closing "Post-processing complete" marker fires even when
    # a post-processing step raises. Without this, an exception inside
    # _discover_and_index_rdrs, _prune_misclassified, or _catalog_hook
    # would leave the operator staring at the last `[post] Pruning …`
    # line — exactly the hung/busy ambiguity Gap 2 was meant to fix.
    post_t0 = time.monotonic()

    def _phase(msg: str) -> None:
        if on_phase is not None:
            on_phase(msg)

    rdr_indexed, rdr_current, rdr_failed = 0, 0, 0
    post_error: BaseException | None = None
    try:
        # Discover and index RDR markdown files → rdr__
        # Pass the local embed_fn so RDR indexing respects NX_LOCAL mode
        # (without it, the RDR branch defaulted to Voyage 1024-dim; query
        # time with local MiniLM 384-dim hit "Collection expecting embedding
        # with dimension of 1024, got 384").
        # RDR indexing lives in doc_indexer which expects shape #2
        # ``(texts, model) -> (embeddings, actual_model)``. In local mode
        # hand over the tuple-returning adapter; in cloud mode pass None so
        # doc_indexer falls back to _embed_with_fallback on its own.
        _phase("Discovering and indexing RDR markdown files…")
        _t = time.monotonic()
        rdr_indexed, rdr_current, rdr_failed = _discover_and_index_rdrs(
            repo, rdr_abs_paths, db, voyage_key, now_iso, force=force,
            embed_fn=_embed_fn_doc, hooks=hooks,
        )
        _phase(
            f"RDR indexing done — {rdr_indexed} indexed, {rdr_current} current, "
            f"{rdr_failed} failed ({time.monotonic() - _t:.1f}s)"
        )

        # Prune misclassified chunks (reclassification cleanup)
        _phase("Pruning misclassified chunks…")
        _t = time.monotonic()
        _prune_misclassified(
            repo, code_collection, docs_collection,
            [f for _, f in code_files],
            [f for _, f in prose_files],
            [f for _, f in pdf_files],
            db,
            file_to_doc_id=file_to_doc_id,
            catalog=_cat,
        )
        _phase(f"Pruning misclassified done ({time.monotonic() - _t:.1f}s)")

        # C3: Prune deleted files. Remove orphan chunks via the catalog
        # manifest (RDR-108 Phase 4 / nexus-dyxe).
        _phase("Pruning deleted files…")
        _t = time.monotonic()
        _prune_deleted_files(
            code_collection, docs_collection, db, catalog=_cat,
        )
        _phase(f"Pruning deleted files done ({time.monotonic() - _t:.1f}s)")

        # Stamp pipeline version after all work completes (nexus-7yfm).
        # Stamps on every successful run, not just --force: the stamp asserts
        # "these embeddings were produced by PIPELINE_VERSION code", and that
        # is true regardless of whether --force was used. Gating the stamp on
        # --force forced operators to re-pay for full re-embedding to repair
        # a state that should never have existed.
        _phase("Stamping pipeline version…")
        _t = time.monotonic()
        # nexus-27u7: stamp only when the collection was created.
        if code_col is not None:
            stamp_collection_version(code_col)
        if docs_col is not None:
            stamp_collection_version(docs_col)
        if rdr_indexed > 0:
            # RDR-103 Phase 3a: catalog-first resolution; legacy
            # fallback retained for catalog-absent test paths.
            rdr_col_name = _repo_collection_or_legacy(repo, "rdr")
            try:
                rdr_col = db.get_or_create_collection(rdr_col_name)
                stamp_collection_version(rdr_col)
            except Exception:
                _log.debug("rdr_stamp_skipped", collection=rdr_col_name)
        _phase(f"Pipeline version stamped ({time.monotonic() - _t:.1f}s)")

        # Catalog registration ran upfront (RDR-101 Phase 3 PR δ Stage B)
        # so prose chunks could carry ``doc_id`` at chunk-write time.
        # The "Registering …" / "Catalog registration done …" phase
        # markers fire from the pre-index path (above the per-file loops),
        # not from this block.
    except BaseException as exc:
        post_error = exc
        raise
    finally:
        suffix = f" (interrupted: {type(post_error).__name__})" if post_error else ""
        _phase(f"Post-processing complete ({time.monotonic() - post_t0:.1f}s){suffix}")

    return {"rdr_indexed": rdr_indexed, "rdr_current": rdr_current, "rdr_failed": rdr_failed}


def _is_under(child: Path, parent: Path) -> bool:
    """Return True if *child* is a descendant of *parent*."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
