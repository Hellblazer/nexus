# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-218 appliance handoff file: the endpoint a Windows client reads over UNC.

nexus-ijue9.29. Record: T2 nexus/rdr-218-appliance-endpoint-handoff-decision
section 3 (amended 2026-09-29 for Sam's O1). The storage-service supervisor
inside the WSL2 appliance projects its endpoint into one small JSON file,
/var/lib/nexus/appliance/endpoint.json (the unit sets the path through
:data:`HANDOFF_FILE_ENV`). The file is rewritten whenever its bytes would
change and left alone otherwise.

What it carries is a ``mint-locked`` credential, never the root bearer: O1 keeps
the root bearer inside the distro. A mint-locked token itself can only call
``POST /v1/data-tokens/mint`` for its own tenant; the Windows client turns it
into short-lived data tokens through ``nexus.db.data_token.DataTokenManager``,
the same path cloud mode uses. What that bounds: a leaked copy grants the
tenant's full DATA plane (through the tokens it mints, rate-limited, each at
most the data-token TTL ceiling) until it is revoked, but no token-admin verb
and no tenant creation. It does not bound a process running as the same Windows
user, which can reach the distro anyway (record section 7).

The credential is issued ONCE, by the supervisor with the root bearer,
persisted on the data volume beside ``pg_credentials``
(:data:`MINT_CREDENTIAL_FILENAME`): an image re-import regenerates the same
file from the volume. Revoking it invalidates only this credential; the root is
untouched. :class:`ApplianceProjector` checks it against the engine on start
and every :data:`VERIFY_INTERVAL_S`: absent from the engine (a re-provisioned
database) is re-issued once per supervisor lifetime; revoked or
rotation-expired is sticky and DURABLE: the credential file is renamed to
:data:`DEAD_MARKER_FILENAME`, nothing is issued while that marker exists (so a
database restore that loses the revoked row cannot resurrect access), the
handoff file is removed, and ``appliance_mint_credential_dead`` names the
remedy; a failed check never counts as absent.

Schema 1, one JSON object, UTF-8 without BOM, keys sorted, one trailing newline::

    {"host": "127.0.0.1", "mint_tenant": "default", "mint_token": "<mint-locked>", "port": 29517, "schema": 1}

``host`` is always the literal loopback address: Windows dials 127.0.0.1
through the WSL relay, never ``localhost`` or ``::1``. Any change to an existing
key's meaning is a schema bump. The committed byte fixture
``tests/fixtures/appliance/endpoint.schema1.json`` is the contract the producer
and ijue9.6's reader both test against.
"""
from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

#: The unit sets this to /var/lib/nexus/appliance/endpoint.json. Unset (every
#: non-appliance install), no handoff file is written and nothing is issued.
HANDOFF_FILE_ENV: str = "NX_APPLIANCE_HANDOFF_FILE"

HANDOFF_SCHEMA: int = 1

#: The issued credential, persisted on the volume beside ``pg_credentials``.
MINT_CREDENTIAL_FILENAME: str = "appliance_mint_credential"

#: A dead credential's file is renamed to this; nothing is issued while it exists.
DEAD_MARKER_FILENAME: str = MINT_CREDENTIAL_FILENAME + ".dead"

#: The label the credential is issued under, so an operator can find and
#: revoke it (``nx service token list`` / ``revoke``).
MINT_CREDENTIAL_LABEL: str = "appliance-windows-client"

#: The tenant the root bearer is bound to (engine TenantConstants.DEFAULT_TENANT);
#: the credential is issued for it, and a mint-locked token mints only for its
#: own tenant.
ROOT_TENANT: str = "default"

_HANDOFF_HOST: str = "127.0.0.1"
_TOKEN_KEY: str = "NX_APPLIANCE_MINT_TOKEN"
_TENANT_KEY: str = "NX_APPLIANCE_MINT_TENANT"


def _require_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"handoff {name} must be a non-empty str")
    return value


def handoff_bytes(port: int, mint_token: str, mint_tenant: str) -> bytes:
    """The exact schema-1 file content.

    Refuses values the reader would refuse, so a bad projection fails here
    rather than on the Windows side.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"handoff port must be an int in 1..65535, got {port!r}")
    body = {
        "host": _HANDOFF_HOST,
        "mint_tenant": _require_str("mint_tenant", mint_tenant),
        "mint_token": _require_str("mint_token", mint_token),
        "port": port,
        "schema": HANDOFF_SCHEMA,
    }
    return (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(target: Path, data: bytes) -> None:
    """Temp file in the same directory, O_EXCL 0600, fsync, os.replace, fsync the dir.

    A leftover temp of the same name from a crash is removed first.
    """
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    # The file is in place once os.replace returns; the directory fsync only
    # hardens the rename against a power loss, so its failure is not a failed write.
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _enforce_0600(path: Path) -> None:
    """Tighten an existing file that is readable beyond its owner."""
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)


def write_handoff_if_changed(target: Path, port: int, mint_token: str, mint_tenant: str) -> bool:
    """Write the handoff file atomically when its bytes would change.

    Returns True when written, False when the file already held exactly these
    bytes (no rewrite, no mtime change). The temp file is ``.<name>.tmp.<pid>``
    in the same directory; a reader opens only the exact name.

    Raises OSError (a missing directory, a permission problem) for the caller
    to report; the supervisor logs it and keeps serving.
    """
    data = handoff_bytes(port, mint_token, mint_tenant)
    with contextlib.suppress(FileNotFoundError):
        if target.read_bytes() == data:
            _enforce_0600(target)
            return False
    _atomic_write(target, data)
    return True


def _read_credential(path: Path) -> tuple[str, str] | None:
    """The persisted (mint_token, mint_tenant), or None when absent.

    A present but unreadable or incomplete file raises ValueError: silently
    re-issuing over it would leave the first credential live and unaccounted
    for.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    _enforce_0600(path)
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    token, tenant = values.get(_TOKEN_KEY, ""), values.get(_TENANT_KEY, "")
    if not token or not tenant:
        raise ValueError(
            f"{path} exists but lacks {_TOKEN_KEY} or {_TENANT_KEY}; refusing to "
            f"issue a second credential over it. Revoke the old one (label "
            f"{MINT_CREDENTIAL_LABEL!r}, `nx service token list`) and remove the file."
        )
    return token, tenant


class CredentialDeadError(RuntimeError):
    """The dead marker exists: nothing may be issued until an operator removes it."""


DEAD_REMEDY: str = (
    f"The appliance's mint credential was revoked, rotated away, or kept "
    f"disappearing, and it is never re-issued automatically. To issue a new one, "
    f"remove <NEXUS_CONFIG_DIR>/{DEAD_MARKER_FILENAME} and restart the unit."
)


def ensure_mint_credential(
    config_dir: Path, issue: Callable[[], Mapping[str, object]],
) -> tuple[str, str]:
    """Return the appliance's (mint_token, mint_tenant), issuing it only once.

    Read from ``<config_dir>/appliance_mint_credential`` when present; ``issue``
    is never called then. When absent, ``issue()`` must return the engine's
    issue response (``{"token", "tenant", ...}`` for a ``mint-locked`` token);
    it is persisted atomically with mode 0600 before being returned.
    """
    marker = config_dir / DEAD_MARKER_FILENAME
    if marker.exists():
        raise CredentialDeadError(
            f"{marker} marks the appliance's mint credential dead; refusing to issue "
            f"a new one. {DEAD_REMEDY}"
        )
    path = config_dir / MINT_CREDENTIAL_FILENAME
    existing = _read_credential(path)
    if existing is not None:
        return existing
    issued = issue()
    token = _require_str("issued token", issued.get("token"))
    tenant = _require_str("issued tenant", issued.get("tenant"))
    _atomic_write(path, f"{_TOKEN_KEY}={token}\n{_TENANT_KEY}={tenant}\n".encode())
    return token, tenant


# ── credential validity and the projector (nexus-ijue9.29 review round) ─────

#: Seconds between validity checks of the persisted credential after the first
#: (which runs on start). A list call per heartbeat would be wasted load.
VERIFY_INTERVAL_S: float = 300.0

#: What :func:`classify_credential` can answer.
LIVE, ABSENT, REVOKED, EXPIRED = "live", "absent", "revoked", "expired"


def token_hash(token: str) -> str:
    """The engine's stored identity for *token* (sha256 hex, TokenHashing.java)."""
    import hashlib  # noqa: PLC0415 — only the validity path needs it

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def classify_credential(rows: list[Mapping[str, object]], mint_token: str) -> str:
    """Where *mint_token* stands among the engine's token *rows*.

    ``absent``: no row carries its hash (the database was re-provisioned under
    a surviving credential file). ``revoked``: an operator revoked it, or it is
    not mint-locked. ``expired``: it carries an expiry, which the issued
    credential never has: ``nx service token rotate`` grace-expired it.
    """
    wanted = token_hash(mint_token)
    for row in rows:
        if row.get("token_hash") != wanted:
            continue
        if row.get("revoked_at") or row.get("scope") not in (None, "mint-locked"):
            return REVOKED
        if row.get("expires_at"):
            return EXPIRED
        return LIVE
    return ABSENT


class ApplianceProjector:
    """Keeps the handoff file true to a usable credential, for one supervisor lifetime.

    Policy (coordinator, 2026-09-29):
    - ``absent``: re-issue ONCE per supervisor lifetime, loudly. Absent again
      after that means something keeps deleting it: treat as dead, no loop.
    - ``revoked`` / ``expired``: sticky. Remove the handoff file (Windows then
      reports "appliance has not published its endpoint") and log the remedy.
      Revocation must stay revoked; never re-issue on it.
    - A failed list call is never read as ``absent``: keep the current file.
    The check runs on the first projection and then every
    :data:`VERIFY_INTERVAL_S`; the file write itself runs on every projection
    and is a no-op when nothing changed.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        target: Path,
        issue: Callable[[], Mapping[str, object]],
        revoke: Callable[[str], object],
        list_rows: Callable[[], list[Mapping[str, object]]],
        log: object,
        clock: Callable[[], float],
        verify_interval_s: float = VERIFY_INTERVAL_S,
    ) -> None:
        self._config_dir = config_dir
        self._target = target
        self._issue = issue
        self._revoke = revoke
        self._list_rows = list_rows
        self._log = log
        self._clock = clock
        self._verify_interval_s = verify_interval_s
        self._last_verified: float | None = None
        self._reissued = False
        self._dead: str | None = None

    @property
    def dead(self) -> str | None:
        """The state that killed the credential this lifetime, else None."""
        return self._dead

    def _issue_and_account(self) -> Mapping[str, object]:
        issued = self._issue()
        self._log.info(  # type: ignore[attr-defined]
            "appliance_mint_credential_issued",
            tenant=issued.get("tenant"), token_hash=issued.get("token_hash"),
            label=MINT_CREDENTIAL_LABEL,
        )
        return issued

    def _credential(self) -> tuple[str, str]:
        issued_here: list[Mapping[str, object]] = []

        def _issue() -> Mapping[str, object]:
            issued = self._issue_and_account()
            issued_here.append(issued)
            return issued

        try:
            return ensure_mint_credential(self._config_dir, _issue)
        except Exception:
            # Issued but not persisted: revoke it, or it stays a live,
            # unaccounted, never-expiring mint credential.
            for issued in issued_here:
                h = str(issued.get("token_hash") or "")
                try:
                    self._revoke(h)
                    self._log.warning(  # type: ignore[attr-defined]
                        "appliance_mint_credential_orphan_revoked", token_hash=h,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log.error(  # type: ignore[attr-defined]
                        "appliance_mint_credential_orphaned", token_hash=h,
                        label=MINT_CREDENTIAL_LABEL, error=str(exc),
                        msg="issued but neither persisted nor revoked: revoke it by hash",
                    )
            raise

    def _go_dead(self, state: str, mint_token: str) -> None:
        self._dead = state
        cred = self._config_dir / MINT_CREDENTIAL_FILENAME
        marker = self._config_dir / DEAD_MARKER_FILENAME
        try:
            os.replace(cred, marker)            # durable: survives restarts and DB restores
        except FileNotFoundError:
            _atomic_write(marker, f"state={state}\n".encode())
        with contextlib.suppress(FileNotFoundError):
            self._target.unlink()
        self._log.error(  # type: ignore[attr-defined]
            "appliance_mint_credential_dead", state=state,
            token_hash=token_hash(mint_token), path=str(self._target), remedy=DEAD_REMEDY,
        )

    def _report_extra_live(self, rows: list[Mapping[str, object]], mint_token: str) -> None:
        """Warn about other live credentials under this label (a forced rotation
        that removed the file without revoking leaves the old one live)."""
        mine = token_hash(mint_token)
        extra = [
            str(r.get("token_hash")) for r in rows
            if r.get("label") == MINT_CREDENTIAL_LABEL and r.get("token_hash") != mine
            and not r.get("revoked_at") and not r.get("expires_at")
        ]
        if extra:
            self._log.warning(  # type: ignore[attr-defined]
                "appliance_mint_credential_extra_live", token_hashes=extra,
                msg="other live credentials carry the appliance label; revoke them "
                "(`nx service token revoke <hash>`) if they are not in use",
            )

    def project(self, port: int) -> bool:
        """Bring the handoff file up to date. Returns True when it was written.

        Raises OSError / engine errors for the caller to log; never loops.
        """
        if self._dead is None and (self._config_dir / DEAD_MARKER_FILENAME).exists():
            self._dead = "marked"
            self._log.error(  # type: ignore[attr-defined]
                "appliance_mint_credential_dead", state="marked",
                path=str(self._target), remedy=DEAD_REMEDY,
            )
        if self._dead is not None:
            with contextlib.suppress(FileNotFoundError):
                self._target.unlink()
            return False
        mint_token, mint_tenant = self._credential()
        now = self._clock()
        if self._last_verified is None or now - self._last_verified >= self._verify_interval_s:
            try:
                rows = self._list_rows()
            except Exception as exc:  # noqa: BLE001 — a failed check is not "absent"
                self._log.warning(  # type: ignore[attr-defined]
                    "appliance_mint_credential_check_failed", error=str(exc),
                    msg="kept the current handoff file; will check again",
                )
            else:
                self._last_verified = now
                state = classify_credential(rows, mint_token)
                if state == LIVE:
                    self._report_extra_live(rows, mint_token)
                if state == ABSENT and not self._reissued:
                    self._reissued = True
                    self._log.warning(  # type: ignore[attr-defined]
                        "appliance_mint_credential_reissued", reason=ABSENT,
                        old_token_hash=token_hash(mint_token),
                        msg="the engine has no row for the persisted credential "
                        "(re-provisioned database?); issuing a new one once",
                    )
                    (self._config_dir / MINT_CREDENTIAL_FILENAME).unlink()
                    mint_token, mint_tenant = self._credential()
                elif state == ABSENT:
                    self._log.error(  # type: ignore[attr-defined]
                        "appliance_mint_credential_keeps_disappearing",
                        msg="the re-issued credential is absent again; not re-issuing",
                    )
                    self._go_dead(ABSENT, mint_token)
                    return False
                elif state in (REVOKED, EXPIRED):
                    self._go_dead(state, mint_token)
                    return False
        return write_handoff_if_changed(self._target, port, mint_token, mint_tenant)
