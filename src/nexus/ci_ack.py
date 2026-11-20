# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ack of a develop red names its poster by identity id in ``from`` (RDR-252, bead nexus-igea9).

The engine never reads an ack's ``from``. It counts an ack when the row's sender-session stamp, mapped through the
session index, names the red's current owner. ``from`` is a label: the one ``nx group ack-times`` and people read, and
the one the engine is to refuse a mismatch on. A row posted by hand with ``from=<session alias>`` (tuple b18af4e1,
2026-11-20) still stopped the hand-off, and only ack-times counted it as no ack.

So every client write of a ``kind=ack`` row to a ``board/ci/<topic>--owner`` subspace carries the caller's own identity
in ``from``, whatever the caller typed. :func:`stamp_ack_from` does it in ``HttpTupleStore._out_raw``, the one place
``out`` and ``out_replace`` send from (so ``nx tuple out``, the ``tuple_out`` MCP tool and ``nx ci ack`` alike), and
:func:`stamp_ack_reply` does it for a reply an ``ack`` writes as it consumes a claim. The identity is the token
record's, as ``nx group`` reads it: no session-bound fallback, so a child that shares its parent's session but holds no
token of its own is refused, not signed as the parent. A caller with no identity is refused rather than written under a
name.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from typing import Any

import structlog

from nexus.tuple_directory import looks_like_identity_id

_log = structlog.get_logger(__name__)

#: ``board/ci/<topic>--owner``: the subspace the stamper posts owner rows on and an owner acknowledges on.
OWNER_SUBSPACE = re.compile(r"board/ci/[^/]+--owner")

NO_IDENTITY_MESSAGE = (
    "this process holds no identity, so it cannot sign an ack: an ack on a board/ci owner subspace carries the "
    "poster's identity id in `from`, never a name. Run `nx identity show` to see whether this session holds one"
)


def own_identity() -> str:
    """The identity id this process's token record names, or "" when it holds none (no bound Claude ancestor, no usable
    token). Nothing else counts: a failure of the reader itself is raised, never read as "no identity"."""
    from nexus.identity import token_header  # noqa: PLC0415 -- deferred: leaf import cost on the write path

    record = token_header.current()
    if record is None or not looks_like_identity_id(str(record.identity)):
        return ""
    return str(record.identity)


def is_ack_write(subspace: str, dims: Mapping[str, str] | None) -> bool:
    """True for a ``kind=ack`` row on a ``board/ci/<topic>--owner`` subspace."""
    return bool(OWNER_SUBSPACE.fullmatch(subspace)) and (dims or {}).get("kind") == "ack"


def stamp_ack_from(subspace: str, dims: dict[str, str] | None) -> dict[str, str] | None:
    """*dims* with ``from`` set to the caller's own identity when this write is an ack of a develop red; every other
    write is returned as it came. The caller's dict is never changed. A different ``from`` than the identity (a
    session alias, an agent name, another identity) is replaced and logged once at info. Raises ``ValueError`` when the
    caller holds no identity."""
    if not is_ack_write(subspace, dims):
        return dims
    assert dims is not None
    identity = own_identity()
    if not identity:
        raise ValueError(NO_IDENTITY_MESSAGE)
    typed = dims.get("from")
    if typed == identity:
        return dims
    _log.info("ci_ack_from_restamped", subspace=subspace, typed=typed, stamped=identity,
              note="an ack's `from` is the poster's identity id; the typed value was replaced")
    return {**dims, "from": identity}


def stamp_ack_reply(reply: Any) -> Any:
    """*reply* (a :class:`~nexus.db.t2.records.ReplySpec`) with its ``dims`` stamped by :func:`stamp_ack_from`: the engine
    writes a reply as an ``out``, so an ack riding on a claim's reply is an ack write too. None and every other reply pass
    through untouched."""
    if reply is None:
        return None
    dims = stamp_ack_from(reply.subspace, reply.dims)
    return reply if dims is reply.dims else dataclasses.replace(reply, dims=dims)
