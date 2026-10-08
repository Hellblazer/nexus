# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Persistent-connection transport for the vector client (nexus-qjjlz).

``urllib``'s ``AbstractHTTPHandler.do_open`` forces ``Connection: close`` and
closes the socket after every response, and ``http_vector_client`` built a
fresh opener per request, so every ``/v1/vectors`` call paid a TCP (+TLS)
handshake and started TCP slow-start cold: measured against the real cloud,
0.23-0.43 s on a new connection against 0.073 s on a reused one, and a search
issues ~28 such requests, many of them in parallel.

This module keeps urllib's machinery (proxy resolution from the environment,
redirects, the ``HTTPErrorProcessor`` that raises ``urllib.error.HTTPError``)
and replaces only the one step that opens and discards a connection. The
handler classes below override ``do_open`` to take a connection from a
process-wide pool keyed by the RESOLVED endpoint, so:

* the error taxonomy is urllib's own, unchanged: a failed connect or send is
  ``URLError(OSError)``, a failure while waiting for the status line is the raw
  ``ConnectionResetError`` / ``RemoteDisconnected`` / ``TimeoutError``, and a
  non-2xx status is ``HTTPError`` with a readable body and headers;
* endpoint rotation (RDR-149 lease rotation) still works: the endpoint is
  resolved on every request, a changed endpoint is a different pool key, and
  the stale key's idle connections age out;
* the pool is thread-safe (search fans out on 8 threads) and fork-safe
  (reset in the child, so a pooled socket is never shared across a fork);
* a connection the server closed while it sat idle is replaced transparently:
  one retry on a fresh connection, only when the failure is the "peer closed
  before answering" family, the connection was reused, and the failure came
  within seconds of the send;
* responses are read eagerly and decoded (``Content-Encoding: gzip``), then
  the connection goes back to the pool in the same call. Every caller read the
  whole body anyway.

Imports of ``urllib.request`` are deferred to :func:`build_opener` (module-load
cost; see the deferred-import convention in ``http_vector_client``).
"""
from __future__ import annotations

import http.client
import io
import os
import select
import threading
import time
import zlib
from collections.abc import Callable
from typing import Any

#: Idle connections kept per endpoint. Search fans out on 8 threads; the cap
#: only bounds what is retained, never what is opened.
MAX_IDLE_PER_ENDPOINT = 16

#: A pooled connection idle longer than this is discarded at checkout rather
#: than risked. Well below the idle timeouts of the managed edge (ALB default
#: 60 s) and the engine; the dead-peer probe and the one-shot retry cover the
#: remainder.
MAX_IDLE_SECONDS = 30.0

#: A reused connection that fails this soon after the send was closed by the
#: server while idle. Later than this the server may have been working on the
#: request, and replaying it is the caller's retry policy, not ours.
_STALE_FAILURE_WINDOW_S = 5.0

#: The "peer closed before answering" family: what a request on a reused
#: connection raises when the server dropped it while idle. Windows reports
#: an abort as ConnectionAbortedError, a sibling of ConnectionResetError.
_STALE_ERRORS: tuple[type[BaseException], ...] = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)

#: ``(is_https, host[:port], tunnel_host, ssl-context identity)``. ``host`` is
#: the PROXY when a proxy is in play, so a proxy change is a different key.
_Key = tuple[bool, str, str | None, int | None]

OnConnect = Callable[[Any], None]


def _looks_dead(sock: Any) -> bool:
    """True when an idle pooled socket is already readable: an idle HTTP
    connection has nothing to say, so readable means the peer sent FIN/RST
    (or stray bytes), and either way it is not reusable. Best-effort."""
    if sock is None:
        return True
    try:
        if sock.fileno() < 0:
            return True
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable)
    except ValueError:  # fd beyond select()'s range: cannot tell; the retry covers it
        return False
    except OSError:
        return True


def _quick(sent_at: float) -> bool:
    return time.monotonic() - sent_at < _STALE_FAILURE_WINDOW_S


def _close_quietly(conn: http.client.HTTPConnection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — closing a dead socket must never raise
        pass


class _ConnectionPool:
    """Idle connections by endpoint key, LIFO (the warmest connection first).

    Only IDLE connections live here: a connection is removed on checkout and
    re-added on release, so one thread owns it for the whole exchange.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[_Key, list[tuple[http.client.HTTPConnection, float]]] = {}
        self._pid = os.getpid()

    # -- lifecycle ----------------------------------------------------------

    def reset_after_fork(self) -> None:
        """Forget every connection without touching its peer state.

        The child shares the parent's file descriptors; a pooled connection
        used from both processes would interleave two request streams on one
        TCP stream. Dropping the references closes only the child's copy of
        each descriptor. The lock is replaced too: a thread in the parent may
        have held it at fork time.
        """
        self._lock = threading.Lock()
        self._idle = {}
        self._pid = os.getpid()

    def close_all(self) -> None:
        with self._lock:
            conns = [c for entries in self._idle.values() for c, _ in entries]
            self._idle = {}
        for c in conns:
            _close_quietly(c)

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._idle.values())

    # -- checkout / release -------------------------------------------------

    def take(self, key: _Key) -> http.client.HTTPConnection | None:
        """The warmest usable idle connection for ``key``, or None."""
        if self._pid != os.getpid():  # a fork the at-fork hook could not see
            self.reset_after_fork()
        now = time.monotonic()
        stale: list[http.client.HTTPConnection] = []
        found: http.client.HTTPConnection | None = None
        with self._lock:
            entries = self._idle.get(key)
            while entries:
                conn, since = entries.pop()
                if now - since > MAX_IDLE_SECONDS or _looks_dead(conn.sock):
                    stale.append(conn)
                    continue
                found = conn
                break
            if entries is not None and not entries:
                self._idle.pop(key, None)
            self._sweep_locked(now, stale)
        for c in stale:
            _close_quietly(c)
        return found

    def release(self, key: _Key, conn: http.client.HTTPConnection) -> None:
        if self._pid != os.getpid():
            self.reset_after_fork()
        overflow: http.client.HTTPConnection | None = None
        with self._lock:
            entries = self._idle.setdefault(key, [])
            if len(entries) >= MAX_IDLE_PER_ENDPOINT:
                overflow = conn
            else:
                entries.append((conn, time.monotonic()))
        if overflow is not None:
            _close_quietly(overflow)

    def drop_endpoint(self, key: _Key) -> None:
        """Discard every idle connection for ``key``: one of them was found
        dead, and the rest of a batch idled through the same server restart
        or idle-timeout sweep."""
        with self._lock:
            entries = self._idle.pop(key, [])
        for c, _ in entries:
            _close_quietly(c)

    def _sweep_locked(
        self, now: float, out: list[http.client.HTTPConnection]
    ) -> None:
        """Age out every endpoint's over-age idle connections (a rotated lease
        leaves its old key behind). Caller holds the lock."""
        for key in list(self._idle):
            keep: list[tuple[http.client.HTTPConnection, float]] = []
            for conn, since in self._idle[key]:
                if now - since > MAX_IDLE_SECONDS:
                    out.append(conn)
                else:
                    keep.append((conn, since))
            if keep:
                self._idle[key] = keep
            else:
                del self._idle[key]


_POOL = _ConnectionPool()

if hasattr(os, "register_at_fork"):  # POSIX; Windows has no fork to guard
    os.register_at_fork(after_in_child=lambda: _POOL.reset_after_fork())


def reset_pool() -> None:
    """Close every pooled connection (tests; endpoint teardown)."""
    _POOL.close_all()


def idle_connection_count() -> int:
    return _POOL.idle_count()


# ── response ──────────────────────────────────────────────────────────────────

def _is_gzip(headers: Any) -> bool:
    return (headers.get("Content-Encoding") or "").strip().lower() in ("gzip", "x-gzip")


def _buffered_response(
    resp: http.client.HTTPResponse, raw: bytes, url: str
) -> Any:
    """A urllib-compatible response over an already-read body: ``read()``,
    ``code``/``status``, ``headers``, ``msg`` (the reason, as urllib sets it),
    context manager. ``HTTPErrorProcessor`` and ``HTTPError`` take it as-is.
    A gzip body is decoded and its encoding/length headers dropped, so a
    reader sees the same thing it would from an identity response."""
    import urllib.response  # noqa: PLC0415 — deferred import — keeps module load light

    headers = resp.msg  # http.client puts the parsed header Message here
    body = raw
    if _is_gzip(headers):
        try:
            body = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        except zlib.error as exc:
            raise OSError(f"undecodable gzip response body: {exc}") from exc
        del headers["Content-Encoding"]
        del headers["Content-Length"]
    out = urllib.response.addinfourl(io.BytesIO(body), headers, url, resp.status)
    out.reason = resp.reason  # type: ignore[attr-defined]
    out.msg = resp.reason  # type: ignore[attr-defined]
    out.version = resp.version  # type: ignore[attr-defined]
    return out


# ── the handlers ──────────────────────────────────────────────────────────────

_ssl_context_lock = threading.Lock()
_ssl_context_slot: tuple[tuple[str | None, str | None], Any] | None = None


def _shared_ssl_context() -> Any:
    """The process-wide client SSL context, rebuilt when the trust environment
    (``SSL_CERT_FILE`` / ``SSL_CERT_DIR``) changes. Same construction as
    ``http.client``'s own default, which is what ``HTTPSHandler`` would build."""
    global _ssl_context_slot
    env = (os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))
    with _ssl_context_lock:
        if _ssl_context_slot is None or _ssl_context_slot[0] != env:
            create = getattr(http.client, "_create_https_context", None)
            if create is not None:
                ctx = create(http.client.HTTPSConnection._http_vsn)  # noqa: SLF001
            else:  # pragma: no cover — future CPython without the private helper
                import ssl  # noqa: PLC0415 — deferred import — keeps module load light

                ctx = ssl.create_default_context()
            _ssl_context_slot = (env, ctx)
        return _ssl_context_slot[1]


_handler_classes: dict[OnConnect, tuple[type, type]] = {}
_handler_classes_lock = threading.Lock()


def _make_handler_classes(on_connect: OnConnect) -> tuple[type, type]:
    import urllib.error  # noqa: PLC0415 — deferred import — keeps module load light
    import urllib.request  # noqa: PLC0415 — deferred import — keeps module load light

    class _HTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()
            on_connect(self.sock)

    class _HTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            on_connect(self.sock)

    def _pooled_open(
        http_class: type[http.client.HTTPConnection],
        req: Any,
        debuglevel: int,
        **conn_args: Any,
    ) -> Any:
        host = req.host
        if not host:
            raise urllib.error.URLError("no host given")
        context = conn_args.get("context")
        tunnel_host = req._tunnel_host  # noqa: SLF001 — urllib's own proxy bookkeeping
        key: _Key = (
            issubclass(http_class, http.client.HTTPSConnection),
            host,
            tunnel_host,
            id(context) if context is not None else None,
        )

        # The same header assembly as urllib's do_open, minus "Connection:
        # close".
        headers = dict(req.unredirected_hdrs)
        headers.update({k: v for k, v in req.headers.items() if k not in headers})
        headers = {name.title(): val for name, val in headers.items()}
        tunnel_headers: dict[str, str] = {}
        if tunnel_host and "Proxy-Authorization" in headers:
            tunnel_headers["Proxy-Authorization"] = headers.pop("Proxy-Authorization")

        timeout = req.timeout
        encode_chunked = req.has_header("Transfer-encoding")

        def _fresh() -> http.client.HTTPConnection:
            conn = http_class(host, timeout=timeout, **conn_args)
            conn.set_debuglevel(debuglevel)
            if tunnel_host:
                conn.set_tunnel(tunnel_host, headers=tunnel_headers)
            return conn

        allow_reuse = True  # one transparent retry: afterwards, fresh connections only
        while True:
            conn = _POOL.take(key) if allow_reuse else None
            reused = conn is not None
            if conn is None:
                conn = _fresh()
            else:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
            sent_at = time.monotonic()
            try:
                try:
                    conn.request(
                        req.get_method(), req.selector, req.data, headers,
                        encode_chunked=encode_chunked,
                    )
                except OSError as err:
                    if reused and isinstance(err, _STALE_ERRORS) and _quick(sent_at):
                        _close_quietly(conn)
                        _POOL.drop_endpoint(key)
                        allow_reuse = False
                        continue
                    raise urllib.error.URLError(err) from err
                try:
                    resp = conn.getresponse()
                except _STALE_ERRORS:
                    if reused and _quick(sent_at):
                        _close_quietly(conn)
                        _POOL.drop_endpoint(key)
                        allow_reuse = False
                        continue
                    raise
                raw = resp.read()
            except BaseException:
                _close_quietly(conn)
                raise
            break

        if resp.will_close or conn.sock is None:
            _close_quietly(conn)
        else:
            _POOL.release(key, conn)
        return _buffered_response(resp, raw, req.get_full_url())

    class _PooledHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return _pooled_open(_HTTPConnection, req, self._debuglevel)

    class _PooledHTTPSHandler(urllib.request.HTTPSHandler):
        def __init__(self) -> None:
            # urllib builds a new SSLContext (a CA-bundle load) per handler
            # instance, i.e. per opener, i.e. per request here -- and a new
            # context is a new pool key. One shared context fixes both.
            super().__init__(context=_shared_ssl_context())

        def https_open(self, req: Any) -> Any:
            return _pooled_open(
                _HTTPSConnection, req, self._debuglevel, context=self._context
            )

    return _PooledHTTPHandler, _PooledHTTPSHandler


def build_opener(on_connect: OnConnect) -> Any:
    """A urllib opener whose HTTP(S) handlers use the process-wide pool.

    ``on_connect(sock)`` runs right after every NEW connection's ``connect()``
    (TCP keepalive options live there); pass a stable module-level function,
    the handler classes are built once per distinct callable. The OPENER is
    built per call, as before: it carries the proxy handler, which reads the
    environment fresh each time.
    """
    import urllib.request  # noqa: PLC0415 — deferred import — keeps module load light

    with _handler_classes_lock:
        classes = _handler_classes.get(on_connect)
        if classes is None:
            classes = _handler_classes[on_connect] = _make_handler_classes(on_connect)
    return urllib.request.build_opener(*classes)
