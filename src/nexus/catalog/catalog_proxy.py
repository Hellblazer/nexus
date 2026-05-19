# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-112 6shq.1 (nexus-lj2l): ``ExecuteProxy`` over ``T2Client.catalog``.

Substrate for the higher-level ``nexus.catalog.Catalog`` wrapper to
operate under ``NX_STORAGE_MODE=daemon``. Phase 4 (nexus-uar6) flipped
``nx catalog`` CLI sites to the daemon-aware T3 path but deferred the
Catalog wrapper itself; it still opened a local ``CatalogDB`` against
``.catalog.db`` independent of the daemon-owned ``memory.db``. This
module ships the duck-typed handle that lets ``Catalog`` swap its
backing store without forking the call sites.

Cursor-vs-list audit (catalog/**)
---------------------------------
``CatalogDB.execute(...)`` returns ``sqlite3.Cursor`` (lazy, supports
``.fetchone()``, ``.fetchall()``, ``.lastrowid``, lazy iteration).
``CatalogStore.execute(...)`` returns ``list[tuple]`` (fully
materialised, per its docstring).

Audited every ``cat._db.execute(...)`` and ``self._db.execute(...)``
call site across the catalog package; catalog.py / catalog_writes.py
/ catalog_links.py / catalog_sync.py / projector.py / catalog_docs.py /
catalog_spans.py:

* ``.fetchone()``: heavily used; safe under list shape if the proxy
  returns ``rows[0] if rows else None`` semantics.
* ``.fetchall()``: used in catalog_links / catalog_writes /
  catalog_sync; trivially compatible with a list return.
* ``for row in cursor:``: no call sites in the catalog package; safe
  via direct list iteration.
* ``.lastrowid``: NOT used at any catalog/** site. The proxy
  intentionally does not expose it.
* ``cursor.execute(...)`` direct re-entrancy; NOT used; only the
  initial ``self._db.execute(...)`` call returns the cursor.
* ``executemany`` / ``executescript``; only used inside
  ``CatalogDB._init_schema`` and ``CatalogStore._init_schema``; not
  reached from the Catalog wrapper.

Rather than rewrite every call site to list-index semantics, this proxy
wraps the daemon's ``list[tuple]`` response in a ``_ResultCursor``
adapter that exposes ``.fetchone()`` / ``.fetchall()`` / iteration with
the same shapes the call sites already expect. The diff stays
localised; cursor-vs-list mismatches cannot surface at runtime.

Supported surface (read-path + simple INSERTs/UPDATEs)
------------------------------------------------------
* ``execute(sql, params)`` -> ``_ResultCursor``; forwards to
  ``T2Client.catalog.execute`` (RPC) and wraps the response.
* ``commit()``: forwards to ``T2Client.catalog.commit`` (RPC).
* ``search(query, *, content_type=None)``: forwards to
  ``T2Client.catalog.search``; FTS5 MATCH used by ``Catalog.find``.
* ``descendants(prefix)``: forwards to ``T2Client.catalog.descendants``;
  used by ``_DocumentOps.descendants``.
* ``next_document_number(owner_prefix)``: forwards to
  ``T2Client.catalog.next_document_number``; legacy fallback for
  pre-migration owners during ``Catalog.register``. Modern callers hit
  the JSONL high-water mark first; the fallback only fires when
  ``owner_rec.next_seq == 0`` which happens only for pre-Phase-3 data.
* ``backfilled_collections()``: public accessor mirroring the
  underscored attribute so the daemon proxy can read the set.
* ``transaction()``: raises ``NotImplementedError``. CatalogStore's
  ``transaction()`` is in ``_RPC_DENY_OPS`` (nexus-7ejx) because
  ``@contextmanager`` methods cannot meaningfully cross the JSON-RPC
  boundary; the yielded ``sqlite3.Connection`` lives daemon-side and
  the with-block never runs there. Write-path call sites that depend on
  multi-statement atomicity (``catalog_sync._SyncOps`` rebuild path,
  ``catalog_links`` mutations) are deferred to 6shq.2-6shq.6 with the
  call-site flips per the parent-bead decomposition.

Explicitly NOT on the proxy (raises ``AttributeError``)
-------------------------------------------------------
* ``rebuild`` / ``bulk_load_documents``; write-path; deferred. The
  daemon owns its projection rebuild under
  ``NX_STORAGE_MODE=daemon`` and ``_SyncOps._ensure_consistent``
  short-circuits when ``Catalog._daemon_proxy`` is set.
* ``_conn`` direct access; yfqv defect class; ``_StoreProxy`` already
  filters underscores by design.
* ``_backfilled_collections`` attribute; replaced by the public
  ``backfilled_collections()`` method (mirrored onto ``CatalogDB`` in
  this bead for parity).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator, Iterator

if TYPE_CHECKING:
    from nexus.daemon.t2_client import T2Client


class _ResultCursor:
    """Cursor-shaped wrapper around a ``list[tuple]`` RPC result.

    The daemon's ``CatalogStore.execute`` returns ``list[tuple]`` for
    JSON-RPC serialisability (the original ``sqlite3.Cursor`` cannot
    round-trip through the socket). This wrapper restores the
    ``.fetchone()`` / ``.fetchall()`` / iteration semantics that
    catalog/** call sites assume so the proxy is drop-in compatible
    with the existing code.

    Row-shape normalisation: JSON-RPC encodes tuples as JSON arrays and
    decodes them back as Python ``list``. Direct-mode callers receive
    ``sqlite3.Row`` (tuple-shaped). To preserve the contract uniformly,
    rows are coerced to ``tuple`` on every accessor; production code
    indexes positionally (``row[0]``) so the difference is invisible
    there, but tests asserting ``row == (value,)`` would otherwise see
    mode-dependent shapes.

    Non-stateful: ``.fetchone()`` is idempotent on repeated calls; it
    returns the first row each time rather than advancing through the
    result set. The audit (see module docstring) confirmed no
    catalog/** site calls ``.fetchone()`` more than once on the same
    cursor; they all use it as "give me the only/first row" or fall
    through to ``.fetchall()`` for multi-row results. Iteration via
    ``__iter__`` walks the full list once.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list) -> None:
        # Coerce each row to tuple so the wrapper's output shape matches
        # direct-mode ``sqlite3.Cursor`` behaviour regardless of how the
        # JSON-RPC layer decoded the rows.
        self._rows: list[tuple] = [tuple(r) for r in rows]

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def __iter__(self) -> Iterator[tuple]:
        return iter(self._rows)


class ExecuteProxy:
    """``CatalogDB``-shaped handle over ``T2Client.catalog`` for daemon mode.

    Injected into ``Catalog`` via the ``db=`` keyword. The proxy is
    duck-typed against the subset of ``CatalogDB``'s surface that the
    Catalog wrapper and its ``_Ops`` facades reach for. See module
    docstring for the supported / unsupported surface.

    Construction is cheap (just stashes the client reference); the
    client owns its connection pool. Callers that cache a ``Catalog``
    (``open_cached``) keep one ``ExecuteProxy`` alive per cache entry
    and let the underlying ``T2Client`` pool manage RPC sockets.
    """

    def __init__(self, t2_client: "T2Client") -> None:
        self._t2 = t2_client

    # -------------------------------------------------------------------------
    # CatalogDB-compatible API
    # -------------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | list = ()) -> _ResultCursor:
        """Execute SQL through the daemon's CatalogStore.

        Returns a ``_ResultCursor`` wrapper so ``.fetchone()`` /
        ``.fetchall()`` / iteration work unchanged. The daemon RPC
        always returns ``list[tuple]``; wrapping happens client-side.

        ``params`` is normalised to ``list`` so the JSON-RPC encoder
        receives a native type even when callers pass a tuple
        (``CatalogDB`` historically accepted both).
        """
        rows = self._t2.catalog.execute(sql=sql, params=list(params))
        return _ResultCursor(rows)

    def commit(self) -> None:
        """Commit any pending writes on the daemon's CatalogStore connection."""
        self._t2.catalog.commit()

    def close(self) -> None:
        """No-op under daemon mode; the proxy does not own a connection.

        RDR-112 6shq.3 (nexus-siy7): ``CatalogDB.close()`` tears down the
        ``sqlite3.Connection`` that the proxy's direct-mode peer owns. The
        proxy borrows the daemon-side connection through the process-singleton
        ``T2Client`` (see ``nexus.catalog.open_catalog``); closing here would
        leave subsequent callers with a dead client and break the cache
        invariant in :func:`nexus.catalog.open_cached`. Lifecycle is owned by
        :func:`nexus.catalog.reset_cache`, which closes the singleton
        deterministically. Existing CLI sites (``commands/dt.py`` finally
        blocks; the doctor-replay path) call ``cat._db.close()`` to release
        the WAL lock for back-to-back ``CliRunner`` invocations; under
        daemon mode the daemon owns that lock, so the close is a no-op
        without behaviour drift.
        """
        return None

    @contextmanager
    def transaction(self) -> Generator[object, None, None]:
        """Not implemented under daemon mode; write-path deferred.

        ``CatalogStore.transaction()`` is RPC-denied (nexus-7ejx,
        ``_RPC_DENY_OPS``): the yielded ``sqlite3.Connection`` lives on
        the daemon side, so the with-block would execute against a
        meaningless local generator value. Call sites that need
        multi-statement atomicity under daemon mode will land in
        6shq.2-6shq.6 with a purpose-built batched RPC; lj2l ships the
        read-path substrate only.
        """
        raise NotImplementedError(
            "ExecuteProxy.transaction() is not supported under "
            "NX_STORAGE_MODE=daemon. Write-path call sites that need "
            "transactional bulk load are deferred to RDR-112 6shq.2-6shq.6. "
            "Use ExecuteProxy.execute() + .commit() for single-statement "
            "writes, or run under NX_STORAGE_MODE=direct."
        )
        # The yield below is unreachable but keeps mypy / typing happy on
        # the ``@contextmanager``-decorated generator contract.
        yield None  # pragma: no cover

    def rebuild(self, *args: object, **kwargs: object) -> None:
        """Not implemented under daemon mode; projection-rebuild deferred.

        ``Catalog._SyncOps._ensure_consistent`` short-circuits under
        ``_daemon_proxy`` (the daemon owns its projection), so this
        stub is defence-in-depth: any future caller that reaches
        ``cat._db.rebuild(...)`` outside the consistency path will fail
        loud with a recovery hint rather than the opaque
        ``AttributeError`` that would surface from a bare
        ``_StoreProxy`` lookup.
        """
        raise NotImplementedError(
            "ExecuteProxy.rebuild() is not supported under "
            "NX_STORAGE_MODE=daemon. The daemon owns catalog rebuild; "
            "client-side replay would race the daemon writer. Use "
            "NX_STORAGE_MODE=direct for offline rebuild, or wait for "
            "RDR-112 6shq.2-6shq.6 which lands the write-path port."
        )

    def bulk_load_documents(self) -> None:
        """Not implemented under daemon mode; FTS5 bulk-load fence deferred.

        ``CatalogStore.bulk_load_documents`` is RPC-denied (it is a
        ``@contextmanager`` whose yield is meaningless across RPC).
        The legacy rebuild path that opens this fence is itself
        short-circuited under ``_daemon_proxy``; this explicit stub
        keeps the failure mode consistent with ``transaction()`` /
        ``rebuild()`` if any future caller reaches here.
        """
        raise NotImplementedError(
            "ExecuteProxy.bulk_load_documents() is not supported under "
            "NX_STORAGE_MODE=daemon. The FTS5 fence runs daemon-side; "
            "client-side invocation has no meaning. Deferred to "
            "RDR-112 6shq.2-6shq.6 with the write-path port."
        )

    def search(
        self, query: str, *, content_type: str | None = None,
    ) -> list[dict]:
        """Forward FTS5 search to the daemon's CatalogStore.

        Used by ``_DocumentOps.find`` (the high-level ``Catalog.find``).
        Returns the dict-shaped rows ``CatalogStore.search`` produces;
        no client-side post-processing.
        """
        return self._t2.catalog.search(query=query, content_type=content_type)

    def descendants(self, prefix: str) -> list[dict]:
        """Forward descendant-by-tumbler query to the daemon."""
        return self._t2.catalog.descendants(prefix=prefix)

    def next_document_number(self, owner_prefix: str) -> int:
        """Forward to the daemon's high-water-mark scan.

        Legacy fallback used by ``Catalog.register`` only when the
        OwnerRecord's JSONL high-water mark is unset (pre-Phase-3
        data). Modern flow allocates from JSONL directly without
        hitting this path.
        """
        return int(self._t2.catalog.next_document_number(owner_prefix=owner_prefix))

    def backfilled_collections(self) -> list[str]:
        """Forward to the daemon's public accessor.

        ``Catalog._emit_backfilled_collection_events`` historically
        reached into ``self._db._backfilled_collections`` (private set
        attribute). The yfqv defect class flagged that the
        ``_StoreProxy`` skips underscored names by design, so the same
        reach-through fails under daemon mode. The public
        ``backfilled_collections()`` method on ``CatalogStore``
        (RDR-112 P2.review S2 / nexus-m0hi) is the RPC-dispatchable
        equivalent; lj2l mirrors the method onto ``CatalogDB`` so the
        Catalog wrapper can call it uniformly.
        """
        return list(self._t2.catalog.backfilled_collections())
