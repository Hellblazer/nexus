# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log- and message-safe forms of a service URL (nexus-q26w1).

A configured ``service_url`` (``NX_SERVICE_URL`` or ``nx config set
service_url``) is used verbatim and nothing validates it, so it can carry
``user:password@`` and a query. The whole URL, and the netloc, must never
reach a log event, an exception message or a file body. This module holds the
three helpers every such site shares; it imports only the standard library, so
any layer may import it.

* :func:`log_endpoint`: ``host[:port]``, or a fixed placeholder.
* :func:`endpoint_host_port`: the same, ``None`` when there is no host.
* :func:`scrub_transport_error`: the text of an exception a transport raised
  about the URL, or a fixed substitute when that text quotes any form of the
  URL's credentials.

It also holds the userinfo gate (nexus-ujbf8): :func:`has_userinfo` and
:func:`require_no_userinfo` refuse a ``service_url`` that carries
``user:password@``. The engine's credential is a bearer token, so userinfo is
incoherent, and the refusal names the SOURCE of the URL, never the URL.
"""
from __future__ import annotations

import re
import urllib.parse

UNPARSEABLE_ENDPOINT = "<unparseable-endpoint>"


class ServiceUrlUserinfoError(ValueError):
    """A ``service_url`` carries ``user:password@``. The message names where
    the URL came from and never the URL."""


def has_userinfo(url: str) -> bool:
    """True when *url* has an ``@`` in its authority.

    The authority is everything after ``://`` up to the first ``/``, NOT
    ``urlsplit``'s netloc: a raw ``#``, ``?`` or ``\\`` in a password ends the
    parsed netloc early (``https://user:pw#x@host`` has netloc ``user:pw``), so
    the parsed form would call that URL clean. An ``@`` in a path is fine.
    """
    rest = url.split("://", 1)[1] if "://" in url else url
    return "@" in rest.partition("/")[0]


#: The repair a refusal names when its caller has no better one.
_DEFAULT_REPAIR = (
    "re-save the address without them with `nx config set service_url <url without user:password@>`"
)


def require_no_userinfo(url: str, *, source: str, repair: str | None = None) -> str:
    """Return *url* unchanged, or raise :class:`ServiceUrlUserinfoError`.

    *source* is a fixed label for where the URL came from (``NX_SERVICE_URL``,
    ``config.yml credentials.service_url``); it is the only thing the error
    says about the URL. *repair* is the clause that tells the operator what to
    do about that source (default: re-save the address with
    ``nx config set service_url``).
    """
    if has_userinfo(url):
        raise ServiceUrlUserinfoError(
            f"{source} carries credentials in the URL (user:password@). "
            "The nexus service authenticates with a bearer token, not URL "
            f"credentials. To fix: {repair or _DEFAULT_REPAIR}; the token goes in "
            "`nx config set service_token --stdin` (or NX_SERVICE_TOKEN)."
        )
    return url

#: A secret piece shorter than this is matched only as a whole token, so a
#: one-letter user name does not withhold every error that contains the letter.
_SUBSTRING_MIN_LEN = 4


def endpoint_host_port(url: str) -> str | None:
    """``host[:port]`` of *url*, never userinfo, path or query; ``None`` when
    the URL does not parse to a host."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if not host:
            return None
        port = parts.port
        return f"{host}:{port}" if port else host
    except Exception:  # noqa: BLE001 -- a log helper must never raise
        return None


def log_endpoint(url: str) -> str:
    """Log- and message-safe endpoint identity: ``host[:port]`` of *url*,
    never userinfo, path or query. A URL that does not parse to a host yields
    a fixed placeholder, never the URL itself."""
    return endpoint_host_port(url) or UNPARSEABLE_ENDPOINT


def _secret_forms(url: str) -> tuple[set[str], set[str]]:
    """``(whole, pieces)`` secrets of *url*: *whole* are the userinfo, user,
    password and query as written and decoded, *pieces* are the ``:``- and ``@``-split parts of the userinfo
    in raw and percent-decoded form (``http.client`` splits the netloc on its
    LAST ``:``, so an error can quote any tail of the password)."""
    try:
        parts = urllib.parse.urlsplit(url)
        # A schemeless value ("tok@host:80") has no netloc: the whole string is
        # the authority, as the old ``_host`` fallback treated it.
        userinfo = (parts.netloc or url.partition("/")[0]).rpartition("@")[0]
        whole = {
            userinfo, urllib.parse.unquote(userinfo),
            parts.username or "", parts.password or "",
            urllib.parse.unquote(parts.username or ""), urllib.parse.unquote(parts.password or ""),
            parts.query, urllib.parse.unquote(parts.query),
        }
        # A raw '#', '?' or '/' in the password ends urlsplit's netloc early
        # ("https://user:pw#x@host" has netloc "user:pw" and no '@'), so the
        # parsed userinfo is empty while http.client quotes "pw" as a port.
        # Take the raw authority up to the LAST '@' as userinfo too, and when
        # the netloc has a ':' but no '@' and no valid port, treat its
        # non-host pieces as secret.
        rest = url.split("://", 1)[1] if "://" in url else url
        raw_userinfo = rest.rpartition("@")[0]
        whole.update({raw_userinfo, urllib.parse.unquote(raw_userinfo)})
        extra: list[str] = [raw_userinfo, urllib.parse.unquote(raw_userinfo)]
        if ":" in parts.netloc and "@" not in parts.netloc:
            try:
                parts.port
            except ValueError:
                extra.extend(
                    p for p in parts.netloc.split(":") if p != (parts.hostname or "")
                )
        pieces: set[str] = set()
        for form in (userinfo, urllib.parse.unquote(userinfo), parts.password or "",
                     urllib.parse.unquote(parts.password or ""), *extra):
            pieces.update(re.split(r"[:@/?#]", form))
    except Exception:  # noqa: BLE001 -- a scrubber must never raise
        return {url}, set()  # unparseable: the whole URL is the secret
    return {s for s in whole if s}, {p for p in pieces if p}


def _quotes(text: str, secret: str) -> bool:
    if len(secret) >= _SUBSTRING_MIN_LEN:
        return secret in text
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", text) is not None


def scrub_transport_error(exc: BaseException, url: str) -> str:
    """The error text for *exc*, a failure a transport raised about *url*.

    The text is returned as it is only when it quotes nothing of the URL's
    credentials. ``http.client`` quotes the part of the netloc it choked on,
    after urllib has percent-decoded it and after a split on the last ``:``:
    ``https://user:p%40ss@host`` gives ``nonnumeric port: 'p@ss@host'``. So
    the check is not a replace of the userinfo as written: when the text holds
    an ``@``, the userinfo or password in raw or decoded form, any ``:``-split
    piece of them, or the query, the text is replaced whole by the exception
    type name and the log-safe ``host[:port]``.
    """
    text = str(exc)
    whole, pieces = _secret_forms(url)
    if "@" in text or any(_quotes(text, s) for s in whole | pieces):
        return f"{type(exc).__name__} (error text withheld: it quoted the URL's credentials) at {log_endpoint(url)}"
    return text
