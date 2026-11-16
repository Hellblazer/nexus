# SPDX-License-Identifier: AGPL-3.0-or-later
"""The correlation id (RDR-167 Phase 1, nexus-dngtz.1.1): one id per ``nx`` invocation or MCP tool call.

One store: the ``ContextVar`` below. The CLI group callback and each MCP ``tools/call`` bind a fresh id
and reset it by token when the invocation or call ends, so nothing stays bound afterwards. The structlog
processor renders it from this variable; structlog's own contextvars are not used.

Standard library only, and outside ``nexus.db``: importing anything under ``nexus.db`` runs the package
init (about 200-320 ms with no bytecode cache, see ``db/client_identity.py``), and hooks and the CLI
cold start must not pay for it.
"""
from __future__ import annotations

import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar, Token
from typing import Any

#: The request header the engine clients send and the engine reads.
HEADER: str = "X-Correlation-ID"

_CORRELATION_ID: ContextVar[str | None] = ContextVar("nexus_correlation_id", default=None)


def new_correlation_id() -> str:
    """A fresh id: 32 lowercase hex characters, no prefix."""
    return uuid.uuid4().hex


def current_correlation_id() -> str | None:
    """The bound id, or ``None`` when nothing is bound."""
    return _CORRELATION_ID.get()


def bind(cid: str) -> Token[str | None]:
    """Bind ``cid`` for the current context. Pass the returned token to :func:`reset`."""
    return _CORRELATION_ID.set(cid)


def reset(token: Token[str | None]) -> None:
    """Undo the :func:`bind` that returned ``token``."""
    _CORRELATION_ID.reset(token)


def correlation_headers() -> dict[str, str]:
    """``{HEADER: id}`` when an id is bound, ``{}`` when not."""
    cid = _CORRELATION_ID.get()
    return {HEADER: cid} if cid is not None else {}


def add_correlation_id(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: add ``correlation_id`` when one is bound and the event has no such key."""
    cid = _CORRELATION_ID.get()
    if cid is not None and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict
