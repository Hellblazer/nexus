# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-218 appliance handoff file: the endpoint a Windows client reads over UNC.

nexus-ijue9.29. Record: T2 nexus/rdr-218-appliance-endpoint-handoff-decision
section 3 (amended 2026-09-29 for Sam's O1). The storage-service supervisor
inside the WSL2 appliance projects its endpoint into one small JSON file,
/var/lib/nexus/appliance/endpoint.json (the unit sets the path through
:data:`HANDOFF_FILE_ENV`). The file is rewritten whenever its bytes would
change and left alone otherwise.

What it carries is a ``mint-locked`` credential, NEVER a bearer: O1 keeps the
root bearer inside the distro. A mint-locked token can only call
``POST /v1/data-tokens/mint`` for its own tenant; the Windows client turns it
into short-lived data tokens through ``nexus.db.data_token.DataTokenManager``,
the same path cloud mode uses. The credential is issued ONCE, by the
supervisor with the root bearer, persisted on the data volume beside
``pg_credentials`` (:data:`MINT_CREDENTIAL_FILENAME`), and never re-issued: an
image re-import regenerates the same file from the volume. Revoking it
invalidates only this credential; the root is untouched.

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
    dir_fd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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


def ensure_mint_credential(
    config_dir: Path, issue: Callable[[], Mapping[str, object]],
) -> tuple[str, str]:
    """Return the appliance's (mint_token, mint_tenant), issuing it only once.

    Read from ``<config_dir>/appliance_mint_credential`` when present; ``issue``
    is never called then. When absent, ``issue()`` must return the engine's
    issue response (``{"token", "tenant", ...}`` for a ``mint-locked`` token);
    it is persisted atomically with mode 0600 before being returned.
    """
    path = config_dir / MINT_CREDENTIAL_FILENAME
    existing = _read_credential(path)
    if existing is not None:
        return existing
    issued = issue()
    token = _require_str("issued token", issued.get("token"))
    tenant = _require_str("issued tenant", issued.get("tenant"))
    _atomic_write(path, f"{_TOKEN_KEY}={token}\n{_TENANT_KEY}={tenant}\n".encode())
    return token, tenant
