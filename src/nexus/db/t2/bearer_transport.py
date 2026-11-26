# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a request that carries a bearer reaches the service (nexus-ec37o).

A repository's ``.claude/settings.json`` env reaches every ``nx`` the model runs, so
``HTTPS_PROXY`` and ``SSL_CERT_FILE`` can be set by a repository: a proxy it names, trusted
through a CA it names, terminates TLS and reads the ``Authorization`` header of a request to the
bound origin. A request that carries a bearer therefore reads NO proxy or CA variable from the
environment (Sam, 2026-11-26, T2 ``nexus/sam-ruling-ec37o-token-host-binding-2026-11-26`` item 6).
A user who needs a corporate proxy or CA sets it once in nexus's own ``config.yml``, which a
repository cannot write::

    nx config set network.https_proxy http://proxy.corp:3128
    nx config set network.ca_bundle /etc/corp/root-ca.pem

``network.ca_bundle`` adds roots to the default trust store (certifi's bundle; on Windows the
operating system's store, through ``truststore``). Plain-http (loopback) requests never use a
proxy. The settings are read from the process's own config directory, the one the stored
credential came from, and never from a per-repo ``.nexus.yml``.

Also not read, because OpenSSL or ``httpx`` would take them from the environment: ``SSL_CERT_DIR``,
``REQUESTS_CA_BUNDLE``, ``CURL_CA_BUNDLE``, ``NO_PROXY``, ``ALL_PROXY``, a ``.netrc`` and
``SSLKEYLOGFILE``. The plugin's stdlib mirror is ``conexus/hooks/scripts/_endpoint_resolve.py``
(``bearer_ssl_context``, ``bearer_opener``).
"""

from __future__ import annotations

import ssl
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    import urllib.request

NETWORK_SECTION = "network"
KEY_HTTPS_PROXY = "https_proxy"
KEY_CA_BUNDLE = "ca_bundle"
#: The keys ``nx config set`` accepts under ``network.``.
NETWORK_KEYS = (KEY_HTTPS_PROXY, KEY_CA_BUNDLE)


class NetworkSettings(NamedTuple):
    """The ``network:`` section of the process's global ``config.yml``; ``""`` is unset."""

    https_proxy: str
    ca_bundle: str


def network_settings() -> NetworkSettings:
    """The proxy and extra CA bundle a bearer-carrying request may use, from ``config.yml`` only."""
    from nexus.config import get_global_config_section  # noqa: PLC0415 — deferred: nexus.config imports the db package

    section = get_global_config_section("network")

    def text(key: str) -> str:
        value = section.get(key)
        return value.strip() if isinstance(value, str) else ""

    return NetworkSettings(text(KEY_HTTPS_PROXY), text(KEY_CA_BUNDLE))


# ── the TLS trust store, with no environment in it ───────────────────────────

_slot: tuple[str, ssl.SSLContext] | None = None
_lock = threading.Lock()


def _build_context(ca_bundle: str) -> ssl.SSLContext:
    import os  # noqa: PLC0415 — deferred: only the build needs it
    import sys  # noqa: PLC0415 — deferred: only the build needs it

    import certifi  # noqa: PLC0415 — deferred: only the build needs it

    # Not ``ssl.create_default_context()``: with no cafile it ends in ``set_default_verify_paths()``, where
    # OpenSSL reads ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``, and it opens the file ``SSLKEYLOGFILE`` names.
    # This is the context it would build, with certifi's bundle loaded explicitly and nothing from the env.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verify_mode CERT_REQUIRED and check_hostname on
    if sys.version_info >= (3, 13):  # what create_default_context added in 3.13
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN | ssl.VERIFY_X509_STRICT
    # The PEM text, not the path: OpenSSL then opens no file (on Windows a file BIO needs an OPENSSL_Applink
    # the host process may not carry, and the process dies with "no OPENSSL_Applink").
    ctx.load_verify_locations(cadata=certifi.contents())
    if ca_bundle:
        if os.path.isdir(ca_bundle):
            ctx.load_verify_locations(capath=ca_bundle)
        else:
            with open(ca_bundle, encoding="ascii", errors="replace") as fh:
                ctx.load_verify_locations(cadata=fh.read())
    return ctx


def bearer_ssl_context() -> ssl.SSLContext:
    """The process-wide client context for bearer-carrying requests: certifi's bundle plus
    ``network.ca_bundle``, and nothing from the environment.

    Built once per ``network.ca_bundle`` value (as :func:`nexus.db.t2.shared_ssl.get_shared_ssl_context`
    is, and for its reason: a long-lived process must not re-read a bundle inside an install
    generation that was removed behind it). A rebuild that fails keeps the context already in use;
    the first build raises, naming the bundle."""
    global _slot
    ca_bundle = network_settings().ca_bundle
    slot = _slot
    if slot is not None and slot[0] == ca_bundle:
        return slot[1]
    with _lock:
        slot = _slot
        if slot is not None and slot[0] == ca_bundle:
            return slot[1]
        try:
            ctx = _build_context(ca_bundle)
        except OSError as exc:
            if slot is not None:
                return slot[1]
            if ca_bundle:
                raise OSError(
                    exc.errno, f"{exc.strerror or exc}: network.ca_bundle in config.yml could not be loaded", ca_bundle,
                ) from exc
            raise
        _slot = (ca_bundle, ctx)
        return ctx


def reset_for_tests() -> None:
    global _slot
    with _lock:
        _slot = None


# ── the proxy ────────────────────────────────────────────────────────────────


def bearer_proxy(url: str | None = None) -> str:
    """The proxy the bearer request to *url* uses: ``network.https_proxy`` for an https URL, ``""``
    for anything else. With no *url* (a client built before its URLs are known) the configured value,
    which the caller pairs with a direct mount for plain http."""
    from nexus.db.service_endpoint import bearer_proxy_for  # noqa: PLC0415 — deferred: service_endpoint is a large module

    configured = network_settings().https_proxy
    return configured if url is None else (bearer_proxy_for(url, configured) or "")


def bearer_httpx_kwargs(url: str | None = None) -> dict[str, Any]:
    """Keyword arguments for an ``httpx.Client`` or ``httpx.get`` that carries a bearer.

    ``trust_env=False`` stops httpx reading ``HTTP(S)_PROXY``, ``ALL_PROXY``, ``NO_PROXY`` and ``.netrc``;
    ``verify`` is :func:`bearer_ssl_context`. With a *url* the proxy applies only if the URL is https.
    Without one (a ``Client``) the configured proxy is set for everything and plain http gets a direct
    transport, so a loopback bearer never meets it."""
    import httpx  # noqa: PLC0415 — deferred: heavy, and this module loads on the CLI's startup path

    ctx = bearer_ssl_context()
    kwargs: dict[str, Any] = {"verify": ctx, "trust_env": False}
    proxy = bearer_proxy(url)
    if proxy:
        # ``ssl_context=ctx`` for the hop to an ``https://`` proxy too: httpcore's default context reads SSL_CERT_FILE
        kwargs["proxy"] = httpx.Proxy(proxy, ssl_context=ctx) if proxy.lower().startswith("https:") else proxy
        if url is None:
            kwargs["mounts"] = {"http://": httpx.HTTPTransport(verify=bearer_ssl_context())}
    return kwargs


def bearer_proxy_handler(url: str | None = None) -> urllib.request.ProxyHandler:
    """A ``ProxyHandler`` built from ``config.yml`` alone. An explicit mapping never reads the
    environment, and ``{}`` means no proxy."""
    import urllib.request  # noqa: PLC0415 — deferred: stdlib, loaded only when a request is built

    proxy = bearer_proxy(url)
    return urllib.request.ProxyHandler({"https": proxy} if proxy else {})


def bearer_opener(url: str | None = None) -> urllib.request.OpenerDirector:
    """A urllib opener for a bearer-carrying request: :func:`bearer_proxy_handler`, an
    ``HTTPSHandler`` on :func:`bearer_ssl_context` and no redirect followed (urllib would copy the
    ``Authorization`` header to the target)."""
    import urllib.request  # noqa: PLC0415 — deferred: stdlib, loaded only when a request is built

    from nexus.db.service_endpoint import no_redirect_handler  # noqa: PLC0415 — deferred: service_endpoint is a large module

    return urllib.request.build_opener(
        bearer_proxy_handler(url), urllib.request.HTTPSHandler(context=bearer_ssl_context()), no_redirect_handler(),
    )
