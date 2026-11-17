# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The CLI HTTP layers' identity token (RDR-253 "The resolver", last sentences; Step 3 item 10).

Every ``nx`` call reads the token file of its nearest Claude ancestor process (a Bash child of the session, or the
MCP server itself) and sends the token in :data:`~nexus.identity.token_contract.IDENTITY_TOKEN_HEADER`:

* a process with no Claude ancestor, or an ancestor with no token file, sends none: behaviour is exactly as before;
* a delegated child (``NX_IDENTITY_TOKEN`` names its token file) sends that file's token;
* a token the file records as past its expiry is not sent: the engine would answer 401 to **every** request,
  fenced or not, and an unfenced read must not break because the server has not refreshed yet. A fenced write then
  gets the engine's own ``403 identity_token_required``, which is the honest refusal;
* on **401** with a token attached the file is re-read once and the request retried once if the file now holds a
  different token (the server refreshed it, or the hook switched identity);
* on **409** ``generation_superseded`` the call stops with :class:`GenerationSupersededError`, which names the holder.
  A fenced write is never retried without a token, which JDR-002's 401 fallback would otherwise do.

The ancestor and its file path are discovered once per process: the ancestry of a running process does not change.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nexus.identity import token_contract
from nexus.identity.token_file import TokenFile, ancestor_token_path, identity_dir, read_token_file

NX_IDENTITY_TOKEN_ENV = "NX_IDENTITY_TOKEN"

_LOCK = threading.Lock()
_DISCOVERED: dict[str, Path | None] = {}


class GenerationSupersededError(RuntimeError):
    """A fenced write was refused because another process holds the identity now (``409 generation_superseded``).
    Not an ``HTTPStatusError``: no retry ladder resends it. ``holder`` is the engine's ``{host, pid}`` of the
    current holder, when it sent one."""

    def __init__(self, identity: str | None, token_generation: int | None, current_generation: int | None,
                 holder: Mapping[str, Any] | None, detail: str = "") -> None:
        self.identity = identity
        self.token_generation = token_generation
        self.current_generation = current_generation
        self.holder = dict(holder) if holder else None
        where = f" (pid {holder.get('pid')} on {holder.get('host')})" if holder else ""
        super().__init__(
            f"this process no longer holds identity {identity}: generation {current_generation} is held by another "
            f"process{where}, and this process's token is for generation {token_generation}. It must stop writing "
            f"{identity}'s state. Restart the session, /resume it, or take the seat over with "
            f"`nx identity claim <seat> --take-over`. {detail}".strip())


def superseded_from_body(status: int, body: Any) -> GenerationSupersededError | None:
    """The typed error for a ``409 generation_superseded`` answer; None for every other response."""
    if not isinstance(body, Mapping) or not token_contract.is_superseded(status, body.get("error")):
        return None
    holder = body.get("holder")
    return GenerationSupersededError(
        body.get("identity"), body.get("token_generation"), body.get("current_generation"),
        holder if isinstance(holder, Mapping) else None)


def superseded_from_response(resp: Any) -> GenerationSupersededError | None:
    """:func:`superseded_from_body` for an httpx response; an unreadable body is never this error."""
    try:
        return superseded_from_body(resp.status_code, resp.json())
    except Exception:  # noqa: BLE001 — the generic error stands
        return None


def _discover() -> Path | None:
    """The token file of this process's Claude ancestor: its pid, and the ``startedAt`` its registry file records.
    None when there is no Claude ancestor or no registry file for it."""
    explicit = (os.environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: leaf import cost; a module attribute so a patched config dir reaches it
    from nexus.session import _command_name_of, find_immediate_claude_pid  # noqa: PLC0415

    pid = find_immediate_claude_pid()
    if pid <= 0 or not _command_name_of(pid).lower().startswith("claude"):
        return None
    return ancestor_token_path(identity_dir(_nx_config.nexus_config_dir()), pid)


def token_file_path() -> Path | None:
    """The path of this process's token file, discovered once. A miss (the registry file had not appeared) is not
    cached: the next call looks again."""
    key = (os.environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip() or "ancestor"
    with _LOCK:
        if key in _DISCOVERED and _DISCOVERED[key] is not None:
            return _DISCOVERED[key]
    found = _discover()
    with _LOCK:
        _DISCOVERED[key] = found
    return found


def reset_for_tests() -> None:
    with _LOCK:
        _DISCOVERED.clear()


def current(*, now: float | None = None) -> TokenFile | None:
    """This process's token file record when it holds a token that may be sent now, else None."""
    path = token_file_path()
    record = read_token_file(path) if path is not None else None
    if record is None or not token_contract.usable(record.token, record.expires_at, now=now):
        return None
    return record


def identity_headers() -> dict[str, str]:
    """The header to add to a request, or ``{}``."""
    record = current()
    return token_contract.headers(record.token) if record is not None else {}


def retry_headers_after_401(sent: Mapping[str, str]) -> dict[str, str] | None:
    """After a 401 on a request that carried an identity token: re-read the file once. The new header when the file
    now holds a different, usable token; None when nothing was attached, nothing changed or nothing is usable (the
    caller then lets the 401 stand: no tokenless retry)."""
    attached = sent.get(token_contract.IDENTITY_TOKEN_HEADER)
    if not attached:
        return None
    record = current(now=time.time())
    if record is None or record.token == attached:
        return None
    return token_contract.headers(record.token)
