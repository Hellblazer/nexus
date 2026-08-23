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
from collections.abc import Callable, Iterable, Mapping
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

#: Systemic-skip signal thresholds (nexus-deyd5; round 3 relocated the
#: DECISION to a run-level verdict computed by ``nexus.indexer._run_index``
#: after every run_file_loop category AND the batcher's drain complete —
#: see :func:`skip_floor_breached`'s own docstring for why, and
#: ``run_file_loop``'s docstring for what round 2's mid-loop-raise version
#: got wrong). :func:`run_file_loop`'s per-file skip tolerance is only
#: safe because ONE unextractable fixture is noise, not signal — a bad
#: dependency version, a corrupt model, or a permissions problem can make
#: EVERY file individually "survivable" the exact same way, and pre-
#: nexus-deyd5 that shape aborted loudly on file 1. Two independent trip
#: conditions:
#:
#: - TOTAL LOSS at any sample size: every file attempted was skipped.
#:   Catches a systemic breakage on a small corpus (3 files, all 3 fail)
#:   exactly as readily as a large one — a pure ratio threshold alone
#:   would need to be near-100% to avoid false-positiving on small
#:   samples, which would make it useless for the case that matters most.
#:   Unambiguous: when literally nothing succeeded there is no completed
#:   work a run-level check could put at risk by firing.
#: - MAJORITY AT SCALE: the sample is large enough
#:   (>= _SYSTEMIC_SKIP_MIN_ATTEMPTED) for a ratio to be statistically
#:   meaningful rather than noise from a couple of legitimately-blank
#:   fixtures, AND at least _SYSTEMIC_SKIP_RATIO_THRESHOLD of it skipped.
#:   Catches, for example, a scanned-PDF archive indexed without OCR
#:   (the default extractor never runs OCR; MinerU is opt-in) going
#:   majority-unextractable — a real, non-anomalous corpus shape (code-
#:   review finding, round 2) the coordinator explicitly wants surfaced
#:   as an ACCURATE non-zero exit ("I indexed almost nothing"), not tuned
#:   away with a PDF-specific threshold.
#:
#: A pure absolute count (e.g. "fail after 10 skips") was rejected: too
#: permissive on a 3000-file repo (10 stray corrupt fixtures is normal
#: noise) and too insensitive on a tiny repo (never fires on a 3-file
#: repo that failed completely). A pure ratio alone was rejected too: on
#: a 3-file repo, 1 legitimately-blank fixture is already 33% —
#: indistinguishable from real breakage without the minimum-sample-size
#: gate below it.
_SYSTEMIC_SKIP_MIN_ATTEMPTED: int = 20
_SYSTEMIC_SKIP_RATIO_THRESHOLD: float = 0.5


def skip_floor_breached(skipped_n: int, attempted_n: int) -> bool:
    """True when *skipped_n* of *attempted_n* files crosses the systemic-
    skip signal thresholds (see the constants above). Pure function, no
    side effects, no raise — the two trip conditions are independently
    testable in isolation.

    PUBLIC (not module-private) and deliberately NOT called by
    :func:`run_file_loop` itself (nexus-deyd5 round 3, coordinator
    directive): the caller is ``nexus.indexer._run_index``, which is the
    only place with the RUN-level totals this needs — the sum of
    *skipped*/*attempted* across all 4 run_file_loop categories (code/
    prose/pdf/rdr), evaluated once after every category's loop AND the
    batcher's drain AND post-processing have all completed normally.
    Calling this per-category, mid-``_run_index`` (round 2's shape) let a
    breach on ONE category raise before the other 3 categories or the
    drain ever ran, discarding already-staged-but-unflushed chunks with
    no ``__del__``/atexit to save them and surfacing as an uncaught
    traceback instead of a clean CLI exit — see ``nexus.indexer.
    _run_index``'s own comment at the point this is called for the full
    account of why the verdict moved here.
    """
    if attempted_n == 0 or skipped_n == 0:
        return False
    if skipped_n == attempted_n:
        return True
    return (
        attempted_n >= _SYSTEMIC_SKIP_MIN_ATTEMPTED
        and skipped_n / attempted_n >= _SYSTEMIC_SKIP_RATIO_THRESHOLD
    )

# Patterns always ignored (mirrors indexer.DEFAULT_IGNORE).
_DEFAULT_IGNORE: list[str] = [
    "node_modules", "vendor", ".venv", "__pycache__", "dist", "build", ".git",
    "*.lock", "go.sum",
]


def orphaned_chashes(reader: object, doc_id: str, candidates: Iterable[str]) -> list[str]:
    """Of *candidates* (chashes no longer in *doc_id*'s manifest), return the
    subset with NO OTHER live document referencing them — safe to delete
    from T3.

    Shared union guard (nexus-tp8yk D3), extracted from
    ``mcp_infra._sweep_superseded_vectors`` (the ORIGINAL guard — this
    function preserves its exact log event and fail-open contract) so
    every T3-deleting prune site gets the identical protection: identical
    chunk TEXT collapses to ONE T3 row shared by every document that
    contains it (CLAUDE.md § catalog/T3 split). "Not in THIS document's
    manifest" is therefore NOT "unreferenced" — deleting on that basis
    would silently remove chunks another live document still depends on.

    FAIL-OPEN, deliberately, on every failure mode: no reader, or a reader
    whose ``docs_for_chashes`` raises. Over-retention is recoverable (the
    next successful sweep catches it); over-deletion is not.

    Args:
        reader: a catalog READER exposing ``docs_for_chashes(chashes) ->
            dict[str, list[str]]`` (never the write-only proxy — see
            nexus-kgos1: a write-only proxy raises AttributeError for
            every read op, which used to be swallowed into a silent
            always-empty sweep). May be ``None`` (uninitialized catalog);
            treated the same as a lookup failure.
        doc_id: the document whose prune candidates are being checked.
            Excluded from the "still referenced" test — finding *doc_id*
            itself in the reverse lookup is not evidence of sharing.
        candidates: chashes to test (typically ``stale_ids`` /
            ``dropped`` — chashes this run no longer references).

    Returns:
        The subset of *candidates* referenced by no live document other
        than *doc_id*. Empty on empty input or any read failure.
    """
    cands = sorted({c for c in candidates if c})
    if not cands:
        return []
    # structlog.get_logger() called AT CALL TIME, deliberately, not the
    # module-level ``_log`` — matches the original _sweep_superseded_
    # vectors's exact pattern, which existing tests patch via
    # ``patch("structlog.get_logger")`` (tests/test_superseded_vector_
    # sweep.py). A pre-bound module logger would not observe that patch.
    import structlog  # noqa: PLC0415 — see rationale above

    if reader is None:
        structlog.get_logger().warning(
            "superseded_sweep_skipped_no_reverse_lookup",
            doc_id=doc_id, candidates=len(cands),
        )
        return []
    try:
        refs = reader.docs_for_chashes(cands) or {}
    except Exception:  # noqa: BLE001 — cannot prove orphanhood: keep everything
        structlog.get_logger().warning(
            "superseded_sweep_skipped_no_reverse_lookup",
            doc_id=doc_id, candidates=len(cands),
        )
        return []
    return [h for h in cands if not any(d != doc_id for d in (refs.get(h) or []))]


def catalog_documents_for_collection(reader: object, collection: str) -> list:
    """Server-scoped catalog document fetch for *collection*.

    nexus-39upx round 2 SIGNIFICANT 1: the ORIGINAL ``live_note_chashes``
    called ``reader.all_documents(content_type="knowledge")`` — an
    UNSCOPED fetch of every ``content_type="knowledge"`` document across
    the whole tenant (every ``knowledge__*`` collection, not just this
    one), filtered down to *collection* client-side. ``list_by_collection``
    is the server-side-filtered equivalent already used elsewhere
    (``catalog/manifest_backfill.py``, ``catalog/orphan_backfill.py``):
    same unbounded-when-limit-omitted contract as ``all_documents``'s
    ``content_type`` branch (``CatalogHandler.handleList``'s ``collection``
    query param, ``filterLimit=0`` when the caller sends no explicit
    limit) — one HTTP round trip, no truncation — just scoped to ONE
    physical collection instead of the entire tenant, and returning
    every content_type registered under it (in practice a single
    physical_collection carries one content_type by the RDR-103 naming
    convention, so this is a strict narrowing, not a behavior change for
    any conformant collection).

    This is the single collection-wide catalog read shared by
    :func:`live_note_chashes` (RDR-145 notes) and
    :func:`non_complete_documents` (nexus-g6k6b RUNFENCE precondition) —
    fetch once, derive both from the same list, and (via
    :class:`CollectionDocumentsCache`) fetch it at most once per batch
    reindex sharing a collection rather than once per orphan-triggering
    document.

    RAISES on failure — deliberately, never a silent empty list. See
    ``live_note_chashes``'s docstring for why a swallowed read here is
    the nexus-kgos1 failure shape one hazard over.

    Args:
        reader: a catalog READER exposing ``list_by_collection(collection)
            -> list[CatalogEntry]``. Read op — routed through the
            write-only proxy it raises AttributeError, same class of
            hazard as nexus-kgos1, so this must be the reader, never
            the writer/write-proxy. ``None`` raises immediately.
        collection: physical collection to fetch.
    """
    if reader is None:
        raise RuntimeError(
            "catalog_documents_for_collection: no catalog reader "
            "available — cannot read catalog documents for a T3 sweep "
            "safety check"
        )
    return list(reader.list_by_collection(collection) or [])


def is_note_shaped(entry: object) -> bool:
    """True when *entry* is a manifest-less MCP ``store_put`` / ``nx store
    put`` note rather than an indexed (file-backed) document.

    A note's catalog row carries no ``file_path`` (store_put origin, not
    an indexed file) and stamps its single T3 chunk's natural id — the
    full content hash — into ``meta["doc_id"]`` at write time
    (``catalog/store_hook.py::single_chunk_manifest_metadata`` +
    ``catalog_store_hook_tracked``).

    THE single identity predicate for "is this a note" across the
    codebase — :func:`live_note_chashes`, :func:`non_complete_documents`,
    and ``nx catalog doctor --store-put-integrity``'s ghost lookup all
    read the SAME shape. Extracted here (nexus-cotmr) so another call
    site does not re-derive it independently — a divergent re-derivation
    is exactly the failure class this predicate exists to prevent (two
    definitions of "note" silently drifting apart). Deliberately NOT
    used by ``health.py``'s RUNFENCE stale-fence check: notes are fenced
    producers since nexus-cotmr fenced the CLI store path (MCP was
    fenced by vw594/F2), so a note-shaped exemption there would mask
    real fence regressions (critique T2 [21535]).

    Args:
        entry: a catalog entry (``CatalogEntry`` or any duck-typed
            fixture exposing ``file_path`` and ``meta``). Attributes are
            read via ``getattr`` with safe defaults so hand-built test
            fixtures that omit one or both fields do not raise.

    Returns:
        True when *entry* has no ``file_path`` AND a truthy
        ``meta["doc_id"]``.
    """
    if getattr(entry, "file_path", ""):
        return False  # has a source file: indexed content, not a note
    return bool((getattr(entry, "meta", None) or {}).get("doc_id", ""))


def live_note_chashes(documents) -> set[str]:
    """Chashes of manifest-less notes among *documents* that a
    T3-deleting sweep must NEVER treat as orphans (nexus-39upx hazard 2
    / RDR-145).

    ``catalog-003-soft-delete.xml``'s ``nexus.live_chunks`` view (and its
    ``purge_trash`` sibling) encode the standing contract: "a chunk is
    live if it has NO manifest rows at all (a note chunk written by MCP
    ``store_put`` / ``nx store put``) OR has at least one live-doc
    manifest row." A manifest-diff sweep (``orphaned_chashes`` above, or
    ``nx t3 gc``'s ``chashes_for_collection`` diff) only ever sees the
    SECOND half of that OR: both ``docs_for_chashes`` and
    ``chashes_for_collection`` query ``catalog_document_chunks``, so a
    chash with ZERO manifest rows — a legitimate note — is
    indistinguishable from a chash that fell out of a live document's
    manifest via re-index. Both simply read as "not referenced".

    A note's catalog row carries no ``file_path`` (store_put origin, not
    an indexed file) and stamps its single T3 chunk's natural id — the
    full content hash — into ``meta["doc_id"]`` at write time
    (``catalog/store_hook.py::single_chunk_manifest_metadata`` +
    ``catalog_store_hook_tracked``; the SAME field the ``nx catalog
    doctor --store-put-integrity`` check reads for its own ghost
    lookup).

    Pure function (nexus-39upx round 2 SIGNIFICANT 1: no I/O) — callers
    fetch *documents* once via :func:`catalog_documents_for_collection`
    (typically through :class:`CollectionDocumentsCache` when the same
    collection is swept for multiple documents in one batch) and pass
    the SAME list here and to :func:`non_complete_documents`.

    Args:
        documents: catalog entries already scoped to one physical
            collection (e.g. the return of
            ``catalog_documents_for_collection(reader, collection)``).

    Returns:
        The set of chashes belonging to manifest-less notes among
        *documents*. Empty when none are note-shaped (the common case
        for code__/docs__/rdr__ collections, which are index-origin
        only).
    """
    notes: set[str] = set()
    for e in documents or []:
        if not is_note_shaped(e):
            continue
        chash = (getattr(e, "meta", None) or {}).get("doc_id", "")
        if chash:
            notes.add(chash)
    return notes


def non_complete_documents(documents) -> list:
    """Documents among *documents* whose RUNFENCE ``index_state`` is not
    ``'complete'`` (nexus-g6k6b — nexus-39upx round 2 CRITICAL).

    Bead nexus-39upx's own comment thread (Hal, 2026-08-02 11:23),
    verbatim, is the binding requirement this implements: "PRECONDITION
    ON OPTION (b) from the 5xn3k RUNFENCE design... The corpus-wide
    sweep (b) MUST filter on ``index_state = 'complete'``. Sweeping a
    document that is mid-index would delete chunks an in-flight run has
    already written but has not yet manifested... Treat this as a
    stated requirement on (b), not a nice-to-have."

    ``nx t3 gc``'s alive-set (``chashes_for_collection``) is a
    COLLECTION-level union of every live document's manifest — post-RDR-108
    a T3 chunk carries no ``doc_id`` at all, so an orphan CANDIDATE
    (a chash referenced by no current manifest row) cannot be attributed
    back to the specific document that most recently owned it. The
    conservative, structurally-honest way to implement "exclude chunks
    belonging to a non-complete document" when candidates cannot be
    attributed to individual documents is a collection-level circuit
    breaker: if this collection contains ANY document that is not
    ``'complete'``, no candidate in the collection can be PROVEN safe,
    so the caller must refuse (or require an explicit operator
    override) rather than delete anything.

    Classification, per Hal's follow-up comment (2026-08-02 17:02) —
    honored verbatim, not paraphrased down:
      - Note-shaped documents (the same ``file_path==""`` +
        ``meta["doc_id"]`` shape :func:`live_note_chashes` protects) are
        EXCLUDED from this list. Hal: "store_put-origin documents NEVER
        carry index_state (registered exclusion on nexus-5xn3k, accepted
        design)... That is the conservative direction (their chunks are
        never swept: over-retention, not deletion) and is correct" —
        i.e. notes are ALREADY, separately, unconditionally protected;
        re-flagging them here would make every knowledge collection
        holding even one note permanently refuse gc, which is not the
        behavior "over-retention... is correct" describes.
      - Every OTHER document with ``index_state`` != ``'complete'``
        counts, INCLUDING ``None`` (reported explicitly as null).
        Hal: "do not 'fix' it by widening the filter to NULL — NULL
        means unknown, and sweeping unknown-state docs is exactly the
        mid-index-deletion hazard the precondition exists to prevent."
        This covers ``'indexing'`` (live), ``'failed'`` (a run that
        fenced a failure — its manifest may be a partial, mid-truncation
        artifact of ``atomic_manifest_replace``'s first-batch replace,
        same hazard shape as ``'indexing'``), and ``None`` (unknown —
        never assumed safe).
      - A document whose ``index_state`` was never REPORTED at all
        (``index_state_reported=False`` — a pre-RUNFENCE engine that
        does not have the column) is EXCLUDED from this list: the same
        floor-tolerance stance the RUNFENCE arc used everywhere else
        (``doc_indexer._index_run_fresh`` et al) — an engine that
        cannot answer the question behaves exactly as it did before
        this check existed, never a refusal the operator cannot act on.

    Pure function (no I/O) — see :func:`live_note_chashes` for the
    fetch-once-reuse-twice rationale.
    """
    out = []
    for e in documents or []:
        if is_note_shaped(e):
            continue  # note-shaped: live_note_chashes's exemption already covers it
        if not getattr(e, "index_state_reported", True):
            continue  # pre-RUNFENCE engine: floor-tolerant, no signal to act on
        if getattr(e, "index_state", None) != "complete":
            out.append(e)
    return out


class CollectionDocumentsCache:
    """Memoizes :func:`catalog_documents_for_collection` for the
    lifetime of one instance (nexus-39upx round 2 SIGNIFICANT 1).

    ``_sweep_superseded_vectors`` (``mcp_infra.py``) is called once per
    orphan-triggering document inside ``_manifest_write_loop``'s
    per-batch loop — a batch reindex over many documents sharing one
    collection would otherwise re-fetch the WHOLE collection's catalog
    documents once per document that happens to have dropped a chash.
    The bead's own corpus data (SHAKEOUT-7.1.1) measured ~20% orphan
    rates in heavily-reindexed collections: a 1000-document batch
    reindex in such a collection would trip the fetch roughly 200 times
    with zero reuse. Construct ONE instance per ``(reader, collection)``
    pair — once per ``_manifest_write_loop`` call — and share it across
    every document processed in that call.

    Caches a raised exception too (re-raised verbatim on every
    subsequent :meth:`get` within the SAME batch) rather than
    re-attempting a call that already failed once this batch — a
    transient catalog outage should not be retried once per document
    either; each caller's own fail-open/fail-loud handling still fires
    per document, just against the same cached failure.
    """

    __slots__ = ("_reader", "_collection", "_value", "_error", "_done")

    def __init__(self, reader: object, collection: str) -> None:
        self._reader = reader
        self._collection = collection
        self._value: list | None = None
        self._error: Exception | None = None
        self._done = False

    def get(self) -> list:
        if not self._done:
            self._done = True
            try:
                self._value = catalog_documents_for_collection(self._reader, self._collection)
            except Exception as exc:  # noqa: BLE001 — cached and re-raised verbatim below, never swallowed
                self._error = exc
        if self._error is not None:
            raise self._error
        assert self._value is not None
        return self._value


# nexus-tbkk1 (2026-08-05, substantive-critic Significant #2):
# prune_orphan_candidates DELETED. It was a THIN wrapper around
# orphaned_chashes (above) built specifically for the tp8yk D3 fix at
# the three doc_indexer.py prune sites and pipeline_stages._prune_
# stale_chunks — its own docstring named exactly those four call sites
# as its reason to exist ("the three doc_indexer.py prune sites and
# pipeline_stages._prune_stale_chunks used to gate..."). nexus-tbkk1
# deleted all four of those call sites (RDR-102 D2 made their
# source_path-keyed candidate queries permanently unable to match any
# real chunk row), leaving this wrapper with ZERO production callers —
# only orphaned_chashes remains live, called directly by mcp_infra.
# _sweep_superseded_vectors. Falsified by deletion (nexus-tbkk1 fix
# round): the dedicated unit test file tests/test_indexer_utils_
# prune_orphan_candidates.py is deleted alongside it; the union-guard
# LOGIC it wrapped (orphaned_chashes) remains fully covered by
# tests/db/test_http_catalog_integration.py::TestPruneUnionGuard and
# tests/integration/test_tp8yk_manifest_never_outruns_chunks.py. If a
# future caller (e.g. nexus-afudo's indexer.py/indexer_utils.py sites)
# needs this exact "no-reader-at-all falls back to unconditional
# delete" shape, recover it from git history rather than resurrecting
# dead code speculatively — it may not even be the right shape for a
# different candidate population.


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


# First-H1 candidates that are section headings, not titles. A MinerU/Docling
# markdown body whose first ``# `` line is one of these has no title heading
# (the real title was rendered as plain text or dropped); fall through to the
# filename stem rather than stamp "Abstract" as the document title — the
# stem-guard in ``_catalog_pdf_hook`` treats any non-stem title as curated,
# so a bad H1 would otherwise wedge the catalog title until a manual
# ``nx catalog update`` (code-review finding on nexus-ov5tc, 2026-08-19).
_H1_SECTION_HEADINGS: frozenset[str] = frozenset({
    "abstract", "introduction", "contents", "table of contents", "references",
    "bibliography", "acknowledgments", "acknowledgements", "appendix",
    "keywords", "summary", "overview", "background", "preface", "foreword",
    "conclusion", "conclusions", "related work", "motivation", "preliminaries",
})


def _h1_is_section_heading(h1: str) -> bool:
    core = re.sub(r"^[\d.\s]+", "", h1).strip().strip(":").lower()
    return core in _H1_SECTION_HEADINGS


def resolve_pdf_title(metadata: dict, pdf_path: Path, text: str | None) -> str:
    """Resolve a PDF's document title — the ONE chain for both PDF paths.

    ``docling_title`` -> ``pdf_title`` (XMP) -> first H1 of *text* -> normalised
    filename stem (:func:`derive_title`). The MinerU extractor reports both
    metadata titles empty, so before 2026-08-19 its documents always fell to
    the stem (``papers/2512.11001.pdf`` -> ``"2512.11001"``) even though its
    markdown opens with ``# <title>``; passing the extracted text lets the
    H1 win. Used by ``doc_indexer._pdf_chunks`` and the streaming post-pass
    in ``pipeline_stages``.
    """
    extracted = (
        str(metadata.get("docling_title") or "").strip()
        or str(metadata.get("pdf_title") or "").strip()
    )
    if extracted:
        return extracted
    if text:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                # Only the FIRST H1 is a title candidate; a section-heading
                # H1 means there is no title heading at all.
                if _h1_is_section_heading(stripped[2:]):
                    return derive_title(pdf_path, body=None)
                break
    return derive_title(pdf_path, body=text)


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

    One index:

    - ``by_doc_id`` keys on the catalog tumbler stored in chunk
      metadata. Populated only for chunks whose stored ``doc_id`` field
      is non-empty. The post-RDR-101-Phase-4 write path stamps
      ``doc_id`` on every new chunk; legacy chunks predating the
      backfill are absent from this index, which the cached
      ``check_staleness`` correctly treats as a cache miss → "stale" →
      re-index → ghost-chunk healed.

    nexus-afudo (2026-08-05): ``by_source_path`` DELETED as dead code —
    RDR-102 D2 (83ac62c7, 2026-05-02) hard-removed ``source_path`` from
    ``make_chunk_metadata``, the single factory every writer
    (code_indexer.py, prose_indexer.py, pipeline_stages.py, and
    doc_indexer.py alike) routes through, so no chunk written since
    then has ever carried the key. A live-store existence probe
    (field>=! boundary-value test, method validated against present/
    absent-field controls) found zero rows carrying ``source_path``
    across 13 representative collections (~115k chunks) spanning both
    continuously-reindexed (code__1-1, code__1-20, code__1-33) and
    long-untouched corpora — see ``nexus.doc_indexer._identity_where``'s
    docstring for the original 6-collection probe and T2
    ``nexus/nexus-afudo-audit-2026-08-05`` for the extension. The index
    was therefore always empty in production; caller-side lookups
    against it always missed, and ``check_staleness`` correctly treated
    every doc_id-less file as unconditionally stale (no behavior
    change from deleting an index that could never hold a row).

    Why the cache exists: ``nx index repo`` on a healthy repo where
    nothing has changed is dominated by per-file
    ``col.get(where={doc_id})`` roundtrips just to confirm "yes,
    current, skip." On ART (~4,800 files) that's ~4,800 sequential
    ChromaDB Cloud calls at 50-200 ms each — 8-30 minutes of pure
    network latency before any actual indexing work. With the cache,
    the entire staleness phase is a single paginated sweep
    (~ceil(total_chunks / 300) calls) plus per-file dict lookups.

    ``never_fresh`` (nexus-cp46b): doc_ids the orchestrator's catalog
    pass found fenced ``index_state in ('indexing', 'failed')`` — a
    stranded/failed run (RUNFENCE, nexus-5xn3k.3), never simply
    "unchanged." Pre-fix, a content-hash match alone made
    :func:`check_staleness` return True forever for such a doc, since
    the fence state was never consulted on this path (only
    ``doc_indexer._index_run_fresh`` did, and this cache's callers
    — ``code_indexer.py`` / ``prose_indexer.py`` / the repo-path PDF
    indexer — never call it). A doc_id present here is always treated
    as a cache MISS regardless of what :attr:`by_doc_id` says, so the
    next normal ``nx index repo`` re-processes it and its clean
    completion stamps the fence back to ``'complete'``. Populated once
    by the orchestrator from data its catalog pass already fetched
    (no extra HTTP round trip); empty by default, matching prior
    behavior exactly for every caller that never sets it.
    """

    by_doc_id: dict[str, tuple[str, str]] = field(default_factory=dict)
    never_fresh: frozenset[str] = field(default_factory=frozenset)


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
        # nexus-afudo: by_source_path population DELETED as dead code —
        # see StalenessCache's docstring. No chunk written since RDR-102
        # D2 (2026-05-02) carries a source_path key.
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
      Looks up ``doc_id`` in :attr:`StalenessCache.by_doc_id`. Pure
      dict lookup, no ChromaDB roundtrip. The orchestrator builds the
      cache once per collection via :func:`build_staleness_cache`
      before the per-file loop, so ``nx index repo`` on a healthy repo
      (most files current) pays one paginated sweep instead of one
      Chroma query per file. A file with no ``doc_id`` (legacy /
      catalog-absent caller) is an unconditional cache miss — see
      nexus-afudo below.
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
            owner-scope changes. Empty means unconditional "stale" (see
            nexus-afudo below) — every production caller
            (code_indexer.py, prose_indexer.py, indexer.py's PDF path)
            supplies a real catalog-resolved doc_id or falls through
            this fast-fail; there is no source_path-keyed lookup left
            to fall back to.
        cache: Optional :class:`StalenessCache`. When supplied the
            check is a dict lookup; when ``None`` the check is a Chroma
            roundtrip.

    Returns:
        True when the stored chunk has the same content_hash AND embedding_model,
        meaning the file is current and can be skipped.  False otherwise.

    nexus-afudo (2026-08-05): the ``source_path``-keyed fallback (both
    the cached ``StalenessCache.by_source_path`` lookup and the
    uncached ``where={"source_path": ...}`` Chroma query) was DELETED
    as dead code. RDR-102 D2 (83ac62c7, 2026-05-02) hard-removed
    ``source_path`` from ``make_chunk_metadata`` — the single factory
    every writer routes through — so no chunk written since then has
    ever carried the key, and the fallback could only ever match a
    genuinely pre-2026-05-02 legacy row. A live-store probe (field>=!
    boundary-value existence test) found zero such rows across 13
    representative collections (~115k chunks), extending nexus-tbkk1's
    original 6-collection / ~47k-chunk probe to the code__/docs__/rdr__
    collections this module's callers (``nx index repo``) actually
    write. Empty ``doc_id`` now returns False immediately (no query,
    no dict lookup) — behaviorally identical to the deleted fallback,
    which always missed in production, just without the doomed I/O.
    """
    if cache is not None:
        if not doc_id:
            # No catalog doc_id for this file (legacy / catalog-absent
            # caller). See the nexus-afudo docstring note above — no
            # source_path-keyed cache entry can ever exist to check
            # against, so this is an unconditional "treat as stale."
            return False
        if doc_id in cache.never_fresh:
            # nexus-cp46b: fenced 'indexing'/'failed' overrides a
            # content-hash match — see StalenessCache.never_fresh.
            return False
        stored = cache.by_doc_id.get(doc_id)
        # Cache miss when the caller has a doc_id heals a ghost
        # chunk by treating the file as stale: re-index will write
        # a chunk carrying doc_id metadata and the next sweep
        # populates by_doc_id for it. Mirrors the Chroma-path
        # behaviour at indexer_utils.check_staleness:291.
        if stored is None:
            return False
        return stored == (content_hash, embedding_model)

    # RDR-108 Phase 3 (nexus-bdag): chunks no longer carry ``doc_id`` —
    # the catalog ``document_chunks`` manifest is authoritative. Query
    # by ``content_hash`` (a file-level fingerprint that all chunks of
    # the same file share). An empty content_hash has no identity to
    # query by — see the nexus-afudo docstring note above; unconditional
    # "treat as stale" rather than a doomed source_path query.
    if not content_hash:
        return False
    existing = _vector_with_retry(
        col.get,  # type: ignore[attr-defined]
        where={"content_hash": content_hash},
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
    (chash/taxonomy/aspect-queue writes). CORRECTION (nexus-eslkl design
    memo, 2026-08-08): the original rationale here — a diverging direct-
    SQLite T2 backend that only ``LockedHookRegistry``'s process-wide lock
    made safe — is dead code-provably. ``storage_backend_for()``
    (``db/storage_mode.py``) has resolved to exactly one backend
    (``StorageBackend.SERVICE``) since RDR-158 P3 (nexus-7bomn); a
    ``=sqlite`` opt-out now fails loud with a stranded-install redirect
    instead of selecting a divergent backend, so no supported
    configuration can produce the hazard this gate was once extended for.
    Every T2 write from every hook already goes through
    ``mcp_infra._service_t2_lock`` regardless of this gate or the hook
    lock, which is what actually protects the shared T2 client's
    lifecycle. The real remaining constraint on narrowing
    ``LockedHookRegistry`` is a DIFFERENT, still-live hazard: the manifest
    hook's superseded-vector sweep did a client-side read-then-write-then-
    read sequence across the catalog that a concurrent flush could race
    (nexus-39upx TOCTOU). nexus-tgrgs/jk88j (2026-08-08) closed the
    intra-batch instance of that race by deferring every sweep decision
    until an entire flush-grain batch's ``write_manifest_many`` POST has
    returned; the cross-FIRE instance (two *different* flush-grain hook
    fires racing each other) remains open and is why flush-grain fires
    still need ``LockedHookRegistry`` (or an equivalent per-chain lock) as
    of this writing — see nexus-eslkl. This gate does not need extending
    for that hazard: it is a hook-serialization question, not a
    concurrency-safety-of-this-loop question. The local bge embedder is
    concurrency-safe regardless: onnxruntime ``InferenceSession.run``
    supports concurrent calls on a shared session.
    """
    import os  # noqa: PLC0415 — leaf module keeps import surface minimal

    def _backend_default() -> int:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — deferred to avoid circular import (db.http_vector_client)

        # nexus-i711w: the catalog conjunct collapsed — the catalog is
        # service-backed in every mode.
        if is_vector_service_mode():
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
    on_skip: Callable[[Path, str], None] | None = None,
) -> int:
    """Drive one per-file indexing loop, sequentially or with a bounded pool.

    Returns the number of files that wrote at least one chunk this run
    (``index_one`` returned > 0); staleness-skipped, skipped (see below),
    and failed files return 0 and are not counted (nexus-qgc4b).

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

    Error semantics — TWO tiers (nexus-deyd5; pre-fix this was a single
    first-exception-cancels-all contract, so one unextractable PDF fixture
    among 3160 files aborted the entire run's finalization at rc=1):

    - **Per-record survivable** — caught EXPLICITLY BY NAME
      (``nexus.errors.UnextractableContentError``), deliberately NOT the
      broader ``nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS`` tuple
      (code-review finding, nexus-deyd5 round 2): that tuple's other
      members (``ChunkLandingUnverifiedError``, ``IndexRunVerifyRefused``,
      ``ExtractionQualityError``, ``UnchunkableContentError``) are raised
      solely from ``doc_indexer.py``'s fence-bracketed per-record command
      path, never reachable from this loop's four ``index_one`` wrappers
      today — and the ``tests/test_rlkgu_per_record_catch_tripwire.py``
      audit that makes tuple membership safe for its ORIGINAL consumers
      (``dt.py`` / ``commands/index.py``'s per-record loops) deliberately
      does not scan this file, so catching the whole tuple here would be
      an unguarded coupling: a future addition to the tuple (or a refactor
      that wires RUNFENCE fencing — ``IndexRunVerifyRefused`` — into the
      bulk crawl) would silently start being swallowed into a green
      summary here with no mechanized gate to notice. Catching only the
      one type this loop actually needs removes that coupling entirely.
      Logged loudly (``index_file_skipped_unextractable``, naming the
      file and the reason), reported via ``on_skip`` when given, the file
      counted as 0 chunks written (identical to a staleness skip), and
      the loop CONTINUES — this file's failure never cancels not-yet-
      started files and never aborts the run. This is the "this file
      cannot be extracted" bucket the bead calls out.
    - **Everything else** is data-loss-class BY DEFAULT — the
      conservative choice for any exception type not explicitly proven
      safe (a store write failure, an auth failure, a substrate outage,
      every OTHER PER_RECORD_SURVIVABLE_EXCEPTIONS member, or simply an
      exception nobody has classified yet). The first such exception
      cancels all not-yet-started files and re-raises, exactly as before.
      In-flight files still run to completion (callbacks included) before
      the raise — the shakeout's count-based assertions are order-
      independent, so a few extra completed files at failure time are
      indistinguishable from the sequential "run died at file X" shape.

    A blanket ``except Exception: continue`` would satisfy the bead's
    literal words while silently converting genuine data-loss failures
    into green runs — deliberately NOT what this does. Only the one
    positively-proven-reachable, positively-proven-safe type above is
    treated as skippable; an unrecognized exception still fails loud.

    Systemic-skip signal (nexus-deyd5 round 3, coordinator directive):
    per-file survivability is only safe reasoning when failures are
    independent noise — a bad dependency version, a corrupt model, or a
    permissions problem can make every file individually "survivable" the
    same way, and skipping everything with no further signal would be a
    silent total failure. This function does NOT decide that on its own
    and NEVER raises for it — an earlier version raised
    mid-``_run_index`` (after only ONE of the 4 code/prose/pdf/rdr
    categories), which discarded already-staged-but-unflushed chunks
    (``ChunkBatcher`` has no ``__del__``/atexit; its drain never ran),
    skipped the remaining categories and all post-processing, and
    surfaced as an uncaught traceback — strictly worse than the bug being
    fixed, and reachable on a routine corpus shape (a scanned-PDF archive
    without OCR, since the default extractor never runs OCR and MinerU is
    opt-in). The verdict is instead a RUN-LEVEL judgment made once, after
    every category's loop AND the batcher's drain AND post-processing all
    complete normally: see :func:`skip_floor_breached` (a pure function,
    no side effects) and ``nexus.indexer._run_index`` / ``commands/index.
    py``'s ``index_repo_cmd``, which compute the aggregate skipped/
    attempted totals across all 4 calls to this function and convert a
    breach into a clean ``click.ClickException`` — never a raise from
    inside this loop, so a mid-run breach never costs already-completed
    work.
    """
    import threading  # noqa: PLC0415 — leaf module keeps import surface minimal
    import time  # noqa: PLC0415 — leaf module keeps import surface minimal

    from nexus.errors import UnextractableContentError  # noqa: PLC0415 — leaf module keeps import surface minimal

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
    skipped = [0]

    def _finish_ok() -> int:
        """Called at every point run_file_loop would otherwise return
        success. Logs the skip summary and returns — this function makes
        NO systemic-skip judgment and never raises for one (nexus-deyd5
        round 3): that verdict is a run-level decision made by the caller
        once every category and the batcher's drain have completed, via
        :func:`skip_floor_breached`. See this function's own docstring."""
        total = len(files)
        if skipped[0]:
            _log.warning(
                "index_file_loop_skip_summary",
                skipped=skipped[0], total=total,
            )
        return written[0]

    def _process(score: float, file: Path) -> None:
        t0 = time.monotonic()
        timers = _make_timers()
        skip_reason: str | None = None
        try:
            chunks = index_one(file, score, timers)
        except UnextractableContentError as exc:
            # nexus-deyd5: this file's own extraction failed in a way its
            # exception type guarantees cost no durable data — log loudly,
            # count it, and let the loop continue rather than cancelling
            # every not-yet-started file behind it.
            _log.error(
                "index_file_skipped_unextractable",
                file=str(file), error=str(exc), error_type=type(exc).__name__,
            )
            chunks = 0
            skip_reason = str(exc)
        except Exception as exc:
            # nexus-1jtob: NAME THE FILE. Both loop paths let an unrecognised
            # exception escape ``run_file_loop`` -- the sequential path by
            # propagating out of this call, the concurrent path via
            # ``raise failures[0][1]`` below. Neither carried the file, so the
            # traceback always surfaced at the phase boundary ("RDR indexing
            # done") with NO indication of which document was rejected. Measured
            # cost: 151 aborted index runs across four days (2026-08-19..08-23)
            # where the offending file could not be identified from the log at
            # all, because the ONE failure that ends the run was the one failure
            # nothing logged -- the SUPPRESSED siblings below were already
            # logged with ``file=``, the raised one was not.
            #
            # ``add_note`` rather than wrapping in a new exception type, ON
            # PURPOSE: callers classify this failure BY TYPE (the
            # ``UnextractableContentError`` arm above, the transient- and
            # quality-gate containment tests, ``skip_floor_breached``), so
            # re-raising a different class would silently change containment
            # behaviour while looking like a logging change. A note leaves
            # ``type(exc)`` and ``str(exc)`` byte-identical and only enriches
            # the rendered traceback.
            #
            # NOTE ONLY -- NO LOGGING HERE, and that is load-bearing. An
            # earlier revision logged from this handler and broke
            # ``test_unclassified_exception_still_fails_the_run``: with
            # concurrency=2 over 50 trivial files, the structlog write is slow
            # relative to the loop body, so the failing task sat in stdout I/O
            # while the pool churned through every remaining file. All 50
            # started before ``wait(..., FIRST_EXCEPTION)`` returned and
            # ``fut.cancel()`` could fire, silently defeating the
            # cancel-pending-work contract. ``add_note`` is in-memory and
            # cannot do that. The log line lives at the two RAISE sites below,
            # after the drain, where it races nothing.
            exc.add_note(f"nexus: raised while indexing {file}")
            raise
        with cb_lock:
            if skip_reason is not None:
                skipped[0] += 1
                if on_skip is not None:
                    on_skip(file, skip_reason)
            if chunks > 0:
                written[0] += 1
            if on_file:
                on_file(file, chunks, time.monotonic() - t0)
            if on_stage_timers is not None and timers is not None:
                on_stage_timers(file, timers)

    if concurrency <= 1:
        for score, file in files:
            try:
                _process(score, file)
            except Exception as exc:  # nexus-1jtob: log at the raise site
                _log.error(
                    "index_file_failed",
                    file=str(file), error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise
        return _finish_ok()

    from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait  # noqa: PLC0415 — leaf module keeps import surface minimal

    with ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="nx-index",
    ) as pool:
        futures = [pool.submit(_process, score, file) for score, file in files]
        done, not_done = wait(futures, return_when=FIRST_EXCEPTION)
        if not not_done and not any(f.exception() for f in done):
            return _finish_ok()
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
            return _finish_ok()
        # Deterministic "first": earliest in submission (frecency) order.
        for file, exc in failures[1:]:
            _log.warning(
                "index_file_concurrent_failure_suppressed",
                file=str(file), error=str(exc),
            )
        # nexus-deyd5 critique: still log the skip summary (informational,
        # never raises) — the batch already fails via failures[0][1] below,
        # so the floor breaker itself is redundant here, but the count is
        # still worth a line since some files may have skipped before the
        # data-loss-class failure hit.
        if skipped[0]:
            _log.warning(
                "index_file_loop_skip_summary",
                skipped=skipped[0], total=len(files),
            )
        # nexus-1jtob: log the failure that ENDS the run, symmetrically with
        # the suppressed siblings above. Before this, the suppressed ones were
        # logged with ``file=`` and the raised one was not -- so the only
        # failure that mattered was the only one nothing named.
        _log.error(
            "index_file_failed",
            file=str(failures[0][0]), error=str(failures[0][1]),
            error_type=type(failures[0][1]).__name__,
        )
        raise failures[0][1]
