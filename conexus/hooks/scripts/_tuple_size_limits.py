#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stdlib-only, no-``nexus``-import mirror of the RDR-205 tuple-space size
limits (bead nexus-r7xao) -- for the two hook scripts under this directory
that POST to ``/v1/tuples`` with plain ``urllib.request`` and therefore
cannot import ``nexus.db.t2.http_tuple_store`` (the ``nexus-vg6d4`` rule
every module under ``conexus/hooks/scripts/`` follows).

The tuple space is a coordination and metadata store, not a value store
(Sam, 2026-09-13). These numbers mirror ``dev.nexus.service.db.TupleLimits``
(the engine's own copy) and ``nexus.db.t2.http_tuple_store``'s module-level
constants of the same name minus the leading underscore; all three are kept
equal by ``tests/db/test_tuple_size_limits_parity.py``, which reads the
Java source's constants directly rather than trusting a fourth hand-typed
copy to stay in sync.

Only the GLOBAL body cap is mirrored here, same as the Python client: a
template's own (possibly lower) ``max_body_bytes`` is engine-side knowledge
neither this module nor the client carries a copy of.
"""
from __future__ import annotations

MAX_BODY_BYTES: int = 4096
MAX_FIELD_VALUE_BYTES: int = 256
MAX_SUBSPACE_BYTES: int = 256
MAX_NONCE_BYTES: int = 128
MAX_CLAIMANT_BYTES: int = 128
MAX_CLAIM_ID_BYTES: int = 128


def check_field_size(field: str, value: str | None, limit: int) -> str | None:
    """Return a SKIP reason string when *value*'s UTF-8 byte length exceeds
    *limit*, or ``None`` when it is within bounds (including ``value is
    None``, which is 0 bytes). Never includes *value* itself in the
    returned reason (bead nexus-r7xao).

    A reason string, not an exception: both hooks that use this module
    treat an oversized field as one more `_Skip`-shaped condition, logged
    and exited 0 like every other refusal on these best-effort, fire-and-
    forget write paths -- never a propagated traceback.
    """
    if value is None:
        return None
    n = len(value.encode("utf-8"))
    if n > limit:
        return f"field '{field}' is {n} bytes, exceeding the limit of {limit} bytes"
    return None
