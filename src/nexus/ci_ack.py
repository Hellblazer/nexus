# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ack of a develop red names its poster by identity id (RDR-252, bead nexus-igea9).

An ack row on ``board/ci/<topic>--owner`` counts as the owner's only when its ``from`` is the owner's identity id: the
engine and ``nx group ack-times`` both match on it. A row posted by hand with ``from=<session alias>`` (tuple b18af4e1,
2026-11-20) counted as no ack at all. :func:`stamp_ack_from` is the one place every client write passes through
(``HttpTupleStore``'s ``out`` and ``out_replace``, so ``nx tuple out``, the ``tuple_out`` MCP tool and ``nx ci ack``
alike): it stamps ``from`` with the caller's own identity, whatever the caller typed, and refuses a caller with no
identity rather than write a name.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

import structlog

_log = structlog.get_logger(__name__)

#: ``board/ci/<topic>--owner``: the subspace the stamper posts owner rows on and an owner acknowledges on.
OWNER_SUBSPACE = re.compile(r"board/ci/[^/]+--owner")

NO_IDENTITY_MESSAGE = (
    "this process holds no identity, so it cannot sign an ack: an ack on a board/ci owner subspace must carry the "
    "poster's identity id in `from`, never a name. Run `nx identity show` to see whether this session holds one"
)


class NoAckIdentityError(ValueError):
    """The caller holds no identity, so an ack it wrote could only carry a name."""


def own_identity() -> str:
    """The identity id this process holds, or "" when it holds none: the leased token file, else the token file that
    binds one of this session's ids (the resolution ``mailbox_send`` checks ``from_address`` against)."""
    from nexus.tuple_directory import _own_identity  # noqa: PLC0415 -- deferred: leaf import cost on the write path

    try:
        return _own_identity(None, None)
    except Exception:  # noqa: BLE001 -- an identity that cannot be read is no identity: the write is refused, not guessed
        return ""


def is_ack_write(subspace: str, dims: Mapping[str, str] | None) -> bool:
    """True for a ``kind=ack`` row on a ``board/ci/<topic>--owner`` subspace."""
    return bool(OWNER_SUBSPACE.fullmatch(subspace)) and (dims or {}).get("kind") == "ack"


def stamp_ack_from(subspace: str, dims: dict[str, str] | None) -> dict[str, str] | None:
    """*dims* with ``from`` set to the caller's own identity when this write is an ack of a develop red; every other
    write is returned as it came. The caller's dict is never changed. A different ``from`` than the identity (a
    session alias, an agent name, another identity) is replaced and logged once at info. Raises
    :class:`NoAckIdentityError` when the caller holds no identity."""
    if not is_ack_write(subspace, dims):
        return dims
    assert dims is not None
    identity = own_identity()
    if not identity:
        raise NoAckIdentityError(NO_IDENTITY_MESSAGE)
    typed = dims.get("from")
    if typed == identity:
        return dims
    _log.info("ci_ack_from_restamped", subspace=subspace, typed=typed, stamped=identity,
              note="an ack's `from` is the poster's identity id; the typed value was replaced")
    return {**dims, "from": identity}
