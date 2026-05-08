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
from collections import deque
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
        self._last_consistency_mtime: float = self._read_consistency_marker()
        if self._documents_path.exists():
            self._ensure_consistent()

    def _read_consistency_marker(self) -> float:
        """Return the persisted ``_last_consistency_mtime`` or 0.0.

        nexus-wehp: stored inside the catalog SQLite as a row in the
        ``_meta`` table (created by ``CatalogDB._SCHEMA_SQL``, so reads
        never issue DDL that would race a concurrent transaction). A
        fresh SQLite cache (no row) returns 0.0, which forces a rebuild
        and preserves the pre-fix invariant that a fresh cache always
        projects from the canonical state. Read failures fall back to
        0.0 (worst case = pre-fix rebuild).
        """
        try:
            row = self._db.execute(
                "SELECT value FROM _meta WHERE key = ?",
                ("last_consistency_mtime",),
            ).fetchone()
            if row is None:
                return 0.0
            return float(row[0])
        except (sqlite3.OperationalError, ValueError, TypeError):
            return 0.0

    def _projection_counts(self) -> tuple[int, int]:
        """Return (document_count, link_count) for the heartbeat summary.

        Read-only; used by the post-rebuild summary line so operators
        can see the size of what they just rebuilt. Tolerates errors
        and returns ``(0, 0)`` on failure — the summary is informational
        and must never mask a real rebuild result.
        """
        try:
            doc_row = self._db.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()
            link_row = self._db.execute(
                "SELECT COUNT(*) FROM links"
            ).fetchone()
            return (int(doc_row[0]) if doc_row else 0,
                    int(link_row[0]) if link_row else 0)
        except Exception:
            return (0, 0)

    def _write_consistency_marker(self, mtime: float) -> None:
        """Persist the highest successfully-projected canonical mtime.

        nexus-wehp: stored inside the catalog SQLite. Tolerates write
        failures silently — failing to update the marker means the next
        process will re-do the rebuild, which is correctness-preserving
        (the rebuild is idempotent at the projection level).

        RDR-104 critic Critical #2 fix: this write MUST live inside the
        same transaction as the projector writes. Pre-fix it called
        ``self._db.commit()`` independently, which created an asymmetric
        failure window: a crash AFTER the marker commit but BEFORE the
        outer transaction's projection writes committed (or while the
        outer ``with self._conn:`` rolled back) advanced the marker
        without advancing the projection. The next ``_ensure_consistent``
        run would observe the new marker, conclude "nothing to do", and
        permanently skip the events that should have been applied —
        silent corruption with no recovery path.

        Caller contract: this method is invoked from inside an active
        ``CatalogDB.transaction()`` block. The connection-as-context-manager
        commits the marker write atomically with the projection writes
        on successful exit; rolls both back together on any exception.
        Do NOT call ``self._db.commit()`` here.
        """
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_consistency_mtime", f"{mtime}"),
            )
        except sqlite3.OperationalError:
            # RDR-104 Round 2 Significant #1: the OperationalError
            # swallow is intentional. Marker-write failure is rare
            # (transient SQLite lock contention is the most likely
            # cause), idempotent re-replay corrects the next run, and
            # propagating would degrade the catalog (``degraded=True``)
            # for a recoverable cause. The in-memory mirror
            # ``self._last_consistency_mtime`` is assigned post-
            # ``with`` so this instance still short-circuits its own
            # subsequent rebuilds; the next process reads the un-
            # advanced DB row and re-rebuilds, which is correct.
            pass

    def _write_offset_marker(
        self, *, offset: int, header_hash: str, window: int,
    ) -> None:
        """Persist the three RDR-104 incremental marker rows atomically.

        RDR-104 Step 2: writes ``last_applied_event_offset``,
        ``last_applied_event_header_hash``, and
        ``last_applied_event_header_window`` to ``_meta``. All three
        rows must commit together with the projector writes (and the
        ``last_consistency_mtime`` row from
        ``_write_consistency_marker``) so the marker is consistent
        with the projection state.

        Caller contract: this method is invoked from inside an active
        ``CatalogDB.transaction()`` block. The connection-as-context-
        manager commits all four marker rows atomically with the
        projection writes on successful exit; rolls them all back
        together on any exception. Do NOT call ``self._db.commit()``
        here — see ``_write_consistency_marker`` for the same atomicity
        contract.

        Tolerates ``sqlite3.OperationalError`` for the same reasoning
        as ``_write_consistency_marker``: rare transient lock
        contention is corrected by the next idempotent re-replay.
        """
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_META_KEY_LAST_OFFSET, str(offset)),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_META_KEY_HEADER_HASH, header_hash),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_META_KEY_HEADER_WINDOW, str(window)),
            )
        except sqlite3.OperationalError:
            # See _write_consistency_marker for the rationale on the
            # silent OperationalError swallow (RDR-104 Round 2 #1).
            pass

    def _read_offset_marker(self) -> tuple[int, str, int] | None:
        """Return ``(offset, header_hash, window)`` or ``None``.

        RDR-104 Step 2: returns ``None`` when any of the three rows is
        missing OR when the offset / window string is unparseable as
        ``int``. The orchestrator (Step 3) treats ``None`` as the
        bootstrap signal and falls through to full rebuild.

        Returning a partial tuple would let the orchestrator act on
        inconsistent metadata; full rebuild is the correctness-
        preserving fallback.
        """
        try:
            rows = self._db.execute(
                "SELECT key, value FROM _meta WHERE key IN (?, ?, ?)",
                (
                    _META_KEY_LAST_OFFSET,
                    _META_KEY_HEADER_HASH,
                    _META_KEY_HEADER_WINDOW,
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        by_key = {key: value for key, value in rows}
        if (
            _META_KEY_LAST_OFFSET not in by_key
            or _META_KEY_HEADER_HASH not in by_key
            or _META_KEY_HEADER_WINDOW not in by_key
        ):
            return None
        try:
            offset = int(by_key[_META_KEY_LAST_OFFSET])
            window = int(by_key[_META_KEY_HEADER_WINDOW])
        except (TypeError, ValueError):
            return None
        return (offset, by_key[_META_KEY_HEADER_HASH], window)

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
        """Rebuild SQLite from the canonical truth when its mtime has advanced.

        With ``NEXUS_EVENT_SOURCED=1`` the canonical truth is
        ``events.jsonl`` (the event log IS the state per RDR-101 §"Core
        invariants"); the rebuild path replays the log through
        ``Projector.apply_all`` so a cross-process write that landed on
        events.jsonl gets re-projected into this process's SQLite cache.
        With the gate OFF the legacy JSONL files (owners/documents/links)
        remain canonical and the rebuild reads them directly.

        **Bootstrap guardrail.** When the gate is on but the legacy
        JSONL holds substantially more documents than events.jsonl
        carries DocumentRegistered events, we are looking at a freshly-
        flipped catalog whose log is sparse against the legacy state:
        the event-sourced rebuild would DELETE every legacy row and
        replay only the few new events, silently wiping the catalog.
        Refuse the event-sourced path in that scenario (fall through to
        legacy + emit a structured warning). The synthesize-log
        migration verb that historically populated the log was retired
        post Phase 5b (nexus-iftc).

        **Atomicity.** The DELETE+replay sequence runs inside
        ``CatalogDB.transaction()`` so a malformed event, a
        ``NotImplementedError`` from the v: 1 projector path, or an
        ``OperationalError`` mid-replay rolls back to the pre-DELETE
        state instead of leaving SQLite empty.

        Sets ``degraded`` flag on failure so callers can surface the stale
        state rather than silently serving outdated data (nexus-f2vp).

        Storage review S-4: skips the rebuild when no canonical file has
        been written since the last successful rebuild. For a large
        catalog this eliminates the O(entries) parse cost on every
        ``Catalog()`` construction — the MCP server instantiates one
        per tool call.
        """
        try:
            # Track all canonical-truth sources for mtime detection so a
            # rebuild kicks in regardless of which path produced the
            # write. With the gate OFF, legacy JSONL is canonical; with
            # the gate ON, events.jsonl is canonical but legacy JSONL is
            # still written (back-compat) and a bootstrap catalog may
            # have JSONL data with an empty events.jsonl.
            paths_with_mtime: list[tuple[Path, float]] = []
            current_mtime = 0.0
            for p in (
                self._owners_path,
                self._documents_path,
                self._links_path,
                self._events_path,
            ):
                if p.exists():
                    m = p.stat().st_mtime
                    paths_with_mtime.append((p, m))
                    current_mtime = max(current_mtime, m)
            if current_mtime <= self._last_consistency_mtime and not self.degraded:
                return
            trigger = _trigger_file_label(
                paths_with_mtime, self._last_consistency_mtime,
            )

            use_event_log = (
                self._event_sourced_enabled
                and self._events_path.exists()
                and self._events_path.stat().st_size > 0
            )
            # nexus-1sy5: once the offset marker is established, the
            # bootstrap guardrail has already passed at least once —
            # ``_write_offset_marker`` is only reached from rebuild
            # branches that ran after the guardrail accepted the event
            # log. Skip the O(N) ``covers_legacy`` scan in that
            # steady state; it would otherwise dominate every post-
            # write rebuild dispatch (~838 ms on a 460K-event log) and
            # cap the RDR-104 incremental fast path well above its
            # <100 ms target. The marker check is a single
            # `SELECT key, value FROM _meta` and short-circuits before
            # the scan so the perf path is microseconds.
            marker_established = (
                use_event_log and self._read_offset_marker() is not None
            )
            if (
                use_event_log
                and not marker_established
                and not self._event_log_covers_legacy()
            ):
                # Bootstrap guardrail: events.jsonl is non-empty but
                # the legacy JSONL has materially more documents than
                # the event log carries DocumentRegistered events for.
                # Refuse to wipe the legacy rows; fall through to the
                # legacy rebuild and flag the state so operators see
                # it via ``nx catalog doctor`` (not just structlog).
                # nexus-iftc retired the synthesize-log migration
                # verb; the warning now points operators at the
                # ``nx catalog setup`` rebuild path.
                self.bootstrap_fallback_active = True
                _log.warning(
                    "catalog_event_log_incomplete_falling_back_to_legacy",
                    catalog_dir=str(self._dir),
                    note=(
                        "events.jsonl is non-empty but has fewer "
                        "DocumentRegistered events than documents.jsonl "
                        "has rows. ES writes are landing in the log "
                        "but reads come from legacy JSONL; replay "
                        "equality is silently broken. The synthesize-log "
                        "and t3-backfill-doc-id remediation verbs were "
                        "retired post Phase 5b (nexus-iftc). Restore by "
                        "deleting the catalog directory and re-running "
                        "'nx catalog setup' to bootstrap from current "
                        "T3 state."
                    ),
                )
                use_event_log = False
            else:
                self.bootstrap_fallback_active = False
            if use_event_log:
                # RDR-104 Step 3: five-way dispatch over the event-
                # sourced rebuild paths.
                #
                #   (a) empty-delta fast path — events.jsonl unchanged,
                #       only the mtime row advances. Mandatory inside
                #       transaction() for the 4.24.4 atomicity contract.
                #   (b) bootstrap full rebuild — no offset marker yet;
                #       DELETE + replay from offset 0 and write all
                #       four marker rows.
                #   (c) invalidated full rebuild — header-hash drift
                #       OR window-size mismatch; same as bootstrap.
                #   (d) incremental — marker valid, delta non-empty;
                #       replay_from(stored_offset, limit_offset=eof)
                #       inside transaction() with apply_all(commit=
                #       False), then write all four marker rows.
                #   (e) corruption escalation — bounded iterator yields
                #       zero events from a non-empty range; escalate
                #       to (c) WITHOUT advancing the marker.
                #
                # The bulk_load_documents FTS5 fence is preserved on
                # the full-rebuild path only — the per-event projector
                # writes there number in the hundreds of thousands and
                # need the trigger-drop-and-rebuild idiom. Incremental
                # writes are bounded by the delta size (typically <100
                # events) so the per-row trigger overhead is
                # unmeasurable.
                from nexus.catalog.event_log import EventLog
                _log.debug(
                    "catalog_consistency_rebuild_event_sourced",
                    mtime=current_mtime,
                )

                eof_offset_now = self._events_path.stat().st_size
                stored = self._read_offset_marker()
                header_hash_now: str | None = None

                # Empty-delta fast path (Round 1 Significant #4 / Round 2 #4).
                # eof_offset_now == stored_offset means events.jsonl has not
                # been appended to since the last successful rebuild. mtime
                # ticked elsewhere (a legacy JSONL write, owners.jsonl, etc.)
                # so we landed in the rebuild branch but there is nothing to
                # replay. Advance only last_consistency_mtime — inside a
                # transaction() for the 4.24.4 atomicity contract.
                if stored is not None and stored[0] == eof_offset_now:
                    def _summary_empty(elapsed: float) -> str:
                        return (
                            f"  Catalog: rebuild triggered by {trigger} — "
                            f"empty delta → mtime-only marker advance "
                            f"in {elapsed:.1f}s"
                        )
                    with _rebuild_heartbeat(
                        "advancing consistency marker",
                        summary_builder=_summary_empty,
                    ):
                        with self._db.transaction():
                            self._write_consistency_marker(current_mtime)
                else:
                    # Decide bootstrap / invalidated / incremental.
                    do_full = False
                    invalidation: str | None = None
                    if stored is None:
                        do_full = True
                        invalidation = "bootstrap"
                    else:
                        stored_offset, stored_hash, stored_window = stored
                        if stored_window != _HEADER_HASH_BYTES:
                            do_full = True
                            invalidation = "window-size mismatch"
                        else:
                            header_hash_now = _compute_header_hash(
                                self._events_path,
                            )
                            if stored_hash != header_hash_now:
                                do_full = True
                                invalidation = "header-hash drift"

                    if not do_full:
                        # Incremental path: replay only the bytes in
                        # [stored_offset, eof_offset_now). The bounded
                        # form is mandatory for concurrent-appender
                        # safety (Round 2 Critical #1) — without it, a
                        # writer landing between the stat() above and
                        # the iterator's read window would extend the
                        # iterator past eof_offset_now, the marker we
                        # then persist (eof_offset_now, the pre-append
                        # snapshot) would be stale below the actual
                        # applied-event tail, and the empty-delta fast
                        # path would never settle for that range.
                        stored_offset, stored_hash, stored_window = stored
                        delta_events = list(
                            EventLog(self._dir).replay_from(
                                stored_offset,
                                limit_offset=eof_offset_now,
                            )
                        )
                        if not delta_events and stored_offset < eof_offset_now:
                            # Round 3 Significant #2: zero events from a
                            # non-empty delta range is the corruption
                            # signal. Escalate to full rebuild WITHOUT
                            # advancing the marker so the recovery is
                            # idempotent under retry.
                            do_full = True
                            invalidation = (
                                "incremental corruption "
                                "(zero events from non-empty delta)"
                            )
                        else:
                            replayed_count = len(delta_events)

                            def _summary_incremental(elapsed: float) -> str:
                                docs, links = self._projection_counts()
                                return (
                                    f"  Catalog: rebuild triggered by "
                                    f"{trigger} — replayed "
                                    f"{replayed_count:,} events "
                                    f"incrementally → {docs:,} docs, "
                                    f"{links:,} links in {elapsed:.1f}s"
                                )

                            with _rebuild_heartbeat(
                                "applying incremental delta",
                                summary_builder=_summary_incremental,
                            ):
                                with self._db.transaction():
                                    # commit=False mirrors the full-
                                    # rebuild path. A nested commit()
                                    # would defeat the rollback fence
                                    # and re-introduce the 4.24.4
                                    # ordering hazard (Round 3
                                    # Significant #3).
                                    self._projector.apply_all(
                                        iter(delta_events), commit=False,
                                    )
                                    self._write_consistency_marker(
                                        current_mtime,
                                    )
                                    self._write_offset_marker(
                                        offset=eof_offset_now,
                                        header_hash=stored_hash,
                                        window=stored_window,
                                    )

                    if do_full:
                        # Bootstrap, invalidated, or escalated-corruption
                        # full rebuild. The bulk_load_documents FTS5
                        # fence is preserved here because the per-event
                        # projector writes can number in the hundreds of
                        # thousands; without the fence each replayed
                        # INSERT queues per-row hash entries that SQLite
                        # cannot merge mid-transaction (15-20 min COMMIT
                        # on a hot catalog).
                        if header_hash_now is None:
                            header_hash_now = _compute_header_hash(
                                self._events_path,
                            )
                        event_count = _count_lines(self._events_path)
                        invalidation_label = invalidation

                        def _summary_full(elapsed: float) -> str:
                            docs, links = self._projection_counts()
                            qualifier = (
                                f" ({invalidation_label} → full rebuild)"
                                if invalidation_label
                                and invalidation_label != "bootstrap"
                                else ""
                            )
                            return (
                                f"  Catalog: rebuild triggered by "
                                f"{trigger} — replayed "
                                f"{event_count:,} events → {docs:,} "
                                f"docs, {links:,} links in "
                                f"{elapsed:.1f}s{qualifier}"
                            )

                        with _rebuild_heartbeat(
                            "rebuilding projection",
                            summary_builder=_summary_full,
                        ):
                            with self._db.transaction() as conn:
                                with self._db.bulk_load_documents():
                                    conn.execute("DELETE FROM links")
                                    conn.execute("DELETE FROM documents")
                                    conn.execute("DELETE FROM owners")
                                    # Step 0 (Critical #1): see the
                                    # earlier comment for the rationale
                                    # and why the COALESCE in
                                    # _v0_collection_created is
                                    # retained for the degraded-path
                                    # retry case (Round 3 Significant
                                    # #1).
                                    conn.execute("DELETE FROM collections")
                                    # commit=False — the transaction
                                    # context owns the commit boundary;
                                    # a nested commit() would defeat
                                    # the rollback fence.
                                    self._projector.apply_all(
                                        EventLog(self._dir).replay(),
                                        commit=False,
                                    )
                                # 4.24.4 atomicity contract: marker
                                # writes happen INSIDE the same
                                # transaction as the projection writes.
                                # The transaction context commits all
                                # rows atomically on success, rolls
                                # them all back together on any
                                # exception. Pre-4.24.4 the marker
                                # write lived OUTSIDE this block in
                                # _write_consistency_marker()'s own
                                # commit(), a refactoring hazard.
                                self._write_consistency_marker(current_mtime)
                                self._write_offset_marker(
                                    offset=eof_offset_now,
                                    header_hash=header_hash_now,
                                    window=_HEADER_HASH_BYTES,
                                )
            else:
                owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
                documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
                links_dict = read_links(self._links_path) if self._links_path.exists() else {}
                _log.debug("catalog_consistency_rebuild", mtime=current_mtime)
                # Pre-rebuild sizes (the bulk dicts are about to be
                # truncated and reloaded). _summary captures these by
                # closure so the post-rebuild line reports what just
                # got loaded.
                n_owners = len(owners)
                n_docs = len(documents)
                n_links = len(links_dict)

                def _summary(elapsed: float) -> str:
                    return (
                        f"  Catalog: rebuild triggered by {trigger} — "
                        f"loaded {n_owners:,} owners, {n_docs:,} docs, "
                        f"{n_links:,} links in {elapsed:.1f}s"
                    )

                with _rebuild_heartbeat(
                    "rebuilding projection", summary_builder=_summary,
                ):
                    # RDR-104 critic Critical #2 fix: pass current_mtime so
                    # the marker write happens INSIDE rebuild's transaction
                    # block, atomic with the projection writes.
                    self._db.rebuild(
                        owners, documents, list(links_dict.values()),
                        consistency_mtime=current_mtime,
                    )
            # In-memory mirror of the persisted marker. The DB write is
            # already inside the rebuild transaction (event-sourced and
            # legacy paths both); this assignment exists so subsequent
            # in-process construction short-circuits without a SELECT.
            self._last_consistency_mtime = current_mtime
            self.degraded = False
        except Exception as exc:
            _log.warning("catalog_consistency_rebuild_failed", error=str(exc), exc_info=True)
            self.degraded = True

    def _event_log_covers_legacy(self) -> bool:
        """Return True when events.jsonl plausibly covers documents.jsonl.

        Bootstrap guardrail for ``_ensure_consistent``: refuses to
        DELETE-and-rebuild from a sparse event log when the legacy
        JSONL still holds the majority of the catalog's content (e.g.
        an operator just flipped ``NEXUS_EVENT_SOURCED=1`` on a
        populated catalog and the first event-sourced write produced
        a one-event log).

        Cheap O(N) line counts on both files. ``documents.jsonl`` may
        contain duplicates (last-line-wins on rebuild) and tombstones
        (``_deleted=True`` markers) — count distinct non-tombstoned
        tumblers as the canonical row count. ``events.jsonl`` may
        contain tombstones too (DocumentDeleted events) — count
        DocumentRegistered events minus DocumentDeleted as the
        replayed-document count. A 5% slop tolerance avoids tripping
        on a single in-flight write or one-event drift.
        """
        if not self._documents_path.exists():
            return True
        try:
            registered: set[str] = set()
            tombstoned: set[str] = set()
            with self._documents_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tumbler = rec.get("tumbler")
                    if not tumbler:
                        continue
                    if rec.get("_deleted"):
                        tombstoned.add(tumbler)
                    else:
                        registered.add(tumbler)
            legacy_doc_count = len(registered - tombstoned)
            if legacy_doc_count == 0:
                return True

            from nexus.catalog import events as _ev
            # Net document registrations: DocumentRegistered − DocumentDeleted.
            # Can go negative (a dedupe-only event stream against a
            # legacy catalog produces only DocumentDeleted), which the
            # ``>= threshold`` check below relies on to fall through to
            # legacy. RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8):
            # renamed from ``event_doc_count`` to make the negative
            # values intentional rather than surprising.
            net_registered = 0
            with self._events_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = obj.get("type")
                    if t == _ev.TYPE_DOCUMENT_REGISTERED:
                        net_registered += 1
                    elif t == _ev.TYPE_DOCUMENT_DELETED:
                        net_registered -= 1

            # RDR-101 Phase 3 follow-up B (nexus-o6aa.9.7): floor the
            # threshold at 1. ``int(1 * 0.95) == 0`` and ``0 >= 0`` is
            # True, so a 1-document legacy catalog with a non-empty-but-
            # ``DocumentRegistered``-free ``events.jsonl`` (e.g. a
            # ChunkIndexed-only log from a partial Phase 2 synthesis,
            # or a dedupe-only event stream that pushes
            # ``event_doc_count`` to 0) used to bypass the guardrail
            # and silently wipe the single legacy row. The floor
            # guarantees a real DocumentRegistered must exist in the
            # log before ES rebuild is allowed at the smallest catalog
            # sizes.
            threshold = max(1, int(legacy_doc_count * 0.95))
            return net_registered >= threshold
        except Exception:
            # On any unexpected failure, refuse the event-sourced
            # rebuild (safer to fall through to legacy than to wipe).
            return False

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
        row = self._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE repo_hash = ?", (repo_hash,)
        ).fetchone()
        return Tumbler.parse(row[0]) if row else None

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        """Return tumblers of all owners with this name.

        UNIQUE constraint is ``(name, owner_type)`` per nexus-7vuw, so
        a single name can map to multiple owners across types (e.g.
        a repo and a curator both named ``nexus``). Callers that need
        a unique answer should disambiguate on the returned list
        (typical CLI flow: error when ``len(...) > 1`` and surface
        the candidates).

        Returns ``[]`` if no owner has this name. Used by the
        ``--owner`` CLI flags on ``nx catalog list`` (and friends)
        to resolve operator-typed names to tumblers without leaking
        the ``Tumbler.parse → int()`` ``ValueError`` (#537,
        nexus-1lx7).
        """
        rows = self._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE name = ? "
            "ORDER BY tumbler_prefix",
            (name,),
        ).fetchall()
        return [Tumbler.parse(r[0]) for r in rows]

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
        """Return the document entry for ``tumbler``.

        With ``follow_alias=True`` (default), transparently dereferences
        ``alias_of`` — external callers get the canonical entry even
        when they asked by an old tumbler. Pass ``follow_alias=False`` to
        see the raw entry (needed by dedupe tooling to inspect the alias
        graph itself).
        """
        target = self.resolve_alias(tumbler) if follow_alias else tumbler
        row = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri "
            "FROM documents WHERE tumbler = ?",
            (str(target),),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
            source_mtime=row[12] or 0.0,
            alias_of=row[13] or "",
            source_uri=row[14] or "",
        )

    def list_by_collection(
        self, physical_collection: str, *, limit: int | None = None,
    ) -> list[CatalogEntry]:
        """Return every document entry whose ``physical_collection``
        matches.

        One entry per source document (NOT per chunk) — what callers
        like ``nx enrich aspects`` need to drive a per-document
        operation. Ordered by ``tumbler ASC`` for deterministic
        iteration. ``limit=None`` returns every match.

        Reads the SQLite cache without acquiring the JSONL-truth
        flock — consistent with ``resolve``, ``find``, and
        ``by_file_path``. Callers driving downstream writes (e.g.
        ``nx enrich aspects``) should treat the result as a
        best-effort sweep; a document registered concurrently may
        be missed and can be picked up by a subsequent run or by
        ``--re-extract`` re-sweeps.
        """
        sql = (
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri "
            "FROM documents WHERE physical_collection = ? "
            "ORDER BY tumbler ASC"
        )
        params: tuple = (physical_collection,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (physical_collection, limit)
        rows = self._db.execute(sql, params).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(row[0]),
                title=row[1],
                author=row[2],
                year=row[3],
                content_type=row[4],
                file_path=row[5],
                corpus=row[6],
                physical_collection=row[7],
                chunk_count=row[8],
                head_hash=row[9],
                indexed_at=row[10],
                meta=json.loads(row[11]) if row[11] else {},
                source_mtime=row[12] or 0.0,
                alias_of=row[13] or "",
                source_uri=row[14] or "",
            )
            for row in rows
        ]

    # ── RDR-101 Phase 6: Collections projection (nexus-o6aa.14) ─────────

    _COLLECTION_COLUMNS = (
        "name",
        "content_type",
        "owner_id",
        "embedding_model",
        "model_version",
        "display_name",
        "legacy_grandfathered",
        "superseded_by",
        "superseded_at",
        "created_at",
    )

    def _row_to_collection_dict(self, row: tuple) -> dict:
        d = dict(zip(self._COLLECTION_COLUMNS, row))
        d["legacy_grandfathered"] = bool(d.get("legacy_grandfathered") or 0)
        return d

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
        """Return every row in the ``collections`` projection, ordered by name."""
        sql = (
            "SELECT " + ", ".join(self._COLLECTION_COLUMNS) + " "
            "FROM collections ORDER BY name"
        )
        rows = self._db.execute(sql).fetchall()
        return [self._row_to_collection_dict(r) for r in rows]

    def get_collection(self, name: str) -> dict | None:
        sql = (
            "SELECT " + ", ".join(self._COLLECTION_COLUMNS) + " "
            "FROM collections WHERE name = ?"
        )
        row = self._db.execute(sql, (name,)).fetchone()
        return self._row_to_collection_dict(row) if row else None

    def is_legacy_collection(self, name: str) -> bool:
        """Return True if ``name`` is registered AND flagged legacy.

        Unknown names return False (read paths are operationally hostile
        to fail-loud per RDR-101 §"Phase 6"). Callers wanting strict
        membership should query :meth:`get_collection` and check for None.
        """
        row = self._db.execute(
            "SELECT legacy_grandfathered FROM collections WHERE name = ?",
            (name,),
        ).fetchone()
        return bool(row and row[0])

    def collection_for(
        self,
        content_type: str,
        owner: Tumbler | str,
        embedding_model: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        """Resolve the canonical ``CollectionName`` for a tuple.

        RDR-103 Phase 2. The catalog is the authority for collection
        naming: callers describe the tuple they want, the catalog renders
        the physical name. Validation is strict at the public boundary
        (per pinned decision #4): ``content_type`` must be in
        :data:`nexus.corpus.CONTENT_TYPES`, ``embedding_model`` must be in
        :data:`nexus.corpus.CANONICAL_EMBEDDING_MODELS`, and the derived
        owner segment must be non-empty.

        Version handling:

        - New tuple ``(c, o, m)`` returns ``v1``.
        - Existing tuple at ``vN`` returns ``vN`` (idempotent).
        - With ``bump=True``, an existing ``vN`` returns ``vN+1``; a
          new tuple still returns ``v1`` (bump only fires when prior
          versions exist).

        Pinned decision #2: a new ``embedding_model`` is NOT a version
        bump. ``(c, o, m_new)`` is a different tuple from ``(c, o, m_old)``
        and naturally lands in ``v1``. The operator runs
        ``nx catalog supersede-collection`` to retire the old tuple.

        Grandfathered legacy rows do NOT contribute to the version
        lookup: their canonical fields are typically empty strings, and
        the WHERE clause filters them out via ``legacy_grandfathered = 0``
        belt-and-suspenders. Pinned decision #1.

        This method does NOT register the returned name in the catalog
        projection. Callers must follow up with
        :meth:`register_collection` once they have actually created (or
        otherwise materialised) the T3 collection. The indexer's
        ``_catalog_hook_repo`` already pairs creation with registration;
        Phase 3 wires that pattern through every indexer call site.

        The returned ``CollectionName`` is constructed directly rather
        than round-tripped through ``CollectionName.parse(render(...))``;
        the fields are validated above against the same closed sets,
        making the round-trip redundant.
        """
        if content_type not in CONTENT_TYPES:
            raise ValueError(
                f"collection_for: unknown content_type {content_type!r}; "
                f"expected one of {CONTENT_TYPES}"
            )
        if embedding_model not in CANONICAL_EMBEDDING_MODELS:
            raise ValueError(
                f"collection_for: non-canonical embedding_model "
                f"{embedding_model!r}; expected one of "
                f"{sorted(CANONICAL_EMBEDDING_MODELS)}"
            )
        owner_id = owner_segment_for_tumbler(owner)
        if not owner_id:
            raise ValueError(
                f"collection_for: cannot derive owner_id segment from "
                f"owner {owner!r}"
            )
        # The compound index ``idx_collections_tuple`` covers this lookup.
        # ``model_version`` is stored as TEXT (``v1``..``vN``). SUBSTR
        # strips the ``v`` prefix so SQLite can CAST the digit string to
        # INTEGER; ``CAST('v3' AS INTEGER)`` returns 0 because SQLite
        # cannot parse a leading non-digit. The INTEGER cast is what
        # gives MAX integer ordering rather than lexical (otherwise
        # ``v10`` would sort before ``v9``).
        row = self._db.execute(
            "SELECT MAX(CAST(SUBSTR(model_version, 2) AS INTEGER)) "
            "FROM collections "
            "WHERE content_type = ? AND owner_id = ? "
            "AND embedding_model = ? AND legacy_grandfathered = 0",
            (content_type, owner_id, embedding_model),
        ).fetchone()
        existing_version = int(row[0]) if row and row[0] is not None else 0
        if existing_version == 0:
            new_version = 1
        elif bump:
            new_version = existing_version + 1
        else:
            new_version = existing_version
        return CollectionName(
            content_type=content_type,
            owner_id=owner_id,
            embedding_model=embedding_model,
            model_version=new_version,
        )

    def collection_for_repo(
        self,
        repo: Path,
        content_type: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        """Resolve the canonical ``CollectionName`` for ``content_type`` in ``repo``.

        Convenience wrapper around :meth:`collection_for` that handles
        the repo-to-owner-to-collection-name pipeline:

        1. Compute ``repo_hash`` via
           :func:`nexus.registry._repo_identity`.
        2. Look up the owner via :meth:`owner_for_repo`. Raises
           ``LookupError`` when no owner exists; the indexer's
           ``_catalog_hook`` flow registers the owner up front, so a
           missing owner indicates a bypass of the standard write path.
        3. Resolve the canonical embedding model via
           :func:`nexus.corpus.canonical_embedding_model`.
        4. Delegate to :meth:`collection_for`.

        This is the helper that Phase 3 indexer call sites use. The
        pre-RDR-103 ``_docs_collection_name(repo)`` family that this
        replaced was removed in Phase 5.
        """
        from nexus.registry import _repo_identity  # noqa: PLC0415

        _, repo_hash = _repo_identity(repo)
        owner = self.owner_for_repo(repo_hash)
        if owner is None:
            raise LookupError(
                f"collection_for_repo: no owner registered for "
                f"repo_hash {repo_hash!r} (repo {repo!s}). "
                f"Call register_owner(...) before requesting a "
                f"collection name; the indexer's _catalog_hook normally "
                f"registers owners up front."
            )
        return self.collection_for(
            content_type=content_type,
            owner=owner,
            embedding_model=canonical_embedding_model(content_type),
            bump=bump,
        )

    def _update_document_collection_locked(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        """Read+validate+write the per-row re-point WITHOUT acquiring
        the flock or committing SQLite. Caller is responsible for both.

        Returns True on a write, False on the not-found or
        same-target idempotency short-circuits. Used by both the
        single-row :meth:`update_document_collection` (one acquire,
        one commit per call) and the batch
        :meth:`update_documents_collection_batch` (one acquire, one
        commit per N calls).
        """
        from nexus.catalog.synthesizer import _owner_prefix_of  # noqa: PLC0415

        row = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, source_uri, alias_of "
            "FROM documents WHERE tumbler = ?",
            (tumbler,),
        ).fetchone()
        if row is None:
            return False
        if (row[7] or "") == new_collection:
            return False

        meta_dict = json.loads(row[11]) if row[11] else {}
        event = _make_event(
            _DocumentRegisteredPayload(
                doc_id=row[0],
                owner_id=_owner_prefix_of(row[0]),
                content_type=row[4] or "",
                source_uri=row[13] or "",
                coll_id=new_collection,
                title=row[1] or "",
                source_mtime=float(row[12] or 0.0),
                indexed_at_doc=row[10] or "",
                tumbler=row[0],
                author=row[2] or "",
                year=int(row[3] or 0),
                file_path=row[5] or "",
                corpus=row[6] or "",
                physical_collection=new_collection,
                chunk_count=int(row[8] or 0),
                head_hash=row[9] or "",
                indexed_at=row[10] or "",
                alias_of=row[14] or "",
                meta=dict(meta_dict),
            ),
            v=0,
        )
        rec = {
            "tumbler": row[0],
            "title": row[1],
            "author": row[2],
            "year": row[3],
            "content_type": row[4],
            "file_path": row[5],
            "corpus": row[6],
            "physical_collection": new_collection,
            "chunk_count": row[8],
            "head_hash": row[9] or "",
            "indexed_at": row[10] or "",
            "meta": meta_dict,
            "source_mtime": row[12] or 0.0,
            "source_uri": row[13] or "",
            "alias_of": row[14] or "",
        }
        if self._event_sourced_enabled:
            self._write_to_event_log(event)
            self._projector.apply(event)
            self._append_jsonl(self._documents_path, rec)
        else:
            self._append_jsonl(self._documents_path, rec)
            self._db.execute(
                "UPDATE documents SET physical_collection = ? "
                "WHERE tumbler = ?",
                (new_collection, row[0]),
            )
            self._emit_shadow_event(event)
        return True

    def update_document_collection(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        """Re-point a single document's ``physical_collection``.

        Per-row analog of :meth:`rename_collection` for migrations
        where each document gets a different target (e.g. RDR-101
        Phase 6 ``nx catalog migrate-fallback``). Emits
        DocumentRegistered v: 0 with the new ``physical_collection``;
        the projector's INSERT OR REPLACE updates the SQLite row.

        Returns True if the doc was re-pointed, False if not found or
        already pointed at ``new_collection`` (idempotent).

        nexus-qpet.2: read + validate + construct payload all inside
        the lock so two concurrent re-points of the same tumbler
        resolve to a deterministic last-write-wins on ONE writer.

        Crash-window discipline (event-sourced mode) matches
        rename_collection: event -> projector apply -> JSONL append,
        with the SQLite commit last. A crash between projector apply
        and JSONL append leaves SQLite uncommitted and JSONL unwritten
        (both old). A crash between JSONL append and commit leaves
        JSONL ahead of SQLite; on rebuild-from-JSONL the new line
        wins; on rebuild-from-events the projector replays correctly.
        """
        dir_fd = self._acquire_lock()
        try:
            wrote = self._update_document_collection_locked(
                tumbler, new_collection,
            )
            if wrote:
                self._db.commit()
            return wrote
        finally:
            self._release_lock(dir_fd)

    def update_documents_collection_batch(
        self, pairs: list[tuple[str, str]],
    ) -> int:
        """Re-point N documents' ``physical_collection`` in one
        flock + one SQLite commit (nexus-qpet.3).

        Each *pair* is ``(tumbler, new_collection)``. Returns the
        count of documents actually re-pointed (no-ops via not-found
        or same-target idempotency are excluded).

        Used by ``nx catalog migrate-fallback`` for the per-document
        re-point loop. Single-row callers should still use
        :meth:`update_document_collection` (which uses this method's
        helper internally so semantics match).
        """
        if not pairs:
            return 0
        dir_fd = self._acquire_lock()
        wrote_any = False
        updated = 0
        try:
            for tumbler, new_collection in pairs:
                if self._update_document_collection_locked(
                    tumbler, new_collection,
                ):
                    updated += 1
                    wrote_any = True
            if wrote_any:
                self._db.commit()
            return updated
        finally:
            self._release_lock(dir_fd)

    def supersede_collection(
        self,
        old_name: str,
        new_name: str,
        *,
        reason: str = "",
    ) -> None:
        """Mark ``old_name`` as superseded by ``new_name``.

        Writes a CollectionSuperseded v: 0 event and updates the old
        collection's ``superseded_by`` / ``superseded_at`` columns. The
        new collection MUST already be registered (the docstring used
        to say "callers usually pair register_collection with
        supersede_collection"; that contract is now enforced).

        Raises ``ValueError`` when:
          - ``old_name`` is not registered (typo-on-explicit-action path)
          - ``new_name`` is not registered (would create a dangling
            ``superseded_by`` pointer that no foreign-key-style join
            can resolve)
          - ``old_name`` is already superseded (silently overwriting
            the previous supersession would orphan the prior
            CollectionSuperseded event in the log)

        Honors the ``_event_sourced_enabled`` split that the rest of the
        catalog writers use.
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        ts = datetime.now(UTC).isoformat()
        event = _make_event(
            _CollectionSupersededPayload(
                old_coll_id=old_name,
                new_coll_id=new_name,
                reason=reason,
                superseded_at=ts,
            ),
            v=0,
            ts=ts,
        )
        dir_fd = self._acquire_lock()
        try:
            # nexus-qpet.2: re-validate inside the locked block. Two
            # concurrent supersedes of the same old_name now produce
            # one success + one ValueError rather than a silent
            # last-write-wins (the in-process projection was
            # last-writer determined; replay was order-deterministic
            # but operator-confusing).
            existing = self.get_collection(old_name)
            if existing is None:
                raise ValueError(
                    f"supersede_collection: {old_name!r} not registered"
                )
            if existing.get("superseded_by"):
                raise ValueError(
                    f"supersede_collection: {old_name!r} is already "
                    f"superseded by {existing['superseded_by']!r}; "
                    f"refusing to chain a second supersede event"
                )
            if self.get_collection(new_name) is None:
                raise ValueError(
                    f"supersede_collection: new {new_name!r} is not "
                    f"registered. Call register_collection({new_name!r}, ...) "
                    f"first so the projection has a row to point at."
                )
            if self._event_sourced_enabled:
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
            else:
                # Legacy mode: SQLite is canonical, no JSONL backing.
                # Reuse the same ``ts`` as the event payload so the row
                # records exactly what the event records (deterministic
                # under replay-equality even in legacy mode).
                self._db.execute(
                    "UPDATE collections SET superseded_by = ?, "
                    "superseded_at = ? WHERE name = ?",
                    (new_name, ts, old_name),
                )
                self._db.commit()
                self._emit_shadow_event(event)
        finally:
            self._release_lock(dir_fd)

    def resolve_alias(self, tumbler: Tumbler, *, max_hops: int = 16) -> Tumbler:
        """Walk the alias chain to its canonical terminus.

        Returns ``tumbler`` itself when no alias is set (the common case
        and the pre-nexus-s8yz behaviour). Walks at most ``max_hops``
        links and bails on cycles — a broken chain is treated as
        terminating at the last-seen tumbler rather than raising, so
        reads stay available even in a pathological catalog.
        """
        seen: set[str] = set()
        current = str(tumbler)
        for _ in range(max_hops):
            if current in seen:
                _log.warning("catalog.alias_cycle", tumbler=str(tumbler), seen=sorted(seen))
                break
            seen.add(current)
            row = self._db.execute(
                "SELECT alias_of FROM documents WHERE tumbler = ?",
                (current,),
            ).fetchone()
            if not row:
                # Dangling alias — return the last valid hop. Callers that
                # need to detect this can compare to the input tumbler.
                break
            target = (row[0] or "").strip()
            if not target:
                # Canonical — this is the terminus.
                return Tumbler.parse(current)
            current = target
        return Tumbler.parse(current)

    def set_alias(self, tumbler: Tumbler, canonical: Tumbler) -> None:
        """Mark ``tumbler`` as an alias for ``canonical``.

        Intended for ``nx catalog dedupe-owners`` (nexus-tmbh). The
        aliased row stays in the catalog so external references continue
        to resolve. Refuses to create a self-alias (which would be a
        1-cycle). A pre-existing alias is overwritten — callers that
        need to preserve the old pointer should snapshot it first.

        No-op if ``tumbler`` is not a known document. JSONL truth is
        updated by appending a new document record with the alias
        populated so subsequent JSONL-driven rebuilds preserve the
        pointer (last-line-wins).
        """
        if str(tumbler) == str(canonical):
            raise ValueError(f"self-alias rejected: {tumbler} → {canonical}")
        # Acquire the catalog directory flock so the JSONL append and
        # the shadow-event emit (which both have a "caller holds the
        # flock" contract) cannot race a concurrent writer. Pre-PR-F
        # this method was unlocked because it was JSONL+SQLite-only;
        # adding the shadow emit made the lock load-bearing.
        dir_fd = self._acquire_lock()
        try:
            # Read current row (by raw tumbler — do not follow alias, we want
            # to update THIS row specifically).
            raw = self.resolve(tumbler, follow_alias=False)
            if raw is None:
                return
            updated = DocumentRecord(
                tumbler=str(tumbler),
                title=raw.title,
                author=raw.author,
                year=raw.year,
                content_type=raw.content_type,
                file_path=raw.file_path,
                corpus=raw.corpus,
                physical_collection=raw.physical_collection,
                chunk_count=raw.chunk_count,
                head_hash=raw.head_hash,
                indexed_at=raw.indexed_at,
                meta=raw.meta,
                source_mtime=raw.source_mtime,
                alias_of=str(canonical),
            )
            event = _make_event(
                _DocumentAliasedPayload(
                    alias_doc_id=str(tumbler),
                    canonical_doc_id=str(canonical),
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._documents_path, updated.__dict__)
            else:
                self._db.execute(
                    "UPDATE documents SET alias_of = ? WHERE tumbler = ?",
                    (str(canonical), str(tumbler)),
                )
                self._db.commit()
                # Append updated JSONL record so a future rebuild sees the alias.
                self._append_jsonl(self._documents_path, updated.__dict__)
                self._emit_shadow_event(event)
        finally:
            self._release_lock(dir_fd)

    def resolve_path(self, tumbler: Tumbler) -> Path | None:
        """Return absolute path for the document's file_path.

        Resolution order:
        1. Look up entry via self.resolve(tumbler)
        2. If entry not found: return None
        3. Find owner: tumbler.owner_address() -> str, look up in JSONL
        4. If owner not found or owner.owner_type == "curator": return None
        5. If entry.file_path is already absolute: return Path(entry.file_path)
        6. If owner.repo_root is non-empty: return Path(owner.repo_root) / entry.file_path
        7. Fallback: iterate registry to find path matching owner.repo_hash
        8. If fallback found: return Path(repo_path) / entry.file_path
        9. Otherwise: return None
        """
        import hashlib

        from nexus.registry import RepoRegistry

        entry = self.resolve(tumbler)
        if not entry:
            return None

        # Find owner via SQLite (avoids re-reading JSONL on every call)
        owner_prefix = str(tumbler.owner_address())
        row = self._db.execute(
            "SELECT owner_type, repo_root, repo_hash FROM owners WHERE tumbler_prefix = ?",
            (owner_prefix,),
        ).fetchone()
        if not row:
            return None
        owner_type, repo_root, repo_hash = row[0], row[1], row[2]

        # Curators (PDFs, standalone docs) are not resolvable
        if owner_type == "curator":
            return None

        # If file_path is already absolute, return it directly
        fp = Path(entry.file_path)
        if fp.is_absolute():
            return fp

        # Primary: use repo_root from owner
        if repo_root:
            return Path(repo_root) / entry.file_path

        # Fallback: find repo_root from registry by matching repo_hash
        if repo_hash:
            registry_path = _default_registry_path()
            if registry_path.exists():
                reg = RepoRegistry(registry_path)
                for path_str in reg.all_info():
                    path_hash = hashlib.sha256(path_str.encode()).hexdigest()[:8]
                    if path_hash == repo_hash:
                        return Path(path_str) / entry.file_path

        return None

    def descendants(self, prefix: str) -> list[dict]:
        """All documents whose tumbler starts with *prefix* (any depth).

        Unlike ``by_owner`` which returns only direct children, this returns
        the full subtree.  The prefix itself is excluded.
        """
        return self._db.descendants(prefix)

    def resolve_chunk(self, tumbler: Tumbler) -> dict | None:
        """Resolve a 4-segment chunk tumbler to its document + chunk metadata.

        Chunks are implicit addresses — the catalog tracks document-level entries
        only; chunk sub-addresses are resolved on demand from the document's
        ``chunk_count``.  Resolution parses the document prefix, verifies the
        document exists, and checks the chunk index is in range.

        Returns ``{"document_tumbler", "chunk_index", "physical_collection", ...}``
        or None if the tumbler is not a chunk address or the document/chunk is
        missing.
        """
        if tumbler.chunk is None:
            return None
        doc_tumbler = tumbler.document_address()
        entry = self.resolve(doc_tumbler)
        if entry is None:
            return None
        chunk_idx = tumbler.chunk
        # chunk_count of 0 or None means count is not yet known — skip bounds check
        if entry.chunk_count and chunk_idx >= entry.chunk_count:
            return None
        return {
            "document_tumbler": str(doc_tumbler),
            "chunk_index": chunk_idx,
            "physical_collection": entry.physical_collection,
            "title": entry.title,
            "content_type": entry.content_type,
        }

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
        dir_fd = self._acquire_lock()
        try:
            entry = self.resolve(tumbler)
            if entry is None:
                raise KeyError(f"no document with tumbler {tumbler}")
            # Build updated record
            rec_dict = {
                "tumbler": str(entry.tumbler),
                "title": entry.title,
                "author": entry.author,
                "year": entry.year,
                "content_type": entry.content_type,
                "file_path": entry.file_path,
                "corpus": entry.corpus,
                "physical_collection": entry.physical_collection,
                "chunk_count": entry.chunk_count,
                "head_hash": entry.head_hash,
                "indexed_at": entry.indexed_at,
                # nexus-ga48: coerce ``None`` → ``{}`` at the source so
                # the downstream merge (line ~1830), event payload
                # (~1874), and SQL serialisation (~1909) all see a
                # dict shape. Pre-fix, a row whose SQLite ``metadata``
                # column held the literal ``'null'`` string decoded
                # back through resolve() as Python ``None``, which
                # then crashed in ``dict(None)`` at the merge or
                # event-payload sites — silently blocking any
                # ``update()`` on the 11 affected rows in Hal's
                # catalog. The boundary serialisation at line 1909
                # also gets ``or {}`` defence-in-depth.
                "meta": entry.meta or {},
                "source_mtime": entry.source_mtime,
                # RDR-096 P3.1: preserve source_uri across updates.
                # Without this carry-over, every update() call would
                # silently clobber source_uri with the column default,
                # erasing the URI persisted at register time.
                "source_uri": entry.source_uri,
                # Round-4 review (reviewer D): carry alias_of into
                # rec_dict so a caller passing ``update(t, alias_of="X")``
                # threads through both the event payload and the legacy
                # SQL VALUES list. Pre-fix both paths read from
                # ``entry.alias_of`` directly, silently dropping the
                # caller-supplied value.
                "alias_of": entry.alias_of or "",
            }
            # Merge meta dict rather than replace
            if "meta" in fields and isinstance(fields["meta"], dict):
                merged_meta = dict(rec_dict["meta"])
                merged_meta.update(fields["meta"])
                fields = dict(fields, meta=merged_meta)
            rec_dict.update(fields)
            # nexus-3e4s C1: always validate the final ``source_uri``,
            # not just when the caller passes it explicitly. Pre-fix
            # this block was gated on ``"source_uri" in fields`` and
            # the production hot path (catalog hook calls update() with
            # head_hash + physical_collection but no source_uri) never
            # exercised the guard. Re-derive only when source_uri or
            # file_path is being mutated; otherwise carry the existing
            # source_uri through but still run the guard so any
            # in-place row whose URI drifted out of the owner's tree
            # cannot be silently extended.
            owner_addr = entry.tumbler.owner_address()
            owner_repo_root = self._owner_repo_root(owner_addr)
            if "source_uri" in fields or "file_path" in fields:
                rec_dict["source_uri"] = _normalize_source_uri(
                    rec_dict["source_uri"], rec_dict.get("file_path", ""),
                    repo_root=owner_repo_root,
                )
            self._check_source_uri_in_repo_root(
                owner_addr, rec_dict["source_uri"],
            )
            event = _make_event(
                _DocumentRegisteredPayload(
                    doc_id=rec_dict["tumbler"],
                    owner_id=str(entry.tumbler.owner_address()),
                    content_type=rec_dict["content_type"],
                    source_uri=rec_dict.get("source_uri", ""),
                    coll_id=rec_dict["physical_collection"],
                    title=rec_dict["title"],
                    source_mtime=float(rec_dict.get("source_mtime", 0.0) or 0.0),
                    indexed_at_doc=rec_dict["indexed_at"],
                    tumbler=rec_dict["tumbler"],
                    author=rec_dict["author"],
                    year=int(rec_dict["year"] or 0),
                    file_path=rec_dict["file_path"],
                    corpus=rec_dict["corpus"],
                    physical_collection=rec_dict["physical_collection"],
                    chunk_count=int(rec_dict["chunk_count"] or 0),
                    head_hash=rec_dict["head_hash"],
                    indexed_at=rec_dict["indexed_at"],
                    alias_of=rec_dict["alias_of"],
                    meta=dict(rec_dict["meta"]),
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                # Phase 3 PR β — event-sourced update path. update() is
                # overloaded (source_uri rename, bib enrichment, etc.);
                # the lossless DocumentRegistered-with-post-update-state
                # captures everything via the projector's INSERT OR
                # REPLACE. Future Phase 3+ work may introduce
                # fine-grained DocumentRenamed/DocumentEnriched events
                # that capture intent rather than state.
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._documents_path, rec_dict)
            else:
                self._append_jsonl(self._documents_path, rec_dict)
                # Upsert SQLite. ``alias_of`` is included in the column
                # list because INSERT OR REPLACE on the tumbler PK
                # deletes the prior row before inserting; omitting the
                # column would let the new row carry the column default
                # (NULL) instead of the prior alias pointer, silently
                # severing the alias graph on every update().
                self._db.execute(
                    "INSERT OR REPLACE INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                    "metadata, source_mtime, source_uri, alias_of) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec_dict["tumbler"], rec_dict["title"], rec_dict["author"],
                        rec_dict["year"], rec_dict["content_type"], rec_dict["file_path"],
                        rec_dict["corpus"], rec_dict["physical_collection"],
                        rec_dict["chunk_count"], rec_dict["head_hash"],
                        rec_dict["indexed_at"],
                        json.dumps(rec_dict["meta"] or {}),
                        rec_dict.get("source_mtime", 0.0),
                        rec_dict.get("source_uri", ""),
                        rec_dict["alias_of"],
                    ),
                )
                self._db.commit()
                self._emit_shadow_event(event)
        finally:
            self._release_lock(dir_fd)

    def rename_collection(self, old: str, new: str) -> int:
        """Re-point every document from ``physical_collection=old`` → ``new``.

        nexus-1ccq: `nx collection rename` cascade. JSONL is the source
        of truth, so for every matching row we append a new record with
        the updated ``physical_collection`` and also update the SQLite
        cache (one UPDATE, no per-row upsert). Rebuild-from-JSONL sees
        the later record and wins — append-only semantics preserved.
        Returns count renamed.
        """
        dir_fd = self._acquire_lock()
        try:
            # Include ``alias_of`` in the SELECT so the rename's shadow
            # emit can preserve it for any aliased document being
            # renamed. Pre-fix the SELECT omitted alias_of and the emit
            # hardcoded it to "", silently severing the alias graph
            # for any renamed alias row on replay.
            rows = self._db.execute(
                "SELECT tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                "metadata, source_mtime, source_uri, alias_of "
                "FROM documents WHERE physical_collection = ?",
                (old,),
            ).fetchall()
            from nexus.catalog.synthesizer import _owner_prefix_of as _opo
            for row in rows:
                # Preserve source_mtime + source_uri + alias_of across
                # the rename — JSONL is the rebuild source of truth, so
                # any column omitted here is reset to its default when
                # Catalog.rebuild() replays the log (review finding —
                # Reviewer B/C1, nexus-1ccq follow-up; RDR-096 P3.1
                # extended this to source_uri; meta-review extended it
                # to alias_of).
                rec = {
                    "tumbler": row[0],
                    "title": row[1],
                    "author": row[2],
                    "year": row[3],
                    "content_type": row[4],
                    "file_path": row[5],
                    "corpus": row[6],
                    "physical_collection": new,
                    "chunk_count": row[8],
                    "head_hash": row[9] or "",
                    "indexed_at": row[10] or "",
                    "meta": json.loads(row[11]) if row[11] else {},
                    "source_mtime": row[12] or 0.0,
                    "source_uri": row[13] or "",
                    "alias_of": row[14] or "",
                }
                if self._event_sourced_enabled:
                    # Per-row event-source: write event, project to
                    # SQLite, append legacy JSONL. SQLite commit is
                    # batched at the end for efficiency.
                    meta_dict = json.loads(row[11]) if row[11] else {}
                    event = _make_event(
                        _DocumentRegisteredPayload(
                            doc_id=row[0],
                            owner_id=_opo(row[0]),
                            content_type=row[4] or "",
                            source_uri=row[13] or "",
                            coll_id=new,
                            title=row[1] or "",
                            source_mtime=float(row[12] or 0.0),
                            indexed_at_doc=row[10] or "",
                            tumbler=row[0],
                            author=row[2] or "",
                            year=int(row[3] or 0),
                            file_path=row[5] or "",
                            corpus=row[6] or "",
                            physical_collection=new,
                            chunk_count=int(row[8] or 0),
                            head_hash=row[9] or "",
                            indexed_at=row[10] or "",
                            alias_of=row[14] or "",
                            meta=dict(meta_dict),
                        ),
                        v=0,
                    )
                    self._write_to_event_log(event)
                    self._projector.apply(event)
                    self._append_jsonl(self._documents_path, rec)
                else:
                    self._append_jsonl(self._documents_path, rec)
            if not self._event_sourced_enabled:
                self._db.execute(
                    "UPDATE documents SET physical_collection = ? "
                    "WHERE physical_collection = ?",
                    (new, old),
                )
            self._db.commit()
            # Shadow-emit one DocumentRegistered per renamed row with
            # the new physical_collection. The projector's INSERT OR
            # REPLACE makes the replay state converge on the new
            # collection name. Pre-fix this method emitted nothing,
            # so a replayed events.jsonl produced rows with the OLD
            # physical_collection, breaking the doctor's replay-equality
            # check. Emitting after db.commit() (same crash-window
            # discipline as unlink/bulk_unlink) keeps the event log
            # consistent with the durable SQLite state.
            #
            # Hoist the gate check above the per-row payload
            # construction: when shadow emit is OFF (the default), a
            # 10k-row rename should not pay the cost of building 10k
            # _DocumentRegisteredPayload objects only to discard them
            # in _emit_shadow_event's first line.
            # When event-sourced is ON the per-row write loop above
            # already emitted + projected each event; skip the
            # shadow-emit loop to avoid duplicate writes.
            if self._shadow_emit_enabled and not self._event_sourced_enabled:
                from nexus.catalog.synthesizer import _owner_prefix_of
                for row in rows:
                    meta_dict = json.loads(row[11]) if row[11] else {}
                    self._emit_shadow_event(_make_event(
                        _DocumentRegisteredPayload(
                            doc_id=row[0],
                            # Use the synthesizer's helper for owner
                            # extraction so malformed tumblers (no dots)
                            # produce "" rather than the whole tumbler
                            # — matches synthesize_from_jsonl's contract.
                            owner_id=_owner_prefix_of(row[0]),
                            content_type=row[4] or "",
                            source_uri=row[13] or "",
                            coll_id=new,
                            title=row[1] or "",
                            source_mtime=float(row[12] or 0.0),
                            indexed_at_doc=row[10] or "",
                            tumbler=row[0],
                            author=row[2] or "",
                            year=int(row[3] or 0),
                            file_path=row[5] or "",
                            corpus=row[6] or "",
                            physical_collection=new,
                            chunk_count=int(row[8] or 0),
                            head_hash=row[9] or "",
                            indexed_at=row[10] or "",
                            alias_of=row[14] or "",
                            meta=dict(meta_dict),
                        ),
                        v=0,
                    ))
            return len(rows)
        finally:
            self._release_lock(dir_fd)

    def delete_document(self, tumbler: Tumbler) -> bool:
        """Soft-delete a document: tombstone in JSONL, DELETE from SQLite.

        Links to/from this tumbler are preserved (RF-9: orphaned links intentional).
        Returns True if deleted, False if not found.
        """
        dir_fd = self._acquire_lock()
        try:
            entry = self.resolve(tumbler)
            if entry is None:
                return False
            tombstone = {
                "tumbler": str(tumbler),
                "title": entry.title,
                "author": entry.author,
                "year": entry.year,
                "content_type": entry.content_type,
                "file_path": entry.file_path,
                "corpus": entry.corpus,
                "physical_collection": entry.physical_collection,
                "chunk_count": entry.chunk_count,
                "head_hash": entry.head_hash,
                "indexed_at": entry.indexed_at,
                "meta": entry.meta,
                "source_mtime": entry.source_mtime,
                "_deleted": True,
            }
            event = _make_event(
                _DocumentDeletedPayload(
                    doc_id=str(tumbler),
                    reason="catalog.delete_document",
                    tumbler=str(tumbler),
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._documents_path, tombstone)
            else:
                self._append_jsonl(self._documents_path, tombstone)
                self._db.execute(
                    "DELETE FROM documents WHERE tumbler = ?",
                    (str(tumbler),),
                )
                self._db.commit()
                self._emit_shadow_event(event)
            return True
        finally:
            self._release_lock(dir_fd)

    def find(self, query: str, *, content_type: str | None = None) -> list[CatalogEntry]:
        rows = self._db.search(query, content_type=content_type)
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r["tumbler"]),
                title=r["title"],
                author=r["author"],
                year=r["year"],
                content_type=r["content_type"],
                file_path=r["file_path"],
                corpus=r["corpus"],
                physical_collection=r["physical_collection"],
                chunk_count=r["chunk_count"],
                head_hash=r["head_hash"] or "",
                indexed_at=r["indexed_at"] or "",
                meta=json.loads(r["metadata"]) if r.get("metadata") else {},
                source_mtime=r["source_mtime"] if "source_mtime" in r.keys() else 0.0,
            )
            for r in rows
        ]

    def by_file_path(self, owner: Tumbler, file_path: str) -> CatalogEntry | None:
        row = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            f"FROM documents WHERE {self._prefix_sql(str(owner))[0]} AND file_path = ?",
            (*self._prefix_sql(str(owner))[1], file_path),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
            source_mtime=row[12] or 0.0,
            source_uri=row[13] or "",
        )

    def by_owner(self, owner: Tumbler) -> list[CatalogEntry]:
        rows = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            f"FROM documents WHERE {self._prefix_sql(str(owner))[0]}",
            self._prefix_sql(str(owner))[1],
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]),
                title=r[1],
                author=r[2],
                year=r[3],
                content_type=r[4],
                file_path=r[5],
                corpus=r[6],
                physical_collection=r[7],
                chunk_count=r[8],
                head_hash=r[9],
                indexed_at=r[10],
                meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        """List all entries with the given content type (code, paper, rdr, knowledge)."""
        rows = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents WHERE content_type = ?",
            (content_type,),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        """List all entries with the given corpus tag."""
        rows = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents WHERE corpus = ?",
            (corpus,),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def doc_count(self) -> int:
        """Return the total number of documents in the catalog."""
        row = self._db.execute("SELECT COUNT(*) FROM documents").fetchone()
        return row[0] if row else 0

    def all_documents(
        self, limit: int = 0, *, content_type: str = "",
    ) -> list[CatalogEntry]:
        """Return all catalog entries. limit=0 means unlimited.

        GH #568: ``content_type`` pushes the filter into the SQL
        ``WHERE`` clause so pagination works correctly when the
        requested content_type is small-cardinality. Pre-fix the
        CLI ``nx catalog list --type rdr`` filtered Python-side
        AFTER ``LIMIT/OFFSET`` and returned empty whenever the
        pre-LIMIT slice held no matching rows -- e.g. 15K-entry
        catalog with only 2 rdr rows: ``--type rdr -n 3`` got 0.
        Mirrors PR #533's fix for the MCP ``catalog_list`` surface.
        """
        sql = (
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents"
        )
        params: tuple = ()
        if content_type:
            sql += " WHERE content_type = ?"
            params = (content_type,)
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = self._db.execute(sql, params).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        """Look up catalog entry by T3 doc_id stored in meta.doc_id."""
        row = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents WHERE json_extract(metadata, '$.doc_id') = ?",
            (doc_id,),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
            source_mtime=row[12] or 0.0,
            source_uri=row[13] or "",
        )

    # ── Links ──────────────────────────────────────────────────────────────

    def _link_unlocked(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        from_span: str,
        to_span: str,
        meta: dict,
        *,
        allow_dangling: bool = False,
    ) -> bool:
        """Core link logic — caller must hold the lock. Returns True if new, False if merged."""
        # Validate span format (Xanadu transclusion addressing)
        for span, label in [(from_span, "from_span"), (to_span, "to_span")]:
            if not _SPAN_PATTERN.match(span):
                raise ValueError(
                    f"invalid {label}: {span!r} — use 'line_start-line_end', "
                    f"'chunk_idx:char_start-char_end', 'chash:<sha256hex>', 'chash:<start>-<end>:<sha256hex>', or '' for whole document"
                )
        if not allow_dangling:
            errors = []
            from_entry = self.resolve(from_t)
            to_entry = self.resolve(to_t)
            if from_entry is None:
                errors.append(f"from_tumbler {from_t} not found")
            if to_entry is None:
                errors.append(f"to_tumbler {to_t} not found")
            if errors:
                raise ValueError(f"dangling link: {'; '.join(errors)}")
            # Validate chash: spans resolve in their document's collection
            for span, entry, label in [
                (from_span, from_entry, "from_span"),
                (to_span, to_entry, "to_span"),
            ]:
                if span.startswith("chash:") and entry and entry.physical_collection:
                    try:
                        from nexus.db import make_t3
                        t3 = make_t3()
                        result = self.resolve_span(span, entry.physical_collection, t3._client)
                        if result is None:
                            errors.append(
                                f"{label} {span!r} does not resolve in "
                                f"collection {entry.physical_collection}"
                            )
                    except Exception:
                        pass  # T3 unavailable — skip validation
            if errors:
                raise ValueError(f"unresolvable span: {'; '.join(errors)}")
        now = datetime.now(UTC).isoformat()
        row = self._db.execute(
            "SELECT id, created_by, metadata, created_at FROM links "
            "WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
            (str(from_t), str(to_t), link_type),
        ).fetchone()

        if row is not None:
            existing_meta = json.loads(row[2]) if row[2] else {}
            existing_meta.update(meta)
            co = existing_meta.get("co_discovered_by", [])
            if created_by != row[1] and created_by not in co:
                co.append(created_by)
            existing_meta["co_discovered_by"] = co
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=row[1], created_at=row[3] or now, meta=existing_meta,
            )
            event = _make_event(
                _LinkCreatedPayload(
                    from_doc=str(from_t),
                    to_doc=str(to_t),
                    link_type=link_type,
                    creator=row[1],
                    from_span=from_span,
                    to_span=to_span,
                    created_at=row[3] or now,
                    meta=dict(existing_meta),
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                # Event-sourced merge: emit the LinkCreated carrying
                # the FINAL merged metadata first, then let the
                # projector's INSERT OR REPLACE overwrite the prior
                # SQLite row with the merged state.
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._links_path, rec.__dict__)
            else:
                self._db.execute(
                    "UPDATE links SET from_span=?, to_span=?, metadata=? WHERE id=?",
                    (from_span, to_span, json.dumps(existing_meta), row[0]),
                )
                self._append_jsonl(self._links_path, rec.__dict__)
                self._db.commit()
                self._emit_shadow_event(event)
            return False
        else:
            combined_meta = dict(meta)
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=created_by, created_at=now, meta=combined_meta,
            )
            event = _make_event(
                _LinkCreatedPayload(
                    from_doc=str(from_t),
                    to_doc=str(to_t),
                    link_type=link_type,
                    creator=created_by,
                    from_span=from_span,
                    to_span=to_span,
                    created_at=now,
                    meta=dict(combined_meta),
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._links_path, rec.__dict__)
            else:
                self._db.execute(
                    "INSERT OR IGNORE INTO links "
                    "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                    "created_by, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(from_t), str(to_t), link_type, from_span, to_span,
                     created_by, now, json.dumps(combined_meta)),
                )
                self._append_jsonl(self._links_path, rec.__dict__)
                self._db.commit()
                self._emit_shadow_event(event)
            return True

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
        """Create or merge a link. Returns True if new, False if merged.

        Spans accept ``chash:<sha256hex>`` for content-addressed chunk identity
        (preferred) or legacy positional formats. See class docstring for full
        span policy.

        Raises ValueError if either endpoint is missing (unless allow_dangling=True)
        or if a span string does not match ``_SPAN_PATTERN``.
        """
        dir_fd = self._acquire_lock()
        try:
            return self._link_unlocked(
                from_t, to_t, link_type, created_by,
                from_span, to_span, dict(meta),
                allow_dangling=allow_dangling,
            )
        finally:
            self._release_lock(dir_fd)

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
        """Create link only if it does not already exist. Returns True=created, False=existed.

        No merge, no co_discovered_by — pure insert-or-skip via UNIQUE constraint.
        No JSONL append on the 'already exists' path.
        Raises ValueError if either endpoint is missing (unless allow_dangling=True).
        """
        # Validate span format before acquiring lock
        for span, label in [(from_span, "from_span"), (to_span, "to_span")]:
            if not _SPAN_PATTERN.match(span):
                raise ValueError(
                    f"invalid {label}: {span!r} — use 'line_start-line_end', "
                    f"'chunk_idx:char_start-char_end', 'chash:<sha256hex>', 'chash:<start>-<end>:<sha256hex>', or '' for whole document"
                )
        dir_fd = self._acquire_lock()
        try:
            row = self._db.execute(
                "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
                (str(from_t), str(to_t), link_type),
            ).fetchone()
            if row is not None:
                return False
            if not allow_dangling:
                errors = []
                from_entry = self.resolve(from_t)
                to_entry = self.resolve(to_t)
                if from_entry is None:
                    errors.append(f"from_tumbler {from_t} not found")
                if to_entry is None:
                    errors.append(f"to_tumbler {to_t} not found")
                if errors:
                    raise ValueError(f"dangling link: {'; '.join(errors)}")
                for span, entry, label in [
                    (from_span, from_entry, "from_span"),
                    (to_span, to_entry, "to_span"),
                ]:
                    if span.startswith("chash:") and entry and entry.physical_collection:
                        try:
                            from nexus.db import make_t3
                            t3 = make_t3()
                            result = self.resolve_span(span, entry.physical_collection, t3._client)
                            if result is None:
                                errors.append(
                                    f"{label} {span!r} does not resolve in "
                                    f"collection {entry.physical_collection}"
                                )
                        except Exception:
                            pass
                if errors:
                    raise ValueError(f"unresolvable span: {'; '.join(errors)}")
            now = datetime.now(UTC).isoformat()
            combined_meta = dict(meta)
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=created_by, created_at=now, meta=combined_meta,
            )
            event = _make_event(
                _LinkCreatedPayload(
                    from_doc=str(from_t),
                    to_doc=str(to_t),
                    link_type=link_type,
                    creator=created_by,
                    from_span=from_span,
                    to_span=to_span,
                    created_at=now,
                    meta=dict(combined_meta),
                ),
                v=0,
            )
            if self._event_sourced_enabled:
                self._write_to_event_log(event)
                self._projector.apply(event)
                self._db.commit()
                self._append_jsonl(self._links_path, rec.__dict__)
            else:
                self._db.execute(
                    "INSERT OR IGNORE INTO links "
                    "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                    "created_by, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(from_t), str(to_t), link_type, from_span, to_span,
                     created_by, now, json.dumps(combined_meta)),
                )
                self._append_jsonl(self._links_path, rec.__dict__)
                self._db.commit()
                self._emit_shadow_event(event)
            return True
        finally:
            self._release_lock(dir_fd)

    def unlink(self, from_t: Tumbler, to_t: Tumbler, link_type: str = "") -> int:
        dir_fd = self._acquire_lock()
        try:
            if link_type:
                rows = self._db.execute(
                    "SELECT id, link_type, created_by FROM links "
                    "WHERE from_tumbler = ? AND to_tumbler = ? AND link_type = ?",
                    (str(from_t), str(to_t), link_type),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, link_type, created_by FROM links "
                    "WHERE from_tumbler = ? AND to_tumbler = ?",
                    (str(from_t), str(to_t)),
                ).fetchall()

            for row_id, lt, original_created_by in rows:
                # Fetch full row for forensic tombstone
                full = self._db.execute(
                    "SELECT from_span, to_span, metadata FROM links WHERE id = ?",
                    (row_id,),
                ).fetchone()
                tombstone = {
                    "from_t": str(from_t),
                    "to_t": str(to_t),
                    "link_type": lt,
                    "_deleted": True,
                    "from_span": full[0] or "" if full else "",
                    "to_span": full[1] or "" if full else "",
                    "created_by": original_created_by,
                    "created_at": datetime.now(UTC).isoformat(),
                    "meta": json.loads(full[2]) if full and full[2] else {},
                }
                event = _make_event(
                    _LinkDeletedPayload(
                        from_doc=str(from_t),
                        to_doc=str(to_t),
                        link_type=lt,
                        reason="catalog.unlink",
                    ),
                    v=0,
                )
                if self._event_sourced_enabled:
                    # Event-sourced: write event, project (DELETE),
                    # then JSONL tombstone. Commit batched at end of
                    # the loop for efficiency on multi-row unlinks.
                    self._write_to_event_log(event)
                    self._projector.apply(event)
                    self._append_jsonl(self._links_path, tombstone)
                else:
                    self._append_jsonl(self._links_path, tombstone)
                    self._db.execute("DELETE FROM links WHERE id = ?", (row_id,))

            self._db.commit()
            # Shadow-emit one LinkDeleted per removed row AFTER
            # db.commit() so a process crash between the DELETE and the
            # commit cannot leave events.jsonl claiming a deletion that
            # SQLite has not yet committed. Skipped when event-sourced
            # is on — the per-row loop above already emitted + applied.
            if not self._event_sourced_enabled:
                for row_id, lt, original_created_by in rows:
                    self._emit_shadow_event(_make_event(
                        _LinkDeletedPayload(
                            from_doc=str(from_t),
                            to_doc=str(to_t),
                            link_type=lt,
                            reason="catalog.unlink",
                        ),
                        v=0,
                    ))
            return len(rows)
        finally:
            self._release_lock(dir_fd)

    def _row_to_link(self, row: tuple) -> CatalogLink:
        return CatalogLink(
            from_tumbler=Tumbler.parse(row[0]),
            to_tumbler=Tumbler.parse(row[1]),
            link_type=row[2],
            from_span=row[3] or "",
            to_span=row[4] or "",
            created_by=row[5],
            created_at=row[6] or "",
            meta=json.loads(row[7]) if row[7] else {},
        )

    def links_from(
        self,
        tumbler: Tumbler,
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list[CatalogLink]:
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE from_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        effective = link_types or ([link_type] if link_type else [])
        if len(effective) == 1:
            sql += " AND link_type = ?"
            params.append(effective[0])
        elif len(effective) > 1:
            placeholders = ",".join("?" * len(effective))
            sql += f" AND link_type IN ({placeholders})"
            params.extend(effective)
        return [self._row_to_link(r) for r in self._db.execute(sql, params).fetchall()]

    def links_to(
        self,
        tumbler: Tumbler,
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list[CatalogLink]:
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE to_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        effective = link_types or ([link_type] if link_type else [])
        if len(effective) == 1:
            sql += " AND link_type = ?"
            params.append(effective[0])
        elif len(effective) > 1:
            placeholders = ",".join("?" * len(effective))
            sql += f" AND link_type IN ({placeholders})"
            params.extend(effective)
        return [self._row_to_link(r) for r in self._db.execute(sql, params).fetchall()]

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
        """Composable link filter. Returns CatalogLink list with LIMIT/OFFSET.

        limit=0 means unlimited (maps to SQLite LIMIT -1).
        """
        conditions: list[str] = []
        params: list[str | int] = []

        if tumbler:
            if direction == "out":
                conditions.append("from_tumbler = ?")
                params.append(tumbler)
            elif direction == "in":
                conditions.append("to_tumbler = ?")
                params.append(tumbler)
            else:
                conditions.append("(from_tumbler = ? OR to_tumbler = ?)")
                params.extend([tumbler, tumbler])
        if from_t:
            conditions.append("from_tumbler = ?")
            params.append(from_t)
        if to_t:
            conditions.append("to_tumbler = ?")
            params.append(to_t)
        if link_type:
            conditions.append("link_type = ?")
            params.append(link_type)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        if created_at_before:
            conditions.append("created_at != '' AND created_at < ?")
            params.append(created_at_before)

        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit if limit > 0 else -1, offset])

        return [self._row_to_link(r) for r in self._db.execute(sql, params).fetchall()]

    def bulk_unlink(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        created_at_before: str = "",
        dry_run: bool = False,
    ) -> int:
        """Delete links matching filters. Returns count removed.

        Tombstones preserve original created_by for JSONL audit trail.
        dry_run=True returns count without deleting.
        """
        has_filter = any([from_t, to_t, link_type, created_by, created_at_before])
        if not has_filter and not dry_run:
            raise ValueError("bulk_unlink requires at least one filter (or dry_run=True)")

        dir_fd = self._acquire_lock()
        try:
            matching = self.link_query(
                from_t=from_t, to_t=to_t, link_type=link_type,
                created_by=created_by, created_at_before=created_at_before,
                limit=0,
            )

            if dry_run:
                return len(matching)

            for lnk in matching:
                tombstone = {
                    "from_t": str(lnk.from_tumbler), "to_t": str(lnk.to_tumbler),
                    "link_type": lnk.link_type, "_deleted": True,
                    "from_span": lnk.from_span, "to_span": lnk.to_span,
                    "created_by": lnk.created_by,
                    "created_at": datetime.now(UTC).isoformat(),
                    "meta": lnk.meta,
                }
                event = _make_event(
                    _LinkDeletedPayload(
                        from_doc=str(lnk.from_tumbler),
                        to_doc=str(lnk.to_tumbler),
                        link_type=lnk.link_type,
                        reason="catalog.bulk_unlink",
                    ),
                    v=0,
                )
                if self._event_sourced_enabled:
                    self._write_to_event_log(event)
                    self._projector.apply(event)
                    self._append_jsonl(self._links_path, tombstone)
                else:
                    self._append_jsonl(self._links_path, tombstone)
                    self._db.execute(
                        "DELETE FROM links WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
                        (str(lnk.from_tumbler), str(lnk.to_tumbler), lnk.link_type),
                    )
            self._db.commit()
            # Shadow-emit AFTER db.commit() (see ``unlink`` for the
            # crash-window rationale). Skipped when event-sourced — the
            # per-row loop already wrote and projected.
            if not self._event_sourced_enabled:
                for lnk in matching:
                    self._emit_shadow_event(_make_event(
                        _LinkDeletedPayload(
                            from_doc=str(lnk.from_tumbler),
                            to_doc=str(lnk.to_tumbler),
                            link_type=lnk.link_type,
                            reason="catalog.bulk_unlink",
                        ),
                        v=0,
                    ))
            return len(matching)
        finally:
            self._release_lock(dir_fd)

    def validate_link(
        self, from_t: Tumbler, to_t: Tumbler, link_type: str
    ) -> list[str]:
        """Validate a proposed link. Returns list of error strings (empty = valid)."""
        errors: list[str] = []
        if self.resolve(from_t) is None:
            errors.append(f"from_tumbler {from_t} not found in documents")
        if self.resolve(to_t) is None:
            errors.append(f"to_tumbler {to_t} not found in documents")
        row = self._db.execute(
            "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
            (str(from_t), str(to_t), link_type),
        ).fetchone()
        if row is not None:
            errors.append(f"duplicate: link ({from_t}, {to_t}, {link_type!r}) already exists")
        return errors

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
        """Audit the links table. Returns stats + orphan + duplicate + chash lists.

        When ``t3`` is provided, verifies each ``chash:`` span resolves to a
        chunk in the corresponding ChromaDB collection. Unresolvable spans
        appear in ``stale_chash``.

        Args:
            t3: Raw ChromaDB client (``chromadb.ClientAPI``), not a ``T3Database``.
                Production callers pass ``t3_db._client``; tests pass an
                ``EphemeralClient`` directly. Injection keeps the method testable
                without ``make_t3()``.
        """
        total = self._db.execute("SELECT count(*) FROM links").fetchone()[0]
        by_type = dict(
            self._db.execute(
                "SELECT link_type, count(*) FROM links GROUP BY link_type"
            ).fetchall()
        )
        by_creator = dict(
            self._db.execute(
                "SELECT created_by, count(*) FROM links GROUP BY created_by"
            ).fetchall()
        )
        orphan_rows = self._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type FROM links l "
            "WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.tumbler = l.from_tumbler) "
            "   OR NOT EXISTS (SELECT 1 FROM documents d WHERE d.tumbler = l.to_tumbler)"
        ).fetchall()
        orphaned = [{"from": r[0], "to": r[1], "type": r[2]} for r in orphan_rows]
        dup_rows = self._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type, count(*) AS cnt "
            "FROM links GROUP BY from_tumbler, to_tumbler, link_type HAVING cnt > 1"
        ).fetchall()
        duplicates = [
            {"from": r[0], "to": r[1], "type": r[2], "count": r[3]} for r in dup_rows
        ]
        # Stale spans: positional spans pointing to documents re-indexed after link creation.
        # Content-hash spans (chash:) are excluded — they survive re-indexing by design
        # (RDR-053). Stale chash spans are detected separately via T3 verification below.
        # Checks both from_span (joined on from_tumbler) and to_span (joined on to_tumbler).
        # datetime() wraps ensure correct comparison regardless of ISO-8601 padding.
        stale_span_rows = self._db.execute(
            "SELECT l.from_tumbler, l.to_tumbler, l.link_type, l.created_at, "
            "       d.indexed_at, 'from' AS side "
            "FROM links l "
            "JOIN documents d ON d.tumbler = l.from_tumbler "
            "WHERE (l.from_span IS NOT NULL AND l.from_span != '') "
            "  AND l.from_span NOT LIKE 'chash:%' "
            "  AND datetime(l.created_at) < datetime(d.indexed_at) "
            "UNION ALL "
            "SELECT l.from_tumbler, l.to_tumbler, l.link_type, l.created_at, "
            "       d.indexed_at, 'to' AS side "
            "FROM links l "
            "JOIN documents d ON d.tumbler = l.to_tumbler "
            "WHERE (l.to_span IS NOT NULL AND l.to_span != '') "
            "  AND l.to_span NOT LIKE 'chash:%' "
            "  AND datetime(l.created_at) < datetime(d.indexed_at)"
        ).fetchall()
        stale_spans = [
            {"from": r[0], "to": r[1], "type": r[2],
             "link_created": r[3], "doc_reindexed": r[4], "side": r[5]}
            for r in stale_span_rows
        ]
        # chash verification: check each chash: span resolves in T3
        stale_chash: list[dict] = []
        if t3 is not None:
            chash_rows = self._db.execute(
                "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span "
                "FROM links WHERE from_span LIKE 'chash:%' OR to_span LIKE 'chash:%'"
            ).fetchall()
            for row in chash_rows:
                from_t, to_t, lt, from_span, to_span = row
                for span, tumbler_str in [(from_span, from_t), (to_span, to_t)]:
                    if not span.startswith("chash:"):
                        continue
                    # Extract hash from chash:<hash> or chash:<hash>:<start>-<end>
                    body = span[len("chash:"):]
                    m_range = re.match(r"^([0-9a-f]{64}):\d+-\d+$", body)
                    chunk_hash = m_range.group(1) if m_range else body
                    entry = self.resolve(Tumbler.parse(tumbler_str))
                    if entry is None:
                        stale_chash.append(
                            {"from": from_t, "to": to_t, "type": lt, "span": span,
                             "reason": "document_deleted"}
                        )
                        continue
                    try:
                        col = t3.get_collection(entry.physical_collection)
                        result = col.get(
                            where={"chunk_text_hash": chunk_hash}, include=[]
                        )
                        if not result["ids"]:
                            stale_chash.append(
                                {"from": from_t, "to": to_t, "type": lt, "span": span,
                                 "reason": "missing"}
                            )
                    except Exception as exc:
                        _log.warning(
                            "link_audit_chash_error",
                            tumbler=tumbler_str, span=span,
                            exc_info=True,
                        )
                        stale_chash.append(
                            {"from": from_t, "to": to_t, "type": lt, "span": span,
                             "reason": "error", "error": type(exc).__name__}
                        )

        return {
            "total": total,
            "by_type": by_type,
            "by_creator": by_creator,
            "orphaned": orphaned,
            "orphaned_count": len(orphaned),
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
            "stale_spans": stale_spans,
            "stale_span_count": len(stale_spans),
            "stale_chash": stale_chash,
            "stale_chash_count": len(stale_chash),
        }

    _MAX_GRAPH_DEPTH = 10
    _MAX_GRAPH_NODES = 500

    def graph(
        self,
        tumbler: Tumbler,
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> dict:
        """BFS traversal to given depth. Returns {"nodes": [...], "edges": [...]}.

        Depth capped at _MAX_GRAPH_DEPTH. Traversal stops at _MAX_GRAPH_NODES visited.
        ``link_types`` (plural list) takes precedence over ``link_type`` (single str).
        """
        depth = min(depth, self._MAX_GRAPH_DEPTH)
        effective_types: list[str] = link_types or ([link_type] if link_type else [])
        visited: set[str] = {str(tumbler)}
        seen_edges: set[tuple[str, str, str]] = set()
        all_edges: list[CatalogLink] = []
        queue: deque[tuple[Tumbler, int]] = deque([(tumbler, 0)])

        while queue:
            if len(visited) >= self._MAX_GRAPH_NODES:
                _log.warning("graph_node_limit", tumbler=str(tumbler), visited=len(visited))
                break
            current, d = queue.popleft()
            if d >= depth:
                continue

            neighbors: list[CatalogLink] = []
            if direction in ("out", "both"):
                neighbors.extend(self.links_from(current, link_types=effective_types or None))
            if direction in ("in", "both"):
                neighbors.extend(self.links_to(current, link_types=effective_types or None))

            for edge in neighbors:
                edge_key = (str(edge.from_tumbler), str(edge.to_tumbler), edge.link_type)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    all_edges.append(edge)
                other = edge.to_tumbler if edge.from_tumbler == current else edge.from_tumbler
                if str(other) not in visited:
                    visited.add(str(other))
                    queue.append((other, d + 1))

        nodes = [self.resolve(Tumbler.parse(t)) for t in visited]
        nodes = [n for n in nodes if n is not None]
        return {"nodes": nodes, "edges": all_edges}

    def graph_many(
        self,
        seeds: list[Tumbler],
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> dict:
        """BFS traversal from multiple seed tumblers — thin wrapper over :meth:`graph`.

        Merges per-seed results with node-key = ``str(tumbler)`` and
        edge-key = ``(from, to, link_type)`` deduplication so a shared
        node or edge discovered from two seeds only appears once.
        """
        merged_nodes: dict[str, object] = {}
        merged_edges: dict[tuple[str, str, str], object] = {}

        for seed in seeds:
            if len(merged_nodes) >= self._MAX_GRAPH_NODES:
                _log.warning("graph_many_node_limit", visited=len(merged_nodes))
                break
            result = self.graph(
                seed, depth=depth, direction=direction,
                link_type=link_type, link_types=link_types,
            )
            for node in result.get("nodes") or []:
                if len(merged_nodes) >= self._MAX_GRAPH_NODES:
                    _log.debug(
                        "graph_many_node_limit_mid_seed",
                        visited=len(merged_nodes),
                    )
                    break
                key = str(node.tumbler) if hasattr(node, "tumbler") else str(node)
                if key not in merged_nodes:
                    merged_nodes[key] = node
            # Drop edges whose endpoints were excluded by the node cap — otherwise
            # callers iterating nodes-then-edges see dangling references.
            for edge in result.get("edges") or []:
                from_key = str(edge.from_tumbler)
                to_key = str(edge.to_tumbler)
                if from_key not in merged_nodes or to_key not in merged_nodes:
                    continue
                edge_key = (from_key, to_key, edge.link_type)
                if edge_key not in merged_edges:
                    merged_edges[edge_key] = edge

        return {
            "nodes": list(merged_nodes.values()),
            "edges": list(merged_edges.values()),
        }

    # ── Rebuild ───────────���──────────────────────────��─────────────────────

    def rebuild(self) -> None:
        """Rebuild SQLite from JSONL. Called at startup and after git pull."""
        dir_fd = self._acquire_lock()
        try:
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
            links_dict = read_links(self._links_path) if self._links_path.exists() else {}
            self._db.rebuild(owners, documents, list(links_dict.values()))
        finally:
            self._release_lock(dir_fd)

    def _defrag_unlocked(self) -> dict[str, int]:
        """Core defrag logic — caller must hold the lock."""
        removed = {}
        for path in [self._owners_path, self._documents_path, self._links_path]:
            if not path.exists():
                continue
            original_lines = sum(1 for line in path.open() if line.strip())
            seen: dict[str, str] = {}
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "owner" in obj:
                        key = obj["owner"]
                    elif "tumbler" in obj:
                        key = obj["tumbler"]
                    elif "from_t" in obj:
                        key = f"{obj['from_t']}|{obj['to_t']}|{obj['link_type']}"
                    else:
                        continue
                    seen[key] = line
            with path.open("w") as f:
                for line in seen.values():
                    f.write(line + "\n")
            removed[path.name] = original_lines - len(seen)
            # Rebuild SQLite from defragged JSONL to stay consistent
        owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
        documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
        links_dict = read_links(self._links_path) if self._links_path.exists() else {}
        self._db.rebuild(owners, documents, list(links_dict.values()))
        return removed

    def defrag(self) -> dict[str, int]:
        """Deduplicate JSONL files — keep latest version of each live record.

        Removes duplicate overwrites but preserves tombstones (deletion markers).
        This is the safe compaction: no history is lost, deleted tumblers remain
        reserved, and the version record is intact for forensic purposes.
        Returns count of lines removed per file.
        """
        dir_fd = self._acquire_lock()
        try:
            return self._defrag_unlocked()
        finally:
            self._release_lock(dir_fd)

    def compact(self) -> dict[str, int]:
        """Full compaction: deduplicate AND remove tombstones.

        This erases deletion history — tombstoned tumblers are no longer
        visible in the JSONL (though they remain reserved via owner next_seq).
        Use defrag() for safe compaction that preserves tombstones.
        """
        dir_fd = self._acquire_lock()
        try:
            removed = {}
            for path, reader in [
                (self._owners_path, read_owners),
                (self._documents_path, read_documents),
                (self._links_path, read_links),
            ]:
                if not path.exists():
                    continue
                original_lines = sum(1 for line in path.open() if line.strip())
                records = reader(path)
                with path.open("w") as f:
                    for record in records.values():
                        f.write(json.dumps(record.__dict__, default=str) + "\n")
                new_lines = len(records)
                removed[path.name] = original_lines - new_lines
            # Rebuild SQLite from compacted JSONL
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
            links_dict = read_links(self._links_path) if self._links_path.exists() else {}
            self._db.rebuild(owners, documents, list(links_dict.values()))
            return removed
        finally:
            self._release_lock(dir_fd)
