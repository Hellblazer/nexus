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


def make_catalog_reader(*, config_dir: Optional[Path] = None) -> Optional[Catalog]:
    """Return a read-only local Catalog, or ``None`` when uninitialised.

    Mirrors the historical ``get_catalog()`` contract (None when the
    catalog dir is not initialised) so call sites keep their existing
    None-guards. The returned Catalog performs no construction-time
    writes and opens its SQLite handle read-only.
    """
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    return Catalog(path, path / ".catalog.db", read_only=True)


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

    def __init__(self, *, config_dir: Optional[Path] = None) -> None:
        self._config_dir = config_dir
        self._client: Any = None
        self._direct: Optional[Catalog] = None
        self._routed = False
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
            return getattr(self._client.catalog_write, name)
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


def make_catalog_writer(*, config_dir: Optional[Path] = None) -> CatalogWriter:
    """Return a write-only catalog proxy (daemon-routed or direct fallback)."""
    return CatalogWriter(config_dir=config_dir)
