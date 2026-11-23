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
# STALENESS TRADE-OFF (deliberate, not an accident): this context is cached
# for the PROCESS lifetime, not re-read per client -- so if the trust store
# changes ON DISK while the process is running (a renewed CA bundle behind
# an already-set SSL_CERT_FILE/SSL_CERT_DIR), the cached context will not
# pick that up until the process restarts, where a per-client context would
# have. This narrows to the SSL_CERT_FILE/SSL_CERT_DIR override path on
# cloud https:// (local http:// never uses verify= at all) -- accepted
# because the MCP server does restart and the alternative is paying this
# fix's ~0.32s/call cost back on every call.
_shared_ssl_context: ssl.SSLContext | None = None
_shared_ssl_context_lock = threading.Lock()


def get_shared_ssl_context() -> ssl.SSLContext:
    """Return the process-wide shared ``ssl.SSLContext``, building it on
    first use. See the comment above for why sharing is safe
    and why it does not weaken verification.

    It is also what keeps a long-lived process working after its install tree
    goes away: the CA bundle (``certifi.where()``, inside the generation the
    process started from) is read once, here, and never again, so a client
    rebuilt hours later does not touch the disk. The T1 scratch store rebuilt
    its client with a fresh context on a 401 heal and, once the bundle was
    gone, failed for the rest of the process (2026-11-21; it shares this
    context now).

    A bundle that cannot be read raises an ``OSError`` of the same type and
    errno that names the bundle's path: ``ssl`` reports a missing ``cafile``
    as a bare ``[Errno 2] No such file or directory``."""
    global _shared_ssl_context
    if _shared_ssl_context is None:
        with _shared_ssl_context_lock:
            if _shared_ssl_context is None:
                try:
                    _shared_ssl_context = httpx.create_ssl_context()
                except OSError as exc:
                    raise OSError(
                        exc.errno,
                        f"{exc.strerror or exc}: the TLS CA bundle could not be read (if it lived in an nx install "
                        "that was removed while this process ran, restart the session to use the current install)",
                        ca_bundle_path(),
                    ) from exc
    return _shared_ssl_context


def ca_bundle_path() -> str:
    """The CA bundle an ``httpx`` client with default arguments loads: ``SSL_CERT_FILE``, then
    ``SSL_CERT_DIR``, then certifi's bundle (httpx's own order)."""
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if value := os.environ.get(name):
            return value
    import certifi  # noqa: PLC0415 — deferred: only on the error path

    return certifi.where()
