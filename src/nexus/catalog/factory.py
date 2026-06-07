# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed reader / writer factories for the catalog (RDR-146 P1.2).

The atomic cutover (bead nexus-5p2ci.21) routes EVERY catalog write
through the T2 daemon (the single owner of the ``.catalog.db`` write
handle + JSONL append path) and keeps reads local. The read/write split
is TOOLING-ENFORCED, not convention: ``CATALOG_BANNED_CONSTRUCTORS``
bans bare ``Catalog(...)`` outside the substrate, so every consumer site
reaches the catalog through exactly one of:

  - :func:`make_catalog_reader` -> a read-only local Catalog. Skips the
    two construction-time WRITES (events.jsonl backfill +
    ``_ensure_consistent`` rebuild); opens its SQLite handle ``mode=ro``.
    Reads are WAL read-committed and see the daemon-writer's committed
    projections (RF-8 Q5). Exposes the full read surface; any write
    method raises at the SQLite layer.

  - :func:`make_catalog_writer` -> a write-only proxy exposing ONLY the
    whitelisted write ops (no read methods, so a dataclass-returning read
    can never accidentally round-trip the wire). Routes to the daemon's
    hosted Catalog when reachable; falls back to a direct in-process
    Catalog when the daemon is down (the documented-irreducible
    availability fallback — with no daemon, this process is the sole
    writer, so the single-writer invariant still holds).

Mixed sites (read AND write) hold BOTH a reader and a writer. That is the
gate-resolved design (re-gate Critical): the two typed factories make the
read/write distinction visible and enforceable instead of relying on a
lint that cannot tell a read-only Catalog from a write-capable one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.catalog.catalog import Catalog
from nexus.daemon.catalog_write_shim import CATALOG_WRITE_OPS

_log = structlog.get_logger(__name__)


def _is_catalog_service_mode() -> bool:
    """Return True when NX_STORAGE_BACKEND_CATALOG=service (or global NX_STORAGE_BACKEND=service)."""
    from nexus.db.storage_mode import StorageBackend, storage_backend_for

    return storage_backend_for("catalog") == StorageBackend.SERVICE


class CatalogAdminDaemonLiveError(Exception):
    """Raised by :func:`make_catalog_admin` when a T2 daemon is live.

    Deep-maintenance commands need exclusive ``.catalog.db`` access; opening
    a second full writer against a running daemon is the two-writer hazard
    RDR-146 closes. CLI callers render the message and exit non-zero.
    """


def make_catalog_reader(*, config_dir: Optional[Path] = None) -> Optional[Any]:
    """Return a read-only catalog, or ``None`` when uninitialised.

    In service mode (``NX_STORAGE_BACKEND_CATALOG=service`` or global
    ``NX_STORAGE_BACKEND=service``) returns an :class:`HttpCatalogClient`
    that forwards reads to the Java Postgres service.  The client is always
    considered "initialised" — if the service is unreachable, the first
    HTTP call will raise.

    In SQLite mode (default) returns a read-only local Catalog.  Mirrors the
    historical ``get_catalog()`` contract (None when the catalog dir is not
    initialised) so call sites keep their existing None-guards.  The returned
    Catalog performs no construction-time writes and opens its SQLite handle
    read-only.
    """
    if _is_catalog_service_mode():
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        _log.debug("catalog_reader_service_mode")
        return HttpCatalogClient()

    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    db_path = path / ".catalog.db"
    if not db_path.exists():
        # Cold cache: the JSONL exists (is_initialized) but the SQLite
        # projection has never been built, so a ``mode=ro`` open would
        # raise "unable to open database file". Materialise the cache
        # once via a normal (read-write) construction, then return a
        # read-only handle over the now-existing file. In the daemon
        # world this never fires — the daemon builds the cache when it
        # constructs the hosted Catalog at startup, so ``.catalog.db``
        # already exists by the time any reader runs. It only fires in
        # no-daemon contexts (one-shot CLI, tests) where this process is
        # the sole actor and the one-time build is safe.
        Catalog(path, db_path)._db.close()
    return Catalog(path, db_path, read_only=True)


def make_catalog_admin(*, config_dir: Optional[Path] = None) -> Optional[Catalog]:
    """Return a FULL read+write rich Catalog for deep-maintenance commands.

    RDR-146 P1.2 escape hatch. A small set of ``nx catalog`` maintenance
    commands (``dedupe-owners``, ``undelete``) operate through low-level
    catalog internals — raw ``_db`` transactions, ``_append_jsonl``, the
    event log, ``_projector`` — via free functions (``dedupe.apply_plan``,
    ``catalog_backup.restore_documents``). Those operations are NOT
    expressible as the 22 whitelisted daemon write ops, so they cannot
    route through :class:`CatalogWriter`, and the read-only reader rejects
    their writes.

    This factory hands back a full local rich Catalog so those commands
    work. It is the deep-maintenance analogue of routing ``sync`` / ``pull``
    / ``compact`` through the daemon: a rare, interactive, whole-catalog
    operation that needs exclusive low-level access. Like those, it should
    be run with the daemon quiesced to respect the single-writer invariant
    (the commands warn / are interactive). Returns ``None`` when the catalog
    is uninitialised. Constructed here (the ``catalog/`` allowlist) so the
    boundary lint stays satisfied; callers must NOT bare-construct Catalog.
    """
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    # RDR-146 P1.2 single-writer guard: a live daemon is the sole legitimate
    # .catalog.db writer. Opening a second full writer here while the daemon
    # is up is the two-writer contention this RDR exists to prevent. Refuse
    # loudly with the recovery action rather than silently racing. The probe
    # is read-only discovery (no spawn/reap).
    try:
        from nexus.daemon.discovery import find_t2_daemon
        if find_t2_daemon() is not None:
            raise CatalogAdminDaemonLiveError(
                "A T2 daemon is running; deep-maintenance catalog commands "
                "(dedupe-owners --apply, undelete) need exclusive .catalog.db "
                "access. Stop it first: `nx daemon t2 stop`, run the command, "
                "then restart with `nx daemon t2 start`."
            )
    except CatalogAdminDaemonLiveError:
        raise
    except Exception:  # noqa: BLE001 — discovery import/probe must not block
        _log.debug("catalog_admin_daemon_probe_failed", exc_info=True)
    return Catalog(path, path / ".catalog.db")


class CatalogWriter:
    """Write-only catalog proxy exposing exactly the whitelisted ops.

    Routing is decided once at construction by an explicit ``hello()``
    probe (matching ``t2_index_write``): daemon reachable -> route writes
    over RPC to the hosted Catalog; daemon down -> a direct in-process
    Catalog. Long-lived: a site constructs one writer and issues many
    writes through it, so the probe cost is paid once per batch.

    Only :data:`CATALOG_WRITE_OPS` are exposed. Reads are intentionally
    absent — callers that also read hold a separate
    :func:`make_catalog_reader` instance.
    """

    def __init__(
        self,
        *,
        config_dir: Optional[Path] = None,
        priority: Optional[str] = None,
    ) -> None:
        self._config_dir = config_dir
        self._client: Any = None
        self._direct: Optional[Catalog] = None
        self._routed = False
        # RDR-146 P2 (nexus-5p2ci.12): resolve once at construction (a writer
        # is long-lived; one resolve per batch). Interactive writers tag every
        # routed write so the daemon opens its fairness window; batch writers
        # send no priority field (daemon defaults batch). Resolution honours
        # NX_WRITE_PRIORITY, then the explicit ``priority`` arg, then isatty.
        from nexus.catalog.write_priority import resolve_write_priority
        self._priority = resolve_write_priority(priority)
        self._connect()

    def _connect(self) -> None:
        from nexus.daemon.t2_client import (
            T2DaemonNotReachableError,
            T2SchemaVersionMismatchError,
            make_t2_client,
        )

        client = None
        try:
            client = make_t2_client(config_dir=self._config_dir)
            client.database.hello()  # force lazy connect; raises if down/skewed
            self._client = client
            self._routed = True
            return
        except (T2DaemonNotReachableError, T2SchemaVersionMismatchError) as exc:
            if client is not None:
                client.close()
            # Documented-irreducible availability fallback (RDR-128 class):
            # with no reachable daemon, this process is the sole writer, so
            # a direct Catalog does not violate single-writer. Logged so the
            # degraded path is visible.
            _log.warning(
                "catalog_writer_daemon_unreachable_fallback",
                error=str(exc),
                hint="start the T2 daemon (`nx daemon t2 start`) to route catalog writes",
            )
            from nexus.config import catalog_path

            path = catalog_path()
            self._direct = Catalog(path, path / ".catalog.db")
            self._routed = False

    @property
    def routed(self) -> bool:
        """True when writes route through the daemon; False on direct fallback."""
        return self._routed

    @property
    def priority(self) -> str:
        """Resolved write priority (``"interactive"`` | ``"batch"``)."""
        return self._priority

    def is_interactive_write_pending(self) -> bool:
        """RDR-146 P2: True when the daemon reports an interactive catalog
        write window is open, so a background (batch) producer should yield.

        Routed through the same daemon connection as this writer's writes.
        Returns False on the direct fallback (no daemon => no cross-process
        catalog-write contention to mediate; the per-repo advisory lock
        handles two same-repo indexers) and on any probe transport error
        (fail-open: never block a write on a probe failure)."""
        if self._client is None:
            return False
        try:
            return bool(self._client.catalog.is_interactive_write_pending())
        except Exception:  # noqa: BLE001 — probe must never break a write
            _log.debug("catalog_interactive_probe_failed", exc_info=True)
            return False

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally, so the
        # instance attributes set in __init__ are never shadowed.
        if name not in CATALOG_WRITE_OPS:
            raise AttributeError(
                f"{name!r} is not a catalog write op; make_catalog_writer "
                f"exposes only the {len(CATALOG_WRITE_OPS)}-op whitelist. "
                f"For reads use make_catalog_reader()."
            )
        if self._client is not None:
            inner = getattr(self._client.catalog_write, name)
            if self._priority == "interactive":
                # Tag every routed write so the daemon opens / refreshes its
                # fairness window. Batch stays untagged (byte-identical wire).
                def _routed(*args: Any, **kwargs: Any) -> Any:
                    return inner(*args, _priority="interactive", **kwargs)

                return _routed
            return inner
        return getattr(self._direct, name)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._direct is not None:
            try:
                self._direct._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._direct = None

    def __enter__(self) -> "CatalogWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def make_catalog_writer(
    *, config_dir: Optional[Path] = None, priority: Optional[str] = None,
) -> Any:
    """Return a write-only catalog proxy (daemon-routed or direct fallback).

    In service mode (``NX_STORAGE_BACKEND_CATALOG=service`` or global
    ``NX_STORAGE_BACKEND=service``) returns a
    :class:`_ServiceCatalogWriter` that enforces the same
    :data:`CATALOG_WRITE_OPS` whitelist but forwards writes to the Java
    Postgres service via HTTP.  *priority* is ignored in service mode
    (the service enforces its own fairness).

    In SQLite mode (default) returns a :class:`CatalogWriter` that routes
    through the T2 daemon or falls back to a direct local Catalog.

    *priority* (RDR-146 P2) sets the interactive-vs-batch fairness intent:
    ``"interactive"`` tags writes so the daemon prioritises them over a
    background batch indexer; ``"batch"`` is the yielding background default.
    ``None`` resolves via ``NX_WRITE_PRIORITY`` env then ``isatty()``.
    """
    if _is_catalog_service_mode():
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        _log.debug("catalog_writer_service_mode")
        return _ServiceCatalogWriter(HttpCatalogClient())
    return CatalogWriter(config_dir=config_dir, priority=priority)


class _ServiceCatalogWriter:
    """Write-only proxy backed by :class:`HttpCatalogClient` in service mode.

    Enforces the same :data:`CATALOG_WRITE_OPS` whitelist as
    :class:`CatalogWriter`.  Reads are blocked.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name not in CATALOG_WRITE_OPS:
            raise AttributeError(
                f"{name!r} is not a catalog write op; _ServiceCatalogWriter "
                f"exposes only the {len(CATALOG_WRITE_OPS)}-op whitelist. "
                f"For reads use make_catalog_reader()."
            )
        return getattr(self._client, name)

    @property
    def routed(self) -> bool:
        return True

    @property
    def priority(self) -> str:
        return "batch"

    def is_interactive_write_pending(self) -> bool:
        return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "_ServiceCatalogWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
