# SPDX-License-Identifier: AGPL-3.0-or-later
"""The token a LOCAL ``nx backup restore`` sends (nexus-csayy, RDR-248, ask ``rdr248-local-root``).

The import routes refuse an operator (root) token: a restore is made with a token bound to one tenant. A local install holds
only the supervisor's root token, so until now the user had to issue a tenant token by hand and export it. This
wrapper does that step: when the token the client would send IS the supervisor's (it equals the one in the live
lease), it asks the engine's token route, the one ``nx service token issue`` uses
(:meth:`~nexus.db.t2.http_token_store.HttpTokenStore.issue_token`), for a short-lived token of the ``default`` tenant and sends
that instead. The root token goes to the token route and to nothing else; it is never sent to an import route.

Any other token wins: an explicit ``NX_SERVICE_TOKEN`` (or config) that is not the supervisor's is the user's choice
of tenant and is passed through untouched. A mint that fails is a refusal that says to issue the token by hand (the
manual step stays as the fallback). The minted token is replaced before it expires, so a session of any length
keeps a live token, and once if the engine rejects it.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

import httpx
import structlog

from nexus.backup.cloud_export import Credentials, ExportRefused

_log = structlog.get_logger(__name__)

#: The tenant a local restore loads into: the install's own.
LOCAL_TENANT = "default"
#: How long the minted token lives, in seconds. The longest session at the engine's defaults is about 3 h 12 min
#: (60 min of upload, ``NX_IMPORT_LOAD_MAX`` waiting for the load slot, ``NX_IMPORT_LOAD_MAX`` of load, 3,960 s each);
#: four hours covers it, and the refresh below covers an operator who raised the load bound.
RESTORE_TOKEN_TTL_S = 4 * 3600
#: A minted token with less than this left is replaced before the next request is sent with it.
REFRESH_MARGIN_S = 600.0
_LABEL = "nx backup restore"

#: ``(base_url, root_token, tenant, ttl_seconds, label) -> token``. The default calls the engine's token route.
Issuer = Callable[[str, str, str, int, str], str]

MANUAL_STEP = (
    "Issue a token for the tenant yourself with `nx service token issue --tenant default` and export it as "
    "NX_SERVICE_TOKEN (a local install reads the token from that variable; `nx config set service_token` applies only "
    "when a service_url is set), then run the restore again."
)


def _lease_token() -> str | None:
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 - deferred, heavy

    return discover_lease()[1]


def issue_through_the_token_route(base_url: str, root_token: str, tenant: str, ttl_seconds: int, label: str) -> str:
    """The route ``nx service token issue`` uses, with the root token as its bearer."""
    from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 - deferred, heavy

    with HttpTokenStore(base_url=base_url, tenant=tenant, _token=root_token) as store:
        issued = store.issue_token(tenant, label, ttl_seconds)
    token = str(issued.get("token") or "")
    if not token:
        raise RuntimeError("the engine's answer carried no token")
    return token


class LocalRestoreCredentials:
    """:class:`~nexus.backup.cloud_export.Credentials` for a local restore: *inner*'s token, unless that is the
    supervisor's root token, in which case a short-lived ``default``-tenant token minted from it."""

    def __init__(
        self,
        inner: Credentials,
        base_url: str,
        *,
        root_token: Callable[[], str | None] = _lease_token,
        issue: Issuer = issue_through_the_token_route,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = RESTORE_TOKEN_TTL_S,
    ) -> None:
        self._inner = inner
        self._base = base_url
        self._root_token = root_token
        self._issue = issue
        self._clock = clock
        self._ttl = ttl_seconds
        self._minted: tuple[str, float] | None = None  # (token, expires at, wall clock)

    def bearer(self) -> str:
        held = self._inner.bearer()
        root = self._root_token()
        if not root or not secrets.compare_digest(held.encode(), root.encode()):
            self._minted = None
            return held  # the user's own token wins
        if self._minted is not None and self._clock() < self._minted[1] - REFRESH_MARGIN_S:
            return self._minted[0]
        return self._mint(held)

    def rejected(self, sent: str) -> bool:
        if self._minted is not None and sent == self._minted[0]:
            self._minted = None  # the next bearer() mints a fresh one
            return True
        return self._inner.rejected(sent)

    def _mint(self, root: str) -> str:
        try:
            token = self._issue(self._base, root, LOCAL_TENANT, self._ttl, _LABEL)
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            _log.warning("restore_local_token_mint_failed", error=type(exc).__name__)
            raise ExportRefused(
                f"this install holds the supervisor's operator token, which a restore refuses, and a token for the "
                f"{LOCAL_TENANT} tenant could not be minted from it ({type(exc).__name__}: {str(exc)[:160]}). {MANUAL_STEP}"
            ) from None
        self._minted = (token, self._clock() + self._ttl)
        _log.info("restore_local_token_minted", tenant=LOCAL_TENANT, ttl_seconds=self._ttl)
        return token
