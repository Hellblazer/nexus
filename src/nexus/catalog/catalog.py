# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from urllib.parse import urlparse
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from chromadb.api import ClientAPI


# Heartbeat cadence for long catalog operations. The first heartbeat
# fires after this delay (so fast operations stay silent) and then every
# ``_HEARTBEAT_INTERVAL`` until the operation completes.
_HEARTBEAT_INTERVAL = 5.0


# Summary lines fire only when a rebuild took at least this long. Fast
# rebuilds (sub-second; the common case post-FTS5-fix) stay completely
# silent — operators have no diagnostic question to answer. Slow ones
# get the rich line with event/document/link counts and elapsed time.
# Same threshold gates the trigger-line emission inside _ensure_consistent.
_PROGRESS_MIN_ELAPSED = 1.0


@contextmanager
def _rebuild_heartbeat(label: str, summary_builder=None):
    """Print elapsed-time heartbeats to stderr while a long catalog
    operation runs, plus a one-line summary at completion.

    Spawns a daemon thread that wakes every :data:`_HEARTBEAT_INTERVAL`
    seconds and writes ``Catalog: {label} (Ns)\\r`` so the user has a
    visible signal during the projection rebuild — which can run for
    tens of minutes on a project with hundreds of thousands of events
    while SQLite FTS5 merges segments at COMMIT.

    Pre-fix the ``_ensure_consistent`` rebuild was completely silent.
    Operators running ``nx index repo`` saw the indexer hook print
    ``Catalog: housekeeping…\\r`` and then nothing for 15-20 minutes
    while the catalog DB churned. Indistinguishable from a hang.

    The first heartbeat is delayed by one full interval so operations
    that finish in <:data:`_HEARTBEAT_INTERVAL`s stay completely
    silent. The exit summary line fires only when the rebuild took at
    least :data:`_PROGRESS_MIN_ELAPSED` seconds — fast rebuilds (the
    common case on a healthy projection) emit nothing, so CLI commands
    that happen to trigger an incidental rebuild don't scribble
    progress over their own output.

    *summary_builder* is an optional callable ``(elapsed: float) -> str``
    invoked at exit when the elapsed-time gate fires. Its return value
    is the summary line. The builder runs after the work is complete,
    so it can read final counts from the catalog DB.
    """
    stop = threading.Event()
    started = time.monotonic()

    def _beat() -> None:
        # Wait one interval before the first beat so fast ops stay silent.
        if stop.wait(_HEARTBEAT_INTERVAL):
            return
        while not stop.is_set():
            elapsed = time.monotonic() - started
            sys.stderr.write(f"  Catalog: {label} ({elapsed:.0f}s)\r")
            sys.stderr.flush()
            if stop.wait(_HEARTBEAT_INTERVAL):
                return

    thread = threading.Thread(target=_beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
        elapsed = time.monotonic() - started
        # NOTE: must not ``return`` from this finally block — a bare
        # return inside a finally inside a contextmanager generator
        # swallows any exception that was propagating out of the
        # ``yield``, masking real failures (e.g. a ``CatalogDB.rebuild``
        # raise was being silently dropped, leaving ``Catalog.degraded``
        # un-set). Use an if/else gate around the write instead so the
        # finally falls off the end and the exception (if any)
        # continues to propagate.
        if elapsed >= _PROGRESS_MIN_ELAPSED:
            if summary_builder is not None:
                try:
                    line = summary_builder(elapsed)
                except Exception:
                    # Never let a summary-rendering bug mask the real
                    # work — fall back to the plain message.
                    line = f"  Catalog: {label} done ({elapsed:.1f}s)"
            else:
                line = f"  Catalog: {label} done ({elapsed:.1f}s)"
            sys.stderr.write(f"{line}\n")
            sys.stderr.flush()


def _count_lines(path: Path) -> int:
    """Return the line count of *path*, or 0 on any error.

    Used to surface the size of an event-log replay or a JSONL rebuild
    in the operator-visible heartbeat. Errors are swallowed so a
    permission glitch on the count never prevents the rebuild itself
    from running.
    """
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _trigger_file_label(
    paths_with_mtime: list[tuple[Path, float]], threshold: float,
) -> str:
    """Return the basename of the canonical-truth file whose mtime is
    newest above *threshold* — i.e. the one that triggered the rebuild.

    Returns ``unknown`` when no file is over threshold, which only
    happens on the degraded-state forced rebuild path; in steady state
    at least one file's mtime is always newer than the persisted marker.
    """
    over = [(p, m) for p, m in paths_with_mtime if m > threshold]
    if not over:
        return "unknown"
    p, _ = max(over, key=lambda pm: pm[1])
    return p.name

# RDR-101 Phase 1 PR F: shadow event-log emit gate. Read once at
# Catalog.__init__ time. Recognised "on" values match the boolean-env
# convention used elsewhere in the codebase (1/true/yes/on).
_SHADOW_EMIT_ENV = "NEXUS_EVENT_LOG_SHADOW"

# RDR-101 Phase 3 PR α: event-sourced write path gate. When ON, the
# new path inverts the JSONL+SQLite write order: emit DocumentRegistered
# event FIRST, project to SQLite via Projector.apply, then append to
# legacy documents.jsonl for back-compat (Phase 5 deprecates legacy
# JSONL). When OFF, the legacy direct-write path runs and shadow emit
# (PR F) optionally appends to events.jsonl after the fact.
#
# RDR-101 Phase 3 PR ζ (nexus-o6aa.9.5): default flipped to ON. The
# irreversibility window opens here: the catalog event log is now the
# canonical write path by default. Existing catalogs without an
# events.jsonl fall through to the legacy rebuild via the
# ``_event_log_covers_legacy`` bootstrap guardrail in
# ``_ensure_consistent``. The synthesize-log migration verb was
# retired post Phase 5b (nexus-iftc); operators with sparse-log
# catalogs restore by deleting the catalog directory and re-running
# ``nx catalog setup`` to bootstrap from current T3 state. Set
# ``NEXUS_EVENT_SOURCED=0`` (or ``false``/``no``/``off``) to opt back
# into the legacy direct-write path at runtime.
_EVENT_SOURCED_ENV = "NEXUS_EVENT_SOURCED"

# RDR-104 Step 2: incremental-rebuild marker constants.
#
# ``_HEADER_HASH_BYTES`` is the prefix size hashed to detect
# events.jsonl rewrites (truncate, atomic-replace, ``git reset``). Any
# rewrite that touches the first 64 KB makes the hash drift, which the
# orchestrator treats as cache invalidation and falls through to full
# rebuild. The window value is persisted alongside the hash so a future
# bump of this constant invalidates prior markers cleanly via the
# ``last_applied_event_header_window`` row rather than silently
# comparing hashes computed over different windows (Round 1 gate
# observation #3).
_HEADER_HASH_BYTES = 64 * 1024
_META_KEY_LAST_OFFSET = "last_applied_event_offset"
_META_KEY_HEADER_HASH = "last_applied_event_header_hash"
_META_KEY_HEADER_WINDOW = "last_applied_event_header_window"


def _compute_header_hash(events_path: Path) -> str:
    """Return ``sha256(open(events_path, 'rb').read(_HEADER_HASH_BYTES)).hexdigest()``.

    RDR-104 Step 2: detection signal for events.jsonl rewrites. Reading
    only the first window avoids a full file scan on every rebuild
    decision; an appender leaves the prefix unchanged so the hash
    matches and the marker stays valid; a truncate/replace/reset
    typically touches early bytes so the hash drifts and the
    orchestrator falls through to full rebuild.

    The pathological adversarial case (rewrite preserving first 64 KB)
    is bounded by the v0 projector verbs' idempotency: redundant
    re-application yields the same projection, no semantic drift,
    documented as known-cost-not-known-corruption.
    """
    with events_path.open("rb") as f:
        return hashlib.sha256(f.read(_HEADER_HASH_BYTES)).hexdigest()


def _read_shadow_gate() -> bool:
    val = os.environ.get(_SHADOW_EMIT_ENV, "").strip().lower()
    return val in ("1", "true", "yes", "on")


_KNOWN_EVENT_SOURCED_VALUES: frozenset[str] = frozenset(
    ("", "0", "1", "true", "false", "yes", "no", "on", "off"),
)
# Module-scoped sentinel — log the unrecognized-value warning at most
# once per process so a tight loop reading the gate doesn't spam logs.
_unrecognized_event_sourced_value_logged: set[str] = set()


def _read_event_sourced_gate() -> bool:
    """Return True when the event-sourced write path is enabled.

    RDR-101 Phase 3 PR ζ: the default is ON. ``NEXUS_EVENT_SOURCED``
    unset or set to ``1`` / ``true`` / ``yes`` / ``on`` (or empty)
    enables ES mode. Explicit ``0`` / ``false`` / ``no`` / ``off``
    opts back into the legacy direct-write path.

    RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8): unrecognized values
    (typos like ``ofg`` / ``nope`` / ``legacy``) silently activate ES
    under the new default-ON semantics — pre-fix the gate flipped
    from safe-by-default (only on for explicit truthy) to dangerous-
    by-default. Log a structured warning the first time we see an
    unrecognized value so an operator's typo is detectable.
    """
    raw = os.environ.get(_EVENT_SOURCED_ENV, "")
    val = raw.strip().lower()
    if val in ("0", "false", "no", "off"):
        return False
    if val not in _KNOWN_EVENT_SOURCED_VALUES:
        if raw not in _unrecognized_event_sourced_value_logged:
            _unrecognized_event_sourced_value_logged.add(raw)
            _log.warning(
                "catalog_event_sourced_gate_unrecognized_value",
                value=raw,
                effective="ON",
                note=(
                    "NEXUS_EVENT_SOURCED carries an unrecognized value; "
                    "treating as ON (default). Use 0/false/no/off to "
                    "opt out, or 1/true/yes/on to make ES explicit."
                ),
            )
    return True


# Module-level imports of the typed event payloads. Aliased with a
# leading underscore so callers outside this module don't reach in for
# the private names (the public API is via Catalog methods that emit
# events on behalf of the caller). The PR-F initial comment described
# these as "lazy" imports — they are not, they run at import time.
from nexus.catalog.events import (  # noqa: E402
    CollectionCreatedPayload as _CollectionCreatedPayload,
    CollectionSupersededPayload as _CollectionSupersededPayload,
    DocumentAliasedPayload as _DocumentAliasedPayload,
    DocumentDeletedPayload as _DocumentDeletedPayload,
    DocumentEnrichedPayload as _DocumentEnrichedPayload,
    DocumentRegisteredPayload as _DocumentRegisteredPayload,
    DocumentRenamedPayload as _DocumentRenamedPayload,
    Event as _Event,
    LinkCreatedPayload as _LinkCreatedPayload,
    LinkDeletedPayload as _LinkDeletedPayload,
    OwnerDeletedPayload as _OwnerDeletedPayload,
    OwnerRegisteredPayload as _OwnerRegisteredPayload,
    SCHEMA_BIB_S2_V1 as _SCHEMA_BIB_S2_V1,
    SCHEMA_SCHOLARLY_PAPER_V1 as _SCHEMA_SCHOLARLY_PAPER_V1,
    make_event as _make_event,
)

# Span format: "line_start-line_end" or "chunk_idx:char_start-char_end" or
# "chash:<sha256hex>" or "".  Empty string means "the whole document".
_SPAN_PATTERN = re.compile(
    r"^$"                              # empty — whole document
    r"|^\d+-\d+$"                      # line range: "42-57"
    r"|^\d+:\d+-\d+$"                  # chunk:char range: "3:100-250"
    r"|^chash:[0-9a-f]{64}$"           # content-hash: chash:<sha256hex>
    r"|^chash:[0-9a-f]{64}:\d+-\d+$"  # content-hash + char range: chash:<sha256hex>:<start>-<end>
)

from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.collection_name import (
    CollectionName,
    owner_segment_for_tumbler,
)
from nexus.catalog.tumbler import (
    DocumentRecord,
    LinkRecord,
    OwnerRecord,
    Tumbler,
    read_documents,
    read_links,
    read_owners,
)
from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    CONTENT_TYPES,
    canonical_embedding_model,
)

_log = structlog.get_logger()


def _default_registry_path() -> Path:
    """Return the default path to the repo registry JSON file."""
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "repos.json"


def make_relative(abs_path: str | Path, repo_root: Path) -> str:
    """Return path relative to repo_root, or original if not under repo_root."""
    try:
        return str(Path(abs_path).relative_to(repo_root))
    except ValueError:
        return str(abs_path)


# nexus-mbm: span resolution + RDR-086 Phase 2 chash-fallback
# machinery moved to :mod:`nexus.catalog.catalog_spans`. The
# Catalog methods further down delegate to that module; this
# re-export keeps the test hook addressable at its historical
# import path.
from nexus.catalog.catalog_spans import (  # noqa: E402
    reset_chash_fallback_warning_for_tests,
)
from nexus.catalog.catalog_writes import ManifestRow as _ManifestRow  # noqa: E402


# Set of URI schemes the catalog will accept verbatim. Each scheme
# corresponds to a reader registered in ``nexus.aspect_readers``;
# adding a new scheme is gated on landing the reader first so
# register-time validation can't silently allow URIs that have no
# downstream consumer. ``file`` and ``chroma`` ship in Phase 1
# (RDR-096); ``https`` and ``nx-scratch`` are reserved for Phase 4.
# ``http`` is intentionally excluded — Phase 4's https reader does
# NOT cover plain http; users with http URIs must upgrade to https
# or wait for a dedicated reader. ``x-devonthink-item`` (nexus-bqda)
# is macOS-only — DEVONthink-managed PDFs carry a stable identity
# URL that resolves to the current filesystem path via osascript;
# the reader gates on ``sys.platform == 'darwin'`` and surfaces a
# clear error elsewhere.
_KNOWN_URI_SCHEMES: frozenset[str] = frozenset({
    "file", "chroma", "https", "nx-scratch", "x-devonthink-item",
})


def _normalize_source_uri(
    source_uri: str, file_path: str, *, repo_root: str = "",
) -> str:
    """RDR-096 P3.1 register-boundary URI validation.

    * Empty ``source_uri`` + non-empty ``file_path`` → derive
      ``file://<abspath>`` (back-compat for callers passing only a
      filesystem path).
    * Empty ``source_uri`` + empty ``file_path`` → return ``""``
      (legacy entries with no identity at all stay shapeless).
    * Non-empty ``source_uri`` → validate via ``urlparse``: must
      have a recognized scheme. Malformed URIs raise ``ValueError``
      at the register boundary, NOT silently persisted (RDR-096
      Risks and Mitigations).

    nexus-3e4s: when ``file_path`` is relative AND ``repo_root`` is
    provided, the abspath is anchored on ``repo_root`` rather than
    the process CWD. This is the upstream fix for the catalog
    contamination bug class — without it, indexing repo ``A`` from
    a CWD inside repo ``B`` produced ``source_uri`` rows pointing
    to ``B``'s tree, leaving the row attributed to ``A``'s owner.
    """
    if not source_uri:
        if file_path:
            base = file_path
            if repo_root and not os.path.isabs(file_path):
                base = os.path.join(repo_root, file_path)
            return "file://" + os.path.abspath(base)
        return ""

    parsed = urlparse(source_uri)
    scheme = parsed.scheme
    if not scheme:
        raise ValueError(
            f"malformed source_uri {source_uri!r}: no scheme. "
            f"Expected one of {sorted(_KNOWN_URI_SCHEMES)} or a "
            f"bare filesystem path (passed via file_path instead).",
        )
    if scheme not in _KNOWN_URI_SCHEMES:
        raise ValueError(
            f"unknown source_uri scheme {scheme!r} in {source_uri!r}. "
            f"Known schemes: {sorted(_KNOWN_URI_SCHEMES)}. To add a "
            f"new scheme, register a reader in nexus.aspect_readers.",
        )
    return source_uri


# nexus-3e4s: env-var escape hatch for the cross-project guard. Set to
# ``"1"`` only to recover from emergency situations (e.g. a known-good
# cleanup script that legitimately needs to register rows across project
# boundaries). Never the right answer for normal indexing.
_CROSS_PROJECT_OVERRIDE_ENV = "NEXUS_CATALOG_ALLOW_CROSS_PROJECT"


@dataclass
class CatalogEntry:
    tumbler: Tumbler
    title: str
    author: str
    year: int
    content_type: str
    file_path: str
    corpus: str
    physical_collection: str
    chunk_count: int
    head_hash: str
    indexed_at: str
    meta: dict = field(default_factory=dict)
    # nexus-8luh: POSIX mtime at index time; 0.0 → not captured.
    source_mtime: float = 0.0
    # nexus-s8yz: alias pointer to a canonical tumbler. '' means this
    # entry is canonical. Populated by dedupe-owners (nexus-tmbh) when
    # consolidating duplicate owner registrations.
    alias_of: str = ""
    # RDR-096 P3.1: persistent URI identity. Populated at register
    # time — bare paths normalize to ``file://<abspath>``; explicit
    # URIs (chroma://, https://, etc.) are stored verbatim. ''
    # only on legacy entries that predate P2.1's column migration.
    source_uri: str = ""

    def to_dict(self) -> dict:
        return {
            "tumbler": str(self.tumbler),
            "title": self.title,
            "author": self.author,
            "year": self.year,
            "content_type": self.content_type,
            "file_path": self.file_path,
            "corpus": self.corpus,
            "physical_collection": self.physical_collection,
            "chunk_count": self.chunk_count,
            "head_hash": self.head_hash,
            "indexed_at": self.indexed_at,
            "meta": self.meta,
            "source_mtime": self.source_mtime,
            "alias_of": self.alias_of,
            "source_uri": self.source_uri,
        }


@dataclass
class CatalogLink:
    from_tumbler: Tumbler
    to_tumbler: Tumbler
    link_type: str
    from_span: str
    to_span: str
    created_by: str
    created_at: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "from": str(self.from_tumbler),
            "to": str(self.to_tumbler),
            "type": self.link_type,
            "from_span": self.from_span,
            "to_span": self.to_span,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "meta": self.meta,
        }


# nexus-mbm: git subprocess helpers and the bodies of init / sync /
# pull live in :mod:`nexus.catalog.catalog_git`. The Catalog methods
# below delegate to those helpers; they keep self-state interactions
# (locking, defrag-on-bloat, projection rebuild) here.
from nexus.catalog import catalog_git as _git


class Catalog:
    """Xanadu-inspired catalog: owners, documents, and links over JSONL + SQLite.

    Deliberate departures from Nelson's Xanadu:
    - TTL expiry: entries with ``expires_at`` set are temporary addresses, violating
      Nelson's "ALL ADDRESSES REMAIN VALID" principle. Tumblers are never reused
      (high-water mark), but expired entries become unresolvable.
    - Chunk addressing is position-based (chunk index), not content-addressed.
      Re-indexing a document may shift which content a chunk span refers to.
    - No tumbler arithmetic (transfinitesimal ADD/SUBTRACT). Ordering and overlap
      detection use integer segment comparison, not Nelson's number space.

    Span Policy (RDR-053):
        Spans are optional on all link types. Accepted formats (validated by
        ``_SPAN_PATTERN``):

        - ``""`` — whole document (no sub-document addressing)
        - ``"N-N"`` — line range (positional, legacy)
        - ``"N:N-N"`` — chunk:char range (positional, legacy)
        - ``"chash:<sha256hex>"`` — content-addressed chunk identity (preferred)
        - ``"chash:<sha256hex>:<start>-<end>"`` — character range within a content-addressed chunk

        Content-hash spans survive re-indexing when chunk boundaries are unchanged
        (RDR-053 D5, RF-3, RF-8). Position-based spans degrade on re-index and are
        detectable via ``link_audit()``.
    """

    def __init__(self, catalog_dir: Path, db_path: Path) -> None:
        self._dir = catalog_dir
        self._db = CatalogDB(db_path)
        self._owners_path = catalog_dir / "owners.jsonl"
        self._documents_path = catalog_dir / "documents.jsonl"
        self._links_path = catalog_dir / "links.jsonl"
        # RDR-101 Phase 1 PR F (nexus-ebz6): shadow event log lives next to
        # the existing JSONL files. Off by default; opt in via
        # NEXUS_EVENT_LOG_SHADOW=1. Phase 3 cuts production writes over to
        # this log; Phase 1 only shadow-emits when an operator opts in.
        self._events_path = catalog_dir / "events.jsonl"
        self._shadow_emit_enabled = _read_shadow_gate()
        # RDR-101 Phase 3 PR α (nexus-8t7z): event-sourced write path
        # gate. Off by default; opt in via NEXUS_EVENT_SOURCED=1. When
        # both gates are on, the event-sourced path takes precedence
        # and shadow emit is unused (the new path emits to events.jsonl
        # by construction).
        self._event_sourced_enabled = _read_event_sourced_gate()
        # RDR-101 Phase 3 PR α/β: cache one Projector instance for the
        # whole catalog lifetime. The projector is stateless (it only
        # holds a reference to the CatalogDB), so a per-mutator
        # ``Projector(self._db)`` was wasted allocation and obscured
        # which DB connection the projection landed on. The cache also
        # means a future change to the projector's constructor (e.g.
        # taking a Phase-5 schema version flag) only has to be
        # threaded once.
        from nexus.catalog.projector import Projector as _Projector
        self._projector = _Projector(self._db)
        self.degraded: bool = False
        # RDR-101 Phase 3 follow-up B (nexus-o6aa.9.7): set when
        # ``_ensure_consistent`` runtime-decides to fall back to the
        # legacy rebuild via ``_event_log_covers_legacy``. Surfaces
        # the silent state where ``_event_sourced_enabled`` is True
        # but reads come from legacy JSONL — operator-visible signal
        # for ``nx catalog doctor``. Never read from the env; the
        # condition is purely runtime.
        self.bootstrap_fallback_active: bool = False
        # Diagnostic: log the active gate state at construction so an
        # operator inspecting structured logs can confirm which write
        # path is in effect without grepping environment dumps. Debug
        # level — every CLI verb that touches the catalog constructs
        # a Catalog at the start of its handler (16 sites in commands/
        # alone), so info-level here would inflate the operator log
        # rate substantially without proportional value. The MCP
        # server constructs once per session and developers running
        # ``NEXUS_LOG_LEVEL=debug`` can still surface the line on
        # demand.
        _log.debug(
            "catalog_gate_state",
            event_sourced=self._event_sourced_enabled,
            shadow_emit=self._shadow_emit_enabled,
            catalog_dir=str(catalog_dir),
        )
        # Storage review S-4: mtime cache so every Catalog() instantiation
        # doesn't re-parse the full JSONL corpus and re-build the SQLite
        # cache. For a 10k-entry catalog this was measurably slow on
        # every MCP tool invocation; the mtime-guarded check skips the
        # rebuild when nothing has changed since the last consistency
        # pass.
        #
        # nexus-wehp: persist the marker INSIDE the catalog SQLite
        # itself so cross-process invocations (CLI verbs while nx-mcp
        # is running) don't each trigger a full DELETE+replay rebuild
        # that contends with the MCP-held SQLite connection. Storing
        # in-DB (not in a sidecar file) means a fresh SQLite cache
        # against an existing catalog dir naturally has no marker,
        # which correctly forces a rebuild to populate the empty cache.
        # nexus-mbm: compose the _Ops facades BEFORE the consistency
        # bootstrap. _read_consistency_marker / _ensure_consistent both
        # delegate through self._sync, so the composition has to be in
        # place when those calls fire.
        from nexus.catalog.catalog_links import _LinkOps
        from nexus.catalog.catalog_docs import _DocumentOps
        from nexus.catalog.catalog_sync import _SyncOps
        from nexus.catalog.catalog_writes import _WriteOps
        self._links = _LinkOps(self)
        self._docs = _DocumentOps(self)
        self._sync = _SyncOps(self)
        # nexus-mbm follow-up: non-registration writes
        # (update, delete, rename, supersede, alias) live in
        # catalog_writes._WriteOps.
        self._writes = _WriteOps(self)

        self._last_consistency_mtime: float = self._read_consistency_marker()

        # nexus-572g K7: emit CollectionCreated events for collections that
        # were direct-INSERT backfilled by CatalogDB.__init__. Without backing
        # events in events.jsonl, rebuild() (DELETE FROM collections + event
        # replay) silently removes the rows. This must run AFTER _events_path
        # is set and the lock helpers are available.
        self._emit_backfilled_collection_events()

        if self._documents_path.exists():
            self._ensure_consistent()

    def _emit_backfilled_collection_events(self) -> None:
        """Emit CollectionCreated events for CatalogDB-backfilled collections.

        CatalogDB.__init__ direct-INSERTs ``collections`` rows for any
        ``documents.physical_collection`` value that has no matching row
        (nexus-mydi). Those rows have no backing event in ``events.jsonl``,
        so ``Catalog.rebuild()`` (DELETE FROM collections + JSONL replay)
        silently removes them.

        This method writes one ``CollectionCreated`` event per backfilled
        name with ``legacy_grandfathered=True`` so the projection is
        durable across rebuilds. Idempotent: only fires for names in
        ``self._db._backfilled_collections`` (the set is populated only
        when an INSERT actually landed; a no-op backfill leaves it empty).

        Events are written unconditionally (not gated on shadow-emit or
        event-sourced mode) because the projection correctness depends on
        them being present -- they are structural, not optional telemetry.
        """
        names = self._db._backfilled_collections
        if not names:
            return
        dir_fd = self._acquire_lock()
        try:
            for name in sorted(names):  # sorted for deterministic JSONL ordering
                event = _make_event(
                    _CollectionCreatedPayload(
                        coll_id=name,
                        owner_id="",
                        content_type="",
                        embedding_model="",
                        model_version="",
                        name=name,
                        created_at="",
                        legacy_grandfathered=True,
                    ),
                    v=0,
                )
                try:
                    self._write_to_event_log(event)
                except Exception:
                    _log.warning(
                        "catalog_backfill_event_write_failed",
                        collection_name=name,
                        exc_info=True,
                    )
        finally:
            self._release_lock(dir_fd)
        _log.debug(
            "catalog_backfill_events_emitted",
            count=len(names),
            names=sorted(names),
        )

    def _read_consistency_marker(self) -> float:
        """Delegates to ``_SyncOps._read_consistency_marker`` (nexus-mbm)."""
        return self._sync._read_consistency_marker()

    def _projection_counts(self) -> tuple[int, int]:
        """Delegates to ``_SyncOps._projection_counts`` (nexus-mbm)."""
        return self._sync._projection_counts()

    def _write_consistency_marker(self, mtime: float) -> None:
        """Delegates to ``_SyncOps._write_consistency_marker`` (nexus-mbm)."""
        return self._sync._write_consistency_marker(mtime)

    def _write_offset_marker(
        self, *, offset: int, header_hash: str, window: int,
    ) -> None:
        """Delegates to ``_SyncOps._write_offset_marker`` (nexus-mbm)."""
        return self._sync._write_offset_marker(offset=offset, header_hash=header_hash, window=window)

    def _read_offset_marker(self) -> tuple[int, str, int] | None:
        """Delegates to ``_SyncOps._read_offset_marker`` (nexus-mbm)."""
        return self._sync._read_offset_marker()

    @staticmethod
    def _prefix_sql(prefix: str) -> tuple[str, list]:
        """Return (WHERE clause, params) for exact tumbler prefix matching.

        Uses segment counting to avoid lexicographic ordering bugs with
        dot-separated integers (e.g., '1.10' < '1.9' lexicographically).
        """
        depth = len(prefix.split("."))
        # Match tumblers that start with prefix. and have exactly depth+1 segments
        # e.g. prefix='1.1' (depth=2) matches '1.1.42' but not '1.10.1' or '1.1.42.7'
        like = prefix + ".%"
        # Exclude deeper segments: count dots must equal depth
        return (
            f"tumbler LIKE ? AND (length(tumbler) - length(replace(tumbler, '.', ''))) = ?",
            [like, depth],
        )

    def _ensure_consistent(self) -> None:
        """Delegates to ``_SyncOps._ensure_consistent`` (nexus-mbm)."""
        return self._sync._ensure_consistent()

    def _event_log_covers_legacy(self) -> bool:
        """Delegates to ``_SyncOps._event_log_covers_legacy`` (nexus-mbm)."""
        return self._sync._event_log_covers_legacy()

    def jsonl_paths(self) -> tuple[Path, ...]:
        """Public accessor for legacy JSONL file paths.

        The three paths returned correspond to the legacy
        ``owners``/``documents``/``links`` tables one-to-one (callers
        like ``_should_compact`` rely on ``Path.stem`` matching the
        table name). Use :meth:`mtime_paths` for change detection — it
        also includes ``events.jsonl`` once event-sourced writes are in
        use.
        """
        return (self._owners_path, self._documents_path, self._links_path)

    def mtime_paths(self) -> tuple[Path, ...]:
        """Return every catalog file whose mtime advances on a write.

        Adds ``events.jsonl`` to :meth:`jsonl_paths`. Callers that watch
        for cross-process writes (the MCP singleton's freshness check)
        must include events.jsonl, otherwise an event-sourced write made
        by another process is invisible to the cache and the projector
        replay never re-runs.
        """
        return (
            self._owners_path,
            self._documents_path,
            self._links_path,
            self._events_path,
        )

    @classmethod
    def init(cls, catalog_path: Path, remote: str | None = None) -> Catalog:
        """Create catalog git repo with empty JSONL files.

        Delegates the git plumbing to :mod:`nexus.catalog.catalog_git`
        (nexus-mbm). When *remote* is provided and no local repo
        exists, prefer cloning so the new machine starts from the
        existing canonical history; otherwise initialise a local
        repo from scratch.
        """
        git_dir = catalog_path / ".git"
        if remote and not git_dir.exists():
            _git.clone_catalog(remote, catalog_path)
            return cls(catalog_path, catalog_path / ".catalog.db")
        _git.init_repo(catalog_path)
        if remote:
            _git.add_remote_origin_if_missing(catalog_path, remote)
        return cls(catalog_path, catalog_path / ".catalog.db")

    @staticmethod
    def is_initialized(catalog_path: Path) -> bool:
        """Return True if catalog git repo exists at path."""
        return (
            (catalog_path / ".git").exists()
            and (catalog_path / "documents.jsonl").exists()
        )

    def _should_compact(self, ratio: float = 3.0) -> bool:
        """Check if JSONL bloat ratio exceeds threshold."""
        try:
            for path in self.jsonl_paths():
                if not path.exists():
                    continue
                total_lines = sum(1 for line in path.open() if line.strip())
                if total_lines == 0:
                    continue
                live_count = self._db.execute(
                    f"SELECT count(*) FROM {path.stem}"  # owners, documents, links
                ).fetchone()[0]
                if live_count > 0 and total_lines / live_count >= ratio:
                    return True
        except Exception:
            pass
        return False

    def sync(self, message: str = "catalog update") -> None:
        """git add -A && git commit && git push (if remote configured).

        Auto-compacts JSONL files when bloat ratio exceeds 3x live
        records. Holds the catalog directory flock for the duration
        so concurrent appenders don't race the commit. Subprocess
        plumbing lives in :mod:`nexus.catalog.catalog_git`.
        """
        dir_fd = self._acquire_lock()
        try:
            if self._should_compact():
                _log.info("catalog_auto_defrag")
                self._defrag_unlocked()
            _git.commit_and_push(self._dir, message)
        finally:
            self._release_lock(dir_fd)

    def pull(self) -> None:
        """git pull && rebuild SQLite from JSONL."""
        _git.pull_origin_if_remote(self._dir)
        self.rebuild()

    # ── Locking ────────────────────────────────────────────────────────────

    def _acquire_lock(self) -> int:
        dir_fd = os.open(str(self._dir), os.O_RDONLY)
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        return dir_fd

    def _release_lock(self, dir_fd: int) -> None:
        fcntl.flock(dir_fd, fcntl.LOCK_UN)
        os.close(dir_fd)

    # ── JSONL append helpers ───────────────────────────────────────────────

    def _append_jsonl(self, path: Path, record: dict) -> None:
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    # ── RDR-101 Phase 1 PR F: shadow event-log emit ─────────────────────

    def _write_to_event_log(self, event: "_Event") -> None:
        """Append ``event`` to ``events.jsonl`` unconditionally.

        Caller MUST hold the catalog directory flock and MUST handle
        TypeError / OSError; this helper does no error suppression
        (compare ``_emit_shadow_event``, which is best-effort).

        The Phase 3 event-sourced write path uses this helper because
        the event log IS the canonical source of truth — a failure to
        append must abort the write, not be silently swallowed.
        """
        if not self._events_path.exists():
            self._events_path.touch()
        line = json.dumps(event.to_dict(), separators=(",", ":"))
        with self._events_path.open("a") as f:
            f.write(line)
            f.write("\n")

    def _emit_shadow_event(self, event: "_Event") -> None:
        """Append ``event`` to ``events.jsonl`` if shadow emit is enabled.

        Caller MUST hold the catalog directory flock — this method does
        not acquire its own. Mirrors ``_append_jsonl``'s contract so the
        emit happens inside the same critical section as the JSONL +
        SQLite writes the event corresponds to (no torn cross-file state
        if the process crashes between writes).

        Off-by-default. Read the gate once at ``Catalog.__init__`` time
        from ``NEXUS_EVENT_LOG_SHADOW``; runtime flips require a fresh
        Catalog construction. Phase 3 will flip the default and remove
        the gate.

        v: 0 schema is used so the existing Phase 1 projector handlers
        apply unchanged. Phase 3 introduces v: 1 native-write semantics
        with new projector handlers.

        Failure handling: the shadow log is non-authoritative in Phase 1
        (nothing reads it yet). A failed emit MUST NOT abort the
        catalog mutation that triggered it — SQLite + the legacy JSONL
        are already committed by the time this method runs. Pre-fix
        ``json.dumps``'s default=str silently coerced bad payload
        values; the post-fix raises TypeError, which would otherwise
        propagate out of the mutator and leave the catalog in a
        committed-but-unobservable state. Catch broadly here, log a
        structured warning, and let the mutation succeed. The doctor
        verb's ``--replay-equality`` will surface the resulting event
        log gap at audit time.
        """
        if not self._shadow_emit_enabled:
            return
        # Skip shadow emit when the event-sourced path is on — the new
        # path writes the same event via _write_to_event_log already,
        # and a duplicate emit would write the line twice.
        if self._event_sourced_enabled:
            return
        try:
            self._write_to_event_log(event)
        except Exception as exc:
            event_type = getattr(event, "type", "?")
            _log.warning(
                "shadow_emit_failed",
                event_type=event_type,
                error=str(exc),
                error_type=type(exc).__name__,
                path=str(self._events_path),
            )

    # ── Owners ─────────────────────────────────────────────────────────────

    def register_owner(
        self, name: str, owner_type: str, *, repo_hash: str = "", description: str = "", repo_root: str = ""
    ) -> Tumbler:
        # nexus-zbne (part of nexus-b34f): owner_type="repo" without a
        # repo_hash is the pathway that produced 83 orphan owners in the
        # live catalog — callers skipped ``owner_for_repo(repo_hash)`` and
        # fell straight through to register_owner(), accumulating one
        # alias per (repo_root, indexing-run) pair. Refuse the call so the
        # invariant is enforced at the API boundary: every repo owner
        # must be keyed by a stable hash that ``owner_for_repo`` can find.
        if owner_type == "repo" and not repo_hash.strip():
            raise ValueError(
                "register_owner(owner_type='repo') requires a non-empty repo_hash. "
                "Use Catalog.owner_for_repo(repo_hash) to look up an existing owner "
                "before falling through to register_owner()."
            )
        if repo_root and not Path(repo_root).is_absolute():
            raise ValueError(f"repo_root must be an absolute path: {repo_root!r}")
        dir_fd = self._acquire_lock()
        try:
            # Compute next owner number. Under event-sourced mode the
            # events.jsonl is canonical and SQLite is its projection,
            # which means SQLite is consistent with all committed
            # events even after a crash that lost the JSONL append
            # (events.jsonl is written FIRST, SQLite committed second,
            # JSONL appended last). Reading the high-water-mark from
            # JSONL would re-allocate a colliding tumbler in that
            # crash window. Under legacy mode JSONL is canonical, so
            # read from JSONL.
            if self._event_sourced_enabled:
                row = self._db.execute(
                    "SELECT COALESCE(MAX(CAST(SUBSTR(tumbler_prefix, "
                    "INSTR(tumbler_prefix, '.') + 1) AS INTEGER)), 0) "
                    "FROM owners WHERE tumbler_prefix LIKE '1.%'"
                ).fetchone()
                next_num = (row[0] or 0) + 1
            else:
                owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
                next_num = max(
                    (Tumbler.parse(k).owner for k in owners), default=0
                ) + 1
            prefix = f"1.{next_num}"
            rec = OwnerRecord(
                owner=prefix,
                name=name,
                owner_type=owner_type,
                repo_hash=repo_hash,
                description=description,
                repo_root=repo_root,
            )
            event = _make_event(
                _OwnerRegisteredPayload(
                    owner_id=prefix,
                    name=name,
                    owner_type=owner_type,
                    repo_root=repo_root,
                    repo_hash=repo_hash,
                    description=description,
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                # Event-sourced path: events.jsonl first, projector
                # writes SQLite, legacy JSONL last for back-compat.
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._owners_path, rec.__dict__)
            else:
                self._append_jsonl(self._owners_path, rec.__dict__)
                # Upsert SQLite
                self._db.execute(
                    "INSERT OR REPLACE INTO owners (tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (prefix, name, owner_type, repo_hash, description, repo_root),
                )
                self._db.commit()
                self._emit_shadow_event(event)
            return Tumbler.parse(prefix)
        finally:
            self._release_lock(dir_fd)

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        """Delegates to ``_DocumentOps.owner_for_repo`` (nexus-mbm)."""
        return self._docs.owner_for_repo(repo_hash)

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        """Delegates to ``_DocumentOps.owner_tumblers_by_name`` (nexus-mbm)."""
        return self._docs.owner_tumblers_by_name(name)

    def ensure_owner_for_repo(
        self, repo: Path, *, repo_name: str = "", description: str = "",
    ) -> Tumbler:
        """Look up or register the owner for ``repo``.

        RDR-103 Phase 4: extracts the owner-registration step from
        :func:`nexus.indexer._catalog_hook` so callers that need the
        owner BEFORE the indexer's hook fires (e.g. ``nx index repo``
        registering the registry entry) can mint it up front. Lookup
        is keyed by ``_repo_identity(repo)`` for stability across
        worktrees.

        Idempotent: existing owners are returned without re-registering.
        ``repo_name`` defaults to the basename returned by
        :func:`nexus.registry._repo_identity`; ``description`` defaults
        to ``"Git repository: {repo_name}"``.
        """
        from nexus.registry import _repo_identity  # noqa: PLC0415

        derived_name, repo_hash = _repo_identity(repo)
        existing = self.owner_for_repo(repo_hash)
        if existing is not None:
            return existing
        return self.register_owner(
            name=repo_name or derived_name,
            owner_type="repo",
            repo_hash=repo_hash,
            repo_root=str(repo),
            description=description or f"Git repository: {repo_name or derived_name}",
        )

    # ── Documents ──────────────────────────────────────────────────────────

    def _owner_repo_root(self, owner: Tumbler) -> str:
        """Return the owner's ``repo_root``, or ``""`` if unknown.

        nexus-3e4s: register() and update() call this to anchor relative
        ``file_path`` values to the owner's working tree instead of CWD.
        """
        row = self._db.execute(
            "SELECT repo_root FROM owners WHERE tumbler_prefix = ?",
            (str(owner),),
        ).fetchone()
        if not row or not row[0]:
            return ""
        # nexus-3e4s S4: defensively absolute-path the stored value. A
        # legacy relative repo_root (pre-RDR-060 migration artifact)
        # would otherwise let _normalize_source_uri's os.path.abspath()
        # fallback re-anchor on CWD, silently re-introducing the bug.
        return os.path.abspath(row[0])

    def _check_source_uri_in_repo_root(
        self, owner: Tumbler, source_uri: str,
    ) -> None:
        """Reject cross-project ``source_uri`` attribution at register time.

        nexus-3e4s: this is the load-bearing guard against the bug class
        where owner ``A``'s tumbler accumulates rows whose ``source_uri``
        lives in a different project's working tree. The signature was
        observed in the live catalog as ~6,500 rows where, for example,
        the ART repo's owner registered files under nexus's source tree.

        The guard is owner-type-aware:

        * ``repo`` owners with a non-empty ``repo_root`` enforce the match;
        * ``curator`` owners (no ``repo_root``) legitimately span sources
          and skip the check;
        * pre-RDR-060 ``repo`` owners persisted before ``repo_root`` was
          a column have ``repo_root=""`` and skip the check (back-compat).

        Only ``file://`` URIs carry a project association — ``chroma://``,
        ``https://``, ``x-devonthink-item://``, etc. have no filesystem
        identity to compare against and pass through unchanged.

        Set ``NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1`` to bypass for
        emergency recovery; this is never the right answer for normal
        indexing.
        """
        if os.environ.get(_CROSS_PROJECT_OVERRIDE_ENV) == "1":
            return
        if not source_uri:
            return
        parsed = urlparse(source_uri)
        if parsed.scheme != "file":
            return
        row = self._db.execute(
            "SELECT owner_type, repo_root FROM owners WHERE tumbler_prefix = ?",
            (str(owner),),
        ).fetchone()
        if not row:
            # Owner-not-found is its own bug class; the existing
            # foreign-key flow surfaces it elsewhere. Don't double-fault.
            return
        owner_type, repo_root = row[0], row[1] or ""
        if owner_type != "repo" or not repo_root:
            return
        # Realpath both sides so symlinked roots (notably macOS's
        # /private/var ↔ /var) and ``..`` segments don't trigger
        # false positives. realpath() tolerates non-existent paths.
        from urllib.parse import unquote

        file_abs = unquote(parsed.path)
        real_file = os.path.realpath(file_abs)
        real_root = os.path.realpath(repo_root)
        try:
            Path(real_file).relative_to(real_root)
        except ValueError:
            raise ValueError(
                f"cross-project source_uri rejected (nexus-3e4s): "
                f"owner {owner} has repo_root={repo_root!r} but "
                f"source_uri {source_uri!r} resolves to {real_file!r} "
                f"which is outside the owner's repo_root. This is the "
                f"signature of the contamination bug class. Set "
                f"{_CROSS_PROJECT_OVERRIDE_ENV}=1 to bypass for "
                f"emergency recovery."
            ) from None

    def register(
        self,
        owner: Tumbler,
        title: str,
        *,
        content_type: str = "",
        file_path: str = "",
        corpus: str = "",
        physical_collection: str = "",
        chunk_count: int = 0,
        head_hash: str = "",
        author: str = "",
        year: int = 0,
        meta: dict | None = None,
        source_mtime: float = 0.0,
        source_uri: str = "",
    ) -> Tumbler:
        # nexus-3e4s: anchor relative file_path values to the owner's
        # repo_root, not CWD. Without this, indexing repo A from a CWD
        # inside repo B writes source_uris pointing to B's tree but
        # attributed to A's owner — the contamination signature.
        owner_repo_root = self._owner_repo_root(owner)
        # RDR-096 P3.1: validate / derive source_uri at the register
        # boundary. Bare paths normalize to ``file://``; explicit URIs
        # are validated for scheme; malformed URIs raise ValueError
        # rather than silently persist (matches the RDR's risk
        # mitigation strategy).
        source_uri = _normalize_source_uri(
            source_uri, file_path, repo_root=owner_repo_root,
        )
        # nexus-3e4s: cross-project attribution guard. Reject when the
        # source_uri lives outside the owner's repo_root prefix.
        self._check_source_uri_in_repo_root(owner, source_uri)
        dir_fd = self._acquire_lock()
        try:
            # Idempotency: check by file_path if non-empty
            if file_path:
                existing = self.by_file_path(owner, file_path)
                if existing is not None:
                    return existing.tumbler

            # Idempotency: check by head_hash + title within same owner
            # (content-addressed dedup for re-indexing the same document)
            if head_hash and title:
                prefix_clause, prefix_params = self._prefix_sql(str(owner))
                row = self._db.execute(
                    f"SELECT tumbler FROM documents WHERE {prefix_clause} "
                    f"AND head_hash = ? AND title = ? LIMIT 1",
                    (*prefix_params, head_hash, title),
                ).fetchone()
                if row:
                    return Tumbler.parse(row[0])

            # Permanent addressing: use owner's high-water mark from JSONL,
            # not SQLite MAX(). This prevents tumbler reuse after delete+compact.
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            owner_rec = owners.get(str(owner))
            if owner_rec and owner_rec.next_seq > 0:
                doc_num = owner_rec.next_seq
            else:
                # Fallback for pre-migration owners without next_seq
                doc_num = self._db.next_document_number(str(owner))

            tumbler = Tumbler((*owner.segments, doc_num))

            # Bump and persist the high-water mark
            new_seq = doc_num + 1
            if owner_rec:
                owner_rec.next_seq = new_seq
                self._append_jsonl(self._owners_path, owner_rec.__dict__)
            else:
                # Fallback: owner exists in SQLite but has no JSONL next_seq.
                # Persist it now so future registrations use the JSONL path.
                row = self._db.execute(
                    "SELECT name, owner_type, repo_hash, description FROM owners "
                    "WHERE tumbler_prefix = ?", (str(owner),)
                ).fetchone()
                if row:
                    fallback_rec = OwnerRecord(
                        owner=str(owner), name=row[0], owner_type=row[1],
                        repo_hash=row[2] or "", description=row[3] or "",
                        next_seq=new_seq,
                    )
                    self._append_jsonl(self._owners_path, fallback_rec.__dict__)
            now = datetime.now(UTC).isoformat()
            rec = DocumentRecord(
                tumbler=str(tumbler),
                title=title,
                author=author,
                year=year,
                content_type=content_type,
                file_path=file_path,
                corpus=corpus,
                physical_collection=physical_collection,
                chunk_count=chunk_count,
                head_hash=head_hash,
                indexed_at=now,
                meta=meta or {},
                source_mtime=source_mtime,
                source_uri=source_uri,
            )
            event = _make_event(
                _DocumentRegisteredPayload(
                    # Phase 1 stand-in: tumbler doubles as doc_id until
                    # Phase 3 mints UUID7 doc_ids via the new write path.
                    doc_id=str(tumbler),
                    owner_id=str(owner),
                    content_type=content_type,
                    source_uri=source_uri,
                    coll_id=physical_collection,
                    title=title,
                    source_mtime=source_mtime,
                    indexed_at_doc=now,
                    # Legacy fields needed for the v: 0 projection.
                    tumbler=str(tumbler),
                    author=author,
                    year=year,
                    file_path=file_path,
                    corpus=corpus,
                    physical_collection=physical_collection,
                    chunk_count=chunk_count,
                    head_hash=head_hash,
                    indexed_at=now,
                    alias_of="",
                    meta=dict(meta or {}),
                ),
                v=0,
            )

            if self._event_sourced_enabled:
                # RDR-101 Phase 3 PR α — event-sourced write path.
                # Order is inverted: events.jsonl FIRST, then SQLite
                # via the projector, then legacy documents.jsonl for
                # back-compat. The event log is canonical; SQLite is a
                # deterministic projection of it.
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._documents_path, rec.__dict__)
            else:
                # Legacy direct-write path. Shadow emit (PR F) optionally
                # writes the event AFTER the SQLite + JSONL commits.
                self._append_jsonl(self._documents_path, rec.__dict__)
                self._db.execute(
                    "INSERT INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                    "metadata, source_mtime, source_uri) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(tumbler), title, author, year, content_type, file_path,
                        corpus, physical_collection, chunk_count, head_hash, now,
                        json.dumps(meta or {}), source_mtime, source_uri,
                    ),
                )
                self._db.commit()
                self._emit_shadow_event(event)
            return tumbler
        finally:
            self._release_lock(dir_fd)

    def resolve(self, tumbler: Tumbler, *, follow_alias: bool = True) -> CatalogEntry | None:
        """Delegates to ``_DocumentOps.resolve`` (nexus-mbm)."""
        return self._docs.resolve(tumbler, follow_alias=follow_alias)

    def list_by_collection(
        self, physical_collection: str, *, limit: int | None = None,
    ) -> list[CatalogEntry]:
        """Delegates to ``_DocumentOps.list_by_collection`` (nexus-mbm)."""
        return self._docs.list_by_collection(physical_collection, limit=limit)

    # ── RDR-101 Phase 6: Collections projection (nexus-o6aa.14) ─────────
    # nexus-mbm follow-up: ``_COLLECTION_COLUMNS`` and
    # ``_row_to_collection_dict`` moved to module-level in
    # :mod:`nexus.catalog.catalog_docs` so the read methods that
    # consume them (``list_collections``, ``get_collection``,
    # ``list_by_collection``) and the writer below can share one
    # source of truth without crossing the ``cat._row_to_...``
    # private-method boundary.

    def register_collection(
        self,
        name: str,
        *,
        content_type: str = "",
        owner_id: str = "",
        embedding_model: str = "",
        model_version: str = "",
        display_name: str = "",
    ) -> None:
        """Register a ChromaDB collection in the catalog projection.

        Idempotent on ``name``. The first call writes the row + emits a
        ``CollectionCreated`` event; subsequent calls with identical
        canonical fields short-circuit (no duplicate event, no log
        bloat). Subsequent calls that change a canonical field re-emit
        the event so the projection picks up the new value.

        For non-conformant names, the canonical fields (``content_type``
        etc.) may be left empty; the row is still written and flagged
        as legacy via the projector's regex. For conformant names,
        callers are encouraged to supply the segments so they round-trip
        exactly.

        Honors the ``_event_sourced_enabled`` split that ``register_owner``
        and the other writers use: in event-sourced mode the event is
        canonical and the projector writes SQLite; in legacy mode SQLite
        is written directly and the event is shadow-emitted.
        """
        from datetime import UTC, datetime  # noqa: PLC0415
        from nexus.corpus import is_conformant_collection_name  # noqa: PLC0415

        if not name:
            raise ValueError("register_collection: name must be non-empty")

        ts = datetime.now(UTC).isoformat()
        # nexus-7m8n: freeze legacy_grandfathered on the event at write
        # time so future regex changes do not drift projected rows.
        legacy_grandfathered = not is_conformant_collection_name(name)
        event = _make_event(
            _CollectionCreatedPayload(
                coll_id=name,
                owner_id=owner_id,
                content_type=content_type,
                embedding_model=embedding_model,
                model_version=model_version,
                name=display_name or name,
                created_at=ts,
                legacy_grandfathered=legacy_grandfathered,
            ),
            v=0,
            ts=ts,
        )
        dir_fd = self._acquire_lock()
        try:
            # Short-circuit: if the row already exists with the same
            # canonical fields, do not re-emit. Event log was previously
            # appended unconditionally on every call; for hot paths
            # (backfill on a 100-collection catalog re-run on every CI
            # build) this produced duplicate CollectionCreated events
            # with no projection effect.
            #
            # nexus-qpet.2: re-check inside the locked block so a
            # concurrent register_collection of the same name cannot
            # slip a duplicate event into the log between the read and
            # the lock acquisition.
            existing = self.get_collection(name)
            if existing is not None and (
                existing["content_type"] == content_type
                and existing["owner_id"] == owner_id
                and existing["embedding_model"] == embedding_model
                and existing["model_version"] == model_version
                and existing["display_name"] == (display_name or name)
            ):
                return
            if self._event_sourced_enabled:
                self._write_to_event_log(event)
                self._projector.apply(event)
                # ``created_at`` lands via the projector's COALESCE
                # using ``payload.created_at`` (RDR-101 Phase 6
                # prophylactic-review fix #2 / nexus-qpet); replay
                # equality holds without an out-of-band UPDATE.
                self._db.commit()
            else:
                # Legacy mode: SQLite is canonical, no JSONL backing
                # file for collections (they did not exist pre-Phase-6).
                # Direct INSERT OR REPLACE matches the projector handler
                # except for the regex-derived legacy_grandfathered flag,
                # which we compute the same way here.
                legacy = 0 if is_conformant_collection_name(name) else 1
                self._db.execute(
                    "INSERT OR REPLACE INTO collections "
                    "(name, content_type, owner_id, embedding_model, "
                    "model_version, display_name, legacy_grandfathered, "
                    "superseded_by, superseded_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, "
                    "COALESCE((SELECT superseded_by FROM collections WHERE name = ?), ''), "
                    "COALESCE((SELECT superseded_at FROM collections WHERE name = ?), ''), "
                    "COALESCE((SELECT created_at FROM collections WHERE name = ?), ?))",
                    (
                        name, content_type, owner_id, embedding_model,
                        model_version, display_name or name, legacy,
                        name, name, name, ts,
                    ),
                )
                self._db.commit()
                self._emit_shadow_event(event)
        finally:
            self._release_lock(dir_fd)

    def list_collections(self) -> list[dict]:
        """Delegates to ``_DocumentOps.list_collections`` (nexus-mbm)."""
        return self._docs.list_collections()

    def get_collection(self, name: str) -> dict | None:
        """Delegates to ``_DocumentOps.get_collection`` (nexus-mbm)."""
        return self._docs.get_collection(name)

    def is_legacy_collection(self, name: str) -> bool:
        """Delegates to ``_DocumentOps.is_legacy_collection`` (nexus-mbm)."""
        return self._docs.is_legacy_collection(name)

    def collection_for(
        self,
        content_type: str,
        owner: Tumbler | str,
        embedding_model: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        """Delegates to ``_DocumentOps.collection_for`` (nexus-mbm)."""
        return self._docs.collection_for(content_type, owner, embedding_model, bump=bump)

    def collection_for_repo(
        self,
        repo: Path,
        content_type: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        """Delegates to ``_DocumentOps.collection_for_repo`` (nexus-mbm)."""
        return self._docs.collection_for_repo(repo, content_type, bump=bump)

    def _update_document_collection_locked(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        """Delegates to ``_WriteOps._update_document_collection_locked`` (nexus-mbm)."""
        return self._writes._update_document_collection_locked(tumbler, new_collection)

    def update_document_collection(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        """Delegates to ``_WriteOps.update_document_collection`` (nexus-mbm)."""
        return self._writes.update_document_collection(tumbler, new_collection)

    def update_documents_collection_batch(
        self, pairs: list[tuple[str, str]],
    ) -> int:
        """Delegates to ``_WriteOps.update_documents_collection_batch`` (nexus-mbm)."""
        return self._writes.update_documents_collection_batch(pairs)

    def supersede_collection(
        self,
        old_name: str,
        new_name: str,
        *,
        reason: str = "",
    ) -> None:
        """Delegates to ``_WriteOps.supersede_collection`` (nexus-mbm)."""
        return self._writes.supersede_collection(old_name, new_name, reason=reason)

    def resolve_alias(self, tumbler: Tumbler, *, max_hops: int = 16) -> Tumbler:
        """Delegates to ``_DocumentOps.resolve_alias`` (nexus-mbm)."""
        return self._docs.resolve_alias(tumbler, max_hops=max_hops)

    def set_alias(self, tumbler: Tumbler, canonical: Tumbler) -> None:
        """Delegates to ``_WriteOps.set_alias`` (nexus-mbm)."""
        return self._writes.set_alias(tumbler, canonical)

    def resolve_path(self, tumbler: Tumbler) -> Path | None:
        """Delegates to ``_DocumentOps.resolve_path`` (nexus-mbm)."""
        return self._docs.resolve_path(tumbler)

    def descendants(self, prefix: str) -> list[dict]:
        """Delegates to ``_DocumentOps.descendants`` (nexus-mbm)."""
        return self._docs.descendants(prefix)

    def resolve_chunk(self, tumbler: Tumbler) -> dict | None:
        """Delegates to ``_DocumentOps.resolve_chunk`` (nexus-mbm)."""
        return self._docs.resolve_chunk(tumbler)

    def resolve_span(
        self, span: str, physical_collection: str, t3: "ClientAPI",
    ) -> dict | None:
        """Resolve a ``chash:`` span to chunk content + metadata in T3.

        Thin delegate to :func:`nexus.catalog.catalog_spans.resolve_span_in_t3`
        (nexus-mbm). See that function for the full contract.
        """
        from nexus.catalog import catalog_spans
        return catalog_spans.resolve_span_in_t3(span, physical_collection, t3)

    def resolve_chash(
        self,
        chash: str,
        t3: "ClientAPI",
        chash_index: "Any",
        *,
        prefer_collection: str | None = None,
    ) -> "dict | None":
        """Globally resolve a chash to the chunk it names (RDR-086 Phase 2).

        Thin delegate to
        :func:`nexus.catalog.catalog_spans.resolve_chash_globally`
        (nexus-mbm). See that function for the full contract.
        """
        from nexus.catalog import catalog_spans
        return catalog_spans.resolve_chash_globally(
            chash, t3, chash_index, prefer_collection=prefer_collection,
        )

    def update(self, tumbler: Tumbler, **fields: object) -> None:
        """Delegates to ``_WriteOps.update`` (nexus-mbm)."""
        return self._writes.update(tumbler, **fields)

    def rename_collection(self, old: str, new: str) -> int:
        """Delegates to ``_WriteOps.rename_collection`` (nexus-mbm)."""
        return self._writes.rename_collection(old, new)

    def delete_document(self, tumbler: Tumbler) -> bool:
        """Delegates to ``_WriteOps.delete_document`` (nexus-mbm)."""
        return self._writes.delete_document(tumbler)

    def write_manifest(self, doc_id: str, chunks: list[dict]) -> None:
        """Write the document_chunks manifest for one document (RDR-108 D2).

        Delegates to ``_WriteOps.write_manifest`` (nexus-j43k).
        See catalog_writes._WriteOps.write_manifest for the contract.
        """
        return self._writes.write_manifest(doc_id, chunks)

    def append_manifest_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        """UPSERT manifest rows for one document (RDR-108 Phase 3,
        nexus-bdag). Use from per-batch hook contexts; see
        :meth:`_WriteOps.append_manifest_chunks` for the contract."""
        return self._writes.append_manifest_chunks(doc_id, chunks)

    def get_manifest(self, doc_id: str) -> list[_ManifestRow]:
        """Return ordered manifest rows for ``doc_id`` (nexus-572g K6)."""
        return self._writes.get_manifest(doc_id)

    def docs_for_chashes(self, chashes: list[str]) -> dict[str, list[str]]:
        """Reverse-lookup: chash -> [doc_id, ...] (nexus-572g K6)."""
        return self._writes.docs_for_chashes(chashes)

    def find(self, query: str, *, content_type: str | None = None) -> list[CatalogEntry]:
        """Delegates to ``_DocumentOps.find`` (nexus-mbm)."""
        return self._docs.find(query, content_type=content_type)

    def by_file_path(self, owner: Tumbler, file_path: str) -> CatalogEntry | None:
        """Delegates to ``_DocumentOps.by_file_path`` (nexus-mbm)."""
        return self._docs.by_file_path(owner, file_path)

    def by_owner(self, owner: Tumbler) -> list[CatalogEntry]:
        """Delegates to ``_DocumentOps.by_owner`` (nexus-mbm)."""
        return self._docs.by_owner(owner)

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        """Delegates to ``_DocumentOps.by_content_type`` (nexus-mbm)."""
        return self._docs.by_content_type(content_type)

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        """Delegates to ``_DocumentOps.by_corpus`` (nexus-mbm)."""
        return self._docs.by_corpus(corpus)

    def doc_count(self) -> int:
        """Delegates to ``_DocumentOps.doc_count`` (nexus-mbm)."""
        return self._docs.doc_count()

    def all_documents(
        self, limit: int = 0, *, content_type: str = "",
    ) -> list[CatalogEntry]:
        """Delegates to ``_DocumentOps.all_documents`` (nexus-mbm)."""
        return self._docs.all_documents(limit, content_type=content_type)

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        """Delegates to ``_DocumentOps.by_doc_id`` (nexus-mbm)."""
        return self._docs.by_doc_id(doc_id)

    # ── Links ──────────────────────────────────────────────────────────────
    # nexus-mbm: implementations live in
    # :class:`nexus.catalog.catalog_links._LinkOps`, composed onto
    # ``self._links`` in ``__init__``. The methods below are one-line
    # delegates that preserve the public Catalog API.
    #
    # ``cat._MAX_GRAPH_DEPTH`` / ``cat._MAX_GRAPH_NODES`` addressable
    # at the historical names — copied here from ``catalog_links``
    # at class-body time so both ``Catalog._MAX_GRAPH_NODES`` (class
    # access, used by ``test_catalog_graph_many``) and
    # ``cat._MAX_GRAPH_NODES`` (instance access) return an integer.
    #
    # Patching contract for tests:
    #   (a) ``patch.object(type(cat), "_MAX_GRAPH_NODES", N)`` — works
    #       (Catalog class attribute replaced; ``getattr(cat, ...)``
    #       returns N).
    #   (b) ``cat._MAX_GRAPH_NODES = N`` — works (instance attribute
    #       shadows the class attribute).
    #   (c) ``monkeypatch.setattr("nexus.catalog.catalog_links._MAX_GRAPH_NODES",
    #       N)`` — does NOT propagate (Catalog's class attribute was
    #       copied by value at class-body time and is not re-read).
    #       Patch via (a) or (b) instead.
    #
    # A ``@property`` re-reading ``catalog_links`` on every access
    # would propagate (c) but breaks ``Catalog._MAX_GRAPH_NODES``
    # class-level reads (returns the property descriptor instead of
    # an int). The class-attribute alias is the simpler contract.
    from nexus.catalog import catalog_links as _links_mod
    _MAX_GRAPH_DEPTH = _links_mod._MAX_GRAPH_DEPTH
    _MAX_GRAPH_NODES = _links_mod._MAX_GRAPH_NODES
    del _links_mod

    def link(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        """Create or merge a link. Returns True if new, False if merged."""
        return self._links.link(
            from_t, to_t, link_type, created_by,
            from_span=from_span, to_span=to_span,
            allow_dangling=allow_dangling, **meta,
        )

    def link_if_absent(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        """Insert-or-skip via the UNIQUE constraint."""
        return self._links.link_if_absent(
            from_t, to_t, link_type, created_by,
            from_span=from_span, to_span=to_span,
            allow_dangling=allow_dangling, **meta,
        )

    def unlink(
        self, from_t: Tumbler, to_t: Tumbler, link_type: str = "",
    ) -> int:
        """Delete one or all link types between *from_t* and *to_t*."""
        return self._links.unlink(from_t, to_t, link_type)

    def links_from(
        self,
        tumbler: Tumbler,
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list[CatalogLink]:
        """All outbound links from *tumbler*."""
        return self._links.links_from(tumbler, link_type, link_types)

    def links_to(
        self,
        tumbler: Tumbler,
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list[CatalogLink]:
        """All inbound links to *tumbler*."""
        return self._links.links_to(tumbler, link_type, link_types)

    def link_query(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        direction: str = "both",
        tumbler: str = "",
        created_at_before: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[CatalogLink]:
        """Composable link filter. ``limit=0`` means unlimited."""
        return self._links.link_query(
            from_t=from_t, to_t=to_t, link_type=link_type,
            created_by=created_by, direction=direction, tumbler=tumbler,
            created_at_before=created_at_before, limit=limit, offset=offset,
        )

    def bulk_unlink(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        created_at_before: str = "",
        dry_run: bool = False,
    ) -> int:
        """Filtered bulk delete with JSONL tombstones."""
        return self._links.bulk_unlink(
            from_t=from_t, to_t=to_t, link_type=link_type,
            created_by=created_by, created_at_before=created_at_before,
            dry_run=dry_run,
        )

    def validate_link(
        self, from_t: Tumbler, to_t: Tumbler, link_type: str,
    ) -> list[str]:
        """Return a list of validation errors (empty = link is valid)."""
        return self._links.validate_link(from_t, to_t, link_type)

    def resolve_span_text(self, tumbler: Tumbler, span: str) -> str | None:
        """Resolve a span to actual text content. Returns None if unavailable.

        Resolves the tumbler to a :class:`CatalogEntry` then delegates
        the span dispatch to
        :func:`nexus.catalog.catalog_spans.resolve_span_text_for_entry`
        (nexus-mbm). See that function for the supported span formats.
        """
        if not span:
            return None
        entry = self.resolve(tumbler)
        if entry is None:
            return None
        from nexus.catalog import catalog_spans
        return catalog_spans.resolve_span_text_for_entry(entry, span)

    def link_audit(self, *, t3: "ClientAPI | None" = None) -> dict:
        """Audit the links table."""
        return self._links.link_audit(t3=t3)

    def graph(
        self,
        tumbler: Tumbler,
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> dict:
        """BFS traversal — see ``_LinkOps.graph`` for the full contract."""
        return self._links.graph(
            tumbler, depth=depth, direction=direction,
            link_type=link_type, link_types=link_types,
        )

    def graph_many(
        self,
        seeds: list[Tumbler],
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> dict:
        """BFS from multiple seeds."""
        return self._links.graph_many(
            seeds, depth=depth, direction=direction,
            link_type=link_type, link_types=link_types,
        )

    # ── Rebuild ───────────���──────────────────────────��─────────────────────

    def rebuild(self) -> None:
        """Delegates to ``_SyncOps.rebuild`` (nexus-mbm)."""
        return self._sync.rebuild()

    def _defrag_unlocked(self) -> dict[str, int]:
        """Delegates to ``_SyncOps._defrag_unlocked`` (nexus-mbm)."""
        return self._sync._defrag_unlocked()

    def defrag(self) -> dict[str, int]:
        """Delegates to ``_SyncOps.defrag`` (nexus-mbm)."""
        return self._sync.defrag()

    def compact(self) -> dict[str, int]:
        """Delegates to ``_SyncOps.compact`` (nexus-mbm)."""
        return self._sync.compact()

