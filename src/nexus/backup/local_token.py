# SPDX-License-Identifier: AGPL-3.0-or-later
"""The token a LOCAL ``nx backup restore`` sends (nexus-csayy, RDR-248, ask ``rdr248-local-root``).

The import routes refuse an operator (root) token: a restore is made with a token bound to one tenant. A local install holds
only the supervisor's root token, so until now the user had to issue a tenant token by hand and export it. This
wrapper does that step: when the token the client would send IS the supervisor's (it equals the one in the live
lease, or one this process has read there), it asks the engine's token route, the one ``nx service token issue`` uses
(:meth:`~nexus.db.t2.http_token_store.HttpTokenStore.issue_token`), for a short-lived token of the ``default`` tenant and sends
that instead. The root token goes to the token route and to nothing else; it is never sent to an import route.

Any other token wins: an explicit ``NX_SERVICE_TOKEN`` (or config) that is not the supervisor's is the user's choice
of tenant and is passed through untouched. A mint that fails is a refusal that says to issue the token by hand (the
manual step stays as the fallback). The minted token is replaced before it expires, and up to twice more if the engine
rejects it (three mints in all, the ceiling of the client's re-authentication). When the restore ends or aborts, the
token is revoked through the token route (best effort: a failure is logged and never changes the restore's outcome).

The lease is read on every call, and a read can fail while the engine restarts. Every lease token this process has seen
is remembered, so a held token that equals one of them is still the supervisor's when the lease cannot be read. When
no lease has ever been read and the held token is not one the user set (env or config), whether it is the supervisor's
cannot be told, and the call refuses rather than send it. A token the user set, with no lease to compare against, is
passed through: it is the user's explicit choice, and the engine refuses a root token on an import route.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

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

#: ``(base_url, root_token, tenant, ttl_seconds, label) -> (token, token_hash)``. The default calls the engine's token route.
Issuer = Callable[[str, str, str, int, str], tuple[str, str | None]]
#: ``(base_url, root_token, token_hash) -> None``: the token route's revoke. Raises on failure; the caller swallows it.
Revoker = Callable[[str, str, str], None]

MANUAL_STEP = (
    "Issue a token for the tenant yourself with `nx service token issue --tenant default` and export it as "
    "NX_SERVICE_TOKEN (a local install reads the token from that variable; `nx config set service_token` applies only "
    "when a service_url is set), then run the restore again."
)


def _lease_token() -> str | None:
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 - deferred, heavy

    return discover_lease()[1]


def _set_by_the_user(token: str) -> bool:
    """True when *token* is the ``NX_SERVICE_TOKEN`` environment value or the configured ``service_token``."""
    from nexus.config import get_credential  # noqa: PLC0415 - deferred, heavy

    return bool(token) and token in {
        os.environ.get("NX_SERVICE_TOKEN", "").strip(),
        (get_credential("service_token") or "").strip(),
    }


def issue_through_the_token_route(
    base_url: str, root_token: str, tenant: str, ttl_seconds: int, label: str,
) -> tuple[str, str | None]:
    """The route ``nx service token issue`` uses, with the root token as its bearer."""
    from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 - deferred, heavy

    with HttpTokenStore(base_url=base_url, tenant=tenant, _token=root_token) as store:
        issued = store.issue_token(tenant, label, ttl_seconds)
    token = str(issued.get("token") or "")
    if not token:
        raise RuntimeError("the engine's answer carried no token")
    return token, (str(issued["token_hash"]) if issued.get("token_hash") else None)


def revoke_through_the_token_route(base_url: str, root_token: str, token_hash: str) -> None:
    """The route ``nx service token revoke`` uses, with the root token as its bearer."""
    from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 - deferred, heavy

    with HttpTokenStore(base_url=base_url, tenant=LOCAL_TENANT, _token=root_token) as store:
        store.revoke_token(token_hash)


@dataclass
class _Minted:
    token: str
    token_hash: str | None
    expires_at: float  # wall clock
    root: str  # the token it was minted from, kept in memory to revoke with


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
        revoke: Revoker = revoke_through_the_token_route,
        explicit: Callable[[str], bool] = _set_by_the_user,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = RESTORE_TOKEN_TTL_S,
    ) -> None:
        self._inner = inner
        self._base = base_url
        self._root_token = root_token
        self._issue = issue
        self._revoke = revoke
        self._explicit = explicit
        self._clock = clock
        self._ttl = ttl_seconds
        self._minted: _Minted | None = None
        #: every supervisor token read from the lease so far: a lease that cannot be read later does not make the held one unknown
        self._seen_roots: set[str] = set()

    def bearer(self) -> str:
        held = self._inner.bearer()
        current = self._read_root()
        if current:
            self._seen_roots.add(current)
        if not self._seen_roots:
            if self._explicit(held):
                self._minted = None
                return held  # the user's own token, with no lease to compare it against
            raise ExportRefused(
                "this install's token could not be told from the supervisor's operator token (the supervisor's lease cannot be "
                f"read), and a restore never sends the operator token to an import route. {MANUAL_STEP}"
            )
        if not any(secrets.compare_digest(held.encode(), root.encode()) for root in self._seen_roots):
            self._minted = None
            return held  # the user's own token wins
        if self._minted is not None and self._clock() < self._minted.expires_at - REFRESH_MARGIN_S:
            return self._minted.token
        return self._mint(held)

    def rejected(self, sent: str) -> bool:
        if self._minted is not None and sent == self._minted.token:
            self._minted = None  # the engine no longer takes it: the next bearer() mints a fresh one
            return True
        return self._inner.rejected(sent)

    def release(self) -> None:
        """Revoke the minted token (the restore ended or aborted). Best effort: never raises, so it cannot mask the restore's outcome."""
        minted, self._minted = self._minted, None
        if minted is not None:
            self._revoke_quietly(minted)

    def _read_root(self) -> str | None:
        try:
            return self._root_token()
        except Exception as exc:  # noqa: BLE001 - a failed lease read is "no lease token now", never a crash
            _log.debug("restore_local_lease_read_failed", error=type(exc).__name__)
            return None

    def _revoke_quietly(self, minted: _Minted) -> None:
        if not minted.token_hash:
            _log.warning("restore_local_token_not_revoked", reason="the engine did not name the token's hash")
            return
        # The supervisor's token can change mid-run (an engine restart): revoke with the one the lease names now, then
        # with the one the token was minted from, so a restart does not leave the minted token alive to its TTL.
        current = self._read_root()
        bearers = [r for r in dict.fromkeys((current, minted.root)) if r]
        failure: Exception | None = None
        for root in bearers:
            try:
                self._revoke(self._base, root, minted.token_hash)
            except Exception as exc:  # noqa: BLE001 - best effort by design; the token expires on its own TTL
                failure = exc
                continue
            _log.info("restore_local_token_revoked", tenant=LOCAL_TENANT)
            return
        _log.warning("restore_local_token_revoke_failed", error=type(failure).__name__ if failure else "no_root")

    def _mint(self, root: str) -> str:
        try:
            token, token_hash = self._issue(self._base, root, LOCAL_TENANT, self._ttl, _LABEL)
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            _log.warning("restore_local_token_mint_failed", error=type(exc).__name__)
            raise ExportRefused(
                f"this install holds the supervisor's operator token, which a restore refuses, and a token for the "
                f"{LOCAL_TENANT} tenant could not be minted from it ({type(exc).__name__}: {str(exc)[:160]}). {MANUAL_STEP}"
            ) from None
        replaced, self._minted = self._minted, _Minted(token, token_hash, self._clock() + self._ttl, root)
        if replaced is not None:
            self._revoke_quietly(replaced)  # a refresh: the old one has minutes left and is not needed
        _log.info("restore_local_token_minted", tenant=LOCAL_TENANT, ttl_seconds=self._ttl)
        return token
