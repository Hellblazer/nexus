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


# nexus-u8n4r: worktree/tempdir path classifier + write-time registration
# guard, moved here from commands/catalog_cmds/reconcile_stale.py
# (nexus-wq1e4's read-only "safe to tombstone" signal) so BOTH the
# reconcile-stale census AND the index-time hooks below share ONE
# predicate instead of a forkable second copy.
#
# A Claude Code worktree lives at ``<repo>/.claude/worktrees/<agent>/`` —
# a subdirectory INSIDE the repo it was spun from. An index run over the
# PRIMARY repo that does not exclude that subtree walks straight into an
# agent's ephemeral checkout and registers its files under the primary
# owner; once the worktree is torn down those catalog rows point at
# nothing (nexus-u8n4r evidence: 4,002 of 4,014 orphaned-path docs in the
# 2026-08-03 production cleanup).
_WORKTREE_MARKER = "/.claude/worktrees/"
# Deliberately NOT "/var/folders/" (macOS's broad per-user cache root —
# also where pytest's own tmp_path fixture lives, so it is too noisy a
# signal): only the canonical, explicitly-a-scratch-dir roots count here.
_TEMP_DIR_PREFIXES = ("/tmp/", "/private/tmp/")


def is_worktree_or_tempdir_path(path_str: str) -> bool:
    """True when *path_str* looks like a worktree or system temp-dir path.

    Shared by ``nx catalog reconcile-stale`` (nexus-wq1e4's operator-
    visible "safe to tombstone" signal for ``orphaned_path`` — a deleted
    worktree or scratch checkout is corroborated evidence the source is
    genuinely gone, not merely unresolved) and
    :func:`should_skip_ephemeral_registration` below (nexus-u8n4r's
    write-time prevention of the same pollution class).
    """
    if _WORKTREE_MARKER in path_str:
        return True
    return path_str.startswith(_TEMP_DIR_PREFIXES)


def canonicalize_worktree_path(path_str: str) -> str:
    """Rewrite a Claude Code agent-worktree path to its primary-repo form.

    nexus-kkumv prevention half. A worktree lives at
    ``<primary>/.claude/worktrees/<agent>/<rel>`` — a git worktree
    nested INSIDE the repo it was spun from. A bulk indexing hook that
    registers a document by its raw on-disk absolute path (the curator-
    owner call sites in ``doc_indexer.py``/``pipeline_stages.py``, whose
    owner carries an empty ``repo_root`` by design and so never trips
    :func:`should_skip_ephemeral_registration`'s owner-root exception)
    mints a catalog identity that points at the worktree instead of the
    primary checkout. Once the worktree is torn down that row orphans
    permanently — the 133-row ``.claude/worktrees/agent-*`` class in the
    nexus-mlu3k census.

    Returns *path_str* unchanged when it carries no worktree marker.
    When it does, returns ``<primary-root>/<rel>`` — *primary-root* is
    everything before ``/.claude/worktrees/`` and *rel* is everything
    after the worktree's own agent-directory segment (the first path
    component past the marker). A worktree-root path with no ``rel``
    (nothing past the agent segment) returns the bare primary root.

    Pure path arithmetic — never touches the filesystem or asserts the
    rewritten path exists. Callers that must not register a document
    under a primary-repo path with no on-disk mirror there (a
    worktree-unique draft) check ``Path(result).is_file()`` themselves,
    mirroring the existing ``indexer.py`` precedent for the same
    hazard on the ``nx index repo`` path.
    """
    idx = path_str.find(_WORKTREE_MARKER)
    if idx == -1:
        return path_str
    primary_root = path_str[:idx]
    rest = path_str[idx + len(_WORKTREE_MARKER):]
    _agent, _sep, rel = rest.partition("/")
    if not rel:
        return primary_root
    return f"{primary_root}/{rel}"


def owner_repo_root_best_effort(reader: Any, owner: Any) -> str:
    """Best-effort fetch of *owner*'s ``repo_root`` from *reader*.

    Returns ``""`` when *reader* lacks ``get_owner_by_prefix`` (several
    lightweight test doubles across the suite implement only the
    read/write surface the hook under test exercises, not the full
    ``CatalogReader`` protocol) or the lookup otherwise raises. This
    degrades exactly like an owner with no ``repo_root`` — see
    :func:`should_skip_ephemeral_registration`'s own "empty repo_root
    never fires" contract — so a reader that cannot answer the question
    never turns a working (if unguarded) call site into a crash.
    """
    getter = getattr(reader, "get_owner_by_prefix", None)
    if getter is None:
        return ""
    try:
        info = getter(str(owner)) or {}
    except Exception:  # noqa: BLE001 — best-effort: a broken lookup must not crash the caller
        # nexus-u8n4r review fix (code-review-expert M1): this branch is a
        # BROKEN reader (raised), not the by-design "curator owner has no
        # repo_root" case (``getter`` returning ``{}``/``None`` cleanly,
        # which reaches the return below without logging). Both degrade to
        # the same "" — the guard never fires either way — but an operator
        # diagnosing "why didn't the guard catch this" needs to be able to
        # tell them apart.
        _log.warning(
            "owner_repo_root_lookup_failed_guard_inert",
            owner=str(owner),
            exc_info=True,
        )
        return ""
    return info.get("repo_root", "") if isinstance(info, dict) else ""


def reconstruct_absolute_registered_path(
    original_file_path: str, relativized_fp: str, owner_repo_root: str,
) -> str:
    """Rebuild the ABSOLUTE registered identity for the ephemeral-path
    guard, undoing a caller-side relativization step.

    nexus-u8n4r review fix (code-review-expert C1): ``nx catalog
    register`` (CLI) and the ``catalog_register`` MCP tool both
    relativize an absolute ``file_path`` against a matching KNOWN repo
    root before storing it (``make_relative`` strips the leading ``/``).
    Testing that post-relativization string against
    :func:`is_worktree_or_tempdir_path` — whose marker requires a
    leading ``/`` — was silently inert for exactly the common shape: a
    worktree nested inside an already-registered repo. This mirrors the
    reconstruction the bulk ``_catalog_hook`` performs
    (``main_repo / rel_path``) for these single-doc registration sites.

    Resolution order:

    * *original_file_path* is absolute -> return it VERBATIM (the
      pre-relativization identity — unaffected by whatever relativizing
      the caller did afterward).
    * else, *relativized_fp* and *owner_repo_root* are both non-empty ->
      ``owner_repo_root / relativized_fp``.
    * else -> whichever of *relativized_fp* / *original_file_path* is
      non-empty (unresolvable either way; same fallback the call sites
      used pre-fix).
    """
    if original_file_path and Path(original_file_path).is_absolute():
        return original_file_path
    if relativized_fp and owner_repo_root:
        return str(Path(owner_repo_root) / relativized_fp)
    return relativized_fp or original_file_path


def should_skip_ephemeral_registration(
    registered_path: str, owner_repo_root: str | None,
) -> bool:
    """True when *registered_path* must be REFUSED catalog registration.

    nexus-u8n4r. The guard tests the REGISTERED identity — the path as it
    will be STORED (owner ``repo_root`` + relative path for repo owners;
    the stored absolute ``file_path`` for hook call sites that store
    paths verbatim) — NEVER the on-disk ``abs_path`` alone. Post-
    nexus-zr2ie, ``nx index repo <worktree>`` legally registers main-
    repo-anchored paths (the on-disk absolute path is inside the
    worktree, but the derived registered identity is clean) —
    ``tests/test_index_cmd.py::test_index_repo_accepts_git_worktree`` and
    the ``TestCatalogHookForeignCwd`` family pin that worktrees stay
    accepted. The pollution class this guards against is registered
    paths that THEMSELVES contain a worktree or temp-dir marker.

    Skip (return True) when: *registered_path* matches
    :func:`is_worktree_or_tempdir_path` AND *owner_repo_root* is
    non-empty AND *owner_repo_root* does NOT itself match the predicate.
    The owner-root exception keeps two legal populations working:

    * throwaway owners explicitly rooted in a worktree/tempdir (gate
      sandboxes with their own config dirs) — population (a);
    * the entire unit/e2e suite, which indexes repos under pytest's own
      tmp dirs — population (b).

    KNOWN RESIDUAL: curator owners (the default owner for ``nx index
    md`` / ``nx index rdr`` / ``nx index pdf``) normally carry an EMPTY
    ``repo_root`` — passing ``""`` or ``None`` here always returns
    ``False``, so the guard never fires for them. Out of scope for
    nexus-u8n4r; documented here rather than silently gapped.
    """
    if not registered_path or not is_worktree_or_tempdir_path(registered_path):
        return False
    if not owner_repo_root:
        return False
    return not is_worktree_or_tempdir_path(owner_repo_root)


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
        except Exception as exc:  # noqa: BLE001 — best-effort catalog resolve; logged then falls through to synthesis
            _log.debug(
                "registry_resolve_catalog_failed",
                repo=str(repo),
                content_type=content_type,
                error=str(exc),
            )
    from nexus.corpus import resolve_write_embedding_model  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus)

    if content_type not in ("code", "docs", "rdr"):
        raise ValueError(
            f"_resolve_repo_collection: unknown content_type {content_type!r}"
        )
    name, path_hash = _repo_identity(repo)
    sanitised = _sanitise_owner_segment(name)

    _db_cache: list = []  # nexus-o5x2c: memoize make_t3() across candidates below

    def _local_token_collection_exists(token: str) -> bool:
        # nexus-o5x2c: this NO-CATALOG / unregistered-owner synthesis
        # fallback has no live T3 handle in scope — best-effort construct
        # one purely to probe for a pre-existing collection to
        # grandfather onto. Wrapped by resolve_write_embedding_model's
        # own try/except, so a failure here degrades to the pre-fix
        # behavior (no grandfather found), never a crash of its own.
        # Constructed ONCE and reused across every candidate in
        # LOCAL_EMBEDDING_MODELS (reviewer follow-up), not once per
        # candidate.
        if not _db_cache:
            from nexus.db import make_t3  # noqa: PLC0415 — deferred, probe-local
            _db_cache.append(make_t3())
        candidate = _safe_collection(
            f"{content_type}__", sanitised, path_hash, suffix=f"__{token}__v1",
        )
        return _db_cache[0].collection_exists(candidate)

    model = resolve_write_embedding_model(
        content_type, collection_exists=_local_token_collection_exists,
    )
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
    from nexus.corpus import (  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus)
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
    except Exception:  # noqa: BLE001 — boundary catch of T3 client errors; degrade to empty sibling list
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
    "is_worktree_or_tempdir_path",
    "list_sibling_collections",
    "owner_repo_root_best_effort",
    "reconstruct_absolute_registered_path",
    "should_skip_ephemeral_registration",
)
