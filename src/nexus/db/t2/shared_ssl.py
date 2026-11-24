# SPDX-License-Identifier: AGPL-3.0-or-later
"""The process-wide client ``ssl.SSLContext`` every nexus ``httpx`` client shares.

Moved out of ``nexus.db.t2._refreshable_client`` (which re-exports it as
``_get_shared_ssl_context``) when the T1 scratch store and the token-admin
store started sharing it too: a per-client context re-reads the CA bundle
from the install tree on every client build, and a long-lived MCP server
whose install generation was removed then failed its next rebuild with a
pathless ``[Errno 2] No such file or directory`` (2026-11-21).
"""

from __future__ import annotations

import os
import ssl
import threading

import httpx

# Every httpx.Client() a store constructs eagerly builds its OWN
# ssl.SSLContext at construction time -- httpx.Client.__init__ ->
# HTTPTransport.__init__ -> httpx.create_ssl_context() ->
# ssl.create_default_context(cafile=certifi.where()) -- UNCONDITIONALLY,
# even for a plain http:// base_url where TLS is never actually used
# (verify= only matters once a connection to an https:// host is opened;
# httpx.Client has no way to know the scheme at construction time). Each
# nx_answer call constructs 5 separate T2Database instances (mcp_infra.t2_ctx()
# returns a fresh one per call -- see that function's docstring), each of
# which builds 8 HTTP domain stores = 40 httpx.Client()s per call. Measured:
# ~90% of a ~0.4s steady-state nx_answer call was this redundant SSL-context
# construction alone (40 raw ssl.create_default_context() calls = 0.347s of
# a 0.410s total for 40 httpx.Client() constructions; the same 40
# constructions sharing one pre-built context measured at 0.025s).
#
# Fix: build ONE ssl.SSLContext per process, via the PUBLIC httpx.create_ssl_context()
# (exported in httpx's own __all__, not a private _config reach-in) with its
# own default arguments (verify=True, cert=None, trust_env=True) -- this is
# byte-identical to what each httpx.Client() used to build for itself (same
# SSL_CERT_FILE -> SSL_CERT_DIR -> certifi.where() precedence), so trust
# store, verification behavior, and failure modes on a bad certificate are
# unchanged; only the redundant per-client rebuild is eliminated.
#
# Sharing ONE ssl.SSLContext across many concurrent httpx.Client instances is
# a supported, unremarkable pattern, not an accident that happens to work:
# it is the SAME sharing httpx/httpcore already do internally for a single
# Client's own connection pool (every connection that Client opens reuses
# the one ssl_context the Client built for itself at construction) --
# widening the audience to multiple Client instances changes nothing about
# how the object is used, only how many readers share it. Nothing in this
# module (or elsewhere in the codebase) calls a mutating method
# (load_cert_chain, set_ciphers, ...) on the shared context after
# construction -- it is built once and only ever read (wrap_socket) from
# then on, which is the read-only usage ssl.SSLContext is designed for.
#
# Double-checked locking (mirrors mcp_infra.py's _t3_instance/_t3_lock
# pattern) because T2 domain stores can be constructed from multiple
# threads concurrently (the aspect-worker background thread, concurrent MCP
# tool calls each constructing their own T2Database).
#
# KEYED ON THE TRUST ENVIRONMENT, as ``nexus.db.pooled_http._shared_ssl_context``
# is: the context is rebuilt when ``SSL_CERT_FILE``, ``SSL_CERT_DIR`` or the
# default-context factory (``ssl._create_default_https_context``, the documented
# override point) changes, so an operator's change of trust store reaches every
# client without a restart. A changed trust store on disk behind an unchanged
# variable is still not re-read (the read-once property is the point). A
# rebuild that fails keeps the context already in use: a process that was
# working keeps working; only the first build can fail a caller.
_slot: tuple[tuple[object, ...], ssl.SSLContext] | None = None
_failed_key: tuple[object, ...] | None = None
_lock = threading.Lock()


def _trust_key() -> tuple[object, ...]:
    return (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("SSL_CERT_DIR"),
        ssl._create_default_https_context,  # noqa: SLF001 — the documented override point
    )


def get_shared_ssl_context() -> ssl.SSLContext:
    """Return the process-wide shared ``ssl.SSLContext``, building it on
    first use. See the comment above for why sharing is safe
    and why it does not weaken verification.

    It is also what keeps a long-lived process working after its install tree
    goes away: the CA bundle (``certifi.where()``, inside the generation the
    process started from) is read once, here, and never again while the trust
    environment is unchanged, so a client rebuilt hours later does not touch the
    disk. The T1 scratch store rebuilt its client with a fresh context on a 401
    heal and, once the bundle was gone, failed for the rest of the process
    (2026-11-21; it shares this context now).

    A first build that fails raises: an ``ssl.SSLError`` (a corrupt bundle)
    unchanged, with a note naming the bundle; any other ``OSError`` as the same
    type and errno with the bundle's path, because ``ssl`` reports a missing
    ``cafile`` as a bare ``[Errno 2] No such file or directory``."""
    global _slot, _failed_key
    key = _trust_key()
    slot = _slot
    if slot is not None and slot[0] == key:
        return slot[1]
    with _lock:
        slot = _slot
        if slot is not None and (slot[0] == key or _failed_key == key):
            return slot[1]  # unchanged, or a change already known not to build: keep what works
        try:
            ctx = httpx.create_ssl_context()
        except OSError as exc:
            if slot is not None:
                _failed_key = key
                return slot[1]
            raise _named(exc) from exc
        _slot, _failed_key = (key, ctx), None
        return ctx


def _named(exc: OSError) -> OSError:
    """*exc* with the CA bundle's path in it (see :func:`get_shared_ssl_context`)."""
    path = ca_bundle_path()
    if isinstance(exc, ssl.SSLError):
        exc.add_note(f"the TLS CA bundle at {path} could not be loaded")
        return exc
    return OSError(
        exc.errno,
        f"{exc.strerror or exc}: the TLS CA bundle could not be read (if it lived in an install generation "
        "that was removed while this process ran, restart the session to use the current one)",
        path,
    )


def ca_bundle_path() -> str:
    """The CA bundle an ``httpx`` client with default arguments loads: ``SSL_CERT_FILE``, then
    ``SSL_CERT_DIR``, then certifi's bundle (httpx's own order)."""
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if value := os.environ.get(name):
            return value
    import certifi  # noqa: PLC0415 — deferred: only on the error path

    return certifi.where()
