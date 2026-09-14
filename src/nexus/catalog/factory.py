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

import time
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.service_handles import SharedClientSlot, cached_endpoint_key

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
# CAS-narrowed (nexus-u2u0n): the lock guards only resolving/evicting the
# singleton, never the call itself — see _SharedServiceCatalogHandle._call.
#
# nexus-w1ip: the singleton's state (lock, refcounts, pending-close,
# per-op stats) now lives on a :class:`~nexus.service_handles.SharedClientSlot`
# INSTANCE instead of module globals -- see that module's docstring for the
# full CAS/refcount contract, preserved verbatim here. Keyed by
# ``_service_catalog_endpoint_key`` so a rotated ``(base_url, token)`` pair
# (e.g. a fresh per-test tenant token) resolves a fresh client instead of
# silently reusing one built against the OLD identity -- the defect this
# bead closes, not just its test-isolation symptom.


def _build_service_catalog_client() -> Any:
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    return HttpCatalogClient()


def _close_service_catalog_client(client: Any) -> None:
    client.close()


def _service_catalog_endpoint_key_uncached() -> tuple[str, str]:
    """The ``(base_url, token)`` a fresh ``HttpCatalogClient()`` would
    resolve RIGHT NOW (nexus-w1ip). A mismatch against the key the
    currently-held client was built under evicts it, so a rotated token
    or endpoint is picked up automatically instead of silently answering
    under the wrong identity. Uncached — wrapped by
    :data:`_service_catalog_endpoint_key` below, which is what the slot
    actually receives."""
    from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deferred to avoid import cycle

    return resolve_service_endpoint()


def _service_catalog_is_env_pinned() -> bool:
    """Cheap freshness check for the TTL wrapper below — see
    ``nexus.db.service_endpoint.is_endpoint_env_pinned``'s docstring."""
    from nexus.db.service_endpoint import is_endpoint_env_pinned  # noqa: PLC0415 — deferred to avoid import cycle

    return is_endpoint_env_pinned()


#: TTL-memoized ``endpoint_key`` (nexus-w1ip review round, finding (b)):
#: the uncached resolver's lease-file fallback is real disk I/O on a path
#: this module's own docstring calls hot (manifest write_many, the
#: RUNFENCE begin_index_run_many, every catalog op). ``is_fresh_required``
#: is the env/config-pinned check, so that CHEAP path is never delayed by
#: the cache — only the genuinely I/O-bound lease-file fallback is
#: memoized. See :func:`~nexus.service_handles.cached_endpoint_key` /
#: :data:`~nexus.service_handles.DEFAULT_ENDPOINT_KEY_CACHE_TTL_S` for the
#: TTL rationale, and that same module's ``resolve_and_apply``/
#: ``resolve_and_acquire`` for why this is called OUTSIDE the slot's lock.
_service_catalog_endpoint_key = cached_endpoint_key(
    _service_catalog_endpoint_key_uncached,
    is_fresh_required=_service_catalog_is_env_pinned,
)


#: Default process-lifetime slot. ``make_catalog_reader``/``make_catalog_writer``
#: and any ``_SharedServiceCatalogHandle()`` constructed with no explicit
#: *slot* argument resolve against this instance — existing callers work
#: unchanged.
_default_catalog_slot: SharedClientSlot = SharedClientSlot(
    _build_service_catalog_client,
    _close_service_catalog_client,
    endpoint_key=_service_catalog_endpoint_key,
)


def reset_shared_service_catalog_client_for_tests(
    slot: Optional[SharedClientSlot] = None,
) -> None:
    """Close and clear the shared SERVICE-mode catalog client (tests only).

    *slot* defaults to the process's shared default slot; pass an explicit
    one to reset a test-injected slot instead.
    """
    (slot if slot is not None else _default_catalog_slot).reset_for_tests()


def service_catalog_op_stats(
    slot: Optional[SharedClientSlot] = None,
) -> dict[str, dict[str, float]]:
    """Snapshot of per-op shared-catalog-handle timings (nexus-jb4pp).

    ``{op: {"calls": n, "lock_wait_s": s, "call_s": s}}`` where
    ``lock_wait_s`` is time blocked on the slot's resolution lock before
    the call began and ``call_s`` is the forwarded call itself (client
    serialization + network + server). Cumulative across threads, so both
    may exceed wall clock.
    """
    return (slot if slot is not None else _default_catalog_slot).op_stats()


def reset_service_catalog_op_stats(slot: Optional[SharedClientSlot] = None) -> None:
    """Zero the per-op counters (review finding on nexus-jb4pp): the stats
    dict is a process-lifetime slot, so a process that runs more than one
    index pass (MCP tool, watch mode, a multi-indexing pytest session)
    would otherwise report cumulative-since-process-start numbers with no
    signal — the same mis-attribution class this instrumentation exists to
    kill. ``_run_index`` calls this at start so every
    ``index_catalog_op_stats`` event covers exactly one run."""
    (slot if slot is not None else _default_catalog_slot).reset_op_stats()


class _SharedServiceCatalogHandle:
    """Read-facing proxy over the shared SERVICE-mode ``HttpCatalogClient``.

    ``close()`` is deliberately a no-op — callers historically closed a
    fresh-per-call client; the shared client outlives any single caller and
    is only torn down via error-triggered eviction or
    :func:`reset_shared_service_catalog_client_for_tests`.

    CAS-NARROWED (nexus-u2u0n): the slot's resolution lock is held ONLY long
    enough to resolve (get-or-build) the current client — never across the
    forwarded call's own network round trip. ``_call`` resolves under the
    lock, releases, makes the call, and on failure re-acquires the lock to
    decide whether to evict.

    REFCOUNTED (nexus-0dpli, critique finding on the first cut of this
    narrowing): releasing the lock around the round trip means MULTIPLE
    threads can be genuinely mid-call against the SAME shared instance at
    once — proven by ``test_call_releases_lock_before_network_round_trip``.
    An eviction that simply ``close()``s the instance it just CAS'd out of
    the shared slot can therefore tear down a sibling's still-in-flight
    call (empirically confirmed: closing a live ``httpx.Client`` from
    another thread aborts a concurrent in-flight request on it). The fix
    is two-part:

    1. EVICTION TRIGGER IS NARROW, not "any exception". Only a genuine
       connectivity failure (:func:`nexus.retry._is_connectivity_error` —
       ``httpx.TransportError``/``ConnectionError``/``TimeoutError``,
       including chained causes) evicts. A routine domain outcome (e.g.
       ``IndexRunVerifyRefused``'s 409) propagates WITHOUT touching the
       shared client — eviction exists to recover from a singleton whose
       underlying connections are broken, not to react to an expected
       per-doc refusal. Under-evict, never over-evict: a wrongly-KEPT
       stale client fails loudly on its next call and gets evicted then;
       a wrongly-CLOSED healthy client silently aborts an innocent
       sibling's call right now.
    2. EVICTION DOES NOT CLOSE OUT FROM UNDER IN-FLIGHT SIBLINGS. Every
       caller acquires an in-flight reference (:func:`_acquire_shared_catalog_ref`)
       on the instance it resolved, under the SAME lock as the resolve, and
       releases it (:func:`_release_shared_catalog_ref`) under the lock
       once its own call returns or raises. An eviction clears the shared
       SLOT immediately (so new callers always build fresh — never
       observe a doomed instance) but only physically closes the OLD
       instance once its reference count has drained to zero — either the
       evicting caller itself (no one else was using it) or whichever
       sibling's release happens to be the last one out. This is a
       COMPARE-AND-SWAP OF THE SLOT plus a REFCOUNTED, DEFERRED CLOSE — a
       resolver arriving after the slot is cleared always builds fresh,
       and no thread ever observes (or is issued) a call against an
       instance already mid-``close()``.

    RELEASE IS UNCONDITIONAL (nexus-0dpli round 3, delta-review touch-up):
    the reference release lives in a single ``finally``, not duplicated
    across ``except``/``else`` branches — a ``BaseException`` that is not
    a plain ``Exception`` (``KeyboardInterrupt``/``SystemExit`` mid-call)
    must still release this call's reference, or it leaks forever and can
    strand a sibling's already-evicted, pending-close instance with no one
    left to drain it to zero. The eviction decision (``_evict``) defaults
    to ``False`` and is set ``True`` only inside ``except Exception`` —
    so a non-``Exception`` ``BaseException`` correctly releases WITHOUT
    evicting, the same safe default as no exception at all.

    nexus-w1ip: the lock, refcounts, pending-close set and per-op stats
    this docstring describes now live on a
    :class:`~nexus.service_handles.SharedClientSlot` instance (``self._slot``)
    rather than module globals — the CAS/refcount/eviction semantics above
    are unchanged, only where the state is held. A slot also auto-evicts
    on a stale ``(base_url, token)`` — see
    :func:`_service_catalog_endpoint_key` — which this class gets for
    free via :meth:`SharedClientSlot.resolve_and_apply` /
    :meth:`SharedClientSlot.resolve_and_acquire`.
    """

    def __init__(self, slot: Optional[SharedClientSlot] = None) -> None:
        self._slot: SharedClientSlot = slot if slot is not None else _default_catalog_slot

    def __getattr__(self, name: str) -> Any:
        # nexus-jb4pp: this acquisition is NOT a formality — before the
        # nexus-u2u0n narrowing it could block for the full duration of any
        # in-flight call on another thread, since _call held the same lock
        # across its network round trip. Instrumenting only _call read a
        # near-zero wait while threads were demonstrably serializing,
        # because they were queueing HERE. A timer that measures the wrong
        # side of a convoy is worse than none. Kept post-narrowing: this
        # resolution is in-process (no round trip), so the wait it can
        # still show is queueing behind ANOTHER thread's brief resolution,
        # not behind a network call.
        #
        # resolve_and_apply folds the getattr INTO the same locked section
        # as the resolve (may raise -- e.g. local-mode-only ._db -- and
        # propagates untouched), preserving the pre-nexus-w1ip convoy
        # semantics this comment describes.
        attr, _wait = self._slot.resolve_and_apply(lambda client: getattr(client, name))
        _non_callable = not callable(attr)
        if _non_callable:
            # Recorded via the slot's own lock — the counters are plain
            # floats whose only mutual exclusion is that lock.
            self._slot.record_op(name, _wait, 0.0, calls=0)
            return attr

        def _call(*args: Any, **kwargs: Any) -> Any:
            current, _wait2 = self._slot.resolve_and_acquire()
            _c0 = time.monotonic()
            _evict = False
            try:
                return getattr(current, name)(*args, **kwargs)
            except Exception as exc:
                # Only a genuine connectivity failure evicts — see the
                # class docstring's point 1. ``_evict`` stays False (the
                # safe default) for any OTHER exception, including a
                # BaseException that skips this clause entirely
                # (KeyboardInterrupt/SystemExit — see the class docstring's
                # "RELEASE IS UNCONDITIONAL" note).
                from nexus.retry import _is_connectivity_error  # noqa: PLC0415 — deferred to avoid import cost on the happy path
                _evict = _is_connectivity_error(exc)
                raise
            finally:
                # nexus-0dpli round 3: release lives in ONE unconditional
                # finally, not duplicated across except/else — a
                # BaseException that _is_connectivity_error never sees
                # (KeyboardInterrupt/SystemExit) must still release this
                # call's reference, or it leaks and can strand a sibling's
                # already-evicted, pending-close instance forever.
                self._slot.release(current, evict=_evict)
                self._slot.record_op(name, _wait + _wait2, time.monotonic() - _c0)

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
#: nexus-fduai: ``record_gc_audit`` is the client-facing gc_audit producer
#: (``POST /v1/catalog/gc_audit/record``) ``nx t3 gc`` reports its own T3
#: delete through — append-only, engine-side, no local equivalent ever.
#: nexus-8tnz2: ``delete_collection`` (RDR-164 P2, http_catalog_client.py's
#: atomic single-transaction collection delete) was reachable on
#: ``HttpCatalogClient`` since its own introduction, and it was ALREADY
#: fully reachable — ungated — through ``_get_catalog()`` /
#: ``make_catalog_reader()`` the whole time: ``_SharedServiceCatalogHandle.
#: __getattr__`` (the reader's proxy) forwards ANY attribute name to the
#: underlying client with no whitelist at all (see that class's own
#: docstring). This frozenset gates ONLY the ``_get_catalog_writer()`` /
#: ``_ServiceCatalogWriter`` path — it is not, and has never been, a
#: safety boundary on the reader side. What this entry actually fixes:
#: ``delete_collection`` was simply ABSENT from this WRITER-side whitelist
#: — the exact nexus-jk88j/nexus-67qsd omission class this module's own
#: comments warn about — unexercised only because nothing called it
#: through the writer until the reconcile-stale ``drop-orphan-collections``
#: arm chose to route through ``_get_catalog_writer()`` deliberately
#: (matching the writer-vs-reader discipline every other mutation in this
#: codebase already follows) rather than through the always-open reader.
#: No SQLite/daemon-mode equivalent (the local catalog is gone, RDR-158
#: P4) — same rationale as ``delete_many``/``purge_trash`` above.
#:
#: nexus-dkymw: ``restore_document`` (the operator-facing caller for
#: ``nexus.document_restore``, catalog-003-soft-delete.xml — the verb
#: nexus-xavu7 found missing) joins this set for the identical reason as
#: ``delete_many``/``purge_trash``/``delete_collection``: it is a
#: service-only op with no SQLite/daemon-mode equivalent, introduced long
#: after the local catalog died (RDR-158 P4), so it never had a canonical
#: ``Catalog`` counterpart to mirror on ``CatalogWriter`` — same disposition
#: as its sibling reclaim verb ``purge_trash`` above, deliberately NOT added
#: to the shared ``CATALOG_WRITE_OPS``/``catalog_protocol.py`` Protocol pair
#: (which requires a matching canonical ``Catalog`` method to fidelity-test
#: parameter shapes against — see ``test_catalog_protocol_fidelity.py``).
_SERVICE_ONLY_WRITE_OPS: frozenset[str] = frozenset({
    "update_many", "delete_many", "purge_trash", "record_gc_audit",
    "delete_collection", "restore_document",
})


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


def make_catalog_writer_for_endpoint(
    *, base_url: str, token: str, tenant: str, client: Any | None = None,
) -> Any:
    """A catalog writer bound to an EXPLICIT endpoint, bearer and tenant.

    For a store client that was itself constructed against a pinned
    ``base_url``/``_token`` (the chash integration harness, tenant tooling)
    and needs to register a collection before a write. The shared writer
    from :func:`make_catalog_writer` resolves its endpoint from the ambient
    environment, so a pinned client that used it registered on whichever
    engine the environment named and then wrote to its own (nexus-w1ip
    follow-up, 2026-09-14: chash rename 422 "not registered" once the slot's
    endpoint-key eviction stopped hiding it). Returns a live
    ``HttpCatalogClient`` the caller must ``close()``; this function and
    :func:`make_catalog_client_for_migration` are the only authorised
    construction sites outside the shared slot (seam audit,
    tests/catalog/test_http_catalog_client.py). *client*, when given, is the
    caller's own ``httpx.Client`` (a store's pool or a test's mocked
    transport); the writer then never owns or closes it.
    """
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    return HttpCatalogClient(base_url=base_url, tenant=tenant, _token=token, client=client)


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
        # code-review WORTH-TRACKING (nexus-wrwb7 fix pass, nexus-ssqk9
        # relay): deliberately stays fully pinned, unlike the no-base_url
        # branch below. This branch exists specifically for a caller that
        # names a NON-DEFAULT target endpoint (module docstring: "a
        # specific Postgres service endpoint that may differ from the
        # configured default"). A configured mint_token credential is
        # scoped to the DEFAULT managed engine's mint contract; self-minting
        # against it and presenting the result to a DIFFERENT, explicitly-
        # named migration target would be silently wrong, not an
        # improvement — so this call site does NOT apply the data-token
        # override. No live call site passes base_url= today (grepped
        # 2026-08-16); revisit if one appears and genuinely wants
        # self-minting against its own explicit target.
        return HttpCatalogClient(base_url=base_url, _token=token)
    if not token:
        return HttpCatalogClient()
    # code-review Sig#1 addendum (nexus-ssqk9): this branch resolves the
    # SAME default managed endpoint every other T2 store uses (no base_url
    # override), so it gets the identical treatment as the SessionEnd
    # summary fix above — apply the data-token override BEFORE
    # construction so a configured mint_token credential is not silently
    # skipped on this low-traffic migration path either.
    from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    resolved_base_url, _ = resolve_service_endpoint()
    data_token = get_data_token_manager().bearer_for(resolved_base_url, DEFAULT_TENANT)
    return HttpCatalogClient(
        base_url=resolved_base_url,
        _token=data_token if data_token is not None else token,
    )
