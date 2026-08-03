# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed reader / writer factories for the catalog (RDR-146 P1.2, service-only
since nexus-i711w).

The catalog lives in the Java Postgres engine; every reader and writer this
module hands out is backed by :class:`HttpCatalogClient`. The read/write split
is TOOLING-ENFORCED, not convention:

  - :func:`make_catalog_reader` -> a read-facing proxy over the shared
    service client (:class:`_SharedServiceCatalogHandle`).

  - :func:`make_catalog_writer` -> a write-only proxy
    (:class:`_ServiceCatalogWriter`) exposing ONLY the whitelisted
    :data:`CATALOG_WRITE_OPS` (+ service-only batch ops), so a
    dataclass-returning read can never accidentally round-trip the wire.

Mixed sites (read AND write) hold BOTH a reader and a writer. That is the
gate-resolved design (re-gate Critical): the two typed factories make the
read/write distinction visible and enforceable.

History: through RDR-158 P4 these factories fronted a local SQLite
``.catalog.db`` (reader: ``mode=ro`` local Catalog; writer: T2-daemon-routed
with a direct in-process fallback). The daemon died in nexus-i711w sub-stage B
and the local SQLite catalog itself in the terminal i711w deletion; the
``make_catalog_admin`` third factory died earlier (Hal ruling 2026-07-29,
GH #1419.4 split-brain: at one backup timestamp ``.catalog.db`` showed
532 docs / 13 links against PG's 592 / 52).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS

_log = structlog.get_logger(__name__)

# nexus-53x7s / nexus-5en9j: SERVICE-mode catalog reader/writer share ONE
# process-lifetime HttpCatalogClient instead of constructing (and
# immediately closing) one per make_catalog_reader()/make_catalog_writer()
# call. This was the LARGEST single reconstruction count in the nexus-53x7s
# shakeout evidence (394x http_catalog_client.init in one run) -- larger
# than any of the T2Database substores that bead's first fix addressed.
#
# The fresh-per-call docstrings on make_catalog_reader/make_catalog_writer
# are SQLite-mode reasoning (avoid accumulating local WAL read locks / write
# handles across a long-lived MCP process) that does not apply to
# HttpCatalogClient -- it owns pooled httpx.Client connections, exactly the
# same shape as the T2 Http*Store classes _service_t2_write_locked already
# fixed in mcp_infra.py. Same design here: a process-lifetime singleton,
# guarded by one lock held for the full call (not just checkout) so a
# concurrent caller can never close() an instance mid-call, with reactive
# eviction on any call failure (self-heals against a rotated
# storage_service lease without polling a TTL clock).
_service_catalog_lock = threading.Lock()
_service_catalog_client: Any = None


def _get_shared_service_catalog_client() -> Any:
    global _service_catalog_client
    if _service_catalog_client is None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        _service_catalog_client = HttpCatalogClient()
    return _service_catalog_client


def reset_shared_service_catalog_client_for_tests() -> None:
    """Close and clear the shared SERVICE-mode catalog client (tests only)."""
    global _service_catalog_client
    with _service_catalog_lock:
        if _service_catalog_client is not None:
            _service_catalog_client.close()
        _service_catalog_client = None


class _SharedServiceCatalogHandle:
    """Read-facing proxy over the shared SERVICE-mode ``HttpCatalogClient``.

    ``close()`` is deliberately a no-op — callers historically closed a
    fresh-per-call client; the shared client outlives any single caller and
    is only torn down via error-triggered eviction or
    :func:`reset_shared_service_catalog_client_for_tests`. Every attribute
    access is resolved against the CURRENT shared client under the shared
    lock (held for the full call, not just checkout) so a concurrent
    caller's eviction-on-error can never race a call in flight, and a
    failing call evicts the client so the next call rebuilds against a
    freshly-resolved endpoint.
    """

    def __getattr__(self, name: str) -> Any:
        with _service_catalog_lock:
            client = _get_shared_service_catalog_client()
            attr = getattr(client, name)  # may raise (e.g. local-mode-only ._db) — let it propagate untouched
        if not callable(attr):
            return attr

        def _call(*args: Any, **kwargs: Any) -> Any:
            global _service_catalog_client
            with _service_catalog_lock:
                current = _get_shared_service_catalog_client()
                try:
                    return getattr(current, name)(*args, **kwargs)
                except Exception:
                    if _service_catalog_client is current:
                        current.close()
                        _service_catalog_client = None
                    raise

        return _call

    def close(self) -> None:
        pass  # nexus-5en9j: shared instance outlives any single caller


def _is_catalog_service_mode() -> bool:
    """Return True — the catalog is service-backed in every mode.

    Collapsed to a constant by the terminal i711w deletion (the local
    SQLite catalog no longer exists). Kept as a function because tests
    and callers patch/probe it by name.
    """
    return True


def make_catalog_reader(*, config_dir: Optional[Path] = None) -> Optional[Any]:
    """Return a read-facing catalog proxy backed by the service.

    Returns a :class:`_SharedServiceCatalogHandle` forwarding reads to the
    Java Postgres service. The client is always considered "initialised" —
    if the service is unreachable, the first HTTP call will raise.

    The ``Optional`` return annotation is historical (the deleted SQLite
    leg returned ``None`` when the catalog dir was uninitialised); callers'
    None-guards are now dead but harmless.

    The resolver call is validation only (RDR-158 P3/Stage 5): with the
    local catalog deleted, no seam resolved ``storage_backend_for("catalog")``
    any more, so a stranded ``NX_STORAGE_BACKEND_CATALOG=sqlite`` export was
    silently ignored — the exact silent-ignore the fail-loud directive bans.
    The factory is where every catalog consumer routes, so it fails here
    with the stranded-install redirect.
    """
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred to avoid import cycle

    storage_backend_for("catalog")
    _log.debug("catalog_reader_service_mode")
    return _SharedServiceCatalogHandle()


def make_catalog_writer(
    *, config_dir: Optional[Path] = None, priority: Optional[str] = None,
) -> Any:
    """Return a write-only catalog proxy backed by the service.

    Returns a :class:`_ServiceCatalogWriter` that enforces the
    :data:`CATALOG_WRITE_OPS` whitelist and forwards writes to the Java
    Postgres service via HTTP. *priority* is ignored (the service enforces
    its own fairness); the parameter survives for call-site compatibility.
    The resolver call is validation only — see :func:`make_catalog_reader`.
    """
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred to avoid import cycle

    storage_backend_for("catalog")
    _log.debug("catalog_writer_service_mode")
    return _ServiceCatalogWriter(_SharedServiceCatalogHandle())


#: nexus-xedhp: extra ops allowed ONLY on the service-mode writer, layered on
#: top of CATALOG_WRITE_OPS rather than added to that shared whitelist. The
#: SQLite/daemon-mode CatalogWriter (below) has no ``update_many`` RPC op in
#: its dispatch table; adding it to the shared CATALOG_WRITE_OPS would make
#: ``getattr(writer, "update_many", None)`` return a bound proxy method there
#: too (CatalogWriter's __getattr__ forwards ANY whitelisted name to a
#: dynamic RPC proxy without validating the daemon actually implements it),
#: defeating the ``callable(getattr(cat, "update_many", None))`` capability
#: check the indexer's catalog hook uses to decide whether to batch — it
#: would look supported and then fail deep in the per-file loop instead of
#: safely falling back. Keeping this service-only means the same capability
#: check is honest for both backends: SQLite mode always falls back to the
#: existing serial ``update()`` loop (unchanged behaviour); service mode
#: gets the batched path.
#: nexus-3ck2g: ``purge_trash`` joins this set for the same reason as
#: ``update_many``/``delete_many`` above — it is a service-only op with no
#: SQLite/daemon-mode equivalent (the local catalog and its daemon are gone,
#: RDR-158 P4). It is service-only for a second, independent reason too: the
#: dry-run COUNT PREVIEW is itself an engine-side read behind the write
#: surface (``purge_trash(dry_run=True)``), not something a caller could
#: compute client-side, so it belongs on the writer even for its read-only
#: mode. Reads never go through ``make_catalog_writer()`` — see the module
#: docstring — so this whitelist entry does not create a reader-through-
#: writer path; it just means the *dry-run preview itself* is a writer op.
_SERVICE_ONLY_WRITE_OPS: frozenset[str] = frozenset({"update_many", "delete_many", "purge_trash"})


class _ServiceCatalogWriter:
    """Write-only proxy backed by :class:`HttpCatalogClient` in service mode.

    Enforces the same :data:`CATALOG_WRITE_OPS` whitelist as
    :class:`CatalogWriter`, plus :data:`_SERVICE_ONLY_WRITE_OPS`. Reads are
    blocked.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name not in CATALOG_WRITE_OPS and name not in _SERVICE_ONLY_WRITE_OPS:
            raise AttributeError(
                f"{name!r} is not a catalog write op; _ServiceCatalogWriter "
                f"exposes only the {len(CATALOG_WRITE_OPS)}-op whitelist "
                f"(+ {sorted(_SERVICE_ONLY_WRITE_OPS)}). "
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


def make_catalog_client_for_migration(
    *,
    base_url: Optional[str] = None,
    token: str = "",
) -> Any:
    """Return an :class:`HttpCatalogClient` for the ``storage migrate catalog`` ETL.

    This is the sole authorised site for constructing an ``HttpCatalogClient``
    with an explicit *base_url* and *token* outside the service-mode defaults.
    Migration needs direct control over the target URL because it runs against
    a specific Postgres service endpoint that may differ from the configured
    default (e.g. a fresh staging instance during an initial data load).

    RDR-176 P2 (Gap 3): the primary callers now pass NO arguments — the CLI
    migrate subcommands resolve ``(base_url, token)`` config-first themselves and
    pass both, while ``migrate all`` / the orchestrator call this no-arg so the
    client resolves URL+token config-first via ``resolve_service_endpoint``. The
    explicit-args form remains for a caller that must target a non-default URL.

    Args:
        base_url: Override the service URL.  ``None`` falls back to the
            client's built-in env/config resolution (``NX_SERVICE_URL``).
        token: Bearer token for ``X-Nexus-Token`` authentication.
            Required; the caller is responsible for sourcing it.

    Returns:
        A live ``HttpCatalogClient`` configured for *base_url* / *token*.
        Callers must call ``.close()`` or use it as a context manager.
    """
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    _log.debug("catalog_client_for_migration", base_url=base_url)
    if base_url:
        return HttpCatalogClient(base_url=base_url, _token=token)
    return HttpCatalogClient(_token=token) if token else HttpCatalogClient()
