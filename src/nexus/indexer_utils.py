# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared utilities for the indexing pipeline.

Extracted from indexer.py to eliminate duplication across code_indexer,
prose_indexer, and _index_pdf_file.

Leaf-ish module: imports from nexus.retry and nexus.errors only.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from nexus.errors import CredentialsMissingError
from nexus.retry import _vector_with_retry

_log = structlog.get_logger(__name__)

#: Surface-a-slow-drain threshold (seconds) when draining in-flight workers after
#: a concurrent index failure (nexus-7yfe6). This is an OBSERVABILITY bound, not a
#: kill: Python threads can't be force-killed, so an in-flight upsert runs until
#: its own socket timeout (600s for /v1/vectors/upsert-chunks). If the drain
#: exceeds this threshold we WARN with the in-flight count so the run reads as
#: "draining N slow workers" instead of a silent hang. The common transient-5xx
#: case never reaches here — it's contained per-file upstream (see
#: indexer._contain_transient_upsert).
_FAILURE_DRAIN_TIMEOUT_S = 120.0

# Patterns always ignored (mirrors indexer.DEFAULT_IGNORE).
_DEFAULT_IGNORE: list[str] = [
    "node_modules", "vendor", ".venv", "__pycache__", "dist", "build", ".git",
    "*.lock", "go.sum",
]


def find_repo_root(path: Path) -> Path | None:
    """Return the git repository root containing *path*, or None.

    Uses ``git rev-parse --show-toplevel`` so it works from any subdirectory.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        pass
    return None


#: Tokens that stay all-caps after filename normalisation. Common
#: initialisms / acronyms used in technical filenames where naive
#: title-casing would mis-render them ("api" → "Api" is wrong).
_PRESERVE_UPPER: frozenset[str] = frozenset({
    "ai", "ml", "api", "url", "uri", "pdf", "html", "css", "js",
    "ts", "json", "yaml", "xml", "sql", "cli", "ide", "io",
    "rdr", "mcp", "llm", "gpu", "cpu", "tcp", "udp", "ssl", "tls",
    "ssh", "ftp", "smtp", "http", "https", "rest", "rpc", "uuid",
    "art", "bert", "lstm", "rnn", "cnn", "gan",
    "nlp", "ocr", "tts", "stt",
    "v1", "v2", "v3", "v4", "v5",
})

_INITIALISM_DIGIT_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _normalise_filename_token(token: str) -> str:
    """Title-case a single filename token, preserving known initialisms."""
    if not token:
        return token
    lowered = token.lower()
    if lowered in _PRESERVE_UPPER:
        return token.upper()
    m = _INITIALISM_DIGIT_RE.match(token)
    if m is not None:
        prefix, digits = m.group(1), m.group(2)
        if prefix.lower() in _PRESERVE_UPPER:
            return prefix.upper() + digits
    return token.capitalize()


def derive_title(path: Path, body: str | None) -> str:
    """Resolve a human-readable document title (nexus-8l6).

    Two-step fallback:

      1. **First H1 in *body*** — the first ``# Title`` line wins.
      2. **Normalised filename stem** — split on ``[_\\- ]``, title-case
         each token (preserving common initialisms via ``_PRESERVE_UPPER``).
    """
    if body:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("# "):
                continue
            title = stripped[2:].strip()
            if title:
                return title

    stem = path.stem or path.name
    if not stem:
        return ""
    tokens = re.split(r"[_\- ]+", stem)
    normalised = [_normalise_filename_token(t) for t in tokens if t]
    return " ".join(normalised) or stem


def detect_git_metadata(path: Path) -> dict[str, str]:
    """Return git provenance metadata for the repo containing *path*.

    Walks up via :func:`find_repo_root`, then collects:

      * ``git_project_name`` — basename of the repo root
      * ``git_branch`` — current branch name
      * ``git_commit_hash`` — full SHA of HEAD
      * ``git_remote_url`` — ``origin`` URL (empty when no remote)

    Returns an empty dict when *path* is not inside a git repository
    so callers can ``**``-merge the result without conditional logic.
    Indexer-side code (PDF / markdown / pipeline) needs this so chunks
    carry the same provenance the repo-walk path gets via
    ``indexer._git_metadata`` (nexus-2my fix #3).
    """
    repo = find_repo_root(path)
    if repo is None:
        return {}

    def _run(args: list[str]) -> str:
        try:
            r = subprocess.run(
                args, cwd=repo, capture_output=True, text=True, timeout=10,
            )
        except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    return {
        "git_project_name": repo.name,
        "git_branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit_hash": _run(["git", "rev-parse", "HEAD"]),
        "git_remote_url": _run(["git", "remote", "get-url", "origin"]),
    }


def _path_glob_match(parts: tuple[str, ...], segs: list[str]) -> bool:
    """Match a tuple of path components against pattern segments.

    Segments are the result of ``pattern.split("/")``. Each non-``**``
    segment is matched against one path component via ``fnmatch`` (so
    ``*`` does NOT cross ``/`` boundaries — the path-aware semantic users
    expect from a slash-separated glob). ``**`` matches zero or more
    components.
    """
    def walk(i: int, j: int) -> bool:
        while j < len(segs):
            seg = segs[j]
            if seg == "**":
                # ** matches zero or more components — try every split.
                for k in range(i, len(parts) + 1):
                    if walk(k, j + 1):
                        return True
                return False
            if i >= len(parts):
                return False
            if not fnmatch.fnmatch(parts[i], seg):
                return False
            i += 1
            j += 1
        return i == len(parts)
    return walk(0, 0)


def should_ignore(rel_path: Path, patterns: list[str]) -> bool:
    """Return True if *rel_path* matches any of *patterns*.

    Pattern semantics (.gitignore-flavored, path-aware):

    - **Path-style** patterns (contain ``/``) match against the full
      relative path component-by-component. ``*`` matches any single
      component (does not cross ``/``); ``**`` matches zero or more
      components. ``docs/papers/**`` matches every file under
      ``docs/papers/`` at any depth; ``src/*.py`` matches Python files
      directly under ``src/`` only.
    - **Part-style** patterns (no ``/``) match against each path
      component independently via ``fnmatch``. ``papers`` matches any
      file under a ``papers/`` directory anywhere in its path;
      ``*.lock`` matches any lock file; ``__pycache__`` matches any
      ``__pycache__`` directory. This preserves the original behaviour
      and the existing ``_DEFAULT_IGNORE`` patterns.

    Pre-fix history: this function used to feed each path component to
    ``fnmatch.fnmatch`` against every pattern. ``fnmatch`` treats ``/``
    as a literal, so a path-style pattern like ``docs/papers/**`` was
    silently ineffective — the matcher only ever saw the parts ``docs``,
    ``papers``, and ``foo.pdf`` independently, none of which can match a
    pattern containing ``/``. Configs that wrote ``docs/papers/**``
    expecting subtree exclusion were silent no-ops; the only patterns
    that worked were single-component or extension globs.
    """
    rel_parts = rel_path.parts
    for pattern in patterns:
        if "/" in pattern:
            segs = pattern.split("/")
            if _path_glob_match(rel_parts, segs):
                return True
        else:
            for part in rel_parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
    return False


def load_ignore_patterns(repo_root: Path | None = None) -> list[str]:
    """Return merged ignore patterns from defaults + ``.nexus.yml``.

    When *repo_root* is provided, picks up the per-repo config.
    """
    from nexus.config import load_config  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    cfg = load_config(repo_root=repo_root)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    return list(dict.fromkeys(_DEFAULT_IGNORE + cfg_patterns))


def is_gitignored(path: Path, repo_root: Path) -> bool:
    """Return True if *path* is ignored by git in *repo_root*.

    Uses ``git check-ignore`` for an authoritative answer that respects
    ``.gitignore``, ``.git/info/exclude``, and global gitignore config.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=repo_root, capture_output=True, timeout=10,
        )
        return result.returncode == 0  # 0 = ignored, 1 = not ignored
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return False


@dataclass
class StalenessCache:
    """Pre-fetched ``{lookup_key: (content_hash, embedding_model)}`` map
    for a single ChromaDB collection.

    Built once by :func:`build_staleness_cache` from a single paginated
    sweep of the collection's chunk metadata; consumed many times by
    :func:`check_staleness` so per-file checks become O(1) dict lookups
    instead of O(N) ChromaDB roundtrips.

    Two indexes:

    - ``by_doc_id`` keys on the catalog tumbler stored in chunk
      metadata. Populated only for chunks whose stored ``doc_id`` field
      is non-empty. The post-RDR-101-Phase-4 write path stamps
      ``doc_id`` on every new chunk; legacy chunks predating the
      backfill are absent from this index, which the cached
      ``check_staleness`` correctly treats as a cache miss → "stale" →
      re-index → ghost-chunk healed.
    - ``by_source_path`` keys on the chunk's ``source_path`` metadata.
      Populated for every chunk that carries a non-empty source_path
      (most of them; RDR-102 D2 dropped source_path from the canonical
      schema, so post-D2 chunks won't appear here — they live only in
      ``by_doc_id``). Used only when the caller has no doc_id (legacy /
      catalog absent code path).

    Why the cache exists: ``nx index repo`` on a healthy repo where
    nothing has changed is dominated by per-file
    ``col.get(where={doc_id})`` roundtrips just to confirm "yes,
    current, skip." On ART (~4,800 files) that's ~4,800 sequential
    ChromaDB Cloud calls at 50-200 ms each — 8-30 minutes of pure
    network latency before any actual indexing work. With the cache,
    the entire staleness phase is a single paginated sweep
    (~ceil(total_chunks / 300) calls) plus per-file dict lookups.
    """

    by_doc_id: dict[str, tuple[str, str]] = field(default_factory=dict)
    by_source_path: dict[str, tuple[str, str]] = field(default_factory=dict)


def build_staleness_cache(col: object) -> StalenessCache:
    """Walk *col* once and index its chunks for fast staleness lookup.

    Service-mode collections expose ``get_all_metadata()`` (nexus-duoak
    follow-up): ids + metadata for the WHOLE collection in one HTTP round
    trip, collapsing the ``ceil(N / 300)`` paginated ``/get`` calls this
    function used to pay (measured ~113s of a ~116s phase on this repo's own
    24k-chunk ``code__`` collection). Falls back to the paginated helper when
    the collection doesn't expose it (local Chroma mode) or the fast path
    raises (e.g. the server's row-count cap, or a transient failure) --
    ``get_all_metadata`` deliberately does NOT catch-and-degrade internally
    (see its docstring), so a fast-path failure here is a genuine signal to
    fall back, not silently swallowed.

    Errors are tolerated: a build failure returns an empty cache and
    callers fall through to the per-file Chroma path. Failing to
    populate the cache must never block indexing; it just costs latency.
    """
    cache = StalenessCache()
    all_chunks: dict | None = None
    _fast_path_failed = False
    _get_all_metadata = getattr(col, "get_all_metadata", None)
    if callable(_get_all_metadata):
        try:
            all_chunks = _get_all_metadata()
        except Exception as exc:  # noqa: BLE001 — fast-path failure is a fallback signal, never fatal
            # nexus-441p5: a fast-path failure must FALL BACK to the paginated
            # sweep, not degrade to an empty cache. Pre-fix, this exception
            # landed in the outer handler and returned an empty cache — every
            # subsequent index run treated all files stale (full re-process;
            # observed live 2026-07-07: wheel v6.3.6 calling get-all-metadata
            # against an install-era engine → 404 → 0-doc cache). The same
            # hole fires on current engines when a large collection trips the
            # server's get-all-metadata row-count cap.
            import structlog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

            if getattr(exc, "code", None) == 404:
                # nexus-5den3: the pre-v0.1.30 404 hint is only actionable in
                # local mode, where the operator IS the end user. Cloud-mode
                # users cannot upgrade a shared multi-tenant managed engine
                # themselves — post-nexus-jn0nm's fail-loud connection-time
                # probe, cloud users should rarely even reach this path, but
                # when they do (or in the local self-hosted version-skew
                # case) the hint text must match who can actually act on it.
                from nexus.config import is_local_mode  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

                _hint_prefix = "engine lacks POST /v1/vectors/get-all-metadata (pre-v0.1.30) — "
                hint = _hint_prefix + (
                    "upgrade the engine this install is pointed at"
                    if is_local_mode()
                    else (
                        "the managed nexus service needs to be upgraded by the "
                        "operator; no local action is possible"
                    )
                )
            else:
                hint = "falling back to the paginated sweep"
            structlog.get_logger(__name__).warning(
                "build_staleness_cache_fast_path_failed_falling_back",
                collection=getattr(col, "name", "<unknown>"),
                hint=hint,
                exc_info=True,
            )
            _fast_path_failed = True
    if all_chunks is None:
        try:
            # Local import to avoid a circular dependency at module-load
            # time. ``_paginated_get`` lives in nexus.indexer (the
            # orchestrator), which itself imports from this module.
            from nexus.indexer import _paginated_get  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

            all_chunks = _paginated_get(col, include=["metadatas"])
        except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            # nexus-lrhg (RDR-108 audit finding 6): pre-fix this swallowed
            # ``_paginated_get`` failures with a bare ``except: pass`` and
            # returned an empty cache. The caller fell back to the per-file
            # Chroma probe, which on a Phase-3 corpus means re-embedding
            # every chunk because the per-file cache misses are
            # indistinguishable from genuine stale rows. WARNING log with
            # the collection identity so a recurring outage (network blip,
            # cloud throttle) surfaces in production logs instead of
            # silently melting the embedder budget.
            import structlog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
            structlog.get_logger(__name__).warning(
                "build_staleness_cache_paginated_get_failed",
                collection=getattr(col, "name", "<unknown>"),
                exc_info=True,
            )
            return cache

    # nexus-441p5 critique (HIGH) — RESOLVED by nexus-ou4tb, comment kept
    # because it records why this warning exists at all. In service mode the
    # fallback rides ``HttpVectorClient.get()``, which USED TO swallow
    # ``VectorServiceError`` into an EMPTY page, making a degraded fallback
    # indistinguishable here from a genuinely empty collection; the except arm
    # above could never fire. ``get()`` now raises, so that arm DOES fire and
    # a degraded fallback returns early with
    # ``build_staleness_cache_paginated_get_failed`` instead of reaching here.
    #
    # This check therefore no longer covers a degraded service — it now means
    # what it literally says: the fast path failed AND the collection really
    # is empty. Still worth a warning (that combination re-processes every
    # file), but it is no longer the only signal an operator has, so the hint
    # no longer points at a log event that no longer exists.
    if _fast_path_failed and not (all_chunks.get("ids") or []):
        import structlog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        structlog.get_logger(__name__).warning(
            "build_staleness_cache_fallback_empty_after_fast_path_failure",
            collection=getattr(col, "name", "<unknown>"),
            hint=(
                "the fast path failed and the paginated fallback found zero "
                "chunks; since nexus-ou4tb a degraded fallback raises instead "
                "of reading empty (see build_staleness_cache_paginated_get_"
                "failed), so this most likely means the collection is "
                "genuinely empty — staleness cache is empty either way, so "
                "this run will re-process every file"
            ),
        )

    # nexus-0ocy (RDR-108 Phase 4 review D-M4): when chunk metadata
    # lacks ``doc_id`` (Phase-3 chunks) but carries ``chunk_text_hash``,
    # resolve via the catalog ``document_chunks`` manifest in one
    # batched call so by_doc_id stays useful for Phase-3 corpora.
    # Empty fallback is a clean cache miss (the existing perf path
    # for legacy chunks).
    metadatas = all_chunks.get("metadatas") or []
    chash_to_doc: dict[str, str] = {}
    needed_chashes = [
        (m or {}).get("chunk_text_hash", "")
        for m in metadatas
        if m and not (m or {}).get("doc_id") and (m or {}).get("chunk_text_hash")
    ]
    if needed_chashes:
        try:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
            _cat = make_catalog_reader()
            if _cat is not None:
                by_chash = _cat.docs_for_chashes(list(set(needed_chashes)))
                for c, doc_ids in by_chash.items():
                    if doc_ids:
                        chash_to_doc[c] = sorted(doc_ids)[0]
        except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            # nexus-8g79.8: pre-fix this swallowed the whole chash→doc_id
            # resolution silently, leaving every result without doc_id
            # in metadata (catalog-aware retrieval gated on doc_id then
            # no-ops). WARNING with the chash count so a recurring
            # catalog outage surfaces in production logs.
            import structlog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
            structlog.get_logger(__name__).warning(
                "docs_for_chashes_failed",
                chash_count=len(needed_chashes),
                exc_info=True,
            )

    for meta in metadatas:
        if not meta:
            continue
        content_hash = meta.get("content_hash", "")
        model = meta.get("embedding_model", "")
        if not (content_hash and model):
            continue
        value = (content_hash, model)
        doc_id = meta.get("doc_id", "")
        if not doc_id:
            chash = meta.get("chunk_text_hash", "")
            if chash:
                doc_id = chash_to_doc.get(chash, "")
        if doc_id:
            cache.by_doc_id[doc_id] = value
        source_path = meta.get("source_path", "")
        if source_path:
            cache.by_source_path[source_path] = value
    return cache


def check_staleness(
    col: object,
    source_file: object,
    content_hash: str,
    embedding_model: str,
    *,
    doc_id: str = "",
    cache: StalenessCache | None = None,
) -> bool:
    """Return True if the file is already indexed with an identical hash and model.

    Two execution modes:

    - **Cached (preferred when the orchestrator passes a cache).**
      Looks up ``doc_id`` in :attr:`StalenessCache.by_doc_id` (or
      ``source_file`` in :attr:`StalenessCache.by_source_path` for
      legacy / no-catalog callers). Pure dict lookup, no ChromaDB
      roundtrip. The orchestrator builds the cache once per collection
      via :func:`build_staleness_cache` before the per-file loop, so
      ``nx index repo`` on a healthy repo (most files current) pays
      one paginated sweep instead of one Chroma query per file.
    - **Per-file (back-compat).** When *cache* is ``None``, performs a
      ChromaDB ``get()`` wrapped in ``_vector_with_retry``. The retry
      logic is part of the staleness check's contract — callers must
      NOT wrap this call. Direct test callers and any caller that has
      not migrated to the cache stay on this path.

    Args:
        col: ChromaDB collection object. Unused when *cache* is supplied.
        source_file: Path (or string) of the source file being checked.
        content_hash: SHA-256 hex digest of the current file content.
        embedding_model: Target embedding model name.
        doc_id: Catalog ``doc_id`` for the file. When non-empty (RDR-101
            Phase 4, nexus-dcym), the chunk lookup keys on ``doc_id`` so
            that the staleness check stays consistent across renames and
            owner-scope changes. Empty falls back to the legacy
            ``source_path``-keyed lookup for chunks predating the
            doc_id backfill.
        cache: Optional :class:`StalenessCache`. When supplied the
            check is a dict lookup; when ``None`` the check is a Chroma
            roundtrip.

    Returns:
        True when the stored chunk has the same content_hash AND embedding_model,
        meaning the file is current and can be skipped.  False otherwise.
    """
    if cache is not None:
        if doc_id:
            stored = cache.by_doc_id.get(doc_id)
            # Cache miss when the caller has a doc_id heals a ghost
            # chunk by treating the file as stale: re-index will write
            # a chunk carrying doc_id metadata and the next sweep
            # populates by_doc_id for it. Mirrors the Chroma-path
            # behaviour at indexer_utils.check_staleness:291.
            if stored is None:
                return False
            return stored == (content_hash, embedding_model)
        # Legacy / no-doc_id caller: fall back to source_path lookup.
        stored = cache.by_source_path.get(str(source_file))
        if stored is None:
            return False
        return stored == (content_hash, embedding_model)

    # RDR-108 Phase 3 (nexus-bdag): chunks no longer carry ``doc_id`` —
    # the catalog ``document_chunks`` manifest is authoritative. Query
    # by ``content_hash`` (a file-level fingerprint that all chunks of
    # the same file share); falling back to ``source_path`` for legacy
    # chunks predating RDR-102 D2.
    where: dict
    if content_hash:
        where = {"content_hash": content_hash}
    else:
        where = {"source_path": str(source_file)}
    existing = _vector_with_retry(
        col.get,  # type: ignore[attr-defined]
        where=where,
        include=["metadatas"],
        limit=1,
    )
    if not existing["metadatas"]:
        return False
    stored = existing["metadatas"][0]
    if (
        stored.get("content_hash") != content_hash
        or stored.get("embedding_model") != embedding_model
    ):
        return False
    return True


def check_credentials(voyage_key: str, chroma_key: str) -> None:
    """Raise CredentialsMissingError if either API key is absent.

    Args:
        voyage_key: Voyage AI API key string (empty string = missing).
        chroma_key: ChromaDB API key string (empty string = missing).

    Raises:
        CredentialsMissingError: When one or both keys are absent.
    """
    missing: list[str] = []
    if not voyage_key:
        missing.append("voyage_api_key")
    if not chroma_key:
        missing.append("chroma_api_key")
    if missing:
        raise CredentialsMissingError(
            f"{', '.join(missing)} not set — run: nx config set <key> <value>"
        )


def check_local_path_writable() -> None:
    """Validate that the local ChromaDB path is writable.

    Raises:
        CredentialsMissingError: When the local path cannot be written to.
    """
    from nexus.stranded_install import legacy_chroma_dir  # noqa: PLC0415 — deferred import; legacy leg, dies at RDR-155 P3
    local_path = legacy_chroma_dir()
    try:
        local_path.mkdir(parents=True, exist_ok=True)
        test_file = local_path / ".write_test"
        test_file.touch()
        test_file.unlink()
    except OSError as exc:
        raise CredentialsMissingError(
            f"Local ChromaDB path {local_path} is not writable: {exc}"
        ) from exc


def build_context_prefix(
    filename: object,
    comment_char: str,
    class_name: str,
    method_name: str,
    line_start: int,
    line_end: int,
) -> str:
    """Return the embed-only context prefix for a code chunk.

    The prefix is prepended to chunk text before embedding (not stored in
    ChromaDB) to improve retrieval quality by giving Voyage AI additional
    context about the chunk's location in the codebase.

    Args:
        filename: Relative file path (str or Path).
        comment_char: Language comment character (e.g. "#", "//", "--").
        class_name: Enclosing class name from _extract_context, or "".
        method_name: Enclosing method name from _extract_context, or "".
        line_start: 1-indexed start line of the chunk.
        line_end: 1-indexed end line of the chunk.

    Returns:
        A single-line string like::

            # File: src/foo.py  Class: MyClass  Method: my_method  Lines: 10-25
    """
    return (
        f"{comment_char} File: {filename}"
        f"  Class: {class_name}  Method: {method_name}"
        f"  Lines: {line_start}-{line_end}"
    )


def build_doc_id_resolver(
    file_to_doc_id: Mapping[Path, str],
) -> Callable[[Path], str]:
    """Return a resolver mapping an indexed file path to its catalog doc_id.

    Lifted from ``indexer._run_index`` (nexus-kgyoz seam 2). The orchestrator
    builds *file_to_doc_id* from the pre-index catalog registration map, then
    wires the returned callable into :attr:`IndexContext.doc_id_resolver` so
    per-file indexers stamp the catalog cross-reference into chunk metadata at
    chunk-write time. Files absent from the map resolve to ``""`` — the legacy
    / no-doc_id signal that ``metadata_schema.normalize`` Step 4c then drops.

    The returned callable closes over *file_to_doc_id* by reference (no
    snapshot): later mutations to the passed mapping are visible through the
    resolver. The orchestrator builds it once from a finalised registration
    map and does not mutate afterward, so this is a non-issue at the call
    site; callers needing a frozen view should pass a copy.

    Args:
        file_to_doc_id: Mapping of indexed file path to catalog ``doc_id``.

    Returns:
        A callable ``(path) -> doc_id`` closing over *file_to_doc_id*.
    """
    def _resolver(path: Path) -> str:
        return file_to_doc_id.get(path, "")

    return _resolver


# ── Bounded file-level concurrency (nexus-cfc72) ─────────────────────────────


def resolve_index_concurrency() -> int:
    """Resolve the per-file indexing concurrency for ``nx index repo``.

    ``NX_INDEX_CONCURRENCY`` (>=1) wins when set and parseable. Otherwise
    the default is 2 when BOTH the vectors and catalog backends are the
    HTTP service (thread-safe httpx clients; the engine's TenantScope
    admission control bounds bursts to typed 503s) and 1 everywhere else
    — the direct-SQLite catalog on the legacy ``=sqlite`` opt-out is not
    thread-safe. The gate self-retires once nexus-7bomn removes that
    opt-out.

    The gate deliberately does NOT check the T2 "memory" backend
    (chash/taxonomy/aspect-queue writes): those routes only run inside
    the hook chains, which ``LockedHookRegistry`` serializes whenever
    concurrency > 1 — the lock, not this gate, is what makes a diverging
    memory backend safe (critique finding, nexus-cfc72). Narrowing the
    hook lock requires extending this gate. The local bge embedder is
    also concurrency-safe: onnxruntime ``InferenceSession.run`` supports
    concurrent calls on a shared session.
    """
    import os  # noqa: PLC0415 — leaf module keeps import surface minimal

    def _backend_default() -> int:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — deferred to avoid circular import (db.http_vector_client)
        from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deferred to avoid circular import (db.storage_mode)

        if (
            is_vector_service_mode()
            and storage_backend_for("catalog") == StorageBackend.SERVICE
        ):
            return 2
        return 1

    raw = os.environ.get("NX_INDEX_CONCURRENCY", "").strip()
    if raw:
        try:
            requested = max(1, int(raw))
        except ValueError:
            _log.warning(
                "nx_index_concurrency_invalid", value=raw,
                hint="expected an integer >= 1; using the backend default",
            )
        else:
            if requested > 1 and _backend_default() == 1:
                # Review finding (nexus-cfc72): the override wins, but
                # never silently — forcing concurrency onto a non-service
                # backend reintroduces the direct-SQLite hazard the
                # default gate exists to avoid.
                _log.warning(
                    "nx_index_concurrency_overrides_backend_gate",
                    value=requested,
                    hint="a non-service catalog/vectors backend is not "
                         "audited for concurrent indexing",
                )
            return requested
    return _backend_default()


def run_file_loop(
    files: list[tuple[float, Path]],
    index_one: Callable[[Path, float, object | None], int],
    *,
    concurrency: int,
    on_file: Callable[[Path, int, float], None] | None,
    on_stage_timers: Callable[[Path, object], None] | None,
) -> int:
    """Drive one per-file indexing loop, sequentially or with a bounded pool.

    Returns the number of files that wrote at least one chunk this run
    (``index_one`` returned > 0); staleness-skipped and failed files return 0
    and are not counted (nexus-qgc4b).

    ``index_one(file, score, timers) -> chunk_count`` is the loop body
    (the ``_index_code_file`` / ``_index_prose_file`` / ``_index_pdf_file``
    call). Contracts preserved from the legacy inline loops (nexus-cfc72):

    - ``concurrency <= 1`` is a plain sequential loop — identical
      ordering and error behavior to the pre-concurrency code.
    - Submission order is the caller's (frecency-descending) order, so
      high-value files start first even when completion interleaves.
    - ``on_file`` / ``on_stage_timers`` are invoked under one lock —
      the CLI progress renderer is not re-entrant. Per-file elapsed is
      measured inside the worker, so durations stay truthful.
    - A per-file ``StageTimers`` is built only when ``on_stage_timers``
      is subscribed, mirroring the nexus-7niu short-circuit.
    - Error semantics match the sequential loop: the first exception
      cancels all not-yet-started files and re-raises. In-flight files
      run to completion (callbacks included) before the raise — the
      shakeout's count-based assertions are order-independent, so a few
      extra completed files at failure time are indistinguishable from
      the sequential "run died at file X" shape.
    """
    import threading  # noqa: PLC0415 — leaf module keeps import surface minimal
    import time  # noqa: PLC0415 — leaf module keeps import surface minimal

    cb_lock = threading.Lock()

    def _make_timers() -> object | None:
        if on_stage_timers is None:
            return None
        from nexus.stage_timers import StageTimers  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep)

        return StageTimers()

    # nexus-qgc4b: count files that actually wrote chunks (index_one > 0).
    # A staleness-skipped or failed file returns 0. The caller gates the
    # expensive post-index passes (taxonomy discover/kmeans/labeling) on this
    # count being non-zero, so an all-skip re-index costs only the scan.
    written = [0]

    def _process(score: float, file: Path) -> None:
        t0 = time.monotonic()
        timers = _make_timers()
        chunks = index_one(file, score, timers)
        with cb_lock:
            if chunks > 0:
                written[0] += 1
            if on_file:
                on_file(file, chunks, time.monotonic() - t0)
            if on_stage_timers is not None and timers is not None:
                on_stage_timers(file, timers)

    if concurrency <= 1:
        for score, file in files:
            _process(score, file)
        return written[0]

    from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait  # noqa: PLC0415 — leaf module keeps import surface minimal

    with ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="nx-index",
    ) as pool:
        futures = [pool.submit(_process, score, file) for score, file in files]
        done, not_done = wait(futures, return_when=FIRST_EXCEPTION)
        if not not_done and not any(f.exception() for f in done):
            return written[0]
        # A failure (or spurious wake). Cancel everything not yet started,
        # let in-flight files finish, then harvest EVERY failure — a
        # concurrent secondary failure must be logged, never silently
        # dropped (critique finding, nexus-cfc72).
        for fut in not_done:
            fut.cancel()
        # nexus-7yfe6: SURFACE a slow drain (observability only — NOT a hard
        # bound). Python threads can't be force-killed, and the harvest loop below
        # calls fut.exception() which blocks until each in-flight future finishes,
        # so the real wall-time bound on a genuine (non-transient) failure racing a
        # wedged sibling remains that sibling's upsert socket timeout (600s at
        # http_vector_client.py). This bounded wait exists only to emit an early
        # WARNING with the in-flight count, so a slow drain reads as "draining N
        # workers" rather than a silent hang. The reported incident does NOT reach
        # here: a transient 5xx is contained per-file upstream
        # (indexer._contain_transient_upsert), so it never propagates into this
        # failure path. Truly bounding a fatal-error drain would require abandoning
        # in-flight threads (shutdown(wait=False)) — deferred, see nexus-7yfe6 notes.
        _still_running = wait(futures, timeout=_FAILURE_DRAIN_TIMEOUT_S).not_done
        if _still_running:
            _log.warning(
                "index_failure_drain_slow",
                in_flight=len(_still_running),
                waited_s=_FAILURE_DRAIN_TIMEOUT_S,
            )
        failures: list[tuple[Path, BaseException]] = []
        for (score, file), fut in zip(files, futures):
            if fut.cancelled():
                continue
            exc = fut.exception()
            if exc is not None:
                failures.append((file, exc))
        if not failures:
            return written[0]
        # Deterministic "first": earliest in submission (frecency) order.
        for file, exc in failures[1:]:
            _log.warning(
                "index_file_concurrent_failure_suppressed",
                file=str(file), error=str(exc),
            )
        raise failures[0][1]
