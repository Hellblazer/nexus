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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus._locking import lock_file, unlock_file
from nexus.corpus import index_model_for_collection
from nexus.db.limits import QUOTAS
from nexus.retry import _vector_with_retry, _voyage_with_retry  # noqa: F401 — re-exported for any existing imports
from nexus.errors import CredentialsMissingError  # re-exported for backward compatibility
from nexus.hook_registry import record_catalog_hook_failure
from nexus.indexer_utils import (
    build_doc_id_resolver,
    build_staleness_cache,
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
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.catalog.tumbler import Tumbler
    from nexus.hook_registry import HookRegistry
    from nexus.indexer_utils import StalenessCache
    from nexus.stage_timers import StageTimers

# Re-export from indexer_utils for backward compatibility (tests import from here).
DEFAULT_IGNORE: list[str] = _DEFAULT_IGNORE


# Pipeline version: bump when indexing changes invalidate existing embeddings.
# History:
#   v1-v3: pre-versioning (no version stamp in collection metadata)
#   v4:    RDR-028 language registry + RDR-014 CCE prefixes
PIPELINE_VERSION: str = "4"

# Concurrent ChunkBatcher flush workers during a repo index run. 3 is the
# empirical choice from the 3midv sweep (sequential flushes cost 76-112s of
# wall vs. the concurrent path); ``min(...)`` with QUOTAS.MAX_CONCURRENT_WRITES
# (the per-collection service quota, ``nexus.db.limits``) ties the literal to
# the ceiling it was chosen to stay under, so a future quota tightening below
# 3 is caught by the pinning test in test_indexer_flush_concurrency.py rather
# than silently indexing outside the quota (nexus-dimrz).
FLUSH_CONCURRENCY: int = min(3, QUOTAS.MAX_CONCURRENT_WRITES)


def stamp_collection_version(col: object) -> None:
    """Write PIPELINE_VERSION to collection metadata, preserving existing keys.

    nexus-kwkkz: collection-level metadata via ``modify`` is Chroma-specific;
    service-backed collections (``_ServiceCollectionStub``) do not expose it, so
    pipeline-version stamping is a no-op there. This is a staleness optimization,
    not a correctness input — the read side (``get_collection_pipeline_version``)
    already degrades to ``None`` (treated as a fresh collection) for these.
    """
    modify = getattr(col, "modify", None)
    if not callable(modify):
        return
    existing = getattr(col, "metadata", None) or {}
    modify(metadata={**existing, "pipeline_version": PIPELINE_VERSION})


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


def _get_owner_head_hash(repo: Path) -> str | None:
    """Read the owner's last-indexed ``head_hash`` (nexus-fltb4).

    The read counterpart of :func:`_set_owner_head_hash`: resolves the owner
    via ``owner_for_repo`` then reads the owner record. ``None`` on any
    degradation (catalog absent, owner unregistered, column empty) — the
    caller falls back to a full index.
    """
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        reader = make_catalog_reader()
        if reader is None:
            return None
        _, repo_hash = _repo_identity(repo)
        owner = reader.owner_for_repo(repo_hash)
        if owner is None:
            return None
        rec = reader.get_owner_by_prefix(str(owner))
        if not rec:
            return None
        head = str(rec.get("head_hash") or "").strip()
        return head or None
    except Exception:  # noqa: BLE001 — best-effort read; any failure means full-index fallback
        _log.debug("get_owner_head_hash_failed", repo=str(repo), exc_info=True)
        return None


def _git_changed_since(
    repo: Path, base: str,
) -> tuple[list[str], list[str]] | None:
    """Worktree-inclusive git delta since *base* (nexus-fltb4).

    Returns ``(changed_relpaths, deleted_relpaths)`` from
    ``git diff --name-status --find-renames <base>`` — worktree-inclusive
    (no ``HEAD`` operand), so uncommitted edits are included exactly like
    the full walk's mtime staleness would catch them. Renames contribute
    the old path to *deleted* and the new to *changed*; copies contribute
    the new path only.

    ``None`` (full-index fallback) when the base commit is unreachable
    (history rewrite, shallow clone, gc), git fails, or parsing sees an
    unexpected status — never guess on a partial delta.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-status",
             "--find-renames", "--no-color", base],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — git unavailable/hung: full-index fallback
        _log.debug("git_changed_since_failed", repo=str(repo), exc_info=True)
        return None
    if proc.returncode != 0:
        _log.info(
            "since_head_base_unusable",
            base=base, stderr=proc.stderr.strip()[:200],
            fallback="full index",
        )
        return None
    changed: list[str] = []
    deleted: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")) and len(parts) == 3:
            if status.startswith("R"):
                deleted.append(parts[1])
            changed.append(parts[2])
        elif status[:1] in ("A", "M", "T") and len(parts) == 2:
            changed.append(parts[1])
        elif status[:1] == "D" and len(parts) == 2:
            deleted.append(parts[1])
        elif status[:1] == "U":
            # merge conflict in progress — indexing mid-merge is undefined
            return None
        else:
            _log.info("since_head_unparsed_status", line=line[:120])
            return None
    return changed, deleted


def _set_owner_head_hash(repo: Path, head_hash: str, *, cat=None) -> None:
    """RDR-137 Phase 3.8 (nexus-tts0d.13): persist *head_hash* on the
    owner row for *repo*.

    Writes ``owners.head_hash`` (Phase 1.5b column from
    ``nexus-tts0d.2``). Silently degrades when the catalog is not
    initialised or the owner is not registered yet — both are
    legitimate states during a first-time index, and ``index_repository``
    will register the owner in its catalog hook.

    RDR-137 followup IMP-26 (nexus-43qgm.26): accepts an optional
    ``cat`` parameter so callers that already have a Catalog open
    can avoid the per-call connection overhead (Catalog.__init__ runs
    migration probes + the RDR-108 D2 backfill scan). The default-
    None path preserves standalone-helper semantics.
    """
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        # RDR-146 P1.2 strict split: read the owner via the reader (an
        # explicit ``cat`` is reused as the reader), write via the
        # write-only daemon proxy. None reader == uninitialised catalog,
        # the prior silent-degrade path.
        reader = cat if cat is not None else make_catalog_reader()
        if reader is None:
            return
        writer = make_catalog_writer()
        _, repo_hash = _repo_identity(repo)
        owner = reader.owner_for_repo(repo_hash)
        if owner is None:
            return
        rowcount = writer.set_owner_head_hash(owner, head_hash)
        # RDR-137 followup SIG-9 (nexus-43qgm.9): owner_for_repo returned
        # a non-None tumbler but the UPDATE matched zero rows — the only
        # plausible cause is a concurrent owner deletion between the
        # lookup and the write. Surface as a warning so the lost write
        # is observable.
        if rowcount == 0:
            _log.warning(
                "set_owner_head_hash_no_match",
                repo=str(repo),
                owner=str(owner),
                repo_hash=repo_hash,
                hint="owner row deleted between lookup and update — re-index will heal",
            )
    except Exception as exc:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
        _log.warning(
            "set_owner_head_hash_failed",
            repo=str(repo), error=str(exc),
        )
    finally:
        if writer is not None:
            writer.close()


def _repo_lock_path(repo: Path) -> Path:
    """Return the per-repo lock file path: ~/.config/nexus/locks/<hash8>.lock.

    Uses the same worktree-stable identity as the registry so two worktrees
    of the same repo map to a single lock.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    _, path_hash = _repo_identity(repo)
    return nexus_config_dir() / "locks" / f"{path_hash}.lock"


_LOCK_STALE_SECONDS = 5  # lock files older than this with no live PID are stale
#: Page size for the batched catalog register_many pass (nexus-9dvqy). Matches
#: the server's MAX_BATCH_DOC_IDS cap. Also the granularity of the RDR-146
#: fairness yield in _catalog_hook. Module-level so tests can shrink it to force
#: multi-page behaviour without a 1000+ file corpus.
_CATALOG_REGISTER_PAGE = 1000

#: nexus-mr89x: minimum orphan count before the GC safety floor can refuse a
#: sweep. Below this, even a 100%-orphan verdict is small enough to be a
#: plausible real cleanup (tiny collections, test corpora) and refusing
#: would just nag; at/above it, a >floor-fraction verdict is the
#: manifest-gap misclassification shape. Pairs with NX_GC_FLOOR_FRACTION
#: (default 0.25) and the NX_GC_FORCE=1 operator override at the check site.
_GC_FLOOR_MIN_CHUNKS = 100

#: Default orphan-fraction floor. 0.25, NOT 0.5 (review e2423e3b Critical-2):
#: the motivating 2026-07-09 field incident condemned 154/551 chunks = 27.9%
#: — under a 0.5 floor the exact incident this guard is named for would have
#: sailed through. The cost asymmetry decides the default: refusing a
#: legitimate large cleanup defers it behind a loud message + NX_GC_FORCE=1
#: (recoverable), while allowing a misclassified sweep deletes live data
#: (not). The c21fk self-heal runs before this GC, so legitimate post-heal
#: orphan fractions are same-run supersede churn — rarely above a quarter of
#: a collection.
_GC_FLOOR_FRACTION_DEFAULT = 0.25


def _gc_floor_fraction() -> float:
    """Parse NX_GC_FLOOR_FRACTION fail-safe (review e2423e3b Critical-1/F6).

    A malformed value must never crash the index run, and nan/inf must not
    silently neutralize the guard — both fall back to the default with a
    loud warning. Values are clamped to [0.0, 1.0].
    """
    raw = os.environ.get("NX_GC_FLOOR_FRACTION", "")
    if not raw:
        return _GC_FLOOR_FRACTION_DEFAULT
    try:
        val = float(raw)
    except ValueError:
        val = float("nan")
    if not (0.0 <= val <= 1.0):  # False for nan; excludes inf
        _log.warning(
            "gc_floor_fraction_invalid",
            raw=raw, using=_GC_FLOOR_FRACTION_DEFAULT,
            note="NX_GC_FLOOR_FRACTION must be a float in [0, 1]",
        )
        return _GC_FLOOR_FRACTION_DEFAULT
    return val


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
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415  — circular-dep avoidance (nexus.catalog.factory)

    try:
        # Service-only since nexus-i711w: the catalog is the remote Postgres
        # service in every mode — no local is_initialized gate remains.
        cat = make_catalog_reader()
        if cat is not None:
            try:
                return cat.collection_for_repo(repo, content_type).render()
            except LookupError:
                # Owner not yet registered; fall through to the
                # path-derived synthesis below. This happens for
                # callers that bypass the ``_catalog_hook`` upfront
                # flow (e.g. ad-hoc CLI invocations on a fresh repo).
                pass
    except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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
    from nexus.corpus import resolve_write_embedding_model  # noqa: PLC0415  — circular-dep avoidance (nexus.corpus)
    from nexus.repo_identity import _repo_identity, _safe_collection  # noqa: PLC0415  — circular-dep avoidance (nexus.repo_identity)

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

    _db_cache: list = []  # nexus-o5x2c: memoize make_t3() across candidates below

    def _local_token_collection_exists(token: str) -> bool:
        # nexus-o5x2c: this NO-CATALOG / unregistered-owner fallback has
        # no live T3 handle in scope (unlike the catalog-aware primary
        # path) — best-effort construct one purely to probe for a
        # pre-existing collection to grandfather onto. Wrapped by
        # resolve_write_embedding_model's own try/except, so a failure
        # here (no service configured, network) degrades to the
        # pre-fix behavior (no grandfather found), never a crash of its
        # own. Constructed ONCE and reused across every candidate in
        # LOCAL_EMBEDDING_MODELS (reviewer follow-up) rather than once
        # per candidate — the probe never spans more than one T3 client.
        if not _db_cache:
            from nexus.db import make_t3  # noqa: PLC0415 — deferred, probe-local
            _db_cache.append(make_t3())
        candidate = _safe_collection(
            prefix=f"{content_type}__", name=sanitised, path_hash=repo_hash,
            suffix=f"__{token}__v1",
        )
        return _db_cache[0].collection_exists(candidate)

    model = resolve_write_embedding_model(
        content_type, collection_exists=_local_token_collection_exists,
    )
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
    from nexus.repo_identity import _repo_identity, _safe_collection  # noqa: PLC0415  — circular-dep avoidance (nexus.repo_identity)

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
    from nexus.corpus import resolve_read_embedding_model  # noqa: PLC0415  — circular-dep avoidance (nexus.corpus)
    from nexus.repo_identity import _repo_identity, _safe_collection  # noqa: PLC0415  — circular-dep avoidance (nexus.repo_identity)

    if content_type not in ("code", "docs", "rdr"):
        raise ValueError(
            f"_migration_source_candidates: unknown content_type {content_type!r}"
        )
    basename, repo_hash = _repo_identity(repo)
    sanitised = basename.replace("_", "-")
    # nexus-o5x2c: this builds a CANDIDATE name to CHECK for pre-migration
    # data — a read/probe shape, not a write mint — so it uses the
    # credential-free read resolver, never
    # effective_embedding_model_for_writes directly (which used to crash
    # this whole migration-detection path on a keyless voyage-configured
    # local install; caught only by an incidental outer except in the
    # caller, per nexus-o5x2c's item #4).
    model = resolve_read_embedding_model(content_type)
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
    writer: object = None,
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
    # nexus-8g79.10 (V5): import from peer module instead of reaching
    # up into commands/. The CLI wrapper in commands/collection.py
    # adds the ``t3_db=_t3()`` default; we pass ``t3_db`` explicitly.
    from nexus.catalog.collection_name import owner_segment_for_tumbler  # noqa: PLC0415  — circular-dep avoidance (nexus.catalog.collection_name)
    from nexus.collection_rename import (  # noqa: PLC0415  — circular-dep avoidance (nexus.collection_rename)
        rename_collection_data_plane,
    )
    from nexus.corpus import resolve_write_embedding_model  # noqa: PLC0415  — circular-dep avoidance (nexus.corpus)
    from nexus.db.collection_state import CollectionState, probe_collection_state  # noqa: PLC0415  — circular-dep avoidance (nexus.db.collection_state)
    from nexus.repo_identity import _repo_identity  # noqa: PLC0415  — circular-dep avoidance (nexus.repo_identity)

    result: dict[str, str] = {}

    # Catalog-absent: nothing to migrate. Return an empty map so the
    # caller falls back to its own resolution (registry value or the
    # path-derived legacy helper).
    if cat is None:
        return result

    cat_obj = cat
    # RDR-146 P1.2 strict split: reads via cat_obj, writes via w
    # (write-only proxy; defaults to cat_obj for single-object callers).
    w = writer if writer is not None else cat_obj

    # Owner-unregistered: nothing to migrate yet. The _catalog_hook
    # registers the owner later in this run; the migration happens on
    # the NEXT run. Return empty so the caller's existing fallback
    # handles this run.
    _, repo_hash = _repo_identity(repo)
    owner = cat_obj.owner_for_repo(repo_hash)
    if owner is None:
        return result

    for ct in _MIGRATION_CONTENT_TYPES:
        owner_id = owner_segment_for_tumbler(owner)
        # nexus-o5x2c: THE chokepoint — grandfathers onto a pre-existing
        # bge/minilm collection in T3 (this function's own substrate,
        # same as the probe_collection_state calls just below) instead
        # of crashing when local.embed_model is voyage-shaped and no key
        # is configured. Previously reached only because an outer
        # ``except Exception`` in the caller (nx index repo) happened to
        # swallow the raise, not by design.
        conformant = cat_obj.collection_for(
            content_type=ct,
            owner=owner,
            embedding_model=resolve_write_embedding_model(
                ct,
                collection_exists=(
                    lambda token, _owner_id=owner_id, _ct=ct: t3_db.collection_exists(
                        f"{_ct}__{_owner_id}__{token}__v1"
                    )
                ),
            ),
        ).render()

        # nexus-7vuw: pick the first source candidate that exists in T3.
        # Two shapes can carry pre-migration data:
        #   1. legacy 2-segment ``<ct>__<basename>-<hash8>`` (pre-RDR-103)
        #   2. Phase-5 path-derived 4-segment synth.
        # If both exist, prefer the legacy 2-segment (rename happens
        # first; the synth becomes the case-3 partial-state collision
        # message and is left for operator cleanup).
        # nexus-9n485: collection_exists() alone can't tell "no chunks ever
        # existed here" from "every chunk here belongs to a trashed
        # document" (HttpVectorClient reads the tombstone-filtered stats
        # view). Both currently fall through this loop identically —
        # a tombstoned candidate is not treated as migratable, same as
        # before this fix — but a tombstoned candidate is no longer
        # silent: it used to vanish from the logs entirely, orphaning the
        # collection with no trace ("trash-then-reindex would create a
        # duplicate fresh collection beside the tombstoned one").
        legacy = None
        for candidate in _migration_source_candidates(repo, ct):
            if candidate == conformant:
                # The candidate IS the target; no migration needed
                # because the indexer is already writing to the right
                # place.
                continue
            candidate_state = probe_collection_state(t3_db, candidate)
            if candidate_state is CollectionState.TOMBSTONED:
                _log.warning(
                    "phase4_migration_candidate_tombstoned",
                    repo=str(repo), ct=ct, candidate=candidate,
                    # nexus-z0idx follow-on: NOT "message" (stdlib logging
                    # reserves that name on LogRecord) — "reason" here to
                    # match this file's OWN established kwarg for this
                    # semantic (11 pre-existing uses), not http_vector_
                    # client.py's "detail" convention.
                    reason=(
                        "candidate has physical chunk rows but every one "
                        "belongs to a trashed document; not treated as "
                        "migratable (skipping to the next candidate / "
                        "greenfield). Restore the trashed document(s) and "
                        "rename manually if this data should not be orphaned."
                    ),
                )
                continue
            if candidate_state is CollectionState.PRESENT:
                legacy = candidate
                break
        if legacy is None:
            legacy = _legacy_collection_name(repo, ct)
            legacy_exists = False
        else:
            legacy_exists = True
        conformant_state = probe_collection_state(t3_db, conformant)
        if conformant_state is CollectionState.TOMBSTONED:
            _log.warning(
                "phase4_migration_conformant_tombstoned",
                repo=str(repo), ct=ct, conformant=conformant,
                reason=(
                    "target collection has physical chunk rows but every "
                    "one belongs to a trashed document; treated as NOT "
                    "present so a fresh write is not silently merged with "
                    "trashed data under the same name."
                ),
            )
        conformant_exists = conformant_state is CollectionState.PRESENT

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
                    legacy, conformant, t3_db=t3_db, catalog=w,
                    on_warn=lambda msg: _log.warning(
                        "phase4_migration_cascade_warn", reason=msg,
                    ),
                )
                data_plane_succeeded = True
            except Exception as exc:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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
                from nexus.corpus import (  # noqa: PLC0415  — circular-dep avoidance (nexus.corpus)
                    is_conformant_collection_name,
                    parse_conformant_collection_name,
                )
                if is_conformant_collection_name(conformant):
                    segments = parse_conformant_collection_name(conformant)
                    w.register_collection(
                        conformant,
                        content_type=segments["content_type"],
                        owner_id=segments["owner_id"],
                        embedding_model=segments["embedding_model"],
                        model_version=segments["model_version"],
                    )
                else:
                    w.register_collection(conformant)
            except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
                _log.warning(
                    "phase4_register_collection_failed_after_rename",
                    old=legacy, new=conformant, exc_info=True,
                )
            try:
                w.supersede_collection(
                    legacy, conformant, reason="rdr-103-phase4-migration",
                )
            except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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
            except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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


def _delete_docs_for_paths(repo: Path, deleted_relpaths: list[str]) -> None:
    """Delete the catalog documents for explicitly-deleted paths (nexus-fltb4).

    The delta path's replacement for housekeeping's miss-count sweep: git
    already told us these worktree-relative paths are gone (rename detection
    applied), so their docs are deleted immediately — the manifest FK CASCADE
    then exposes their chunks to ``_prune_deleted_files``'s orphan sweep.
    Best-effort per path; a failed lookup/delete is logged and skipped (the
    next FULL run's housekeeping recovers it).
    """
    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        reader = make_catalog_reader()
        if reader is None:
            return
        writer = make_catalog_writer()
        _, repo_hash = _repo_identity(repo)
        owner = reader.owner_for_repo(repo_hash)
        if owner is None:
            return
        for rel in deleted_relpaths:
            try:
                entry = reader.by_file_path(owner, rel)
                if entry is None:
                    continue
                writer.delete_document(entry.tumbler)
                _log.info(
                    "since_head_deleted_doc",
                    file_path=rel, tumbler=str(entry.tumbler),
                )
            except Exception:  # noqa: BLE001 — per-path best-effort; full-run housekeeping recovers
                _log.warning(
                    "since_head_delete_doc_failed", file_path=rel, exc_info=True,
                )
    except Exception:  # noqa: BLE001 — catalog unavailable: nothing to delete from
        _log.debug("since_head_delete_docs_unavailable", exc_info=True)


def _catalog_progress(
    msg: str,
    *,
    is_tty: bool,
    on_phase: Callable[[str], None] | None,
    write: Callable[[str], None],
) -> None:
    """TTY-gated progress reporter for ``_catalog_hook``'s ``"  Catalog:
    ..."`` lines (registering / linking / housekeeping / done).

    nexus-c14zi: this used to be a raw ``sys.stderr.write`` bound directly,
    un-gated on TTY. Its ``\\r`` in-place-repaint writes are harmless on an
    interactive terminal but a literal control character in a redirected
    (non-TTY) log — two of these writes used to land on the SAME physical
    line with no separator (observed: "Catalog: linking 251 new
    entries...  Catalog: housekeeping..." mashed together, since neither
    write is ever followed by a real newline until the very next call).

    On a TTY, *write* is called with *msg* completely unchanged — same
    behavior as before this fix, repaint included (``_maybe_run_
    housekeeping`` is also passed this function as its ``progress``
    callback, so its own ``\\r`` writes get identical treatment for free).

    On a non-TTY stderr, route through the phase channel instead: strip
    the trailing ``\\r``/``\\n`` (``on_phase``'s own ``click.echo`` always
    terminates the line) and emit exactly one clean, discrete line —
    never a bare ``\\r``. Falls back to a plain ``write``-terminated line
    when no ``on_phase`` was supplied (some callers invoke
    ``_catalog_hook`` without one, e.g. tests).
    """
    if is_tty:
        write(msg)
        return
    stripped = msg.rstrip("\r\n")
    if not stripped:
        return
    if on_phase is not None:
        on_phase(stripped)
    else:
        write(stripped + "\n")


def _catalog_hook(
    repo: Path,
    repo_name: str,
    repo_hash: str,
    head_hash: str,
    indexed_files: list[tuple[Path, str, str]],
    on_locked: str = "wait",
    skip_housekeeping: bool = False,
    stale_fence_doc_ids: set[str] | None = None,
    on_phase: Callable[[str], None] | None = None,
    complete_doc_hashes: dict[str, str] | None = None,
) -> dict[Path, str]:
    """Register/update indexed files in catalog. Silently skipped if catalog absent.

    ``complete_doc_hashes`` (nexus-vayt7) is the sibling OUT-param of
    ``stale_fence_doc_ids``: every EXISTING document whose reported
    ``index_state`` is ``'complete'`` has ``tumbler -> index_content_hash``
    added in place, from the same owner-scoped fetch. The orchestrator
    overlays it onto the staleness caches, which otherwise key a doc's
    content hash off content-addressed chunk metadata that is never
    refreshed (see ``indexer_utils.apply_catalog_content_hashes``).

    ``on_phase`` (nexus-jg3x5): the orchestrator's ``[post]`` phase channel.
    When supplied, each link generator that can do work for this batch
    emits ``Catalog linking: <kind>…`` on entry and ``Catalog linking:
    <kind> done (<s>s)`` on exit, carrying the SAME measured duration the
    ``catalog_hook_stage_timing`` event records. ``None`` (every other
    caller) emits nothing.

    Returns a ``{abs_path: doc_id}`` map (empty when the catalog is absent
    or cannot be opened) so the orchestrator can build a
    ``doc_id_resolver`` closure and inject it into ``IndexContext``
    before per-file indexing runs (RDR-101 Phase 3 PR δ Stage B). The
    return type is additive — existing test call sites that ignore the
    return value continue to work unchanged.

    ``stale_fence_doc_ids`` (nexus-cp46b) is an optional OUT-param: when
    supplied, every EXISTING document (Pass 1's ``existing is not None``
    branch below) whose reported ``index_state`` is ``'indexing'`` or
    ``'failed'`` has its tumbler added to the set in place. This reuses
    the SAME owner-scoped catalog fetch (``path_to_entry`` /
    ``cat.by_file_path``) this pass already pays for — no extra per-file
    or per-run HTTP round trip. The orchestrator threads the populated
    set into the per-collection ``StalenessCache.never_fresh`` so a doc
    stranded mid-run (content unchanged, fence stuck at 'indexing'/
    'failed') is forced stale on the next normal ``nx index repo`` pass
    instead of being permanently skipped by the content-hash-only
    staleness check (the bug this bead fixes). A document whose
    ``index_state`` was never reported at all (pre-fence engine —
    ``index_state_reported=False``) is excluded, matching the
    floor-tolerant stance ``doc_indexer._index_run_fresh`` /
    ``indexer_utils.non_complete_documents`` already use elsewhere.

    RDR-146 P2 (nexus-5p2ci.12): this is the background batch producer that
    GH #1046 showed starving a foreground ``nx dt index``. The writer is
    constructed with the resolved write priority (the hook-spawned indexer
    is non-tty -> batch); a batch writer polls the daemon's interactive
    window before each file's catalog write and yields. ``on_locked``
    retargets (PC-5 collapse) from the per-repo advisory file lock to the
    catalog-contention signal: ``"skip"`` defers the remaining catalog
    writes to the next idempotent index pass once the bounded yield budget
    is exhausted; ``"wait"`` proceeds after the budget (never permanently
    starve the batch). The per-repo advisory lock keeps its orthogonal job
    (two ``nx index repo`` on the same repo) up in ``index_repository``.
    """
    from nexus.catalog.write_priority import await_fair_window  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    file_to_doc_id: dict[Path, str] = {}
    reader = None
    writer = None
    try:
        # Service-only since nexus-i711w: the catalog is the remote Postgres
        # service in every mode (the local is_initialized gate died with the
        # local catalog; the service-backed writer below fails loud if the
        # service is unreachable — RDR-168 P4 / nexus-pwclh lineage).

        # RDR-146 P1.2 strict split: reads via ``cat`` (read-only reader),
        # writes via ``writer`` (write-only daemon proxy).
        from nexus.catalog.factory import (  # noqa: PLC0415  — circular-dep avoidance (nexus.catalog.factory)
            make_catalog_reader,
            make_catalog_writer,
        )
        reader = make_catalog_reader()
        # RDR-146 P2: resolve priority (non-tty hook spawn -> batch). Only a
        # batch writer yields to interactive writes; an interactive/foreground
        # ``nx index repo`` is itself latency-sensitive and does not throttle.
        writer = make_catalog_writer()
        cat = reader
        _batch_producer = writer.priority == "batch"

        # ``_catalog_hook`` accepts an explicit ``repo_hash`` (and
        # ``repo_name``) so callers and tests can override the
        # path-derived identity. ``ensure_owner_for_repo`` would
        # recompute the hash from ``repo`` and ignore the explicit
        # arg, so the inline lookup-or-register is preserved. The
        # idempotency guarantee matches ``ensure_owner_for_repo``:
        # existing owners short-circuit without a re-register.
        # nexus-u8n4r: hoisted out of the ``owner is None`` branch (it used
        # to be computed only on first registration) so ``main_repo`` is
        # ALWAYS available for the per-file ephemeral-path guard below, not
        # just when this call happens to be the owner's first registration.
        # ``_repo_identity_with_main`` uses ``git rev-parse --git-common-dir``
        # to resolve; LRU-memoized (nexus.repo_identity), so this costs at
        # most one subprocess per unique repo path per process.
        from nexus.repo_identity import (  # noqa: PLC0415  — circular-dep avoidance (nexus.repo_identity)
            _repo_identity_with_main,
            should_skip_ephemeral_registration,
        )
        _name, _hash, main_repo = _repo_identity_with_main(repo)
        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            # nexus-zr2ie (RDR-137 gate critique 2026-05-28): derive the
            # canonical main-repo path so ``repo_root`` is worktree-
            # stable. Pre-fix this wrote ``str(repo)`` and contaminated
            # the catalog when the caller's ``repo`` argument was a
            # worktree path; ``resolve_path`` then produced broken
            # paths for every relative-path document under this owner
            # once the worktree was deleted.
            owner = writer.register_owner(
                name=repo_name,
                owner_type="repo",
                repo_hash=repo_hash,
                repo_root=str(main_repo),
                description=f"Git repository: {repo_name}",
            )
            _log.info("catalog_owner_created", owner=str(owner), repo=repo_name)

        import functools  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        import sys  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        # nexus-c14zi: TTY-gate the raw ``\r`` writes below (and the ones
        # ``_maybe_run_housekeeping`` makes through the same ``progress``
        # callback) — see ``_catalog_progress``'s own docstring for why.
        _progress = functools.partial(
            _catalog_progress,
            is_tty=sys.stderr.isatty(), on_phase=on_phase, write=sys.stderr.write,
        )
        _progress(f"  Catalog: registering {len(indexed_files)} files…\r")

        # nexus-dst5h: ONE owner-scoped fetch + local join instead of a
        # per-file ``by_file_path`` round-trip. In service mode each
        # per-file lookup was a WAN HTTPS call (and the server returns the
        # full owner list per call — GH #1350 class), so a warm run paid
        # ~len(indexed_files) serial round-trips before indexing anything.
        # The snapshot is taken once: a file registered by a CONCURRENT
        # writer after this point is misclassified as new, takes the
        # register-branch, and register()'s own idempotency check returns
        # the existing tumbler (fields refresh on the next pass) — same
        # class the fairness/yield machinery below already acknowledges.
        # A fetch failure must NOT no-op the whole hook (the nexus-o6aa.10.4
        # ghost class): fall back to the per-file lookups, loudly.
        path_to_entry: dict[str, object] | None
        try:
            path_to_entry = {e.file_path: e for e in cat.by_owner(owner)}
        except Exception as exc:  # noqa: BLE001 — degraded path must keep the hook alive
            path_to_entry = None
            _log.warning(
                "catalog_hook_owner_list_failed_falling_back_per_file",
                repo=repo_name, error=str(exc),
            )
        new_tumblers = []
        # content_type of every doc landing in new_tumblers, tracked
        # alongside it (populated in the same register_many/register
        # success branches below). Lets the link-generation call site
        # skip a generator's catalog fetches entirely when none of this
        # run's new documents are of that generator's source type — see
        # link_generator._no_qualifying_seed.
        new_content_types: set[str] = set()
        # nexus-o6aa.10.4 follow-up: track per-file failures so the
        # catalog hook stops failing silently. Pre-fix, a single
        # cat.register() exception inside the loop tripped the outer
        # except at line ~362 which logs at DEBUG, suppressing the
        # rest of the registrations and leaving file_to_doc_id empty
        # for every subsequent file in this run. Found via Hal's
        # ghost-chunk class on 2026-05-02.
        skipped_files: list[tuple[Path, str]] = []
        fairness_yielded = 0  # RDR-146 P2: files deferred to the next pass.
        ephemeral_skipped = 0  # nexus-u8n4r: worktree/tempdir registrations refused.
        import hashlib as _hl  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        # Pass 1: resolve each file to existing-or-new. Changed existing docs are
        # updated inline; NEW docs are accumulated for ONE batched register_many
        # (nexus-9dvqy, duoak.11 sink #2) — the per-file writer.register() loop
        # was the 333s serial-WAN registration sink. The per-file try/except keeps
        # the ghost-class isolation intact: one bad file must never strip doc_ids
        # from the rest of the repo.
        #
        # RDR-146 P2 fairness: the yield stays PER-FILE here (it guards the inline
        # writer.update() calls, which on a warm re-run fire for the whole existing
        # population — a HEAD bump flips every doc's stored head_hash to changed —
        # so this is a large serial write burst, not a rare one). ``skip`` defers
        # the remaining files to the next idempotent pass. Pass 2 adds a second,
        # per-page yield for the new-doc batch.
        # nexus-oub13: per-stage wall timing. The live "Catalog registration
        # done (115.5s)" phase wraps FIVE stages (resolve/update, update_many,
        # register_many pages, link generation, housekeeping) with no way to
        # tell which owns the wall — the ~38s/page inference attributed the
        # whole phase to register_many unproven. One summary log per run +
        # one log per register page.
        _stage_t0 = time.monotonic()
        _stage_s: dict[str, float] = {}
        new_batch: list[tuple[Path, dict]] = []
        # nexus-xedhp: changed-but-existing docs, batched via update_many
        # (Pass 1b, below) instead of an inline per-file writer.update().
        changed_batch: list[tuple[Path, dict]] = []
        # Sam's ruling 2026-09-04 (nexus-y8bkt): supersedes edges are part
        # of indexing. Link generation below is incremental over NEW
        # registrations, so an RDR whose frontmatter gained supersedes /
        # superseded_by after it was first registered never got its edge
        # (measured: zero extractor edges on a tenant with 189 RDRs). An
        # existing RDR whose CONTENT hash changed is re-fed to the
        # dependency generator as if new; head-hash-only bumps are not.
        relink_rdr_tumblers: list = []
        for abs_path, content_type, collection_name in indexed_files:
            if _batch_producer and await_fair_window(
                writer.is_interactive_write_pending, on_locked,
            ) == "skip":
                # Deferred = files not yet resolved this pass. new_batch entries
                # already collected are still registered in pass 2; the broken-off
                # tail (neither accumulated nor updated) is picked up next pass.
                fairness_yielded = (
                    len(indexed_files) - len(file_to_doc_id)
                    - len(skipped_files) - len(new_batch)
                )
                _log.info(
                    "catalog_write_yielded_skipped",
                    repo=repo_name, deferred=fairness_yielded,
                    reason="interactive_write_pending",
                )
                break
            try:
                rel_path = str(abs_path.relative_to(repo))
            except ValueError:
                rel_path = abs_path.name

            # nexus-u8n4r: refuse registration of files whose REGISTERED
            # identity (owner's main-repo root + rel_path — never the raw
            # on-disk abs_path, see should_skip_ephemeral_registration's
            # docstring) sits under an agent worktree or system temp dir.
            # This is the write-time guard for the 4,002-doc rdr__1-1
            # orphan class (nexus-wq1e4 production evidence): a Claude
            # Code worktree lives INSIDE the primary repo's tree at
            # ``.claude/worktrees/<agent>/``, so an unfiltered walk over
            # the primary repo sweeps the agent's ephemeral checkout in
            # too. ``str(main_repo)`` (freshly resolved via
            # _repo_identity_with_main above, NOT the owner's stored
            # ``repo_root``) is deliberate and correct post-zr2ie — but a
            # legacy owner registered PRE-zr2ie could in principle carry a
            # stored ``repo_root`` that has since diverged from what git
            # resolves today; this guard always trusts the live
            # resolution, matching register_owner's own zr2ie contract.
            # Never fires when the owner's own repo_root is itself under
            # such a path (throwaway owners / the pytest tmp-dir suite) —
            # see zr2ie's worktree-indexing precedent, which must stay
            # registrable.
            _registered_path = str(main_repo / rel_path)
            _skip_reason: str | None = None
            if should_skip_ephemeral_registration(_registered_path, str(main_repo)):
                _skip_reason = "worktree_or_tempdir"
            elif repo != main_repo and not Path(_registered_path).is_file():
                # nexus-u8n4r review fix (substantive-critic Critical,
                # empirically reproduced): the predicate above only
                # catches paths that are STRUCTURALLY under a worktree
                # marker. Indexing FROM WITHIN a worktree (repo !=
                # main_repo) over a file that is WORKTREE-UNIQUE — a
                # new/uncommitted draft with no mirror at the same
                # relative path in the main repo — reconstructs a
                # CLEAN-shaped, main-repo-anchored path that predicate
                # never flags, even though nothing exists there; once the
                # worktree is torn down that reconstructed path never
                # existed and the row is a permanent orphan exactly like
                # the marker-matched case. Existence at the reconstructed
                # identity is the SECOND signal — deliberately checked
                # ONLY when worktree-invoked (repo != main_repo), so the
                # ordinary non-worktree indexing path pays no extra
                # per-file stat. ``is_file()`` (not ``exists()``, review
                # fix Minor #2) so a same-named DIRECTORY in the main repo
                # never counts as a mirror. Content DRIFT between the
                # worktree copy and a same-named main-repo file is fine
                # and expected — this checks PATH existence, not content
                # equality; a worktree-unique file registers correctly
                # once it merges to main and is indexed from there. Case-
                # sensitivity caveat (review fix Minor #3): on a case-
                # INSENSITIVE filesystem (macOS default) a differently-
                # cased unrelated main-repo file at the same path also
                # counts as a mirror — this check is case-sensitive-
                # correct only on Linux (CI) / other case-sensitive
                # filesystems.
                _skip_reason = "worktree_unique_no_main_mirror"

            if _skip_reason is not None:
                ephemeral_skipped += 1
                _log.warning(
                    "ephemeral_path_registration_skipped",
                    path=_registered_path,
                    owner=str(owner),
                    repo=repo_name,
                    reason=_skip_reason,
                )
                from nexus.mcp_infra import _record_ephemeral_registration_skip  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
                _record_ephemeral_registration_skip(
                    _registered_path, str(owner), reason=_skip_reason,
                )
                continue

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
            try:
                file_hash = _hl.sha256(abs_path.read_bytes()).hexdigest()
            except OSError:
                file_hash = ""

            try:
                existing = (
                    path_to_entry.get(rel_path)
                    if path_to_entry is not None
                    else cat.by_file_path(owner, rel_path)
                )
                if existing is None:
                    # Defer the write: accumulate for the batched register_many
                    # in pass 2. new_tumblers / file_to_doc_id are populated there.
                    new_batch.append((abs_path, {
                        "title": abs_path.name,
                        "content_type": content_type,
                        "file_path": rel_path,
                        "physical_collection": collection_name,
                        "head_hash": head_hash,
                        "meta": {"content_hash": file_hash} if file_hash else None,
                        "source_mtime": source_mtime,
                    }))
                else:
                    # nexus-dst5h: skip the write when nothing changed —
                    # a warm re-run otherwise pays one serial update
                    # round-trip per file. An empty file_hash (read
                    # failure) is inconclusive, not a change. The mtime
                    # compare relies on st_mtime round-tripping bit-exact
                    # through storage (SQLite REAL / PG DOUBLE PRECISION /
                    # JSON); a storage change that truncates precision
                    # flips this to always-changed (harmless) — never
                    # compare with tolerance, drift means changed.
                    changed = (
                        existing.head_hash != head_hash
                        or existing.physical_collection != collection_name
                        or existing.source_mtime != source_mtime
                        or (
                            file_hash
                            and existing.meta.get("content_hash", "") != file_hash
                        )
                    )
                    if (
                        content_type == "rdr"
                        and file_hash
                        and existing.meta.get("content_hash", "") != file_hash
                    ):
                        relink_rdr_tumblers.append(existing.tumbler)
                    if changed:
                        # nexus-xedhp: accumulate for the batched update_many
                        # below instead of an inline per-file writer.update()
                        # round trip — a HEAD bump (any new git commit) flips
                        # EVERY indexed doc's stored head_hash to "changed",
                        # so on a warm re-run this is the whole repo's
                        # population, not a rare exception.
                        changed_batch.append((abs_path, {
                            "tumbler": str(existing.tumbler),
                            "head_hash": head_hash,
                            "physical_collection": collection_name,
                            "meta": {"content_hash": file_hash} if file_hash else None,
                            "source_mtime": source_mtime,
                        }))
                    file_to_doc_id[abs_path] = str(existing.tumbler)
                    # nexus-cp46b: a doc fenced 'indexing'/'failed' is a
                    # stranded run (RUNFENCE, nexus-5xn3k.3), not merely
                    # "unchanged" — the content-hash staleness cache must
                    # never skip it just because content still matches.
                    # ``index_state_reported`` False = pre-fence engine
                    # (nothing to say, not a stale signal) — same floor-
                    # tolerant convention as _index_run_fresh/
                    # non_complete_documents.
                    if (
                        stale_fence_doc_ids is not None
                        and getattr(existing, "index_state_reported", True)
                        and getattr(existing, "index_state", None) in ("indexing", "failed")
                    ):
                        stale_fence_doc_ids.add(str(existing.tumbler))
                    # nexus-vayt7: the fence's content hash is the doc-level
                    # hash of record; chunk metadata cannot carry one.
                    if (
                        complete_doc_hashes is not None
                        and getattr(existing, "index_state_reported", True)
                        and getattr(existing, "index_state", None) == "complete"
                        and getattr(existing, "index_content_hash", "")
                    ):
                        complete_doc_hashes[str(existing.tumbler)] = (
                            existing.index_content_hash
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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

        _stage_s["pass1_resolve"] = time.monotonic() - _stage_t0
        _stage_mark = time.monotonic()

        # Pass 1b: batch-update the CHANGED existing docs (nexus-xedhp,
        # duoak.11 follow-up). A HEAD bump flips every doc's stored head_hash
        # to "changed", so on a warm re-run this is the whole repo's
        # population — the serial per-file writer.update() this replaces was
        # measured at 175.5s / 1718 files (~102ms/file, all WAN round trips).
        # ``update_many`` is service-mode-only (nexus_xedhp scope; see
        # catalog/factory.py's _SERVICE_ONLY_WRITE_OPS) — SQLite/daemon-mode
        # writers don't expose it, so this capability check safely falls
        # back to the original per-file loop there, matching the
        # write_manifest_many precedent in Pass 2's sibling below.
        if changed_batch:
            _update_many = getattr(writer, "update_many", None)
            if callable(_update_many):
                for _start in range(0, len(changed_batch), _CATALOG_REGISTER_PAGE):
                    if _batch_producer and await_fair_window(
                        writer.is_interactive_write_pending, on_locked,
                    ) == "skip":
                        _log.info(
                            "catalog_write_yielded_skipped",
                            repo=repo_name,
                            deferred=len(changed_batch) - _start,
                            reason="interactive_write_pending",
                        )
                        break
                    page = changed_batch[_start : _start + _CATALOG_REGISTER_PAGE]
                    page_docs = [doc for _, doc in page]
                    try:
                        counts = _update_many(page_docs)
                        if len(counts) != len(page):
                            raise ValueError(
                                f"update_many returned {len(counts)} counts "
                                f"for {len(page)} updates"
                            )
                    except Exception:  # noqa: BLE001 — batch unrecoverable; per-file isolation fallback
                        _log.warning(
                            "catalog_update_many_failed_falling_back_per_file",
                            repo=repo_name, page_size=len(page), exc_info=True,
                        )
                        for path, doc in page:
                            try:
                                writer.update(
                                    doc["tumbler"],
                                    **{k: v for k, v in doc.items() if k != "tumbler"},
                                )
                            except Exception as exc:  # noqa: BLE001 — ghost-class per-file isolation
                                skipped_files.append((path, str(exc)))
                                _log.warning(
                                    "catalog_hook_update_failed",
                                    abs_path=str(path), error=str(exc), exc_info=True,
                                )
            else:
                # SQLite/daemon-mode writer: no batched path, unchanged
                # behaviour (the original per-file inline update() loop).
                for path, doc in changed_batch:
                    try:
                        writer.update(
                            doc["tumbler"],
                            **{k: v for k, v in doc.items() if k != "tumbler"},
                        )
                    except Exception as exc:  # noqa: BLE001 — ghost-class per-file isolation
                        skipped_files.append((path, str(exc)))
                        _log.warning(
                            "catalog_hook_update_failed",
                            abs_path=str(path), error=str(exc), exc_info=True,
                        )

        _stage_s["pass1b_update_many"] = time.monotonic() - _stage_mark
        _stage_mark = time.monotonic()

        # Pass 2: batch-register the NEW docs. The RDR-146 fairness yield moves
        # from per-file to a per-PAGE check — a page is ONE register_many round-
        # trip (one multi-row INSERT server-side), not 1000 serial writes, so the
        # foreground-starvation window is bounded to a single batch. ``skip``
        # defers the remaining pages to the next idempotent pass. register_many
        # is 1:1-or-raise; the client already degrades a failed batch to per-doc
        # register() internally, and if even that raises we fall back here to a
        # per-file register with the same ghost-class isolation as pass 1.
        for _start in range(0, len(new_batch), _CATALOG_REGISTER_PAGE):
            if _batch_producer and await_fair_window(
                writer.is_interactive_write_pending, on_locked,
            ) == "skip":
                fairness_yielded = len(new_batch) - _start
                _log.info(
                    "catalog_write_yielded_skipped",
                    repo=repo_name, deferred=fairness_yielded,
                    reason="interactive_write_pending",
                )
                break
            page = new_batch[_start : _start + _CATALOG_REGISTER_PAGE]
            page_docs = [doc for _, doc in page]
            _page_t0 = time.monotonic()
            _page_ok = False
            try:
                tumblers = writer.register_many(owner, page_docs)
                _page_ok = True
                # register_many is 1:1-or-raise; guard the invariant explicitly so
                # a short return can never SILENTLY truncate via zip() (the ghost-
                # doc class). A mismatch routes to the per-file fallback below.
                if len(tumblers) != len(page):
                    raise ValueError(
                        f"register_many returned {len(tumblers)} tumblers for "
                        f"{len(page)} docs"
                    )
                for (path, doc), tum in zip(page, tumblers):
                    new_tumblers.append(tum)
                    file_to_doc_id[path] = str(tum)
                    new_content_types.add(doc.get("content_type", ""))
            except Exception:  # noqa: BLE001 — batch unrecoverable; per-file isolation fallback
                _log.warning(
                    "catalog_register_many_failed_falling_back_per_file",
                    repo=repo_name, page_size=len(page), exc_info=True,
                )
                for path, doc in page:
                    try:
                        tum = writer.register(
                            owner, doc["title"],
                            **{k: v for k, v in doc.items() if k != "title"},
                        )
                        new_tumblers.append(tum)
                        file_to_doc_id[path] = str(tum)
                        new_content_types.add(doc.get("content_type", ""))
                    except Exception as exc:  # noqa: BLE001 — ghost-class per-file isolation
                        skipped_files.append((path, str(exc)))
                        _log.warning(
                            "catalog_hook_register_failed",
                            abs_path=str(path), error=str(exc), exc_info=True,
                        )
            finally:
                # Fires on success AND failure — the slow/failing pages are
                # exactly what this profiler must not miss (review M-2).
                _log.info(
                    "catalog_register_page",
                    page=_start // _CATALOG_REGISTER_PAGE + 1,
                    docs=len(page),
                    elapsed_s=round(time.monotonic() - _page_t0, 2),
                    ok=_page_ok,
                )

        if skipped_files or ephemeral_skipped:
            _updated = (
                len(indexed_files) - len(new_tumblers)
                - len(skipped_files) - ephemeral_skipped
            )
            _skip_bits = []
            if skipped_files:
                _skip_bits.append(f"{len(skipped_files)} skipped (see structlog warnings)")
            if ephemeral_skipped:
                _skip_bits.append(
                    f"{ephemeral_skipped} worktree/tempdir path(s) refused (nexus-u8n4r)"
                )
            _progress(
                f"  Catalog: {len(new_tumblers)} new, {_updated} updated, "
                f"{', '.join(_skip_bits)}\n"
            )
        else:
            _progress(
                f"  Catalog: {len(new_tumblers)} new, "
                f"{len(indexed_files) - len(new_tumblers)} updated\n"
            )

        _stage_s["pass2_register"] = time.monotonic() - _stage_mark
        _stage_mark = time.monotonic()

        # Auto-generate links after registration (incremental: only new entries)
        links_created = 0
        if new_tumblers:
            _progress(f"  Catalog: linking {len(new_tumblers)} new entries…\r")
        try:
            from nexus.catalog.link_generator import (  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
                bind_rdr_dependency_generator,
                generate_pdf_corpus_links,
                generate_prose_filepath_links,
                generate_rdr_filepath_links,
                incremental_generator_applies,
            )
            # nexus-oub13 critique M5: per-generator wall so the "linking"
            # stage bucket names WHICH generator owns it, not just that
            # linking in aggregate is slow. nexus-sob9: prose + pdf run
            # with the same incremental scope so a single bulk-index pass
            # closes the prose/pdf 0% gap from the 2026-05-08 prod shakeout.
            # nexus-jg3x5: each generator is also its own [post] sub-phase
            # on the orchestrator's on_phase channel — start marker on
            # entry, done marker carrying the SAME duration the stage
            # event below records. A generator the seed predicate says
            # cannot link anything in this batch (no new tumbler of its
            # source type) still runs (it returns 0 without fetching) but
            # emits no pair: claiming a phase that never ran is the
            # honesty defect this emission exists to fix.
            _link_counts: dict[str, int] = {}
            _generators: list[tuple[str, Callable[..., int]]] = [
                ("rdr", generate_rdr_filepath_links),
                ("prose", generate_prose_filepath_links),
                ("pdf", generate_pdf_corpus_links),
            ]
            # RDR-201 P3.2 (nexus-j9z30.21): bind_rdr_dependency_generator
            # is the single adapter that satisfies generate_rdr_dependency_
            # links's required (no-default) current_owner/repo_source_prefix
            # while still slotting into the SAME (cat, writer=, new_tumblers=,
            # new_content_types=) shape the other three generators use.
            # Returns None when no owner is registered yet for this repo (a
            # catalog that has never completed an index has nothing to scope
            # endpoint resolution against) — skip rather than pass a bogus
            # owner.
            _dep_entry = bind_rdr_dependency_generator(cat, repo)
            if _dep_entry is not None:
                _generators.append(_dep_entry)
            else:
                _log.debug("rdr_dependency_links_skipped_no_owner", repo=repo_name)
            for _kind, _gen in _generators:
                _seed_tumblers = new_tumblers
                _seed_types = new_content_types
                if _kind == "rdr-dependency" and relink_rdr_tumblers:
                    _seed_tumblers = [*new_tumblers, *relink_rdr_tumblers]
                    _seed_types = {*new_content_types, "rdr"}
                _announce = on_phase is not None and incremental_generator_applies(
                    _kind, _seed_tumblers, _seed_types,
                )
                if _announce:
                    on_phase(f"Catalog linking: {_kind}…")
                _lg_t = time.monotonic()
                try:
                    _link_counts[_kind] = _gen(
                        cat, writer=writer, new_tumblers=_seed_tumblers,
                        new_content_types=_seed_types,
                    )
                except Exception:
                    # Close the phase before the outer handler logs and
                    # audits the failure: the phase heartbeat repeats the
                    # LAST on_phase label, and the next one would only
                    # fire after housekeeping — a "still running" tick for
                    # a phase that already died (code-review-expert).
                    if _announce:
                        on_phase(
                            f"Catalog linking: {_kind} failed "
                            f"({time.monotonic() - _lg_t:.1f}s)"
                        )
                    raise
                _lg_s = time.monotonic() - _lg_t
                _stage_s[f"linking_{_kind}"] = _lg_s
                if _announce:
                    on_phase(f"Catalog linking: {_kind} done ({_lg_s:.1f}s)")
            fp_count = _link_counts["rdr"]
            prose_count = _link_counts["prose"]
            pdf_count = _link_counts["pdf"]
            dep_count = _link_counts.get("rdr-dependency", 0)
            links_created = fp_count + prose_count + pdf_count + dep_count
            if links_created:
                _log.info(
                    "catalog_links_generated",
                    filepath=fp_count, prose=prose_count, pdf=pdf_count,
                    rdr_dependency=dep_count,
                    repo=repo_name,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort path; error surfaced via log + audit, must not crash caller
            # nexus-ou4tb: links missing is a quieter loss than missing
            # registration, but still silent at DEBUG.
            _log.warning("catalog_link_generation_failed", exc_info=True)
            record_catalog_hook_failure(
                source_path=str(repo), collection="",
                hook_name="catalog_link_generation", error=str(exc),
            )

        _stage_s["linking"] = time.monotonic() - _stage_mark
        _stage_mark = time.monotonic()

        # Housekeeping: detect and evict orphaned catalog entries.
        _maybe_run_housekeeping(
            cat, owner, indexed_files, repo,
            writer=writer, skip_housekeeping=skip_housekeeping,
            progress=_progress,
        )
        _stage_s["housekeeping"] = time.monotonic() - _stage_mark

        _log.info(
            "catalog_hook_stage_timing",
            total_s=round(time.monotonic() - _stage_t0, 1),
            **{k: round(v, 1) for k, v in _stage_s.items()},
            files=len(indexed_files),
            new_docs=len(new_tumblers),
            changed_docs=len(changed_batch),
            links=links_created,
        )
        _progress(f"  Catalog: done ({len(new_tumblers)} new, {links_created} links)\n")
    except Exception as exc:  # noqa: BLE001 — best-effort path; error surfaced via log + audit, must not crash caller
        # nexus-ou4tb: WARNING, not DEBUG, and audited. A failure here means
        # documents landed in T3 and were never registered in the catalog — no
        # doc_id, no manifest, no links — and only a rebuild recovers them.
        # At DEBUG that was invisible: the run reported success.
        _log.warning("catalog_hook_failed", exc_info=True)
        record_catalog_hook_failure(
            source_path=str(repo), collection="",
            hook_name="catalog_index_hook", error=str(exc),
        )
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe; Catalog._db.close() is internal
    return file_to_doc_id


def _maybe_run_housekeeping(
    cat,
    owner,
    indexed_files: list,
    repo: "Path",
    *,
    writer,
    skip_housekeeping: bool,
    progress: "Callable[[str], object]" = lambda _s: None,
) -> None:
    """The nexus-fltb4 mass-delete gate, as a behavioral seam (nexus-4kh95).

    Housekeeping's miss-count sweep interprets "not in this run's indexed
    set" as "gone" — run against a PARTIAL (delta-filtered) walk it marks
    every unvisited doc as missing and evicts them after two runs (the
    mass-delete class). ``skip_housekeeping=True`` (set by the --since-head
    delta path) must therefore never reach :func:`_run_housekeeping`.
    Extracted from ``_catalog_hook``'s body so the gate is testable by
    CALLING it, not by source-text inspection.
    """
    if skip_housekeeping:
        _log.debug("catalog_housekeeping_skipped", reason="partial_walk")
        return
    progress("  Catalog: housekeeping…\r")
    indexed_set = _indexed_relpaths(indexed_files, repo)
    _run_housekeeping(cat, owner, indexed_set, writer=writer)


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
    cat: "CatalogReader",
    owner: "Tumbler",
    indexed_set: set[str],
    *,
    writer: object = None,
) -> None:
    """Orphan detection with miss_count tracking and rename detection.

    For each catalog entry owned by *owner*:
    - If the file is present in *indexed_set*, reset miss_count to 0.
    - If absent and a content_hash match exists at a new path, treat as rename:
      transfer links to the new entry and delete the old one.
    - If absent with no rename match, increment miss_count. Delete at threshold >= 2.
    """
    # RDR-146 P1.2 strict split: reads via ``cat``, writes via ``w``
    # (the write-only proxy; defaults to ``cat`` for single-object callers).
    w = writer if writer is not None else cat
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
                w.update(entry.tumbler, meta=meta)
            continue

        # Check for rename: orphan's content_hash matches a newly-indexed entry
        orphan_hash = (entry.meta or {}).get("content_hash", "")
        if orphan_hash and orphan_hash in hash_to_entry:
            new_entry = hash_to_entry[orphan_hash]
            # Transfer links from old entry to new entry
            old_links = cat.links_from(entry.tumbler)
            for lnk in old_links:
                w.link_if_absent(
                    new_entry.tumbler, lnk.to_tumbler, lnk.link_type,
                    created_by=lnk.created_by,
                )
            # Also transfer incoming links
            incoming = cat.links_to(entry.tumbler)
            for lnk in incoming:
                w.link_if_absent(
                    lnk.from_tumbler, new_entry.tumbler, lnk.link_type,
                    created_by=lnk.created_by,
                )
            w.delete_document(entry.tumbler)
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
            w.delete_document(entry.tumbler)
            _log.info(
                "housekeeping_orphan_deleted",
                tumbler=str(entry.tumbler),
                file_path=entry.file_path,
            )
        else:
            meta["miss_count"] = miss_count
            w.update(entry.tumbler, meta=meta)
            _log.debug(
                "housekeeping_miss_count_incremented",
                tumbler=str(entry.tumbler),
                miss_count=miss_count,
            )


def select_local_mode_embed_fn(local_ef: Callable[[list[str]], list[list[float]]]) -> Callable[[list[str]], list[list[float]]]:
    """The client-side ``embed_fn`` for a LOCAL-mode index run (nexus-b7s8t).

    In local mode T3 is still the service-backed ``HttpVectorClient``
    (``make_t3()`` is unconditional since RDR-155 P4a.2) and its
    ``upsert_chunks_with_embeddings`` discards caller embeddings. Handing
    the bge ONNX model in as ``embed_fn`` therefore embedded every chunk
    TWICE — once here (466% CPU, ~2 GB RSS on a 16 GB box, competing with
    the engine JVM that then did the same work again) and once server-side.
    The client embeds only when the vector backend is opted OUT of service
    mode (the in-memory / chroma-injected test substrate); otherwise the
    server-embed stub ``[[]] * n`` is returned, the same one the cloud
    branch uses.
    """
    from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)

    if is_vector_service_mode():
        return lambda texts: [[]] * len(texts)
    return local_ef


def index_repository(
    repo: Path,
    registry: "object",
    *,
    frecency_only: bool = False,
    chunk_lines: int | None = None,
    force: bool = False,
    force_stale: bool = False,
    since_head: bool = False,
    on_locked: str = "wait",
    on_start: Callable[[int], None] | None = None,
    on_file: Callable[[Path, int, float], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_flush: "Callable[[int, int, str, float, str | None], None] | None" = None,
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

    *on_flush* (nexus-rhwg5): fires once per settled MID-LOOP chunk-batch
    flush -- ``(flush_num, chunks, collection, elapsed_s, error)`` --
    threaded straight to ``ChunkBatcher(on_flush=...)``. Never fires for
    the end-of-run drain (that has its own ``on_phase``-driven progress
    via ``_drain_batcher_with_markers``) or for a bisect attempt (only
    the resulting settled halves). ``None`` is a complete no-op.

    Returns a stats dict (empty for frecency_only runs) with keys:
    ``rdr_indexed``, ``rdr_current``, ``rdr_failed``, and ``files_changed``
    (count of files — code/prose/pdf plus rdr_indexed — that wrote at least
    one chunk this run; drives the caller's post-index taxonomy gate, nexus-qgc4b).
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
        from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        hooks = HookRegistry()
        install_default_hooks(hooks)

    try:
        # RDR-137 Phase 3.8 (nexus-tts0d.13): registry.update(status=...)
        # writes dropped per A2 verdict — status is write-only with no
        # consumers. head_hash now writes to owners.head_hash on the
        # catalog (Phase 1.5b column) via _set_owner_head_hash.
        # RDR-137 followup IMP-21 (nexus-43qgm.21): inner try/except
        # removed — both handlers unconditionally re-raised and added
        # zero behaviour; the outer try/finally is the only meaningful
        # guard. Vestige of the dropped status-write path.
        if frecency_only:
            _run_index_frecency_only(repo, registry)
            stats: dict[str, int] = {}
        else:
            stats = _run_index(repo, registry, chunk_lines=chunk_lines, force=force, force_stale=force_stale, since_head=since_head, on_locked=on_locked, on_start=on_start, on_file=on_file, on_phase=on_phase, on_flush=on_flush, on_stage_timers=on_stage_timers, hooks=hooks)
            _set_owner_head_hash(repo, _current_head(repo))
        return stats
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
    frecency-only update can key chunk lookups on ``doc_id``. Returns a
    ``{abs_path: doc_id}`` mapping; files without a catalog entry are
    absent from the map — the caller now SKIPS the frecency refresh
    for those files (nexus-afudo, 2026-08-05: the legacy source_path
    filter this used to fall back to was deleted as dead code; RDR-102
    D2 removed source_path from make_chunk_metadata for every writer).

    Best-effort: catalog absent / owner missing / lookup failure all
    return an empty map, which now means "no frecency refresh this run
    for the affected files" rather than a doomed source_path query.
    """
    file_to_doc_id: dict[Path, str] = {}
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        # Service-only since nexus-i711w: no local is_initialized gate (the
        # gate must never short-circuit service mode — RDR-168 P4 /
        # nexus-njrcn.6 lineage).
        cat = make_catalog_reader()
        _, repo_hash = _repo_identity(repo)
        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            return file_to_doc_id
        # nexus-dst5h: ONE owner-scoped fetch + local join instead of a
        # per-file ``by_file_path`` pass (a second full serial-WAN sweep
        # in service mode, paid on every warm run). A by_owner failure
        # raises into the outer except -> empty map, which the documented
        # contract tolerates (caller now skips the frecency refresh for
        # the affected files — nexus-afudo).
        path_to_entry = {e.file_path: e for e in cat.by_owner(owner)}
        for abs_path in files:
            try:
                rel_path = str(abs_path.relative_to(repo))
            except ValueError:
                rel_path = abs_path.name
            entry = path_to_entry.get(rel_path)
            if entry is not None:
                file_to_doc_id[abs_path] = str(entry.tumbler)
    except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
        _log.debug("frecency_doc_id_map_failed", exc_info=True)
    return file_to_doc_id


def _run_index_frecency_only(repo: Path, registry: "object") -> None:
    """Update frecency_score metadata on all indexed chunks without re-embedding.

    Handles the code__, docs__, and rdr__ collections (rdr added by
    nexus-e0w01 — it was previously a total omission from this mode).

    Routing (nexus-enehl, RDR-152):
    - Service mode (NX_STORAGE_BACKEND_VECTORS=service): obtains an
      :class:`HttpVectorClient` and routes the metadata update through
      the Java service's ``/v1/vectors/update-metadata`` endpoint so the
      frecency_score lands in the service's Chroma — the one that
      service-mode search reads.  No credential check is needed; the
      service handles its own Chroma/Voyage.  This replaces the
      nexus-67ljl early-return skip-guard that previously prevented
      split-brain writes to daemon-Chroma.
    - Local mode: checks the local path is writable, then obtains a
      :class:`T3Database` via ``make_t3()`` and updates directly.
      Non-service cloud mode is retired (nexus-sghyo) and raises loud.
    """
    from nexus.frecency import batch_frecency  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.db import make_t3  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    info = registry.get(repo)
    if info is None:
        return

    # RDR-103 Phase 3a: registry value preserves the legacy name when the
    # repo was added before the migration; fallback queries the catalog
    # for a conformant name (Phase 5 drops the legacy branch entirely).
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _repo_collection_or_legacy(repo, "docs")
    # nexus-e0w01: rdr__ was a total omission here — RDR chunks' frecency_score
    # was never refreshed by --frecency-only, only by a full index pass. Same
    # resolution chain the full pass uses; the not-yet-written case is handled
    # below by the existing NotFound skip (no zombie mint).
    rdr_collection = info.get("rdr_collection") or _repo_collection_or_legacy(repo, "rdr")

    from nexus.db.http_vector_client import is_vector_service_mode as _is_svc  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)
    if _is_svc():
        # nexus-enehl: service mode — route frecency metadata updates through
        # the Java service's /v1/vectors/update-metadata endpoint.
        # No credential check needed: the service handles its own Chroma/Voyage.
        from nexus.db.http_vector_client import get_http_vector_client as _get_svc  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)
        db = _get_svc()
        _log.info(
            "frecency_service_mode",
            repo=str(repo),
            reason="NX_STORAGE_BACKEND_VECTORS=service; routing frecency updates "
                   "through Java service /v1/vectors/update-metadata endpoint.",
        )
    else:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        if not is_local_mode():
            # nexus-sghyo: non-service cloud-mode ingestion was retired —
            # the client no longer embeds via Voyage.
            raise CredentialsMissingError(
                "non-service cloud-mode ingestion was retired: the client "
                "no longer embeds via Voyage. Set NX_STORAGE_BACKEND_"
                "VECTORS=service (the default) or unset it."
            )
        check_local_path_writable()
        db = make_t3()

    frecency_map = batch_frecency(repo)

    # nexus-f4z9: pre-resolve doc_ids once for all files so the chunk
    # lookup can key on ``doc_id`` when the catalog has the entry.
    # Files predating the catalog backfill (no doc_id) are skipped by
    # the per-file loop below — nexus-afudo (2026-08-05) deleted the
    # legacy source_path-keyed filter they used to fall through to.
    file_to_doc_id = _build_frecency_doc_id_map(repo, list(frecency_map.keys()))

    # Update frecency in all three content-type collections (nexus-e0w01).
    collection_names = [code_collection]
    if docs_collection:
        collection_names.append(docs_collection)
    if rdr_collection:
        collection_names.append(rdr_collection)

    # nexus-ks40: frecency_only is a read-update flow; if the
    # collection has not yet been written, skip rather than mint an
    # empty zombie via get_or_create_collection.
    from nexus.errors import collection_not_found_errors  # noqa: PLC0415 — deferred import (RDR-155 P4b P0c: substrate-neutral missing-collection contract)

    # nexus-7zcv (RDR-108 Phase 4 review D-H4): the legacy
    # ``where={"doc_id": <id>}`` lookup matches nothing for Phase-3
    # chunks (the doc_id metadata field is gone). Use the catalog
    # document_chunks manifest to map doc_id -> chashes, then fetch
    # by ``chash[:32]`` IDs. Falls back to the legacy where-filter
    # when the catalog is unavailable (correct only for pre-Phase-3
    # chunks).
    _cat = None
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        _cat = make_catalog_reader()
    except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
        _log.debug("frecency_only_catalog_lookup_failed", exc_info=True)

    for collection_name in collection_names:
        try:
            col = db.get_collection(collection_name)
            if col is None:
                continue
        except collection_not_found_errors():
            continue
        for file, score in frecency_map.items():
            doc_id = file_to_doc_id.get(file, "")

            existing: dict | None = None
            if _cat is not None and doc_id:
                # Manifest-based path: resolve chashes for this doc.
                try:
                    manifest = _cat.get_manifest(doc_id)
                except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
                    manifest = []
                natural_ids = [r.chash for r in manifest if r.chash]  # RDR-180: full digest
                if natural_ids:
                    try:
                        present = col.get(
                            ids=natural_ids, include=["metadatas"],
                        )
                    except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
                        present = None
                    if present and present.get("ids"):
                        existing = present

            if existing is None:
                if not doc_id:
                    # nexus-afudo (2026-08-05): the legacy source_path
                    # where-filter this used to fall back to is DELETED
                    # dead code. RDR-102 D2 (2026-05-02) removed
                    # source_path from make_chunk_metadata for every
                    # writer; a live-store probe (field>=! existence
                    # test) found zero source_path rows across 13
                    # representative collections (~115k chunks,
                    # including this run's own code__/docs__/rdr__
                    # collections). A file with no catalog doc_id has no
                    # frecency-refresh path left — skip it; the next run
                    # retries once the catalog hook backfills a doc_id.
                    continue
                # doc_id-keyed where-filter. Returns nothing for
                # post-Phase-3 chunks whose manifest resolution above
                # failed or found no chashes; still worth trying — a
                # transient manifest-read failure with an otherwise
                # healthy doc_id should not skip the file outright.
                where = {"doc_id": doc_id}
                try:
                    existing = _paginated_get(
                        col, include=["metadatas"], where=where,
                    )
                except Exception:  # noqa: BLE001 — nexus-ou4tb: isolate this FILE, not the run
                    # The client fails loud now (a degraded service no longer
                    # reads as an empty collection), but one file's failed
                    # staleness lookup must not abort the whole repo's
                    # frecency pass. Skip this file; the next run retries it.
                    _log.warning(
                        "frecency_staleness_lookup_failed_skipping_file",
                        file=str(file), collection=getattr(col, "name", "?"),
                        exc_info=True,
                    )
                    continue

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
#: Transient upsert HTTP statuses (gateway timeout / pool exhaustion / rate
#: limit) where a per-file DIRECT upsert (prose/PDF — code files are
#: contained by the ChunkBatcher's file-atomic failure handling) should
#: DEFER the file to the next run's staleness retry instead of propagating.
#: Propagating instead (a) fails the whole run on one transient blip and
#: (b) under concurrency wedges the run for up to the upsert timeout while
#: run_file_loop waits on the sibling in-flight worker (nexus-7yfe6).
#: Idempotent ON CONFLICT upsert → the deferred file re-uploads cleanly
#: next run. 429 added at nexus-cy9u7 CRITICAL-1: this is retried
#: internally first (HttpVectorClient.upsert_chunks now routes through
#: ``_vector_with_retry`` + the shared rate-limit brake), so a request only
#: reaches here once that retry budget is exhausted — a sustained 429
#: window must still defer, not abort the whole ``nx index repo`` run (the
#: literal 2026-08-15 incident symptom: run_file_loop's first-exception-
#: cancels-all contract turned one file's 429 into a full-run abort).
_TRANSIENT_UPSERT_CODES = frozenset({429, 502, 503, 504})


def _contain_extraction_quality_gate(
    fn: "Callable[[], int]", file: "Path", failed: list[str],
) -> int:
    """Run per-file PDF index ``fn``; on ``ExtractionQualityError`` (nexus-wi1uv's
    post-extraction quality gate), log an ERROR naming the file + remedy,
    append *file* to *failed*, and return 0 (file NOT written this run)
    instead of propagating.

    Round-2 review finding (code-review-expert + substantive-critic
    Critical, both independently): pre-fix, ``run_file_loop``'s
    first-exception-cancels-all contract meant ONE PDF tripping the
    quality gate during ``nx index repo`` aborted the ENTIRE run —
    cancelling all not-yet-started code/prose/PDF/RDR files, not just the
    offending PDF. Same containment shape as :func:`_contain_transient_
    upsert` (same layer, same file), but for a PERMANENT per-document
    condition rather than a transient upsert blip — so this does not
    retry next run the way a deferred transient upsert does; the file
    stays quality-gate-failed until explicitly re-indexed with
    ``nx index pdf --allow-degraded-extraction <file>``.
    """
    from nexus.errors import ExtractionQualityError  # noqa: PLC0415 — circular-dep avoidance: nexus.errors

    try:
        return fn()
    except ExtractionQualityError as exc:
        _log.error(
            "index_file_quality_gate_failed",
            file=str(file), error=str(exc),
            remedy=f"nx index pdf --allow-degraded-extraction {file}",
        )
        failed.append(str(file))
        return 0


def _contain_transient_upsert(fn: "Callable[[], int]", file: "Path") -> int:
    """Run per-file index ``fn``; on a TRANSIENT upsert 5xx (gateway/pool), log
    and return 0 (file deferred to staleness) instead of propagating. Permanent
    errors (4xx, transport, non-transient 5xx) still raise (nexus-7yfe6).
    """
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — circular-dep avoidance: nexus.db.http_vector_client

    try:
        return fn()
    except VectorServiceError as exc:
        if exc.code in _TRANSIENT_UPSERT_CODES:
            _log.warning(
                "index_file_transient_upsert_deferred",
                file=str(file), code=exc.code, error=str(exc),
            )
            return 0
        raise


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
    batcher: object | None = None,
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
    from nexus.code_indexer import index_code_file  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.index_context import IndexContext  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

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
        batcher=batcher,
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
    batcher: object | None = None,
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
    from nexus.prose_indexer import index_prose_file  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.index_context import IndexContext  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

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
        batcher=batcher,
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
    batcher: object | None = None,
) -> int:
    """Index a single PDF file into the docs__ collection.

    Uses PDF extraction + chunking from doc_indexer, embeds via ``embed_fn``
    (local/service mode; non-service embedding was retired, nexus-sghyo).
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
    import hashlib as _hl  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.doc_indexer import _pdf_chunks  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    content_hash = _hl.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            content_hash.update(block)
    content_hash_hex = content_hash.hexdigest()

    # Staleness check.
    # nexus-dcym: prefer doc_id-keyed lookup when the catalog hook
    # supplied a resolver. Empty doc_id is an unconditional "stale" —
    # nexus-afudo (2026-08-05) deleted check_staleness's legacy
    # source_path fallback as dead code (RDR-102 D2 removed
    # source_path from make_chunk_metadata for every writer).
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
        from contextlib import nullcontext  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
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
            # nexus-sghyo: non-service embedding was retired — the client
            # no longer embeds via Voyage. Unreachable in shipping config
            # (the service/local-mode gate upstream always sets embed_fn).
            raise CredentialsMissingError(
                "non-service embedding was retired: the client no longer "
                "embeds via Voyage. Set NX_STORAGE_BACKEND_VECTORS=service "
                "(the default) or unset it."
            )
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model

    # duoak 2C (nexus-1ugqs): stage in the cross-file batcher; hooks
    # defer to the orchestrator's completion callback on batch-land.
    # add() returning False (file exceeds one batch) falls through to
    # the legacy per-file upsert — file-atomicity preserved either way.
    if batcher is not None and batcher.add(  # type: ignore[attr-defined]
        str(file),
        collection_name,
        ids,
        documents,
        metadatas,
        context={
            "ids": ids,
            "documents": documents,
            "embeddings": embeddings,
            "metadatas": metadatas,
            "catalog_doc_id": catalog_doc_id,
            "collection": collection_name,
            "hooks": hooks,
        },
    ):
        return len(ids)

    # nexus-vw594 F1: producer #9 (nx index repo, PDF path, legacy
    # per-file fallback — reached when the ChunkBatcher rejects the file
    # or is absent). Fence begin BEFORE the upload, mirroring
    # doc_indexer.py's single-flush producers; this path was previously
    # entirely unfenced.
    if catalog_doc_id:
        from nexus.doc_indexer import _fence_begin  # noqa: PLC0415 — deferred import; test patch target
        _fence_begin(catalog_doc_id, content_hash_hex, collection_name)

    with _stage("upload"):
        try:
            db.upsert_chunks_with_embeddings(
                collection_name=collection_name,
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
                force_re_embed=force,
            )
        except Exception as upload_exc:
            # nexus-bhlfy: mirrors commands/store.py's cotmr fix — stamp
            # 'failed' unconditionally so the fence does not wedge at
            # 'indexing' with only the 6h doctor sweep as signal.
            # _fence_fail never raises, so the re-raise below always
            # carries the original exception unmasked.
            if catalog_doc_id:
                from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 — deferred import; test patch target
                _fence_fail(catalog_doc_id, str(upload_exc))
            raise

        # Post-store hook chains (RDR-095). Both single-doc and batch
        # chains fire from every storage event; consumers register in
        # whichever shape fits their work. Single-doc fire iterates the
        # batch one document at a time so per-doc hooks (e.g. RDR-089
        # aspect extraction) cover CLI ingest the same way they cover
        # MCP store_put.
        if hooks is None:
            from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
            hooks = HookRegistry()
            install_default_hooks(hooks)
        # nexus-vw594 F1: this file's whole chunk set lands in the ONE
        # upsert above (file-atomic) — manifest_complete rides this
        # existing call through manifest_write_batch_hook's
        # write_manifest_many completion stamp, no extra round trip.
        hooks.fire_batch(
            ids, collection_name, documents, embeddings, metadatas,
            catalog_doc_id=catalog_doc_id,
            manifest_complete={catalog_doc_id: content_hash_hex} if catalog_doc_id else None,
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


_CHROMA_PAGE_SIZE: int = 300
"""ChromaDB Cloud hard cap per get() call — paginate above this."""


def _paginated_get(
    col: object,
    include: list[str],
    where: dict | None = None,
    *,
    on_page: Callable[[int, int], None] | None = None,
) -> dict:
    """Fetch all matching chunks from *col* by paginating in _CHROMA_PAGE_SIZE batches.

    ChromaDB Cloud silently truncates unbounded col.get() calls at 300 records.
    This helper accumulates pages until a short page signals the end.

    ``on_page(page_num, scanned_so_far)``, when provided, fires after each
    page lands (index-output-ux-assessment-2026-08-10 §7.2/a3): this loop
    was previously a black hole (427.5s unbroken on a 96-page collection,
    nexus-vatx) with no way for a caller to surface real progress.
    ``page_num`` is 1-based; ``scanned_so_far`` is the cumulative id count.

    Returns a dict with ``"ids"`` and, when ``"metadatas"`` is in *include*,
    a ``"metadatas"`` key — matching the shape returned by col.get().
    """
    offset = 0
    page_num = 0
    all_ids: list[str] = []
    all_metas: list[dict] = []
    has_metas = "metadatas" in include

    while True:
        page_num += 1
        kwargs: dict = {"include": include, "limit": _CHROMA_PAGE_SIZE, "offset": offset}
        if where is not None:
            kwargs["where"] = where
        batch = _vector_with_retry(col.get, **kwargs)
        batch_ids: list[str] = batch["ids"] or []
        all_ids.extend(batch_ids)
        if has_metas:
            all_metas.extend(batch.get("metadatas") or [])
        if on_page is not None:
            on_page(page_num, len(all_ids))
        if len(batch_ids) < _CHROMA_PAGE_SIZE:
            break
        offset += _CHROMA_PAGE_SIZE

    result: dict = {"ids": all_ids}
    if has_metas:
        result["metadatas"] = all_metas
    return result


# _fetch_all_chunk_metadata DELETED here (RDR-191 Phase 6, nexus-o8dil.33,
# 2026-08-15) — its ONE caller (_prune_deleted_files' client-side fallback
# sweep) is retired alongside it; see that function's own docstring for
# the full rationale. _paginated_get (above) stays — it has other,
# unrelated callers.



def _batched_delete(col: object, ids: list[str]) -> int:
    """Delete *ids* from *col* in batches of _CHROMA_PAGE_SIZE (Cloud quota: 300).

    nexus-o8dil.45 (RDR-191 F10c follow-up): returns the sum of each batch's
    ACTUAL deleted count, not ``len(ids)``. ``col.delete()`` is duck-typed —
    the real service-mode backend (``_ServiceCollectionStub``, post
    nexus-o8dil.5) reports an ``int`` that can legitimately be less than
    requested (the server's anti-join retains a chash a live document still
    references). Test-double / legacy backends without that counting
    contract (e.g. ``InMemoryCollection``, which returns ``None`` and never
    partially refuses) fall back to ``len(batch)`` — unchanged prior
    behavior for those backends.
    """
    deleted = 0
    for i in range(0, len(ids), _CHROMA_PAGE_SIZE):
        batch = ids[i : i + _CHROMA_PAGE_SIZE]
        result = _vector_with_retry(col.delete, ids=batch)
        deleted += result if isinstance(result, int) else len(batch)
    return deleted


def _row_to_chunk_dict(row: object) -> dict:
    """Duck-typed ``ManifestRow`` (or test-double, e.g. ``SimpleNamespace``)
    -> the dict shape ``CatalogWriter.atomic_manifest_replace`` expects.
    ``getattr`` throughout (rather than ``dataclasses.asdict``) so both the
    real dataclass and lightweight test fakes carrying only a subset of
    fields work identically."""
    return {
        "chash": getattr(row, "chash", "") or "",
        "position": getattr(row, "position", None),
        "chunk_index": getattr(row, "chunk_index", None),
        "line_start": getattr(row, "line_start", None),
        "line_end": getattr(row, "line_end", None),
        "char_start": getattr(row, "char_start", None),
        "char_end": getattr(row, "char_end", None),
    }


def _prune_misclassified_in_collection(
    col: object,
    target_paths: set[Path],
    file_to_doc_id: dict[Path, str],
    *,
    kind: str,
    catalog: object | None = None,
    catalog_writer: object | None = None,
) -> int:
    """Find and delete chunks in *col* whose document matches any
    file in *target_paths*. Returns the number of chunks ACTUALLY deleted
    (an id refused by the manifest guard below does not count — F8c's
    over-report finding).

    *catalog_writer* (nexus-ch0gk): when given, ``atomic_manifest_replace``
    calls route through it instead of *catalog*. *catalog* is a
    ``make_catalog_reader()`` handle; its forwarding of an unwhitelisted
    write only works because ``_SharedServiceCatalogHandle.__getattr__``
    has no whitelist (factory.py's documented "mixed sites hold BOTH a
    reader and a writer" convention says an explicit writer is the
    sanctioned shape). ``catalog_writer=None`` falls back to *catalog* for
    the write, preserving prior behaviour for callers (tests) that only
    ever pass one object playing both roles.

    nexus-7zcv (RDR-108 Phase 4 review D-H4): the legacy path used
    ``where={"doc_id": {"$in": [batch]}}`` against chunk metadata.
    RDR-108 Phase 3 removed ``doc_id`` from chunk metadata; the
    where-filter matches nothing for Phase-3 chunks and the prune
    silently no-ops. Post-fix: when a catalog is provided, resolve
    each doc_id's chashes via the document_chunks manifest and
    delete by ``chash[:32]`` (the RDR-108 D1 natural id). Legacy
    where-filter retained as fallback for catalog-absent callers.

    Files not present in *file_to_doc_id* (legacy / pre-RDR-102 D2
    rows) are silently skipped by this prune — nexus-afudo (2026-08-05)
    deleted the per-path ``source_path`` lookup that used to cover them
    as dead code (RDR-102 D2 made it permanently unable to match any
    real chunk row). See ``_prune_misclassified_in_collection``'s
    trailing comment for the evidence and the real backstop
    (``mcp_infra._sweep_superseded_vectors`` + ``nx t3 gc``).

    F8c (nexus-o8dil.2, RDR-191): a chash's LIVE manifest reference must
    never be left dangling by this sweep. ``chash_to_docs`` / ``doc_rows``
    below are the manifest-guard's ground truth, built ONCE (when a
    catalog is present) and consulted by BOTH arms before every delete:
    an id with NO live manifest reference (the common legacy/seeded shape
    — see ``test_indexer_e2e.py::test_migration_moves_prose_from_code_to_
    docs``) is safe to delete outright, unchanged from before. An id WITH
    a live reference gets that reference removed via
    ``catalog.atomic_manifest_replace`` FIRST — only once that write
    succeeds is the T3 chunk itself deleted, so a process abort between
    the two steps leaves at worst an ORPHAN chunk (recoverable via
    ``nx t3 gc`` / ``_sweep_superseded_vectors``), never a dangling
    manifest row. A failed or unavailable manifest write REFUSES the
    delete entirely (status quo — neither reference nor chunk touched)
    rather than risk creating either failure mode.

    nexus-ch0gk: ``PgVectorRepository.delete`` (commit 936e05a0) is
    server-side anti-join-scoped and already refuses to physically remove
    a chunk any live ``catalog_document_chunks`` row still references —
    including a reference from a document OUTSIDE ``doc_ids`` that
    legitimately shares the same chash (RDR-108 content-addressed dedup:
    identical chunk text in a collection collapses to one row shared by
    every document containing it). A prior revision of this function
    added a client-side pre-check (``catalog.docs_for_chashes``, a
    catalog-wide reverse lookup) to avoid sending a delete the server
    would silently refuse; it was removed (nexus-ch0gk substantive
    critique) because the unconditional catalog-wide round trip it fired
    on every batch was measured expensive at scale (this same call class
    cost 226s / ~11% of wall at 2,133 files — nexus-qgc4b / duoak.11)
    while its only benefit was a marginally more accurate ``pruned``
    count with no downstream consumer. The server-side anti-join remains
    the sole data-safety mechanism for this case; it does not depend on
    anything in this function.
    """
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — circular-dep avoidance: nexus.db.http_vector_client

    doc_ids: list[str] = [
        d for path in target_paths if (d := file_to_doc_id.get(path, ""))
    ]

    pruned = 0

    # Manifest-guard ground truth (F8c). Populated once, below, when a
    # catalog is available; consulted by BOTH arms via _guarded_delete.
    doc_rows: dict[str, list] = {}
    chash_to_docs: dict[str, set[str]] = {}

    def _guarded_delete(ids: list[str]) -> int:
        """Delete *ids* from *col*, clearing any LIVE manifest reference
        FIRST so no dangling row can result. Returns ``_batched_delete``'s
        ACTUAL deleted count (ids refused for a failed/absent manifest write
        are NOT counted as pruned — same as before; nexus-o8dil.45: an id
        the F8c guard approved as *deletable* can still be legitimately
        anti-join-refused by the server, since ``chash_to_docs`` above is
        scoped to the doc_ids in THIS prune call, not catalog-wide. A
        mismatch between what was handed to ``_batched_delete`` and what it
        actually reports is therefore a real, if rare, signal — logged, not
        silently absorbed into an inflated count)."""
        deletable: list[str] = []
        # nexus-ch0gk: *catalog_writer* may already be CLOSED by the time
        # this runs -- the production call site's ``_migrate_writer.close()``
        # (indexer.py ~4126) fires well before the prune phase reuses this
        # same writer reference. Safe by construction: close() is a
        # documented no-op end to end (_ServiceCatalogWriter.close() /
        # _SharedServiceCatalogHandle.close(), factory.py:339-340,
        # :455-456) -- do not "fix" this by re-minting or guarding on a
        # closed check.
        _writer_source = catalog_writer if catalog_writer is not None else catalog
        for cid in ids:
            owners = chash_to_docs.get(cid)
            if not owners:
                # No manifest anywhere currently claims this id -- nothing
                # can go dangling. Delete exactly as before the F8c fix.
                deletable.append(cid)
                continue
            writer = getattr(_writer_source, "atomic_manifest_replace", None)
            ok = writer is not None
            if ok:
                for did in owners:
                    kept = [r for r in doc_rows.get(did, []) if getattr(r, "chash", None) != cid]
                    # nexus-7ygt0: RDR-191 made ``collection`` a required
                    # keyword-only argument on ``atomic_manifest_replace`` --
                    # the engine no longer infers it. Derive it from the
                    # manifest rows' OWN stamped ``.collection`` (ManifestRow,
                    # nexus-kzso5's wire field), never from ``col``'s
                    # physical_collection (Hal ruling, T2 [22364]/[22367],
                    # nexus-pewng: "writers SEND it, stamped verbatim, NO
                    # resolution"). Source rows are ``kept`` if non-empty,
                    # else the PRE-filter ``doc_rows[did]`` (covers retracting
                    # the last row, where ``kept`` is empty but the row being
                    # retracted still carries the correct collection).
                    source_rows = kept if kept else doc_rows.get(did, [])
                    observed_collections = {getattr(r, "collection", None) for r in source_rows}
                    resolved_collection = (
                        next(iter(observed_collections))
                        if len(observed_collections) == 1
                        else None
                    )
                    if not resolved_collection:
                        # Either no rows to derive from, an older engine
                        # that never sent the wire field (None present), or
                        # a mixed-collection manifest (nexus-pewng
                        # territory, unreachable today). Do NOT guess and
                        # do NOT fall back to physical_collection -- refuse
                        # fail-visibly with a DISTINCT event name so this
                        # refusal class is distinguishable from a write
                        # failure.
                        ok = False
                        _log.warning(
                            "prune_misclassified_manifest_guard_collection_unresolved",
                            doc_id=did, chash=cid,
                            collection=getattr(col, "name", "?"),
                            kind=kind,
                            observed_collections=sorted(
                                c for c in observed_collections if c
                            ),
                            note="manifest rows carry no single resolvable "
                                 "collection -- refusing to guess or fall "
                                 "back to physical_collection (RDR-191: "
                                 "writers send it, stamped verbatim, zero "
                                 "resolution)",
                        )
                        break
                    try:
                        writer(
                            did,
                            [_row_to_chunk_dict(r) for r in kept],
                            collection=resolved_collection,
                        )
                    except Exception:  # noqa: BLE001 — refuse rather than risk a dangling manifest row
                        ok = False
                        _log.warning(
                            "prune_misclassified_manifest_guard_write_failed",
                            doc_id=did, chash=cid,
                            collection=getattr(col, "name", "?"),
                            kind=kind, exc_info=True,
                        )
                        break
                    doc_rows[did] = kept
            if ok:
                deletable.append(cid)
            else:
                _log.warning(
                    "prune_misclassified_manifest_guard_refused_delete",
                    chash=cid, owners=sorted(owners),
                    collection=getattr(col, "name", "?"), kind=kind,
                    note="no writer available or the manifest write failed "
                         "-- refusing to delete rather than orphan the "
                         "manifest reference",
                )
        if not deletable:
            return 0
        actual = _batched_delete(col, deletable)
        if actual < len(deletable):
            _log.warning(
                "prune_misclassified_anti_join_partial_delete",
                requested=len(deletable), actual=actual,
                collection=getattr(col, "name", "?"), kind=kind,
                note="the server's anti-join refused some chashes the F8c "
                     "manifest guard believed had no live reference -- the "
                     "guard's chash_to_docs ground truth is scoped to this "
                     "prune call's doc_ids, not catalog-wide, so a live "
                     "reference elsewhere is invisible to it",
            )
        return actual

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
        # nexus-8g79.4: bare ``continue`` on get_manifest failure silently
        # skipped doc_ids whose manifest lookup raised (catalog miss,
        # transient SQLite error). Those chunks were then never pruned
        # from T3 — over successive runs T3 grew with unreachable orphan
        # chunks. Log at WARNING with the doc_id so operators see the
        # affected scope; count for the post-run summary.
        skipped_doc_ids: dict[str, str] = {}
        # nexus-yz8bt (duoak.11 sink #3): resolve every doc's manifest in
        # ONE batched call instead of a per-doc serial loop. At index
        # scale (a --force run registers ~N docs) the old loop paid N
        # sequential catalog round-trips — 226.6s on the 2,133-file gate,
        # ~11% of wall — to prove a mostly-empty prune. ``get_manifests``
        # (nexus-7lm3q, backed by /manifest/get_many) is internally paged
        # and returns one dict; a doc ABSENT from the result has no
        # manifest rows, i.e. nothing to prune (manifest is the canonical
        # chunk list, so absent == no chunks anywhere == safe skip). The
        # batch fails loud on any page error (its documented contract), so
        # a failure falls back to the per-doc loop below — byte-identical
        # to the pre-batch behaviour, preserving the skipped-doc accounting.
        # The try guards ONLY the RPC (its contract is "a page failure
        # PROPAGATES"). Row processing stays OUTSIDE it — exactly as the
        # original per-doc loop kept ``for row in manifest`` outside its
        # ``get_manifest`` try — so a malformed ``ManifestRow`` surfaces as
        # a local bug instead of being masked as an infra failure and
        # silently downgraded to the O(N) fallback.
        manifests = None
        try:
            manifests = catalog.get_manifests(doc_ids)
        except Exception:  # noqa: BLE001 — batch failed loud; fall back to the resilient per-doc path
            _log.warning(
                "prune_misclassified_batch_manifest_failed_falling_back_per_doc",
                collection=getattr(col, "name", "?"),
                kind=kind,
                doc_count=len(doc_ids),
                exc_info=True,
            )
        if manifests is not None:
            for did in doc_ids:
                rows = manifests.get(did)
                if rows is None:
                    continue
                doc_rows[did] = list(rows)  # F8c manifest-guard ground truth
                for row in rows:
                    if row.chash:
                        chash_to_docs.setdefault(row.chash, set()).add(did)
        else:
            for did in doc_ids:
                try:
                    manifest = catalog.get_manifest(did)
                except Exception as exc:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
                    skipped_doc_ids[did] = f"{type(exc).__name__}: {exc}"
                    _log.warning(
                        "prune_misclassified_manifest_lookup_failed",
                        doc_id=did,
                        collection=getattr(col, "name", "?"),
                        kind=kind,
                        exc_info=True,
                    )
                    continue
                doc_rows[did] = list(manifest)  # F8c manifest-guard ground truth
                for row in manifest:
                    if row.chash:
                        chash_to_docs.setdefault(row.chash, set()).add(did)
        all_natural_ids: list[str] = list(chash_to_docs.keys())
        # Batched ``col.get`` to fetch the present subset, then batched
        # delete. _CHROMA_PAGE_SIZE caps the ids list per call.
        for i in range(0, len(all_natural_ids), _CHROMA_PAGE_SIZE):
            batch_ids = all_natural_ids[i : i + _CHROMA_PAGE_SIZE]
            if not batch_ids:
                continue
            try:
                present = col.get(ids=batch_ids, include=[])
            except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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
                n = _guarded_delete(list(present_ids))
                pruned += n
                _log.debug(
                    f"pruned misclassified chunks from {kind} collection (manifest)",
                    count=n,
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
            except VectorServiceError as exc:
                # nexus-ou4tb walk: a degraded service is NOT "no
                # matching rows" — isolate the batch but say so. Prune
                # stays best-effort. (The sibling legacy source_path arm
                # this comment used to reference was deleted as dead
                # code — nexus-afudo, 2026-08-05.)
                _log.warning(
                    "legacy_prune_in_query_failed",
                    batch_size=len(batch),
                    error=str(exc),
                )
                continue
            except Exception:  # noqa: BLE001 — boundary catch of undocumented third-party exceptions; non-fatal
                # Some Chroma deployments reject ``$in`` on absent keys;
                # treat as empty result (logged for the nexus-ou4tb audit
                # trail — this arm was fully silent before).
                _log.debug("legacy_prune_in_query_rejected", exc_info=True)
                continue
            if existing["ids"]:
                # F8c manifest guard (nexus-o8dil.2): an id here MAY be one
                # this same catalog's manifest still lists (e.g. a legacy
                # Phase-2-shaped chunk whose native id happens to equal a
                # live chash) -- _guarded_delete consults chash_to_docs and
                # clears that reference first. An id with no live reference
                # (the common seeded/legacy shape) is unaffected.
                n = _guarded_delete(list(existing["ids"]))
                pruned += n
                _log.debug(
                    f"pruned misclassified chunks from {kind} collection (doc_id-keyed)",
                    count=n,
                    doc_id_batch_size=len(batch),
                )

    # nexus-afudo (2026-08-05): the legacy source_path where-filter loop
    # (files absent from file_to_doc_id) DELETED as dead code. RDR-102 D2
    # (83ac62c7, 2026-05-02) hard-removed source_path from
    # make_chunk_metadata — the single factory every writer routes
    # through — so `where={"source_path": src}` matched zero rows for
    # any chunk written since then, regardless of how many files landed
    # in legacy_paths. A live-store probe (field>=! existence test)
    # found zero source_path rows across 13 representative collections
    # (~115k chunks), extending nexus-tbkk1's original 6-collection
    # probe to the code__/docs__ collections this exact prune targets.
    # The real cross-document protection is mcp_infra.
    # _sweep_superseded_vectors (manifest-diff, fires on every re-index)
    # plus nx t3 gc (chash-vs-manifest, RDR-108 Phase 4) as the
    # comprehensive manual backstop for manifest-absent legacy rows —
    # same framing nexus-tbkk1 used for the sibling doc_indexer.py /
    # pipeline_stages.py sites this bead closes the remainder of.

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
    catalog_writer: object | None = None,
) -> None:
    """Remove chunks from the wrong collection after reclassification.

    If a file was previously classified as code but is now prose (or vice versa),
    its chunks in the old collection must be removed.

    nexus-dcym: when ``file_to_doc_id`` is supplied (always populated by
    the catalog hook), the chunk-prune lookup keys on ``doc_id``. Files
    not in the map are silently skipped — nexus-afudo (2026-08-05)
    deleted the legacy ``source_path`` lookup they used to fall back
    to as dead code (RDR-102 D2 made it permanently unable to match a
    real chunk row).

    Pre-batching history: this function used to do one
    ``col.get(where={"doc_id": <id>})`` per file. For ART (~4,800 files)
    that meant ~9,600 sequential ChromaDB Cloud roundtrips at
    50-200 ms each — 8 to 30 minutes of pure round-trip cost where the
    actual work (chunks to delete) was almost always zero. The batched
    ``where={"doc_id": {"$in": [batch]}}`` form collapses that to
    ~ceil(N / _CHROMA_PAGE_SIZE) queries per direction (~34 total for
    ART) — about a 300x reduction in roundtrips.
    """
    from nexus.errors import collection_not_found_errors  # noqa: PLC0415 — deferred import (RDR-155 P4b P0c: substrate-neutral missing-collection contract)
    from tqdm import tqdm  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

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
        except collection_not_found_errors():
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
                catalog=catalog, catalog_writer=catalog_writer,
            )
        bar.set_postfix_str(f"chunks={pruned_chunks}", refresh=False)
        bar.update(1)

        if docs_col is not None:
            pruned_chunks += _prune_misclassified_in_collection(
                docs_col, docs_targets, file_to_doc_id, kind="docs",
                catalog=catalog, catalog_writer=catalog_writer,
            )
        bar.set_postfix_str(f"chunks={pruned_chunks}", refresh=False)
        bar.update(1)
    finally:
        bar.close()


def _prune_collection_serverside(
    db: object, collection_name: str, quarantine_name: str, quarantined_at: str,
) -> bool:
    """RDR-191 Phase 1: attempt restore + quarantine + expire entirely
    server-side (``nexus.gc_restore_rereferenced`` / ``gc_quarantine_orphans``
    / ``gc_expire_quarantine``, catalog-023) for one collection. Zero chunk
    rows and zero embeddings cross the wire on this path.

    Returns ``True`` when the quarantine leg (the expensive one this RDR
    targets — the old path's full-collection metadata read plus
    embedding-carrying copy) ran server-side: the caller treats this
    collection as DONE for this pass. Returns ``False`` when the route is
    unavailable (an engine predating the ``gc_quarantine_orphans`` /
    ``gc_restore_rereferenced`` / ``gc_expire_quarantine`` server-side
    routes — catalog-023-quarantine-functions.xml, which has shipped since
    it merged; naming the changeset rather than a version pin here so this
    docstring cannot go stale the way its predecessor did — or a non-HTTP
    ``db``, e.g. local/in-memory mode).

    RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: the caller
    (``_prune_deleted_files``) NO LONGER falls back to a client-side path
    on ``False`` — that fetch-diff-copy-delete fallback is retired (the
    manifest-chunk FK makes the completeness apparatus it existed to prove
    correct unreachable by construction). A ``False`` return now means the
    caller logs a loud WARNING
    (``gc_serverside_prune_unavailable_no_client_fallback``) and leaves
    this collection UNPRUNED for the pass — never a silent no-op, and
    never a fallback read/write. Every currently supported deployment has
    these routes (``REQUIRED_ENGINE_VERSION`` is far past catalog-023, and
    every mode is PG-via-engine per the NO-SQLite directive), so ``False``
    is not expected to fire in practice any more.

    The restore leg running server-side even when the quarantine leg's
    route then turns out to be unavailable (forcing an overall ``False``
    return) is still safe and non-wasteful: restore only moves chashes the
    manifest ALREADY references back to origin, unconditionally correct
    regardless of what the caller does next with the collection.

    Expire-route unavailability after a successful server-side quarantine
    (an engine inexplicably missing the sibling route — should not occur
    since all three ship in the same changeset) is logged and skipped, not
    treated as a fallback trigger: matches the OLD code's own best-effort
    tolerance for ``expire_quarantine`` failures (``gc_expiry_pass_failed``).
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    from nexus.catalog.chunk_quarantine import (  # noqa: PLC0415 — deferred import
        expire_quarantine_serverside,
        quarantine_days,
        quarantine_orphans_serverside,
        restore_rereferenced_serverside,
    )

    restored = restore_rereferenced_serverside(db, quarantine_name, collection_name)
    if restored is None:
        return False  # route unavailable — client-side path handles restore too

    quarantined = quarantine_orphans_serverside(
        db, collection_name, quarantine_name, quarantined_at, sample_limit=20,
    )
    if quarantined is None:
        return False  # route unavailable
    moved, sample = quarantined

    if moved:
        _log.info(
            "gc_pruned_orphan_chunks_serverside",
            collection=collection_name, count=moved, restored=restored,
            sample=sample, mode="quarantine-serverside",
        )

    import os  # noqa: PLC0415 — deferred: branch-local, matches chunk_quarantine.py's own NX_GC_FORCE read
    force = os.environ.get("NX_GC_FORCE", "") == "1"
    cutoff = (
        datetime.now(UTC) - timedelta(days=quarantine_days())
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    expired = expire_quarantine_serverside(
        db, quarantine_name, collection_name, cutoff,
        floor_fraction=_gc_floor_fraction(), floor_min_chunks=_GC_FLOOR_MIN_CHUNKS,
        force=force,
    )
    if expired is None:
        _log.warning(
            "gc_expire_quarantine_route_missing_after_quarantine_succeeded",
            collection=collection_name,
        )
    return True


def _prune_deleted_files(
    code_collection: str,
    docs_collection: str,
    db: object,
    *,
    catalog: object | None,
    rdr_collection: str | None = None,
    on_phase: Callable[[str], None] | None = None,
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

    ``on_phase`` (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15: now UNUSED —
    kept in the signature so this function's ONE caller needs no edit).
    It used to receive a liveness ping for the single-shot
    ``get_all_metadata`` read and real page progress for a client-side
    fetch-diff-copy-delete fallback sweep (index-output-ux-assessment-
    2026-08-10 §7.2/a3: the 547.8s observed run of this function was 96
    pages over 28,564 chunks with zero output). That entire fallback —
    the 422-not-truncate contract, the cap-boundary test, the alive-set
    count reconciliation, the per-collection isolation, the copy-then-
    delete quarantine discipline it existed to prove correct — is RETIRED:
    it existed solely to prevent an incomplete/incorrect alive-set read
    from misclassifying a live chunk as orphan and deleting it, leaving a
    dangling manifest row; the manifest-chunk FK (catalog-029, VALIDATEd)
    now REJECTS that state at the database, making the completeness-
    proving apparatus unreachable by construction. Server-side prune
    (``_prune_collection_serverside``, RDR-191 Phase 1,
    ``nexus.gc_quarantine_orphans``/``gc_restore_rereferenced``/
    ``gc_expire_quarantine``, catalog-023) is the ONLY prune path left; a
    collection whose engine predates those routes now goes UNPRUNED with a
    loud WARNING (``gc_serverside_prune_unavailable_no_client_fallback``),
    never a silent no-op — every currently supported deployment has them
    (REQUIRED_ENGINE_VERSION is far past catalog-023).
    """
    if catalog is None:
        return
    # nexus-ks40: read-only GC must use get_collection so an absent
    # T3 collection is a clean skip, not a speculative empty creation
    # (the latter is the leak that fed the doctor's "T3 collections
    # without projection rows" zombie list).
    from nexus.errors import collection_not_found_errors  # noqa: PLC0415 — deferred import (RDR-155 P4b P0c: substrate-neutral missing-collection contract)
    # nexus-3lswy: rdr_collection was missing here even after RDR became a
    # first-class 4th collection (catalog registration, doc_id_resolver, and
    # staleness cache all cover it) — this GC pass silently never swept
    # rdr__ orphans. None when there are no RDR files this run (mirrors
    # code_collection/docs_collection, which are always non-empty strings
    # today but would have the same silent-skip contract if empty).
    _collections = (code_collection, docs_collection) + (
        (rdr_collection,) if rdr_collection else ()
    )
    _n_collections = len(_collections)
    for _coll_idx, collection_name in enumerate(_collections, start=1):
        # nexus-ir6eh: the alive-set read is count-reconciled client-side
        # (HttpCatalogClient.chashes_for_collection raises on a truncated
        # or count-stripped response). GC must never classify orphans off
        # an unverifiable list — a truncated list reads live chunks as
        # orphans and deletes real data — so a failed read skips THIS
        # collection with a structured error (never a silent continue,
        # never a sweep-wide abort; same per-collection isolation as the
        # nexus-ou4tb degraded-read guard below).
        try:
            referenced = catalog.chashes_for_collection(collection_name)
        except Exception:  # noqa: BLE001 — one collection's failed alive-set read must not end the sweep
            # .error, not the .warning its sibling skip-guards below use:
            # those absorb transient READ failures, whereas this fires on a
            # count-reconciliation breach — a contract violation by the
            # engine or an interposing hop, which is never routine.
            _log.error(
                "gc_manifest_read_failed_skipping_collection",
                collection=collection_name,
                exc_info=True,
            )
            continue
        try:
            col = db.get_collection(collection_name)
        except collection_not_found_errors():
            continue

        # nexus-oqku (moved up for RDR-191 P1): when the catalog manifest
        # has zero referenced chashes for a collection, every chunk would
        # read as orphan and the sweep below would delete the entire
        # collection. This fires on the first ``nx index repo`` after the
        # RDR-108 schema migration lands on a system that has not yet run
        # manifest backfill. An empty manifest cannot prove ANY chunk is
        # live or dead; skip and log instead of a silent full-collection
        # wipe. Checked HERE (before either path) because it must gate the
        # RDR-191 server-side anti-join too — that SQL would otherwise read
        # "no manifest rows" as "every chunk is unreferenced" just as
        # readily as the client-side diff would.
        if not referenced:
            # Best-effort diagnostic count only — col.count() is the same
            # cheap COUNT(*) the page-progress denominator above uses, NOT
            # the expensive full-metadata read this guard exists to avoid
            # paying before we know it's safe to. Never raises into the
            # warning path; a test double or degraded read just omits it.
            try:
                _t3_chunks = col.count()
            except Exception:  # noqa: BLE001 — diagnostic only
                _t3_chunks = None
            _log.warning(
                "manifest_empty_skipping_gc",
                collection=collection_name,
                **({"t3_chunks": _t3_chunks} if isinstance(_t3_chunks, int) else {}),
                note=(
                    "catalog manifest has zero referenced chashes for this "
                    "collection. GC cannot safely decide orphans without a "
                    "manifest. Run a fresh `nx index repo` (populates "
                    "manifest via post-store hook) or backfill manually "
                    "before retrying GC."
                ),
            )
            continue

        # RDR-191 Phase 1: try the server-side anti-join prune first — zero
        # chunk rows, zero embeddings cross the wire (nexus.gc_quarantine_orphans
        # / gc_restore_rereferenced / gc_expire_quarantine, catalog-023).
        # Falls back to the client-side fetch-diff-copy-delete path below
        # when the engine predates the route (404) or `db` has no HTTP
        # capability (local/in-memory mode).
        from nexus.catalog.chunk_quarantine import (  # noqa: PLC0415 — deferred import
            now_stamp, quarantine_collection_name,
        )
        qname = quarantine_collection_name(collection_name)
        # nexus-ou4tb contract (see the comment below): a degraded read must
        # skip THIS collection, not abort the sweep for every collection after
        # it. `_prune_collection_serverside` returns False for the benign
        # "engine predates the route / no HTTP capability" case and re-raises
        # everything else — including a bare ConnectionError/TimeoutError from
        # `_post` on a local-mode blip. Unwrapped, one transient hiccup on one
        # collection would abort the whole prune AND every pipeline phase after
        # it, where the legacy arms below merely log and continue.
        try:
            if _prune_collection_serverside(db, collection_name, qname, now_stamp()):
                continue
        except Exception:  # noqa: BLE001 — best-effort; failure logged, sweep continues
            _log.warning("gc_serverside_prune_failed", collection=collection_name,
                         exc_info=True)
            continue

        # RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: the client-side
        # fetch-diff-copy-delete fallback (fetch every chunk's metadata,
        # diff against the alive set in Python, quarantine/restore/expire
        # via chunk_quarantine.py's non-serverside client-loop functions)
        # is RETIRED — the 422-not-truncate contract, the cap-boundary
        # test, the alive-set count reconciliation, the per-collection
        # isolation, and the copy-then-delete quarantine discipline this
        # apparatus existed to prove ALL existed for one reason: an
        # incomplete/incorrect alive-set read here could misclassify a
        # LIVE chunk as orphan and DELETE it, leaving a dangling manifest
        # row — exactly the defect class the manifest-chunk FK
        # (catalog-029, VALIDATEd) now REJECTS at the database, converting
        # a silent-corruption failure mode into a loud constraint
        # violation. The completeness-proving apparatus that only existed
        # to prevent that silent failure is therefore unreachable by
        # construction. `_prune_collection_serverside` returning False
        # (an engine predating catalog-023's quarantine routes, or a
        # non-HTTP `db`) is now a genuine, loud, unrecovered gap for this
        # collection — not a silent no-op — since every currently
        # supported deployment (REQUIRED_ENGINE_VERSION floor is well
        # past catalog-023, and every mode is PG-via-engine per the
        # NO-SQLite directive) has the server-side routes available.
        _log.warning(
            "gc_serverside_prune_unavailable_no_client_fallback",
            collection=collection_name,
            detail=(
                "server-side prune routes are unavailable for this "
                "collection (engine predates catalog-023, or db has no "
                "HTTP capability) and the client-side fallback sweep is "
                "retired (RDR-191 Phase 6, nexus-o8dil.33) — this "
                "collection was NOT pruned this pass. Upgrade the engine "
                "this install is pointed at."
            ),
        )


# ── Main indexing pipeline ───────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    """Compact human duration: 42s / 1m30s / 2h05m (nexus-zedf7)."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _drain_batcher_with_markers(
    batcher: "ChunkBatcher",
    on_phase: Callable[[str], None] | None,
) -> int:
    """Drain *batcher* with operator-visible phase markers (nexus-uizok).

    The end-of-run drain previously ran DARK for multiple minutes on big
    repos (hundreds of upsert round-trips after "RDR indexing done") —
    indistinguishable from a hang. Emits an opening marker with the
    pending summary, a heartbeat per completed flush, and a closing
    marker with drain-scoped flush count + wall. Quiet drains (nothing
    pending, nothing in flight) stay silent — no phantom markers.

    Returns the drain's flush count (``batcher.drain``'s return).
    """
    pend = batcher.pending_summary
    busy = bool(pend["chunks"] or pend["in_flight"])
    t0 = time.monotonic()
    progress = None
    if on_phase is not None and busy:
        parts = [
            f"{pend['chunks']:,} staged chunks across "
            f"{pend['collections']} collections"
        ]
        if pend["in_flight"]:
            parts.append(f"{pend['in_flight']} in-flight batches")
        on_phase(f"Flushing {' + '.join(parts)}…")

        # nexus-lde88 G2: the printed "(Xs" used to be time.monotonic() - t0
        # — CUMULATIVE since the drain started, not this flush's own
        # duration — so three consecutive flushes printed the identical
        # cumulative-so-far number and read as per-flush timing. Track the
        # PREVIOUS flush's timestamp and print the per-flush delta instead;
        # the rate/ETA projection below still needs the cumulative wall,
        # so that arithmetic is unchanged.
        #
        # CAVEAT (review round 2, Suggestion): this delta is exact only at
        # ``flush_concurrency=1`` — the ChunkBatcher default here is >1 (3,
        # via ``_run_index``'s batcher construction), so ``on_progress``
        # fires in the ORDER concurrent flushes complete
        # (``as_completed()`` in ``ChunkBatcher.drain``), not the order
        # they started. The delta is therefore the time between two
        # JOINER OBSERVATIONS at this callback, not necessarily that one
        # flush's own individual wall-clock duration — e.g. three flushes
        # that ran mostly in parallel and land within a tight window print
        # small deltas that don't individually represent ~1/3 of the real
        # per-flush cost each. Still strictly more honest than the old
        # cumulative-since-drain-start number (which was wrong in the same
        # direction for EVERY flush, not just under concurrency), and
        # exact at concurrency=1 (the drain-phase default when
        # ``flush_pool`` is unset). For the ACTUAL per-flush attribution
        # under concurrency, use the per-flush ``chunk_flush_complete``
        # structlog event (nexus-lde88 G1) instead of this progress line.
        _last_flush_t = [t0]

        def progress(done: int, total: int) -> None:
            # nexus-zedf7: rolling rate + projected remaining, so an hour of
            # legitimate server-side embedding never reads as a hang.
            now = time.monotonic()
            elapsed = now - t0  # cumulative — feeds the rate/ETA projection only
            per_flush = now - _last_flush_t[0]
            _last_flush_t[0] = now
            line = f"  flush {done}/{total} complete ({per_flush:.1f}s"
            if 0 < done < total and elapsed > 0:
                rate = done / elapsed
                line += f", {rate * 60:.1f} flushes/min"
                line += f", ~{_fmt_duration((total - done) / rate)} remaining"
            # nexus-4s1ww / GH #1432: surface failures AS THEY HAPPEN during
            # the drain, not only in the end-of-run summary — the drain can
            # run for minutes, and a batcher-less duck-typed stand-in (e.g.
            # test doubles) has no ``failed_files`` at all, hence the
            # defensive getattr instead of a hard attribute access.
            _failed_so_far = len(getattr(batcher, "failed_files", {}) or {})
            if _failed_so_far:
                line += f", {_failed_so_far} file(s) failed"
            on_phase(line + ")")
    flushed = batcher.drain(on_progress=progress)
    if on_phase is not None and busy:
        _failed_final = len(getattr(batcher, "failed_files", {}) or {})
        _fail_suffix = (
            f" — {_failed_final} file(s) failed to flush" if _failed_final else ""
        )
        on_phase(
            f"Flush drain complete — {flushed} flushes, "
            f"{time.monotonic() - t0:.1f}s{_fail_suffix}"
        )
    return flushed


def _aggregate_flush_metas(file_contexts: list) -> list[dict]:
    """Rebuild a flush's chunk metadata with doc_id + file-local
    chunk_index injected (post-RDR-108 chunk metadata carries neither).

    Shared, module-level (nexus-wxjr6 — moved out of ``_run_index``'s
    nested closures so it is independently testable), by
    ``_fire_flush_grain_hooks`` (taxonomy/manifest dispatch) and
    ``_build_combined_write_payload`` below (the combined-write's
    manifest-row grouping) — both closures need the IDENTICAL
    doc_id/position assignment. Pure: no closure state, only
    ``file_contexts`` (the same ``(path, context)`` pairs
    ``on_batch_begin``/``on_batch_complete`` already receive).
    """
    agg: list[dict] = []
    for _path, _c in file_contexts:
        if not isinstance(_c, dict):
            continue
        for _j, _m in enumerate(_c["metadatas"]):
            agg.append({
                **_m,
                "doc_id": _c["catalog_doc_id"],
                "chunk_index": _j,
            })
    return agg


class _CombinedWritePositionZeroViolation(RuntimeError):
    """Raised by :func:`_build_combined_write_payload` when a doc's batch
    lacks position 0 — see that function's docstring for why this is a
    defended invariant, not a routed branch."""


def _build_combined_write_payload(
    ids: list, docs: list, metas: list, file_contexts: list,
) -> "tuple[list[dict], list[tuple[str, list[dict]]], dict[str, str], list[str], list[str], list[dict]]":
    """Build the nexus-wxjr6 combined-write request pieces from one
    ChunkBatcher flush. Pure — no network calls, no catalog dependency —
    so it is unit-testable independent of ``_batch_flush``'s I/O.

    Returns ``(chunks_payload, full_docs, complete_map, orphan_ids,
    orphan_docs, orphan_metas)``:

    * ``chunks_payload`` — ``[{chash, text, metadata}, ...]`` for chunks
      belonging to a file that HAS catalog identity, deduped by chash
      (=id for T3, RDR-180), first occurrence wins — matches the engine's
      own dedup discipline (design memo §1.1: "chunk text is carried ONCE
      at the top level, keyed by chash").
    * ``full_docs`` — ``[(doc_id, rows), ...]`` for every doc in this
      flush whose batch includes position 0 (ChunkBatcher's file-atomic
      contract guarantees this for every REAL doc — see the
      ``_CombinedWritePositionZeroViolation`` raise below).
    * ``complete_map`` — ``{doc_id: content_hash}`` restricted to
      ``full_docs``' own ids, for the completion-stamp ride-along
      (nexus-5xn3k.4).
    * ``orphan_ids``/``orphan_docs``/``orphan_metas`` — chunks belonging
      EXCLUSIVELY to files with NO catalog identity (code review Critical
      C1, 2026-08-09, review T2 [22014]): the engine's
      ``upsertManifestChunkVectors`` (``CatalogRepository.java`` ~3966-
      3984) only persists chashes referenced by a doc's OWN manifest
      ``rows`` — the ``resolved`` chash map it receives is the FULL
      flush-wide set, but only entries a REFERENCING doc names ever get
      written. Before this fix, a mixed flush (some files with catalog
      identity, some without — routine: ``code_indexer.py`` resolves
      ``catalog_doc_id`` per FILE and stages the file into the batcher
      regardless of whether resolution succeeded) sent EVERY file's
      content in ``chunks_payload`` while ``full_docs`` only ever covered
      identity-having docs: identity-less content was embedded (Voyage
      cost paid) then silently discarded server-side — no log, no
      counter, no error, and a REGRESSION vs pre-commit behavior (that
      content used to land in T3 via the old direct upsert-chunks call,
      orphaned-but-searchable). The caller (``_batch_flush``) sends these
      through the OLD ``db.upsert_chunks_with_embeddings`` call, exactly
      preserving the pre-commit orphaned-but-searchable behavior for
      files this function cannot manifest.

      A chash claimed by BOTH an identity file and an identity-less file
      (duplicate content across files, routine boilerplate) rides BOTH
      paths (nexus-3mwuo, C1-residual from the wxjr6 delta re-review, T2
      review-wxjr6-client-2026-08-09 [22014]): it stays in
      ``chunks_payload`` (the cheap, common-case-correct path — safely
      covered by the identity file's manifest reference IF that file's
      per-doc write succeeds) AND is ALSO included in
      ``orphan_ids``/``orphan_docs``/``orphan_metas``. This split is
      computed client-side, before the request is sent, so it cannot
      react to what happens to any individual doc's per-doc transaction
      server-side — if the identity file's doc lands in the response's
      ``failed_doc_ids`` (a real, already-modeled outcome), a chash
      staked ONLY on that doc's manifest reference would be embedded
      (cost paid) then referenced by no committed doc, lost for both
      files with no recovery path. Duplicating the shared chash into the
      legacy upsert call closes that corner: the legacy path is
      idempotent server-side (``ON CONFLICT`` per chash/collection), so
      the redundant write in the common case (identity doc succeeds) is
      a cheap, safe cost, and the orphan copy survives regardless of the
      identity doc's outcome.

    Raises :class:`_CombinedWritePositionZeroViolation` if any
    identified doc's batch lacks position 0 — a DEFENDED invariant, not
    a routed branch: unlike ``_manifest_write_loop`` (also called for
    streaming producers that genuinely emit continuation slices), the
    ChunkBatcher flush path has exactly one producer and that producer
    guarantees the invariant by construction (module docstring — "a
    file's chunks never straddle a flush boundary"). A violation here is
    a real bug upstream, not routine continuation traffic, so this fails
    loud rather than silently routing it onto a path this function does
    not implement (nexus-wxjr6 non-goal guard; no silent fallbacks for
    correctness, CLAUDE.md).
    """
    from collections import defaultdict  # noqa: PLC0415 — stdlib deferred to function scope

    from nexus.mcp_infra import _manifest_chunk_rows  # noqa: PLC0415 — deferred: avoid module-load cross-import

    # Split by catalog identity FIRST (code review Critical C1): a chash
    # is orphan-eligible if ANY file that stages it lacks catalog
    # identity — a chash claimed EXCLUSIVELY by identity-having files
    # never touches the orphan path (it's cheaply, safely referenceable
    # via chunks_payload alone).
    identity_ids: set[str] = set()
    orphan_candidate_ids: set[str] = set()
    for _path, _c in file_contexts:
        if not isinstance(_c, dict):
            continue
        target = identity_ids if _c.get("catalog_doc_id") else orphan_candidate_ids
        target.update(_c.get("ids") or [])
    orphan_only_ids = orphan_candidate_ids - identity_ids
    # nexus-3mwuo: a chash claimed by BOTH an identity file and an
    # identity-less file — rides BOTH paths (see docstring). Distinct
    # from orphan_only_ids: those chashes skip chunks_payload entirely;
    # shared_ids stay in chunks_payload too.
    shared_ids = orphan_candidate_ids & identity_ids

    seen_chash: set[str] = set()
    # Dedup ONLY the shared-chash orphan copy (first occurrence wins,
    # mirroring chunks_payload's own dedup) — a shared chash necessarily
    # appears once per claiming file (once from its identity file, once
    # from its orphan file), and one legacy-upsert copy is enough to
    # close the recovery gap. orphan_only_ids intentionally keeps its
    # pre-existing no-dedup behavior (unchanged by nexus-3mwuo; out of
    # this fix's scope).
    seen_shared_orphan: set[str] = set()
    chunks_payload: list[dict] = []
    orphan_ids: list[str] = []
    orphan_docs: list[str] = []
    orphan_metas: list[dict] = []
    for _cid, _cdoc, _cmeta in zip(ids, docs, metas):
        if _cid in orphan_only_ids:
            orphan_ids.append(_cid)
            orphan_docs.append(_cdoc)
            orphan_metas.append(_cmeta)
            continue
        if _cid in shared_ids and _cid not in seen_shared_orphan:
            seen_shared_orphan.add(_cid)
            orphan_ids.append(_cid)
            orphan_docs.append(_cdoc)
            orphan_metas.append(_cmeta)
        if _cid in seen_chash:
            continue
        seen_chash.add(_cid)
        chunks_payload.append({"chash": _cid, "text": _cdoc, "metadata": _cmeta})

    agg_metas = _aggregate_flush_metas(file_contexts)
    by_doc: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for _i, _meta in enumerate(agg_metas):
        _doc_id = _meta.get("doc_id", "")
        if _doc_id:
            by_doc[_doc_id].append((_i, _meta))

    full_docs: list[tuple[str, list[dict]]] = []
    for _doc_id, _indexed_metas in by_doc.items():
        _chunks = _manifest_chunk_rows(_indexed_metas)
        if not any(_c["chash"] for _c in _chunks):
            # Review Suggestion (T2 review-wxjr6-client-2026-08-09
            # [22014]): this exclusion isn't mirrored by the identity/
            # orphan split above — the split only checks
            # ``catalog_doc_id`` truthiness, not chash validity, so a
            # doc WITH catalog identity but zero non-empty chashes here
            # would still have its file classified ``identity`` (not
            # ``orphan``) by the split. Structurally unreachable via the
            # real call path today: ``chunk_text_hash`` is populated
            # unconditionally per chunk (``code_indexer.py``:
            # ``chunk_text_hash=chunk_text_hash_full``), so ``_chunks``
            # cannot be all-empty for a doc that actually staged
            # content. Not defended with a raise (unlike the position-0
            # invariant below) since this branch legitimately fires for
            # an empty ``_indexed_metas`` group, which is not itself a
            # bug.
            continue
        if not any(_c["position"] == 0 for _c in _chunks):
            raise _CombinedWritePositionZeroViolation(
                f"combined-write flush: doc {_doc_id!r} batch lacks "
                "position 0 — ChunkBatcher's file-atomic contract "
                "guarantees every flushed file is whole; this violates "
                "that invariant and must not be silently routed onto "
                "the continuation-append path (nexus-wxjr6 non-goal "
                "guard)"
            )
        full_docs.append((_doc_id, _chunks))

    full_ids = {d for d, _ in full_docs}
    complete_map = {
        _c["catalog_doc_id"]: _c["metadatas"][0].get("content_hash", "")
        for _path, _c in file_contexts
        if isinstance(_c, dict) and _c.get("catalog_doc_id") in full_ids
        and _c.get("metadatas")
        and _c["metadatas"][0].get("content_hash")
    }
    return chunks_payload, full_docs, complete_map, orphan_ids, orphan_docs, orphan_metas


def _run_index(
    repo: Path,
    registry: "object",
    chunk_lines: int | None = None,
    *,
    force: bool = False,
    force_stale: bool = False,
    since_head: bool = False,
    on_locked: str = "wait",
    on_start: Callable[[int], None] | None = None,
    on_file: Callable[[Path, int, float], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_flush: "Callable[[int, int, str, float, str | None], None] | None" = None,
    on_stage_timers: Callable[[Path, "StageTimers"], None] | None = None,
    hooks: "HookRegistry | None" = None,
) -> dict[str, int]:
    """Full indexing pipeline: classify → route → embed → upsert → prune.

    Routes files to the appropriate collection based on content classification:
    - Code files → code__ collection (voyage-code-3, AST chunking)
    - Prose files → docs__ collection (voyage-context-3 via CCE)
    - PDF files → docs__ collection (PDF extraction + voyage-context-3)
    - RDR markdown → rdr__ collection (nexus-3lswy: 4th run_file_loop
      category via _index_prose_file, same batching/staleness-cache/
      doc_id_resolver machinery as the prose loop)

    Returns a stats dict with ``rdr_indexed``, ``rdr_current``, ``rdr_failed``.
    """
    from nexus.classifier import ContentClass, classify_file, looks_like_binary_content  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.config import load_config  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.frecency import batch_frecency  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.ripgrep_cache import build_cache  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    info = registry.get(repo)
    if info is None:
        return {}

    # The per-op catalog counters are a process-lifetime global; zero them
    # here so the end-of-run index_catalog_op_stats event covers exactly
    # THIS run, not cumulative-since-process-start (review finding,
    # nexus-jb4pp — the multi-index-per-process mis-attribution class).
    from nexus.catalog.factory import reset_service_catalog_op_stats  # noqa: PLC0415 — deferred to avoid circular import (catalog.factory)

    reset_service_catalog_op_stats()
    # nexus-ldab2: mirrors the catalog reset immediately above — same
    # process-lifetime-global-must-not-accumulate-across-runs rationale,
    # now for the service-T2 singleton's per-op counters.
    from nexus.mcp_infra import reset_service_t2_op_stats  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)

    reset_service_t2_op_stats()
    # nexus-7lw6a: same process-lifetime-global rationale as the two resets
    # immediately above — zero the taxonomy-assign batch outcome counters
    # so the end-of-run summary covers exactly THIS run.
    from nexus.mcp_infra import reset_taxonomy_assign_run_stats  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)

    reset_taxonomy_assign_run_stats()

    # RDR-103 Phase 3a: registry value preserves the legacy name when
    # the repo was added before the migration; fallback queries the
    # catalog for a conformant name. Phase 4 migration runs further
    # down (after credentials check + T3 connect) and may overwrite
    # these with conformant names before the indexer reads from T3.
    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection") or _repo_collection_or_legacy(repo, "docs")
    # nexus-5ut2a: the registry value can carry a legacy 2-segment name
    # (``code__<owner>``) when repos.json holds a pre-RDR-103 entry for a
    # repo that has no catalog owner yet (e.g. a stale entry from a prior
    # failed run, or the RDR-137 migration's un-cataloged residue). The
    # Phase-4 migration below is a no-op without an owner, so an
    # un-reconciled legacy name would reach the strict get_or_create_collection
    # guard (db/t3.py) and crash the whole index. Re-route any
    # non-conformant value through the path-derived conformant synth, which
    # builds ``code__<owner>__<model>__v1`` without needing a registered
    # owner (model segment self-adjusts to local/cloud mode).
    from nexus.corpus import is_conformant_collection_name  # noqa: PLC0415  — circular-dep avoidance (nexus.corpus)
    if not is_conformant_collection_name(code_collection):
        code_collection = _repo_collection_or_legacy(repo, "code")
    if not is_conformant_collection_name(docs_collection):
        docs_collection = _repo_collection_or_legacy(repo, "docs")
    _migrated_names: dict[str, str] = {}

    # Load config (picks up per-repo .nexus.yml if present)
    cfg = load_config(repo_root=repo)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    ignore_patterns: list[str] = list(dict.fromkeys(DEFAULT_IGNORE + cfg_patterns))
    indexing_config: dict = cfg.get("indexing", {})
    rdr_paths: list[str] = indexing_config.get("rdr_paths", ["docs/rdr"])
    read_timeout_seconds: float = cfg.get("voyageai", {}).get("read_timeout_seconds", 120.0)

    # Load tuning config and use its chunk_lines if not overridden by caller
    from nexus.config import _tuning_from_dict  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    tuning = _tuning_from_dict(cfg.get("tuning", {}))
    effective_chunk_lines: int | None = chunk_lines if chunk_lines is not None else tuning.code_chunk_lines

    # Collect git metadata once for all chunks
    git_meta = _git_metadata(repo)

    # nexus-fltb4 (--since-head): git-diff-driven incremental scope. When a
    # usable delta exists, ONLY the changed files are walked/indexed, the
    # full-collection staleness pulls are skipped, deleted paths get their
    # catalog docs removed explicitly (git already did rename detection),
    # and every full-tree pass that would misread a partial walk
    # (housekeeping miss-counting, misclassified/orphan prunes, the rg
    # cache rebuild) is skipped. Any doubt about the delta -> full index.
    delta_changed: "set[str] | None" = None
    delta_deleted: list[str] = []
    if since_head and not force and not force_stale:
        _base = _get_owner_head_hash(repo)
        _delta = _git_changed_since(repo, _base) if _base else None
        if _delta is None:
            _log.info(
                "since_head_fallback_full_index",
                repo=str(repo), base=_base or "<none>",
            )
            if on_phase is not None:
                on_phase("--since-head: no usable base — full index")
        else:
            delta_changed = set(_delta[0])
            delta_deleted = _delta[1]
            _log.info(
                "since_head_delta",
                repo=str(repo), base=_base,
                changed=len(delta_changed), deleted=len(delta_deleted),
            )
            if on_phase is not None:
                on_phase(
                    f"--since-head: {len(delta_changed)} changed, "
                    f"{len(delta_deleted)} deleted since {_base[:12]}"
                )
            if not delta_changed and not delta_deleted:
                if on_phase is not None:
                    on_phase("--since-head: nothing changed — done")
                return {"rdr_indexed": 0, "rdr_current": 0, "rdr_failed": 0,
                        "files_changed": 0}
    elif since_head:
        _log.info(
            "since_head_ignored_with_force",
            reason="force/force_stale requests a full pass by definition",
        )

    # Compute frecency scores in a single git log pass
    frecency_map = batch_frecency(repo, decay_rate=tuning.decay_rate, timeout=tuning.git_log_timeout)

    # Build absolute RDR path set for exclusion (resolved, so symlink/..
    # normalization matches the classification loop's ``path.resolve()``
    # comparison below).
    rdr_abs_paths: set[Path] = set()
    for rdr_rel in rdr_paths:
        rdr_abs = (repo / rdr_rel).resolve()
        rdr_abs_paths.add(rdr_abs)

    # nexus-3lswy: RDR markdown files, scored via the same frecency_map as
    # code/prose/pdf, so they can flow through the main run_file_loop as a
    # 4th category instead of the separate, unbatched _discover_and_index_rdrs
    # path. Symlinks excluded (preserved from the retired function's walk;
    # the indexed_for_catalog walk below reuses this same list, so the
    # exclusion now applies there too — a slightly stricter catalog
    # registration surface than before, intentionally).
    #
    # Walk the UNRESOLVED ``repo / rdr_rel`` dirs, not ``rdr_abs_paths``
    # (resolved) — ``_index_prose_file``/``index_prose_file`` calls
    # ``file_path.relative_to(ctx.repo_path)`` where ``ctx.repo_path`` is
    # the caller's raw, unresolved ``repo``. A resolved rdr_dir (e.g. macOS
    # ``/tmp`` -> ``/private/tmp``) produces md_file paths that no longer
    # share ``repo``'s prefix, raising ValueError. code/prose/pdf files
    # don't hit this because ``_git_ls_files``/rglob fallback both build
    # paths as ``repo / rel`` (unresolved) already.
    # nexus-rqsh1: files that would register a catalog document but never
    # produce a chunk (zero-byte, or binary content misclassified as
    # prose). Tracked separately from skipped_oversize/ContentClass.SKIP
    # for observability — see the per-skip debug logs below. Declared
    # here (round 2, substantive-critic Critical 2026-08-17) so the
    # rdr_md_paths walk below can share it with the candidate_files loop
    # further down — a SEPARATE discovery pass feeding the SAME
    # indexed_for_catalog registration, which had no zero-byte guard of
    # its own.
    skipped_unchunkable: list[tuple[Path, str]] = []

    rdr_md_paths: list[tuple[float, Path]] = []
    for rdr_rel in dict.fromkeys(rdr_paths):  # de-dupe while preserving order
        rdr_dir = repo / rdr_rel
        if rdr_dir.is_dir():
            for md_file in sorted(rdr_dir.rglob("*.md")):
                if md_file.is_file() and not md_file.is_symlink():
                    if (delta_changed is not None
                            and str(md_file.relative_to(repo)) not in delta_changed):
                        continue  # nexus-fltb4: outside the delta
                    # nexus-rqsh1 round 2: a zero-byte RDR file yields zero
                    # chunks (same mechanism as the candidate_files zero-byte
                    # guard below) and would otherwise still reach
                    # indexed_for_catalog, minting an unclearable phantom.
                    # RDR files are markdown text by construction (unlike
                    # candidate_files' arbitrary extensions), so a zero-byte
                    # check alone suffices here — no binary-content sniff
                    # needed.
                    try:
                        if md_file.stat().st_size == 0:
                            skipped_unchunkable.append((md_file, "zero_byte"))
                            _log.debug(
                                "unchunkable_zero_byte_rdr_skipped",
                                path=str(md_file),
                            )
                            continue
                    except OSError:
                        pass  # defer to per-file path
                    rdr_md_paths.append((frecency_map.get(md_file, 0.0), md_file))
    rdr_md_paths.sort(key=lambda x: x[0], reverse=True)
    have_rdr_files = bool(rdr_md_paths)

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
    # skipped_unchunkable declared above, before the rdr_md_paths walk —
    # shared by both discovery passes.

    for path in candidate_files:
        if not path.is_file():
            continue  # git ls-files may list deleted files not yet committed
        rel = path.relative_to(repo)
        if delta_changed is not None and str(rel) not in delta_changed:
            continue  # nexus-fltb4: outside the delta
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

        # nexus-rqsh1: a zero-byte file yields zero chunks under every
        # classifier — CODE's chunk_file returns [] (code_indexer.py "if
        # not chunks: return 0"), PROSE's chunker has no content to split,
        # PDF parsing yields no pages. Registering a catalog document for
        # it (below, in indexed_for_catalog) would mint a permanent
        # chunk_count=0 phantom that no re-index can ever clear — the
        # indexer must not register a document for a file it will not
        # chunk (Hal directive 2026-08-15). Skip before classification so
        # it never reaches any of the code/prose/pdf lists.
        if file_size == 0:
            skipped_unchunkable.append((path, "zero_byte"))
            _log.debug("unchunkable_zero_byte_skipped", path=str(path))
            continue

        score = frecency_map.get(path, 0.0)
        classification = classify_file(path, indexing_config=indexing_config)

        match classification:
            case ContentClass.CODE:
                code_files.append((score, path))
                all_text_scored.append((score, path))
            case ContentClass.PROSE:
                # nexus-rqsh1: extensions unknown to classify_file's
                # code/skip/binary-asset tables fall through to PROSE
                # (classifier.py step 8), but binary content (.npz, git
                # .bundle, ...) decodes as neither UTF-8 nor markdown —
                # the prose indexer's own read_text() raises and returns
                # 0 chunks. Sniff for that here, before registration,
                # rather than minting another un-clearable phantom.
                if looks_like_binary_content(path):
                    skipped_unchunkable.append((path, "binary_content"))
                    _log.debug(
                        "unchunkable_binary_content_skipped",
                        path=str(path),
                        ext=path.suffix.lower(),
                    )
                else:
                    prose_files.append((score, path))
                    all_text_scored.append((score, path))
            case ContentClass.PDF:
                pdf_files.append((score, path))
                # PDF files not included in ripgrep text cache
            case ContentClass.SKIP:
                # Known-noise or binary asset (nexus-6e6u1). Logged at debug so
                # the operator can see what got dropped at classification time
                # rather than having it silently vanish — WeakAuras2 dropped 366
                # binary files this way. Distinct from the byte-sniff "skipped
                # non-text file" emitted in prose/code indexers on decode failure.
                _log.debug(
                    "skipped non-indexable file",
                    path=str(path),
                    ext=path.suffix.lower(),
                )

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

    # nexus-rqsh1: summary log for the unchunkable-skip population (zero-
    # byte files, binary content misclassified as prose). Info level, not
    # warning — this is the fix draining a known phantom class, not an
    # anomaly the operator needs to act on.
    if skipped_unchunkable:
        _log.info(
            "indexer_unchunkable_skip",
            count=len(skipped_unchunkable),
            sample=[
                {"path": str(p.relative_to(repo)), "reason": reason}
                for p, reason in skipped_unchunkable[:5]
            ],
        )

    # Fire on_start with total non-RDR file count.
    # Note: this fires before the credential check below.  Phase 2 (CLI) must
    # handle CredentialsMissingError by closing the tqdm bar before re-raising.
    if on_start:
        on_start(len(code_files) + len(prose_files) + len(pdf_files))

    # Update ripgrep cache (code + prose text files, not PDFs)
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    _repo_basename, _repo_hash = _repo_identity(repo)
    cache_path = nexus_config_dir() / f"{_repo_basename}-{_repo_hash}.cache"
    if delta_changed is None:
        build_cache(repo, cache_path, all_text_scored)
    else:
        # nexus-fltb4: rebuilding the rg cache from a delta-filtered file
        # list would CLOBBER the full cache with a sliver. Leave the cache
        # slightly stale; the next full run refreshes it.
        _log.debug("since_head_rg_cache_skipped", files=len(all_text_scored))

    # Credential check and T3 setup
    from nexus.config import is_local_mode as _is_local  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from datetime import UTC, datetime as _dt  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.db import make_t3  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    _local_mode = _is_local()
    _embed_fn = None
    _service_mode: bool = False  # resolved in the cloud/service branch below; False in local mode

    if _local_mode:
        check_local_path_writable()
        from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        _local_ef = LocalEmbeddingFunction()
        local_model = _local_ef.model_name

        # nexus-3lswy: RDR files now route through _index_prose_file (shape
        # #1: ``embed_fn(texts) -> embeddings``), the same as code/prose/PDF
        # — the standalone doc_indexer shape #2 adapter this function used
        # to build for the RDR-only path (``(texts, model) -> (embeddings,
        # model)``) is no longer needed here. doc_indexer.py's own shape #2
        # remains for its OTHER callers (`nx collection reindex`, standalone
        # RDR-only index), which build their own adapter independently.
        # nexus-b7s8t: in local mode T3 is STILL the service-backed
        # HttpVectorClient (make_t3() is unconditional since RDR-155
        # P4a.2) and ``upsert_chunks_with_embeddings`` discards caller
        # embeddings — so running the bge ONNX model here embedded every
        # chunk TWICE: once in this process (466% CPU, ~2 GB RSS on a
        # 16 GB box, competing with the engine JVM that then did the
        # same work again) and once server-side. ``_local_ef`` is kept
        # only for its model name (construction is lazy, no model load);
        # the client embeds only when the vector backend is opted OUT of
        # service mode (the in-memory test substrate).
        _embed_fn = select_local_mode_embed_fn(_local_ef)  # shape #1 for code / prose / PDF / RDR

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
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)
        _service_mode = is_vector_service_mode()  # captured here; reused for T3 routing below
        if _service_mode:
            # RDR-152 Seam B (nexus-gmiaf.22): in service mode, embedding
            # happens server-side in the JVM.  Python must NOT create a
            # voyageai.Client — the embed + Chroma-write pipeline runs in
            # the Java nexus-service.  Non-service embedding was retired
            # (nexus-sghyo); the else branch below raises loud.
            voyage_key = ""
            voyage_client = None
            code_model = index_model_for_collection(code_collection)
            docs_model = index_model_for_collection(docs_collection)
            # Service-mode embed_fn (code / prose / PDF / RDR path):
            # returns empty embeddings as a no-op.  HttpVectorClient.
            # upsert_chunks_with_embeddings ignores them and embeds
            # server-side (Seam B contract).
            _embed_fn = lambda texts: [[]] * len(texts)  # noqa: E731
        else:
            # nexus-sghyo: non-service embedding was retired — the client
            # no longer embeds via Voyage (Hal determination 2026-07-28).
            raise CredentialsMissingError(
                "non-service embedding was retired: the client no longer "
                "embeds via Voyage. Set NX_STORAGE_BACKEND_VECTORS=service "
                "(the default) or unset it."
            )

    _log.debug("connecting to ChromaDB")
    # RDR-152 Seam B (nexus-gmiaf.22): in service mode, route through
    # mcp_infra.get_t3() which returns HttpVectorClient.  In legacy mode,
    # use make_t3() to preserve the existing daemon-backed path.
    # Reuse _service_mode computed above to avoid a second module-import round-trip.
    if _service_mode:
        from nexus.mcp_infra import get_t3 as _get_t3  # noqa: PLC0415  — circular-dep avoidance (nexus.mcp_infra)
        db = _get_t3()
    else:
        db = make_t3()
    _log.debug("ChromaDB connected")
    now_iso = _dt.now(UTC).isoformat()

    # RDR-103 Phase 4: legacy-to-conformant migration on first index
    # after the catalog upgrade. Runs BEFORE the conformant collection
    # creation below so the rest of the pipeline only ever sees the
    # conformant name. Idempotent: subsequent runs are a no-op (legacy
    # absent from T3). Catalog-absent is a safe no-op (returns legacy).
    _cat: object | None = None
    _migrate_writer = None
    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        _cat = make_catalog_reader()
        if _cat is not None:
            _migrate_writer = make_catalog_writer()

        def _emit_migration_msg(msg: str) -> None:
            _log.info("phase4_migration", reason=msg)
            if on_phase is not None:
                on_phase(msg)

        _migrated_names = _migrate_legacy_collections(
            repo, cat=_cat, t3_db=db, registry=registry,
            on_message=_emit_migration_msg, writer=_migrate_writer,
        )
        # Migration may have promoted legacy names to conformant. Pick
        # up the new names so the rest of the pipeline (collection
        # creation, indexing, catalog hook) sees the post-migration
        # state.
        if _migrated_names.get("code"):
            code_collection = _migrated_names["code"]
        if _migrated_names.get("docs"):
            docs_collection = _migrated_names["docs"]
    except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
        # Unexpected failure outside _migrate_legacy_collections's own
        # exception handling (which catches per-content-type rename
        # errors and returns legacy names). This catch is the safety
        # net for catalog-open / import / unforeseen failures; warn
        # so the operator can act, but proceed with pre-migration
        # collection names so the indexing run still completes.
        _log.warning("phase4_migration_failed", exc_info=True)
    finally:
        # NB: _cat (read-only) is reused by the post-index GC / prune
        # passes later in index_repository — do NOT close it here. Only
        # the migration writer is scoped to this block.
        if _migrate_writer is not None:
            _migrate_writer.close()

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
    # nexus-3lswy: RDR-103 Phase 3a + Phase 4 name resolution, lifted here
    # (from the indexed_for_catalog block below) so the 4th run_file_loop's
    # rdr_col can be built alongside code_col/docs_col. None when there are
    # no RDR files — same zombie-collection avoidance as code/docs.
    rdr_col_name: str | None = (
        (_migrated_names.get("rdr") or _repo_collection_or_legacy(repo, "rdr"))
        if have_rdr_files else None
    )
    _log.debug(
        "creating collections",
        code=code_collection if have_code_files else "(deferred; no files)",
        docs=docs_collection if have_docs_files else "(deferred; no files)",
        rdr=rdr_col_name or "(deferred; no files)",
    )
    code_col = (
        db.get_or_create_collection(code_collection)
        if have_code_files else None
    )
    docs_col = (
        db.get_or_create_collection(docs_collection)
        if have_docs_files else None
    )
    rdr_col = (
        db.get_or_create_collection(rdr_col_name)
        if rdr_col_name is not None else None
    )
    _log.debug("collections ready")

    # Check pipeline version staleness (informational warning only)
    if code_col is not None:
        check_pipeline_staleness(code_col, code_collection)
    if docs_col is not None:
        check_pipeline_staleness(docs_col, docs_collection)
    if rdr_col is not None:
        check_pipeline_staleness(rdr_col, rdr_col_name)

    # --force-stale: escalate to force if any collection is stale
    if force_stale:
        any_stale = (
            (code_col is not None and get_collection_pipeline_version(code_col) not in (None, PIPELINE_VERSION))
            or (docs_col is not None and get_collection_pipeline_version(docs_col) not in (None, PIPELINE_VERSION))
            or (rdr_col is not None and get_collection_pipeline_version(rdr_col) not in (None, PIPELINE_VERSION))
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
    # nexus-3lswy: reuse the shared rdr_md_paths walk (built + resolved to
    # rdr_col_name above, alongside code_col/docs_col) instead of a second
    # rglob pass. rdr_col_name is None when there are no RDR files.
    if rdr_col_name is not None:
        for _, md_file in rdr_md_paths:
            indexed_for_catalog.append((md_file, "rdr", rdr_col_name))

    if on_phase is not None:
        on_phase(f"Registering {len(indexed_for_catalog)} catalog entries…")
    _catalog_t0 = time.monotonic()
    # nexus-cp46b: doc_ids the catalog hook's own owner-scoped fetch (below)
    # finds fenced 'indexing'/'failed' — reused to force those docs stale in
    # the staleness caches built further down, no extra catalog round trip.
    _stale_fence_doc_ids: set[str] = set()
    # nexus-vayt7: tumbler -> index_content_hash for every doc fenced
    # 'complete', from the same fetch; overlaid onto the caches below.
    _complete_doc_hashes: dict[str, str] = {}
    file_to_doc_id = _catalog_hook(
        repo=repo,
        repo_name=_repo_basename,
        repo_hash=_repo_hash,
        head_hash=_current_head(repo),
        indexed_files=indexed_for_catalog,
        on_locked=on_locked,
        # nexus-fltb4: miss-counting against a delta-filtered walk would mark
        # every unvisited doc as missing and mass-delete after two runs.
        skip_housekeeping=delta_changed is not None,
        stale_fence_doc_ids=_stale_fence_doc_ids,
        on_phase=on_phase,
        complete_doc_hashes=_complete_doc_hashes,
    )
    if on_phase is not None:
        on_phase(
            f"Catalog registration done ({time.monotonic() - _catalog_t0:.1f}s)"
        )

    # nexus-kgyoz seam 2: the resolver closure is lifted to
    # indexer_utils.build_doc_id_resolver so _run_index stays a thin
    # orchestrator. Behaviour identical — missing files resolve to "".
    _doc_id_resolver = build_doc_id_resolver(file_to_doc_id)

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
    from nexus.indexer_utils import StalenessCache  # noqa: PLC0415  — circular-dep avoidance (nexus.indexer_utils)

    # nexus-uizok: per-collection markers so the (up to minutes-long on
    # the paginated fallback) sweep never goes dark between the opening
    # line above and the "built" summary below — one heartbeat per
    # collection with the previous one's wall time.
    def _build_with_marker(label: str, col: object) -> "StalenessCache":
        if col is None:
            return StalenessCache()
        if on_phase is not None:
            on_phase(
                f"  staleness sweep: {label}… "
                f"({time.monotonic() - _staleness_t0:.1f}s elapsed)"
            )
        return build_staleness_cache(col)

    if delta_changed is not None:
        # nexus-fltb4: the delta files are known-changed; the caches exist to
        # SKIP unchanged files, which the delta already excludes. Empty caches
        # route the (small) delta set through the per-file staleness path,
        # which still no-ops content-identical rewrites.
        code_staleness = StalenessCache()
        docs_staleness = StalenessCache()
        rdr_staleness = StalenessCache()
    else:
        code_staleness = _build_with_marker("code", code_col)
        docs_staleness = _build_with_marker("docs", docs_col)
        # nexus-3lswy: distinct cache object from docs_staleness — rdr__ is a
        # separate physical collection from docs__, so a shared object would
        # cross-contaminate staleness lookups between the two.
        rdr_staleness = _build_with_marker("rdr", rdr_col)
    if _stale_fence_doc_ids:
        # nexus-cp46b: a doc_id only ever belongs to one collection's cache,
        # so sharing the same frozenset across all three is harmless — each
        # cache only ever looks up doc_ids for files routed to it.
        _never_fresh = frozenset(_stale_fence_doc_ids)
        code_staleness.never_fresh = _never_fresh
        docs_staleness.never_fresh = _never_fresh
        rdr_staleness.never_fresh = _never_fresh
    # nexus-vayt7: the catalog fence hash overrides the chunk-metadata hash
    # for every 'complete' doc the cache holds. A doc_id belongs to one
    # collection, so the same map is safe to apply to all three.
    from nexus.indexer_utils import (  # noqa: PLC0415  — circular-dep avoidance (nexus.indexer_utils)
        apply_catalog_content_hashes,
        complete_doc_hashes_for,
    )
    # The hook map covers this repo's owner only; a chash in rdr__/docs__
    # can resolve to a doc registered under another owner (this checkout's
    # RDRs sit under eight). One paged resolve_many fills those in.
    _cached_doc_ids = (
        set(code_staleness.by_doc_id)
        | set(docs_staleness.by_doc_id)
        | set(rdr_staleness.by_doc_id)
    )
    if _cached_doc_ids - set(_complete_doc_hashes):
        from nexus.catalog.factory import make_catalog_reader as _mk_reader  # noqa: PLC0415  — circular-dep avoidance (nexus.catalog.factory)

        try:
            _fence_reader = _mk_reader()
        except Exception:  # noqa: BLE001 — overlay is best-effort; a miss re-indexes, never fails the run
            _fence_reader = None
        _complete_doc_hashes = complete_doc_hashes_for(
            _fence_reader, _cached_doc_ids, known=_complete_doc_hashes,
        )
    _fence_hash_applied = (
        apply_catalog_content_hashes(code_staleness, _complete_doc_hashes)
        + apply_catalog_content_hashes(docs_staleness, _complete_doc_hashes)
        + apply_catalog_content_hashes(rdr_staleness, _complete_doc_hashes)
    )
    _log.info(
        "staleness_caches_built",
        code_doc_ids=len(code_staleness.by_doc_id),
        docs_doc_ids=len(docs_staleness.by_doc_id),
        rdr_doc_ids=len(rdr_staleness.by_doc_id),
        complete_fence_docs=len(_complete_doc_hashes),
        fence_hash_overrides=_fence_hash_applied,
        elapsed_seconds=time.monotonic() - _staleness_t0,
    )
    if on_phase is not None:
        on_phase(
            f"Staleness caches built — "
            f"code: {len(code_staleness.by_doc_id):,} docs, "
            f"docs: {len(docs_staleness.by_doc_id):,} docs, "
            f"rdr: {len(rdr_staleness.by_doc_id):,} docs "
            f"({time.monotonic() - _staleness_t0:.1f}s)"
        )

    # nexus-cfc72: bounded file-level concurrency. Sequential (exact
    # legacy loop) unless both the vectors and catalog backends are the
    # HTTP service (or NX_INDEX_CONCURRENCY overrides). Staleness caches
    # are read-only after build; on_file/on_stage_timers are serialized
    # inside run_file_loop; hook chains are serialized via
    # LockedHookRegistry so the manifest/chash/taxonomy/aspect writes
    # never interleave.
    from nexus.indexer_utils import resolve_index_concurrency, run_file_loop, skip_floor_breached  # noqa: PLC0415  — circular-dep avoidance (nexus.indexer_utils)
    _concurrency = resolve_index_concurrency()
    if _concurrency > 1 and hooks is not None:
        # hooks may be None at direct test call sites; wrapping None would
        # defeat every downstream ``hooks is None`` fallback (review
        # finding, nexus-cfc72) — leave None alone.
        from nexus.hook_registry import LockedHookRegistry  # noqa: PLC0415 — deferred to avoid circular import
        hooks = LockedHookRegistry(hooks)
        _log.info("index_file_concurrency", workers=_concurrency)

    # duoak 2C (nexus-1ugqs): cross-file chunk batching. Per-file upserts
    # amortize ~nothing (median file 3-15 chunks -> ~1200 embed calls for
    # a 1200-file repo). Stage chunks in a shared accumulator, flush at
    # the service cap, fire each file's post-store hook chains once its
    # chunks land in a successful flush. Service mode only — the flush
    # posts raw text for server-side embedding (Seam B).
    # Gate on the ACTUAL client type, not is_vector_service_mode(): tests
    # (and any legacy topology) inject a local T3 db that embeds
    # client-side and cannot accept the empty-embeddings Seam B batches.
    from nexus.db.http_vector_client import HttpVectorClient  # noqa: PLC0415 — deferred to avoid circular import
    _batcher = None
    # nexus-4s1ww / GH #1432: path -> error for every file whose chunks
    # PERMANENTLY failed to flush this run (populated below from
    # ChunkBatcher.failed_files — the count that SURVIVED bisect-retry
    # settlement, never a raw attempt/retry count). Defaults to empty so
    # the stats dict always carries the key, even when db isn't an
    # HttpVectorClient (legacy per-file path, no batcher constructed).
    _batch_failures: dict[str, str] = {}
    if isinstance(db, HttpVectorClient):
        from nexus.chunk_batcher import ChunkBatcher  # noqa: PLC0415 — deferred to avoid circular import

        def _batch_flush(
            collection: str, _ids: list, _docs: list, _metas: list,
            _file_contexts: list,
        ) -> None:
            # nexus-wxjr6 (kl2z6 combined write, client half): ONE POST
            # carries chunk content AND manifest rows for every full-file
            # doc in this flush, landing atomically per-doc server-side
            # (design memo T2 design-kl2z6-combined-write §0). Replaces
            # the old two-call shape (an upsert-chunks POST here, then a
            # SEPARATE write_many POST later from
            # _fire_flush_grain_hooks's manifest_write_batch_hook
            # dispatch) for THIS flush-grain path only — see
            # mcp_infra._apply_combined_write_response's RETIREMENT SCOPE
            # docstring and the nexus-wxjr6 §7.2 per-caller table for
            # which OTHER upsert-chunks callers stay on the old shape.
            # Payload construction (dedup, by_doc grouping, the
            # position-0 defended invariant) lives in the module-level,
            # independently-tested _build_combined_write_payload.
            #
            # RDR-181 §Approach step 3: force_re_embed closes over the
            # enclosing _run_index's ``force`` (constant for the whole
            # run, like ``db`` above) so ``--force`` reaches the server's
            # forceReEmbed escape for the batched flush path too, not
            # just the per-file fallback below.
            (
                chunks_payload, full_docs, complete_map,
                orphan_ids, orphan_docs, orphan_metas,
            ) = _build_combined_write_payload(_ids, _docs, _metas, _file_contexts)

            if orphan_ids:
                # Code review Critical C1 (2026-08-09, review T2 [22014]):
                # chunks whose file has NO catalog identity can never be
                # referenced by any doc's manifest rows, so the engine
                # would embed them (Voyage cost paid) and then silently
                # discard them (upsertManifestChunkVectors only persists
                # chashes a doc's OWN rows name). Route them through the
                # OLD direct upsert-chunks call instead — preserves the
                # pre-nexus-wxjr6 behavior EXACTLY (content lands in T3,
                # orphaned but searchable, no manifest) rather than either
                # silently losing it or changing what "orphaned" means.
                # INFO, not WARNING: this is the SAME state the pre-commit
                # code produced silently for identity-less chunks; logging
                # it is strictly better observability, not a new failure.
                _log.info(
                    "combined_write_orphan_chunks_routed_to_legacy_upsert",
                    collection=collection,
                    count=len(orphan_ids),
                )
                db.upsert_chunks_with_embeddings(
                    collection_name=collection,
                    ids=orphan_ids,
                    documents=orphan_docs,
                    embeddings=[[] for _ in orphan_ids],  # Seam B: server embeds
                    metadatas=orphan_metas,
                    force_re_embed=force,
                )

            if not full_docs:
                # Mirrors manifest_write_batch_hook's identical early-out
                # (GH #1397 / nexus-94fxl): no file in this flush carries
                # catalog identity — record it the same way, but do NOT
                # attempt the old T3-write-without-manifest fallback: the
                # combined write's whole point is that content and
                # manifest land together or not at all (design memo §0).
                # Structurally this should not happen for the
                # ChunkBatcher path (catalog registration precedes
                # indexing), so this is a defended invariant, not routine
                # traffic. (A MIXED flush with SOME identity-having files
                # already got its combined write above/below this branch
                # — this is the ALL-orphan case, already fully handled by
                # the orphan routing above.)
                from nexus.mcp_infra import _record_manifest_identity_drop  # noqa: PLC0415 — deferred (lazy import, rare path)
                _record_manifest_identity_drop(collection, len(_ids))
                _log.warning(
                    "combined_write_batch_missing_doc_identity",
                    collection=collection,
                    batch_size=len(_ids),
                )
                return

            from nexus.mcp_infra import (  # noqa: PLC0415 — deferred: avoid module-load cross-import
                _apply_combined_write_response,
                get_catalog_writer,
            )
            from nexus.retry import _manifest_write_with_retry  # noqa: PLC0415 — deferred (leaf module, avoid import cost on the no-op path)

            cat = get_catalog_writer()
            try:
                # Bounded backoff against a flapping connection, matching
                # every other catalog write in this module (_manifest_write_
                # loop's write_many branch, atomic_manifest_replace, etc.)
                # — safe to retry: the combined write is idempotent
                # (ON CONFLICT DO UPDATE per chash/doc; design memo §0),
                # and _manifest_write_with_retry raises immediately (no
                # retry) on a non-connection error, so a genuine 4xx/
                # validation failure or the ack-echo RuntimeError still
                # propagates on the first attempt.
                res = _manifest_write_with_retry(
                    cat.write_manifest_many,
                    full_docs,
                    complete=complete_map or None,
                    sweep=True,
                    chunks=chunks_payload,
                    collection=collection,
                    force_re_embed=force,
                )
            finally:
                _close = getattr(cat, "close", None)
                if callable(_close):
                    _close()

            _apply_combined_write_response(res, complete_map, collection)

        # nexus-duoak follow-up: split "file" into its 3 constituent calls
        # for diagnosis. manifest_write_batch_hook/taxonomy_assign_batch_hook
        # are flush-grain (nexus-u2kwq; the chash dual-write hook was too,
        # until RDR-187/nexus-piwya.4 retired it) so
        # fire_batch(grain="file") matches zero registered hooks by default —
        # the file-grain bucket's cost, if any, is fire_single (no default
        # consumers) or fire_document (aspect_extraction_enqueue_hook, which
        # early-returns for collections without an extractor config and does
        # a real T2 queue INSERT for the ones that have one).
        _hook_seconds = {
            "file": 0.0, "flush": 0.0,
            "file_batch": 0.0, "file_single": 0.0, "file_document": 0.0,
        }
        # nexus-lde88 G4: "flush" above stayed as the merged total (taxonomy +
        # manifest + aspect-enqueue, indistinguishable from one number). This
        # dict carries the SAME total split by name — taxonomy/manifest come
        # straight from HookRegistry.fire_batch's own hook_timings= (it knows
        # each hook's __name__), aspect_enqueue is timed separately below
        # since it isn't dispatched through the registry.
        _flush_hook_by_name: dict[str, float] = {}
        _hook_seconds_lock = threading.Lock()

        def _fire_deferred_hooks(_path: str, context: object) -> None:
            if not isinstance(context, dict):
                return
            _t0 = time.monotonic()
            reg = context["hooks"]
            if reg is None:
                from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid circular import
                reg = HookRegistry()
                install_default_hooks(reg)
            # File grain: manifest (needs catalog_doc_id) + any other
            # default-grain consumer. Flush-grain hooks (taxonomy, chash,
            # aspect-extraction enqueue) fire once per upload batch via
            # _fire_flush_grain_hooks.
            _t_batch = time.monotonic()
            reg.fire_batch(
                context["ids"], context["collection"], context["documents"],
                context["embeddings"], context["metadatas"],
                catalog_doc_id=context["catalog_doc_id"],
                grain="file",
            )
            _t_single = time.monotonic()
            for _did, _doc in zip(context["ids"], context["documents"]):
                reg.fire_single(_did, context["collection"], _doc)
            _t_document = time.monotonic()
            # nexus-nj4ch: the document-grain fire_document() call that used
            # to live here (aspect_extraction_enqueue_hook, the ONLY default
            # document-hook consumer) is now batched in
            # _fire_flush_grain_hooks below instead of firing once per file
            # here — this eliminated ~34.7s across ~250 real per-document T2
            # queue inserts in this repo's own shakeout. This closure is
            # local to the ChunkBatcher-driven `nx index repo` path (code/
            # prose/RDR/PDF files under the batcher); the OTHER
            # fire_document call sites (doc_indexer.py, pipeline_stages.py,
            # mcp/core.py's store_put) are untouched and still fire per-call
            # — they serve separate, non-batcher commands.
            _t_end = time.monotonic()
            with _hook_seconds_lock:
                _hook_seconds["file"] += _t_end - _t0
                _hook_seconds["file_batch"] += _t_single - _t_batch
                _hook_seconds["file_single"] += _t_document - _t_single
                _hook_seconds["file_document"] += _t_end - _t_document

        def _batched_file_failed(_path: str, error: str, _context: object) -> None:
            _log.error("indexed_file_upload_failed", file=_path, error=error)
            # nexus-bhlfy: ChunkBatcher already fires this callback once
            # per PERMANENTLY-failed file (settlement at file granularity
            # — see ``_settle_file_locked``/``_invoke_callbacks``; a
            # bisected batch retries each half independently so a
            # genuinely failing file is reported exactly once here, never
            # for a batch-mate that went on to succeed). The begin stamp
            # for this file already landed via ``_fire_flush_grain_begin``
            # (``on_batch_begin``, before the upload) — without this arm
            # the fence wedged at 'indexing' forever on a flush failure
            # (the gap this bead closes). ``_fence_fail`` never raises.
            if isinstance(_context, dict):
                _cdid = _context.get("catalog_doc_id")
                if _cdid:
                    from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 — deferred import; test patch target
                    _fence_fail(_cdid, error)

        def _fire_flush_grain_begin(
            collection: str, _file_contexts: list,
        ) -> None:
            # nexus-vw594 F1: the RUNFENCE begin-many stamp for the repo-index
            # hot path. Fired by ChunkBatcher's on_batch_begin callback
            # BEFORE the network upsert (memo §3.5 T0 ordering) — this is
            # the "TOP of the flush, before the upload" call site the
            # investigation memo (T2 nexus/vw594-investigation-2026-08-04)
            # names, expressed as ChunkBatcher's symmetric on_batch_begin/
            # on_batch_complete split rather than literally inlined into
            # _batch_flush's body: the flush closure only ever sees the
            # flattened chunk arrays, never per-file catalog_doc_id, so the
            # fence-relevant identity has to come from the SAME file_contexts
            # extraction _fire_flush_grain_hooks below already does for the
            # completion ride. ONE begin-many round trip per FLUSH (not per
            # file) — the exact cost the :3631-era comment objected to.
            pairs: list[tuple[str, str]] = []
            for _path, _c in _file_contexts:
                if not isinstance(_c, dict):
                    continue
                _cdid = _c.get("catalog_doc_id")
                if not _cdid:
                    continue
                _metas_c = _c.get("metadatas")
                _chash = _metas_c[0].get("content_hash", "") if _metas_c else ""
                if _chash:
                    pairs.append((_cdid, _chash))
            if not pairs:
                return
            from nexus.doc_indexer import _fence_begin_many  # noqa: PLC0415 — deferred import; test patch target
            _fence_begin_many(pairs, collection)

        def _fire_flush_grain_hooks(
            collection: str, _ids: list, _docs: list, _metas: list,
            _file_contexts: list,
        ) -> None:
            # nexus-duoak.7: taxonomy + chash are file-agnostic and
            # round-trip-dominated — one call per upload batch (~6/run)
            # instead of one per file (~177/run). Uses the run's shared
            # registry; embeddings placeholder (server embedded).
            reg = hooks
            if reg is None:
                from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid circular import
                reg = HookRegistry()
                install_default_hooks(reg)
            # Attribution (critic S1): a flush-grain consumer failure
            # affects every file in this batch — log the roster so an
            # operator can map an aggregate chash/taxonomy warning back
            # to files (the widened blast radius vs per-file firing is
            # an accepted, documented tradeoff; chash falls back to the
            # resolve_span scan, taxonomy heals on the next assign run).
            _log.debug(
                "flush_grain_hooks_firing",
                collection=collection,
                chunks=len(_ids),
                files=[p for p, _ in _file_contexts],
            )
            # Rebuild the aggregate from per-file contexts with doc_id +
            # FILE-LOCAL chunk_index injected: post-RDR-108 chunk metadata
            # carries neither, and the manifest hook's enumeration
            # fallback would otherwise assign batch-global positions
            # (manifest corruption). taxonomy/chash ignore both keys.
            # agg_metas is built via the SHARED, module-level
            # _aggregate_flush_metas helper (nexus-wxjr6) so this closure
            # and _build_combined_write_payload's combined write compute
            # the identical doc_id/position assignment.
            agg_ids: list = []
            agg_docs: list = []
            for _path, _c in _file_contexts:
                if not isinstance(_c, dict):
                    continue
                agg_ids.extend(_c["ids"])
                agg_docs.extend(_c["documents"])
            agg_metas = _aggregate_flush_metas(_file_contexts)
            if not agg_ids:
                return
            # nexus-5xn3k.4 RUNFENCE C2: ChunkBatcher is file-atomic (every
            # file's whole chunk set lands in one flush — see the write_many
            # call-site comments), so the completion claim is sound here.
            # No PER-FILE begin call here (memo's explicit design: no extra
            # round trips on this hot path) — the begin-many stamp for this
            # flush already fired once, in _fire_flush_grain_begin above via
            # ChunkBatcher's on_batch_begin, before the upload even started
            # (nexus-vw594 F1). This closure only carries the ride-along
            # complete map.
            _manifest_complete: dict[str, str] = {}
            for _path, _c in _file_contexts:
                if not isinstance(_c, dict):
                    continue
                _cdid = _c["catalog_doc_id"]
                if not _cdid:
                    continue
                _chash = _c["metadatas"][0].get("content_hash", "") if _c["metadatas"] else ""
                if _chash:
                    _manifest_complete[_cdid] = _chash
            _t0 = time.monotonic()
            # nexus-lde88 G4: hook_timings= gets the registry's own per-hook
            # (taxonomy_assign_batch_hook / manifest_write_batch_hook) split
            # for free — the registry already resolves each hook's
            # __name__ for its failure-logging path; this just times around
            # the same call instead of merging both into one bucket.
            _batch_hook_timings: dict[str, float] = {}
            # nexus-wxjr6: manifest_write_batch_hook is excluded from this
            # dispatch — _batch_flush already wrote (and swept) this
            # flush's manifest atomically alongside the chunk upload via
            # the combined write, BEFORE this hook chain even fires.
            # Re-firing it here would double-write the manifest (wasted
            # round trip at best, a duplicate/conflicting write at worst)
            # and double-count sweep accounting. taxonomy_assign_batch_hook
            # (the OTHER grain="flush" hook) is unaffected and still fires.
            from nexus.mcp_infra import manifest_write_batch_hook  # noqa: PLC0415 — deferred: avoid module-load cross-import
            reg.fire_batch(
                agg_ids, collection, agg_docs,
                [[] for _ in agg_ids], agg_metas,
                grain="flush",
                manifest_complete=_manifest_complete or None,
                hook_timings=_batch_hook_timings,
                skip_hooks={manifest_write_batch_hook},
            )
            with _hook_seconds_lock:
                _hook_seconds["flush"] += time.monotonic() - _t0
                for _hname, _hsecs in _batch_hook_timings.items():
                    _flush_hook_by_name[_hname] = (
                        _flush_hook_by_name.get(_hname, 0.0) + _hsecs
                    )

            # nexus-nj4ch: batched aspect-extraction enqueue, replacing the
            # per-file fire_document(...) call this closure's file-grain
            # sibling (_fire_deferred_hooks) used to make. `collection` is
            # uniform across the whole upload batch, so the extractor-config
            # gate (aspect_extraction_enqueue_hook's own early-return) can be
            # checked ONCE here instead of once per file.
            from nexus.aspect_extractor import select_config  # noqa: PLC0415 — deferred to avoid circular import (aspect_extractor)
            if select_config(collection) is None:
                return  # No extractor for this collection — nothing to enqueue.
            from nexus.aspect_worker import _canonicalize_source_path  # noqa: PLC0415 — deferred to avoid circular import (aspect_worker)
            rows: list[dict] = []
            for _path, _c in _file_contexts:
                if not isinstance(_c, dict):
                    continue
                rows.append({
                    "collection": collection,
                    # content="" (CLI ingest scope, matches the per-file
                    # hook's contract exactly): the worker falls back to a
                    # disk read for content it needs.
                    "source_path": _canonicalize_source_path(collection, _path),
                    "content": "",
                    "doc_id": _c["catalog_doc_id"],
                })
            if not rows:
                return
            _t_aspect = time.monotonic()
            try:
                from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
                t2_index_write(
                    lambda t2: t2.aspect_queue.enqueue_many(rows),
                    op="aspect_enqueue",
                )
            except Exception:  # noqa: BLE001 — enqueue is best-effort; ingest must never block on it (RDR-089 P0.1)
                _log.warning(
                    "aspect_enqueue_many_batch_failed",
                    collection=collection, count=len(rows), exc_info=True,
                )
            else:
                # Auto-spawn gate (mirrors aspect_extraction_enqueue_hook's own
                # gate exactly — see its docstring for the unit-suite rationale).
                if os.environ.get("NX_ASPECT_WORKER_AUTOSTART", "1") not in (
                    "0", "false", "False", "no", "",
                ):
                    from nexus.aspect_worker import _ensure_aspect_worker  # noqa: PLC0415 — deferred to avoid circular import (aspect_worker)
                    _ensure_aspect_worker()
            _aspect_elapsed = time.monotonic() - _t_aspect
            with _hook_seconds_lock:
                _hook_seconds["flush"] += _aspect_elapsed
                _flush_hook_by_name["aspect_enqueue"] = (
                    _flush_hook_by_name.get("aspect_enqueue", 0.0) + _aspect_elapsed
                )

        # Shared with HttpVectorClient's internal upsert paging (nexus-nf3n7) so
        # the batcher flush cap and the client's oversize-fallback page size are
        # ONE source of truth. CCE collections (docs/knowledge/rdr) embed far
        # slower server-side: a 172-chunk CCE batch 504'd the gateway on the
        # 2026-07-04 2C smoke. Bisection-on-failure self-tunes below these.
        from nexus.db.http_vector_client import per_collection_chunk_cap as _cap_for  # noqa: PLC0415 — circular-dep avoidance: nexus.db.http_vector_client

        _batcher = ChunkBatcher(
            flush=_batch_flush,
            on_file_complete=_fire_deferred_hooks,
            on_file_failed=_batched_file_failed,
            on_batch_complete=_fire_flush_grain_hooks,
            on_batch_begin=_fire_flush_grain_begin,
            on_flush=on_flush,
            max_chunks=_cap_for,
            # See FLUSH_CONCURRENCY's module-level comment for the
            # empirical-vs-quota derivation (nexus-dimrz).
            flush_concurrency=FLUSH_CONCURRENCY,
        )
        _log.info("index_chunk_batching_enabled")

    # nexus-deyd5: files SKIPPED because a per-file extraction failure hit
    # them (nexus.errors.UnextractableContentError, caught directly by
    # run_file_loop — never raises). Nothing was ever written for a
    # skipped file, so skipping ONE file costs no data. Collected across
    # all 4 run_file_loop categories (code/prose/pdf/rdr) via the shared
    # on_skip callback so the run's summary can report them by path +
    # reason. A single skip stays informational (never a non-zero exit —
    # contrast pdf_quality_gate_failed, an intentionally separate policy);
    # round 3 added a run-level "too many of them" verdict computed AFTER
    # everything below (drain, RDR loop, post-processing) completes — see
    # "systemic_extraction_failure" at the return dict.
    # Declared before the FIRST run_file_loop call (code files) since all
    # four loops below share this one collector.
    _skipped_files: list[tuple[str, str]] = []

    def _on_skip(file: Path, reason: str) -> None:
        _skipped_files.append((str(file), reason))

    # Index code files → code__ (voyage-code-3, AST chunking)
    # NOTE: calls _index_code_file (the module-level wrapper) so that tests
    # patching nexus.indexer._index_code_file continue to intercept correctly.
    _log.debug("indexing code files", count=len(code_files))

    def _index_one_code(file: Path, score: float, timers: object | None) -> int:
        _log.debug("indexing", file=str(file))
        # nexus-7yfe6 / T2 22168 (engine-w0-503-client-degradation): contain a
        # transient upsert 5xx (gateway/pool) exactly like the prose/PDF/RDR
        # loops below. Code files are contained by the ChunkBatcher ONLY while
        # ChunkBatcher.add(...) accepts the file — it returns False for any
        # file exceeding per_collection_chunk_cap (16 chunks in onnx-local
        # mode), which is most real source files; that rejection falls
        # through to the identical legacy direct-upsert path prose/PDF use
        # (code_indexer.py's _index_code_file, ~line 487-522). Without this
        # wrapper a transient 5xx on that fallback propagates through
        # run_file_loop's first-exception-cancels-all contract and aborts the
        # WHOLE nx index repo run — pre-existing latent code, activated by an
        # engine that starts emitting 503 under embed-admission saturation.
        return _contain_transient_upsert(lambda: _index_code_file(
            file, repo, code_collection, code_model, code_col, db,
            voyage_client, git_meta, now_iso, score,
            chunk_lines=effective_chunk_lines,
            force=force,
            embed_fn=_embed_fn,
            stage_timers=timers,
            doc_id_resolver=_doc_id_resolver,
            staleness_cache=code_staleness,
            hooks=hooks,
            batcher=_batcher,
        ), file)

    # nexus-qgc4b: tally files that actually wrote chunks across all three
    # loops; used below to skip the expensive post-index passes on all-skip runs.
    # nexus-tevzq: kept per-kind so the caller can gate taxonomy discovery per
    # collection (code loop → code__, prose+pdf → docs__, rdr loop → rdr__).
    _code_written = run_file_loop(
        code_files, _index_one_code, concurrency=_concurrency,
        on_file=on_file, on_stage_timers=on_stage_timers,
        on_skip=_on_skip,
    )
    _files_written = _code_written

    # Index prose files → docs__ (voyage-context-3 via CCE)
    # NOTE: calls _index_prose_file (the module-level wrapper) — same reason.
    _log.debug("indexing prose files", count=len(prose_files))

    def _index_one_prose(file: Path, score: float, timers: object | None) -> int:
        _log.debug("indexing", file=str(file))
        # nexus-7yfe6: contain a transient upsert 5xx (gateway/pool) — defer the
        # file to staleness instead of failing (and, under concurrency, hanging)
        # the whole run. CORRECTED (nexus-acvi7, 2026-08-10): "Prose/PDF always
        # take the direct upsert path (no ChunkBatcher involvement)" — the
        # prior wording here — is REFUTED by prose_indexer.py:255
        # (`ctx.batcher.add(...)`): prose goes through the exact same
        # ChunkBatcher every code file does. `add()` only falls through to
        # the direct upsert (prose_indexer.py:282-290) when it REJECTS the
        # file (oversize, chunk_batcher.py:253-260) — same trigger as code's.
        # The wrapper's PLACEMENT here was never wrong despite that: it wraps
        # the whole per-file call, so it transparently covers the fallback
        # branch whenever `add()` rejects and is inert (no upsert call in
        # this frame to catch) whenever `add()` accepts — either way the
        # per-file call is correctly contained. What was wrong was only the
        # STATED REASON, not the code.
        # ALSO CORRECTED (T2 22168, engine-w0-503-client-degradation): code files
        # do NOT always get equivalent containment "via the ChunkBatcher" as
        # this comment used to claim — that was only true while
        # ChunkBatcher.add(...) accepts the file. It returns False (falling
        # through to the same direct-upsert path prose/PDF use) for any file
        # exceeding per_collection_chunk_cap, 16 chunks in onnx-local mode —
        # most real source files. _index_one_code below now wraps with the
        # identical _contain_transient_upsert to close that gap.
        return _contain_transient_upsert(lambda: _index_prose_file(
            file, repo, docs_collection, docs_model, docs_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
            timeout=read_timeout_seconds,
            embed_fn=_embed_fn,
            stage_timers=timers,
            doc_id_resolver=_doc_id_resolver,
            staleness_cache=docs_staleness,
            hooks=hooks,
            batcher=_batcher,
        ), file)

    _prose_written = run_file_loop(
        prose_files, _index_one_prose, concurrency=_concurrency,
        on_file=on_file, on_stage_timers=on_stage_timers,
        on_skip=_on_skip,
    )
    _files_written += _prose_written

    # Index PDF files → docs__ (PDF extraction + voyage-context-3)
    _log.debug("indexing PDF files", count=len(pdf_files))

    # nexus-wi1uv round-2 (code-review-expert + substantive-critic Critical,
    # both independently): one PDF tripping the post-extraction quality
    # gate must fail THAT file, never cancel the rest of this run's
    # code/prose/PDF/RDR files via run_file_loop's first-exception-
    # cancels-all contract. Collected here so the summary + stats key
    # below can report it (and index_repo_cmd can drive a non-zero exit).
    _quality_gate_failed: list[str] = []

    def _index_one_pdf(file: Path, score: float, timers: object | None) -> int:
        _log.debug("indexing", file=str(file))
        # nexus-7yfe6: same transient-upsert containment as prose (direct path).
        # nexus-wi1uv: quality-gate containment nests inside — extraction
        # (where the gate fires) happens before the vector upload (where a
        # transient 5xx fires), so the inner wrapper sees it first.
        return _contain_transient_upsert(lambda: _contain_extraction_quality_gate(lambda: _index_pdf_file(
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
            batcher=_batcher,
        ), file, _quality_gate_failed), file)

    _pdf_written = run_file_loop(
        pdf_files, _index_one_pdf, concurrency=_concurrency,
        on_file=on_file, on_stage_timers=on_stage_timers,
        on_skip=_on_skip,
    )
    _files_written += _pdf_written
    if _quality_gate_failed:
        # nexus-wi1uv: same "WARNING: N file(s) FAILED ... (see logs)" shape
        # as the batcher.failed_files block below (duoak 2C) — informational
        # during the run; index_repo_cmd converts a non-zero count into a
        # non-zero exit (uqq9z-equivalent for this exception class) after
        # post-processing completes, at the same point manifest_problems_
        # detected does today.
        _log.error(
            "index_pdf_quality_gate_failures",
            count=len(_quality_gate_failed),
            files=sorted(_quality_gate_failed)[:20],
        )
        if on_phase is not None:
            on_phase(
                f"WARNING: {len(_quality_gate_failed)} PDF(s) FAILED the "
                f"post-extraction quality gate (nexus-wi1uv, see logs): "
                + ", ".join(sorted(_quality_gate_failed)[:5])
                + ("…" if len(_quality_gate_failed) > 5 else "")
                + " — retry with 'nx index pdf --allow-degraded-extraction "
                "<file>' to accept the extraction deliberately."
            )

    # Index RDR markdown files → rdr__ (nexus-3lswy: 4th run_file_loop
    # category, same wiring as prose — batched register_many/doc_id
    # resolution and the staleness cache already ran above; this replaces
    # the old, unbatched _discover_and_index_rdrs -> doc_indexer.index_markdown
    # path, which redundantly re-registered each file's catalog entry over
    # the network despite _catalog_hook having already resolved it.
    # MUST run before _batcher.drain() below so RDR chunks flush with
    # everything else instead of being silently dropped.
    #
    # Exception-containment note (substantive-critic, nexus-3lswy): the
    # retired batch_index_markdowns caught ANY per-file exception and
    # recorded it as "failed", continuing the run — stronger isolation
    # than prose/pdf ever had. This loop deliberately does NOT restore
    # that broader catch: it only contains TRANSIENT upsert errors via
    # _contain_transient_upsert, exactly matching _index_one_prose/
    # _index_one_pdf below. A genuine non-transient failure now fails
    # the whole run for RDR too — an accepted, intentional consequence
    # of "full parity with the main loop", not an oversight. Special-
    # casing RDR with broader containment would reintroduce the same
    # inconsistency this fix removes, just inverted.
    _log.debug("indexing RDR files", count=len(rdr_md_paths))
    if on_phase is not None:
        on_phase("Discovering and indexing RDR markdown files…")
    _rdr_t0 = time.monotonic()

    def _index_one_rdr(file: Path, score: float, timers: object | None) -> int:
        _log.debug("indexing", file=str(file))
        return _contain_transient_upsert(lambda: _index_prose_file(
            file, repo, rdr_col_name, docs_model, rdr_col, db,
            voyage_key, git_meta, now_iso, score,
            force=force,
            timeout=read_timeout_seconds,
            embed_fn=_embed_fn,
            stage_timers=timers,
            doc_id_resolver=_doc_id_resolver,
            staleness_cache=rdr_staleness,
            hooks=hooks,
            batcher=_batcher,
        ), file)

    _rdr_written = run_file_loop(
        rdr_md_paths, _index_one_rdr, concurrency=_concurrency,
        on_file=on_file, on_stage_timers=on_stage_timers,
        on_skip=_on_skip,
    )
    _files_written += _rdr_written
    rdr_indexed = _rdr_written
    rdr_current = len(rdr_md_paths) - _rdr_written
    # nexus-3lswy: unlike the retired _discover_and_index_rdrs, there is no
    # distinct "failed" count here — like code/prose/pdf, a failed upload
    # is contained (batcher.failed_files / _contain_transient_upsert) and
    # surfaced via logs, not a separate summary bucket. A failing file
    # simply isn't counted as indexed, folding into rdr_current.
    rdr_failed = 0
    if on_phase is not None:
        on_phase(
            f"RDR indexing done — {rdr_indexed} indexed, {rdr_current} current, "
            f"{rdr_failed} failed ({time.monotonic() - _rdr_t0:.1f}s)"
        )

    # nexus-deyd5: report every file skipped this run (across all 4
    # run_file_loop categories) — loud and counted, never silent. This
    # per-file event alone never fails the run (no chunk was ever written
    # for a skipped file, so nothing was lost); whether the AGGREGATE
    # crosses the run-level systemic-skip verdict is decided separately,
    # below "systemic_extraction_failure" — this block does not know or
    # care about that verdict, it only reports the raw population.
    if _skipped_files:
        _log.error(
            "index_files_skipped_unextractable",
            count=len(_skipped_files),
            files=sorted(f for f, _ in _skipped_files)[:20],
        )
        if on_phase is not None:
            on_phase(
                f"NOTE: {len(_skipped_files)} file(s) could not be extracted "
                f"and were skipped (nexus-deyd5, see logs for path + reason) "
                f"— every other file still indexed normally."
            )

    # duoak 2C: flush remaining staged chunks and surface per-file upload
    # failures (containment: a failed batch fails its contributing files,
    # never the run — nexus-wcs39).
    if _batcher is not None:
        _drain_batcher_with_markers(_batcher, on_phase)
        _bstats = _batcher.stats
        # nexus-lde88 G3: "flush_seconds" is now the FULL flush wall (upload
        # + hooks); "upload_seconds" is the network write alone — the number
        # the old "flush_seconds" name silently under-reported by ~4x while
        # calling itself "upload". Both are reported explicitly, never
        # conflated under one name.
        _upload_s = _bstats["upload_seconds"]
        _flush_wall_s = _bstats["flush_seconds"]
        # nexus-lde88 G4: taxonomy/manifest/aspect used to merge into one
        # "flush-grain" number — split by hook name (see _flush_hook_by_name
        # above). .get(..., 0.0) covers a hook that never fired this run
        # (e.g. no extractor configured for any collection -> no aspect
        # enqueues) without pretending it ran for 0.0s of real work.
        _taxonomy_s = _flush_hook_by_name.get("taxonomy_assign_batch_hook", 0.0)
        _manifest_s = _flush_hook_by_name.get("manifest_write_batch_hook", 0.0)
        _aspect_s = _flush_hook_by_name.get("aspect_enqueue", 0.0)
        # nexus-jb4pp: the pre-upload RUNFENCE stamp (on_batch_begin ->
        # _fence_begin_many). A real round trip per flush that no timer
        # covered — not upload, not any file/flush hook bucket — so the
        # per-grain report could not be made to sum honestly.
        _fence_s = _bstats.get("begin_hook_seconds", 0.0)
        # nexus-itpdc: seconds threads spent WAITING for the
        # LockedHookRegistry mutex. Reported separately because it is NOT
        # a sixth kind of work — it is already inside the file_*/flush_*
        # buckets above, inflating whichever grain happened to queue. It
        # is the difference between "the taxonomy hook is slow" and "the
        # taxonomy hook is BLOCKING the other workers".
        _lock_wait_s = float(getattr(hooks, "lock_wait_seconds", 0.0) or 0.0)
        _log.info(
            "index_chunk_batch_stats",
            flushes=int(_bstats["flushes"]),
            upload_seconds=round(_upload_s, 1),
            flush_wall_seconds=round(_flush_wall_s, 1),
            flush_fence_seconds=round(_fence_s, 1),
            file_batch_seconds=round(_hook_seconds["file_batch"], 1),
            file_single_seconds=round(_hook_seconds["file_single"], 1),
            file_document_seconds=round(_hook_seconds["file_document"], 1),
            flush_taxonomy_seconds=round(_taxonomy_s, 1),
            flush_manifest_seconds=round(_manifest_s, 1),
            flush_aspect_seconds=round(_aspect_s, 1),
            hook_lock_wait_seconds=round(_lock_wait_s, 1),
        )
        # nexus-jb4pp: per-op split for the SHARED service-catalog handle
        # (manifest write_many, RUNFENCE begin_index_run_many, ...). Its
        # module lock is held across the network call, so a hook's measured
        # cost silently includes other threads' catalog traffic; this is
        # the only place the two are separable.
        from nexus.catalog.factory import service_catalog_op_stats  # noqa: PLC0415 — deferred to avoid circular import (catalog.factory)
        for _op, _st in sorted(service_catalog_op_stats().items()):
            _log.info(
                "index_catalog_op_stats",
                op=_op,
                calls=int(_st["calls"]),
                lock_wait_seconds=round(_st["lock_wait_s"], 1),
                call_seconds=round(_st["call_s"], 1),
            )
        # nexus-ldab2: mirrors the catalog per-op report immediately above,
        # for the service-T2 singleton (taxonomy_assign, aspect_enqueue).
        from nexus.mcp_infra import service_t2_op_stats  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
        for _op, _st in sorted(service_t2_op_stats().items()):
            _log.info(
                "index_t2_op_stats",
                op=_op,
                calls=int(_st["calls"]),
                lock_wait_seconds=round(_st["lock_wait_s"], 1),
                call_seconds=round(_st["call_s"], 1),
            )
        if on_phase is not None and _bstats["flushes"]:
            on_phase(
                f"Chunk batching: {int(_bstats['flushes'])} upload batches, "
                f"{_upload_s:.1f}s upload ({_flush_wall_s:.1f}s full flush "
                f"wall); fence {_fence_s:.1f}s; hooks "
                f"{_hook_seconds['file']:.1f}s file-grain "
                f"(batch={_hook_seconds['file_batch']:.1f}s "
                f"single={_hook_seconds['file_single']:.1f}s "
                f"document={_hook_seconds['file_document']:.1f}s) + "
                f"{_hook_seconds['flush']:.1f}s flush-grain "
                f"(taxonomy={_taxonomy_s:.1f}s "
                f"manifest={_manifest_s:.1f}s "
                f"aspect={_aspect_s:.1f}s)"
                + (f"; hook lock wait {_lock_wait_s:.1f}s (included above)"
                   if _lock_wait_s else "")
            )
        _batch_failures = _batcher.failed_files
        if _batch_failures:
            _log.error(
                "index_batch_upload_failures",
                count=len(_batch_failures),
                files=sorted(_batch_failures)[:20],
            )
            if on_phase is not None:
                on_phase(
                    f"WARNING: {len(_batch_failures)} file(s) FAILED chunk "
                    f"upload (see logs); their vectors are absent or partial: "
                    + ", ".join(sorted(_batch_failures)[:5])
                    + ("…" if len(_batch_failures) > 5 else "")
                )

        # nexus-7lw6a: taxonomy_assign_batch_failed (e.g. an HTTP 500 from
        # the assign endpoint) used to be logged at WARNING and counted
        # nowhere — the run reported clean while the affected chunks lost
        # their topic assignments. Same "SAME shape as the existing
        # flush-failure reporting" the bead asks for: an on_phase WARNING
        # line, conditional (no phantom line on a clean run), read from the
        # nexus-7lw6a run-scoped counters reset above.
        from nexus.mcp_infra import taxonomy_assign_run_stats  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
        _taxonomy_stats = taxonomy_assign_run_stats()
        _taxonomy_batches_failed = _taxonomy_stats["failed_batches"]
        if _taxonomy_batches_failed:
            _log.error(
                "index_taxonomy_assign_batch_failures",
                batches_failed=_taxonomy_batches_failed,
                chunks_failed=_taxonomy_stats["failed_chunks"],
                batches_attempted=_taxonomy_stats["attempted"],
            )
            if on_phase is not None:
                on_phase(
                    f"WARNING: {_taxonomy_batches_failed} taxonomy-assign "
                    f"batch(es) FAILED ({_taxonomy_stats['failed_chunks']} "
                    f"chunk(s) affected) — see the taxonomy_assign_batch_"
                    f"failed log events for details."
                )

    # Post-processing phase markers (nexus-vatx Gap 2): the per-file
    # progress bar ends at "[N/N]" but the pipeline keeps running for
    # pruning, stamping, and catalog registration. Without markers the
    # operator sees silence and cannot tell hung from busy.
    #
    # Review remediation (Reviewer A/I-1): wrap the block in try/finally
    # so the closing "Post-processing complete" marker fires even when
    # a post-processing step raises. Without this, an exception inside
    # _prune_misclassified or _catalog_hook would leave the operator
    # staring at the last `[post] Pruning …` line — exactly the hung/busy
    # ambiguity Gap 2 was meant to fix.
    post_t0 = time.monotonic()

    def _phase(msg: str) -> None:
        if on_phase is not None:
            on_phase(msg)

    post_error: BaseException | None = None
    try:
        # Prune misclassified chunks (reclassification cleanup). Kept
        # UNCONDITIONAL: prune safety when a file's classification changes
        # without a content re-write is not airtight, and the only durable
        # signal (a catalog physical_collection delta) is consumed by the
        # register pass BEFORE this runs — a crash between the two would
        # orphan stale chunks if we gated on it (nexus-yz8bt, nx_plan_audit
        # CRITICAL). nexus-qgc4b originally called the prune passes "cheap";
        # the duoak.11 gate disproved that (226.6s / ~11% of wall at 2,133
        # files) — the cost was the per-doc serial manifest fetch, now
        # batched inside _prune_misclassified_in_collection (nexus-yz8bt),
        # so the pass stays unconditional AND fast.
        # nexus-c21fk: manifest self-heal. The staleness check keys on T3
        # chunk state only, so a doc whose chunks exist in T3 but whose
        # document_chunks manifest rows were dropped is skipped as
        # "current" forever — and the post-store manifest hooks that
        # skipped files never trigger can never repair it. This pass
        # rebuilds those manifests from the T3 chunks already stored (NO
        # re-embedding) via the shared heal core. PLACEMENT IS LOAD-BEARING
        # (critique 4711f521 Critical): it runs AFTER per-file indexing so
        # gaps created by THIS run's own manifest-write hook are healed
        # too, and BEFORE the prune passes below so the manifest-keyed GC
        # (the nexus-mr89x hazard) never sees an unhealed gap. Cost
        # decision (review 4711f521 Medium-3, accepted deliberately): the
        # detection is one owner-scoped by_owner + one batched
        # get_manifests per run — bounded, and the price of self-heal
        # actually meaning self-heal; ghost-class T3 fetches dedupe to
        # near-nothing because identical empty files share one
        # content_hash. Best-effort: a heal failure must never fail the
        # index run.
        _phase("Catalog manifest self-heal…")
        _t = time.monotonic()
        try:
            from nexus.catalog.factory import make_catalog_writer as _mk_writer  # noqa: PLC0415 — deferred import
            from nexus.catalog.manifest_heal import heal_manifest_gaps  # noqa: PLC0415 — deferred: keeps indexer import-light
            from nexus.db import make_t3 as _mk_t3  # noqa: PLC0415 — deferred import

            if _cat is not None:
                _, _heal_repo_hash = _repo_identity(repo)
                _heal_owner = _cat.owner_for_repo(_heal_repo_hash)
                if _heal_owner is not None:
                    from nexus.catalog.write_priority import await_fair_window  # noqa: PLC0415 — deferred import
                    _heal_writer_box: list = []

                    def _tracked_writer():
                        _heal_writer_box.append(_mk_writer())
                        return _heal_writer_box[0]

                    def _yield_fair():
                        # RDR-146 P2: yield to a foreground interactive
                        # writer before every heal write (GH #1046 class).
                        if _heal_writer_box:
                            await_fair_window(
                                _heal_writer_box[0].is_interactive_write_pending,
                                on_locked,
                            )

                    heal = heal_manifest_gaps(
                        _cat.by_owner(_heal_owner), _cat, _mk_t3,
                        _tracked_writer, yield_before_write=_yield_fair,
                    )
                    if heal.reconciled or heal.lost or heal.write_failed:
                        _phase(
                            f"Catalog manifest self-heal: "
                            f"{heal.reconciled} restored"
                            + (f", {len(heal.lost)} chunks LOST (real gap)"
                               if heal.lost else "")
                            + (f", {heal.write_failed} write failure(s)"
                               if heal.write_failed else "")
                        )
                    _log.info(
                        "catalog_manifest_self_heal",
                        candidates=heal.candidates, gapped=heal.gapped,
                        ghost_gapped=heal.ghost_gapped,
                        reconciled=heal.reconciled,
                        write_failed=heal.write_failed,
                        lost=len(heal.lost),
                        never_chunked=len(heal.never_chunked),
                        elapsed_s=round(time.monotonic() - _t, 1),
                    )
        except Exception:  # noqa: BLE001 — best-effort self-heal; error surfaced via log, must not crash the index run
            _log.warning("catalog_manifest_self_heal_failed", exc_info=True)
        _phase(f"Catalog manifest self-heal done ({time.monotonic() - _t:.1f}s)")

        _phase("Pruning misclassified chunks…")
        _t = time.monotonic()
        if delta_changed is not None:
            _phase("  skipped (--since-head: full-tree pass)")
        else:
            _prune_misclassified(
                repo, code_collection, docs_collection,
                [f for _, f in code_files],
                [f for _, f in prose_files],
                [f for _, f in pdf_files],
                db,
                file_to_doc_id=file_to_doc_id,
                catalog=_cat,
                # nexus-ch0gk: route the manifest-guard's
                # atomic_manifest_replace write through the writer already
                # in scope (minted at 4089-4095, its own .close() is a
                # documented no-op for the shared service handle) instead
                # of forwarding a write through the READER (_cat) via
                # _SharedServiceCatalogHandle.__getattr__'s unwhitelisted
                # forwarding — factory.py's documented "mixed sites hold
                # BOTH a reader and a writer" convention, made explicit.
                catalog_writer=_migrate_writer,
            )
        _phase(f"Pruning misclassified done ({time.monotonic() - _t:.1f}s)")

        # C3: Prune deleted files. Remove orphan chunks via the catalog
        # manifest (RDR-108 Phase 4 / nexus-dyxe).
        _phase("Pruning deleted files…")
        _t = time.monotonic()
        if delta_changed is not None and not delta_deleted:
            _phase("  skipped (--since-head: no deletions in delta)")
        else:
            if delta_deleted:
                # nexus-fltb4: git said these paths are GONE (rename detection
                # already applied) — delete their catalog docs NOW so the
                # manifest CASCADE exposes their chunks to the orphan sweep
                # below. The full-walk path never reaches here with
                # delta_deleted set.
                _delete_docs_for_paths(repo, delta_deleted)
            _prune_deleted_files(
                code_collection, docs_collection, db, catalog=_cat,
                rdr_collection=rdr_col_name, on_phase=on_phase,
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
        # nexus-3lswy: rdr_col is already the real object built alongside
        # code_col/docs_col above — no need to recompute the name or
        # re-fetch the collection here.
        if rdr_col is not None:
            try:
                stamp_collection_version(rdr_col)
            except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
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

    # nexus-deyd5 round 3 (coordinator directive, closing the round-2 HIGH
    # finding): the systemic-skip VERDICT is computed HERE — after every
    # run_file_loop category, the batcher's drain, and post-processing have
    # ALL completed normally — never as a raise from inside a loop. Round
    # 2's mid-loop raise fired after only ONE category (whichever hit the
    # floor first), before the drain or the remaining categories ran,
    # discarding already-staged-but-unflushed chunks (ChunkBatcher has no
    # __del__/atexit) and surfacing as an uncaught traceback instead of a
    # clean CLI exit — reachable on a routine corpus shape (a scanned-PDF
    # archive: the default extractor never runs OCR, MinerU is opt-in).
    # "Attempted" is the aggregate across all 4 categories, not just PDF —
    # a run-level verdict, matching how the skip count itself is already
    # aggregated via the shared _on_skip callback.
    #
    # AGGREGATE, not per-category — a DELIBERATE trade-off (coordinator
    # decision, round 3), recorded here so it stays visible rather than an
    # invisible choice a future reader has to rediscover. Round 2 (before
    # the mid-loop-raise regression) evaluated the floor separately for
    # each of the 4 categories; round 3 sums them into one run-level
    # check instead. Cost of that choice, named explicitly: a code/prose-
    # dominant repo whose PDF subset is COMPLETELY unextractable now
    # dilutes below the ratio against the much larger code/prose
    # denominator and gets only the informational
    # "skipped_unextractable_files" note, never the non-zero exit — a
    # per-category version would have hard-failed that PDF subset on its
    # own. A PDF-DOMINANT corpus (the original round-2 finding's actual
    # scenario — a scanned-PDF archive) is still caught either way, since
    # there PDFs already dominate the denominator. Accepted deliberately,
    # not overlooked: per-category re-introduces exactly the "which
    # category's breach fires first, before the others run" shape this
    # round's fix moved away from, for a narrower benefit (catching a
    # small broken subcategory inside an otherwise-healthy large repo).
    _files_attempted_total = (
        len(code_files) + len(prose_files) + len(pdf_files) + len(rdr_md_paths)
    )
    _systemic_extraction_failure = skip_floor_breached(
        len(_skipped_files), _files_attempted_total,
    )

    # nexus-7lw6a: final snapshot for the return dict below — read fresh
    # here (rather than reusing the `if _batcher is not None:` block's
    # local above) so this holds even in the degenerate case of no batcher
    # at all (nothing attempted, all zeros — never a phantom failure).
    from nexus.mcp_infra import taxonomy_assign_run_stats  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
    _final_taxonomy_stats = taxonomy_assign_run_stats()

    # nexus-qgc4b: files_changed drives the caller's post-index gate. RDR
    # (re)indexing is a content change too, so an RDR-only run still triggers
    # taxonomy discovery. nexus-3lswy: RDR files are now folded into
    # _files_written (4th run_file_loop category) — do NOT also add
    # rdr_indexed, or an RDR-only re-index would double-count.
    return {
        "rdr_indexed": rdr_indexed,
        "rdr_current": rdr_current,
        "rdr_failed": rdr_failed,
        "files_changed": _files_written,
        # nexus-wi1uv round-2: count of PDFs that failed the post-extraction
        # quality gate this run (contained per-file, never aborted the run).
        # index_repo_cmd uses this to drive a non-zero exit after
        # post-processing completes.
        "pdf_quality_gate_failed": len(_quality_gate_failed),
        # nexus-4s1ww / GH #1432: count of files whose chunk-batch flush
        # PERMANENTLY failed this run (ChunkBatcher.failed_files — post
        # bisect-retry survivors only, see chunk_batcher.py). Zero chunks
        # from these files landed. index_repo_cmd uses this the same way
        # as pdf_quality_gate_failed: a non-zero count drives a non-zero
        # exit after the rest of the run completes, so a total-write-path
        # failure is never reported as a clean "Done." at rc=0.
        "chunk_flush_failed_files": len(_batch_failures),
        # nexus-deyd5: files skipped this run because they could not be
        # extracted (nexus.errors.UnextractableContentError, caught by
        # run_file_loop). Deliberately NOT wired into a non-zero exit on
        # its own — unlike pdf_quality_gate_failed / chunk_flush_failed_
        # files, no data was lost by skipping ONE file. index_repo_cmd
        # reports this count informationally UNLESS
        # systemic_extraction_failure is also True (see below).
        "skipped_unextractable_files": len(_skipped_files),
        # nexus-deyd5 round 3: the denominator for the ratio above, and
        # for index_repo_cmd's "attempted N, skipped M" message.
        "files_attempted_total": _files_attempted_total,
        # nexus-deyd5 round 3: the run-level systemic-skip verdict (see
        # nexus.indexer_utils.skip_floor_breached). True means the skip
        # population is no longer plausible per-file noise — index_repo_
        # cmd converts this into a non-zero exit AFTER everything above
        # (drain, RDR loop, post-processing) has already completed and
        # been committed; nothing successful is discarded by this flag
        # being True.
        "systemic_extraction_failure": _systemic_extraction_failure,
        # nexus-tevzq: per-collection-kind attribution for the caller's
        # discover gate. Keys match the collection-name content_type prefix
        # (RDR-103 shape). prose+pdf both land in docs__.
        "files_changed_by_kind": {
            "code": _code_written,
            "docs": _prose_written + _pdf_written,
            "rdr": _rdr_written,
        },
        # nexus-7lw6a: taxonomy-assign batch outcomes for this run (reset at
        # the top of this function, accumulated by every
        # taxonomy_assign_batch_hook flush-grain fire). index_repo_cmd uses
        # "attempted" as the denominator for the exit-code policy: a
        # PARTIAL loss (some batches failed) stays rc=0 — the summary line
        # is the signal — while a TOTAL loss (every attempted batch failed,
        # the GH #1432 class) drives a non-zero exit.
        "taxonomy_assign_batches_attempted": _final_taxonomy_stats["attempted"],
        "taxonomy_assign_batches_failed": _final_taxonomy_stats["failed_batches"],
        "taxonomy_assign_chunks_failed": _final_taxonomy_stats["failed_chunks"],
    }


def _is_under(child: Path, parent: Path) -> bool:
    """Return True if *child* is a descendant of *parent*."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
