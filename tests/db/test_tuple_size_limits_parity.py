# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-r7xao: one set of numbers, two copies, pinned equal.

The RDR-205 tuple-space size limits (Sam's decision 2026-09-13 — the tuple
space is a metadata store, not a value store) are decided ONCE and must
never drift between:

1. The engine — ``service/src/main/java/dev/nexus/service/db/
   TupleLimits.java``, the authority (it is what actually enforces them).
2. The Python client — ``nexus.db.t2.http_tuple_store``'s module-level
   ``_MAX_*`` constants, which the wheel's ledger projector
   (``nexus.hooks.tuple_ledger_project``) imports rather than copying.

A third copy, the plugin's stdlib-only ``_tuple_size_limits.py``, was
deleted with the last plugin hook script that read it (nexus-z9cz2).

This test reads the Java source directly (never a third hand-typed copy
here) and compares it to the client, so a number changed in one place and
forgotten in the other reds instead of shipping silently mismatched.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAVA_FILE = (
    _REPO_ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"
    / "db" / "TupleLimits.java"
)

#: Java field name -> its recorded value. The client carries each one
#: underscore-prefixed and module-private.
_FIELDS = {
    "MAX_BODY_BYTES": 4096,
    "MAX_FIELD_VALUE_BYTES": 256,
    "MAX_SUBSPACE_BYTES": 256,
    "MAX_NONCE_BYTES": 128,
    "MAX_CLAIMANT_BYTES": 128,
    "MAX_CLAIM_ID_BYTES": 128,
    "MAX_REQUEST_BODY_BYTES": 8192,
}

_JAVA_CONST = re.compile(
    r"public static final int (MAX_[A-Z_]+)\s*=\s*(\d+);"
)


def _java_constants() -> dict[str, int]:
    assert _JAVA_FILE.is_file(), f"expected {_JAVA_FILE}"
    text = _JAVA_FILE.read_text()
    return {name: int(value) for name, value in _JAVA_CONST.findall(text)}


def _client_constants() -> dict[str, int]:
    import nexus.db.t2.http_tuple_store as mod  # noqa: PLC0415 -- deliberately deferred

    return {
        name: getattr(mod, f"_{name}")
        for name in _FIELDS
        if hasattr(mod, f"_{name}")
    }


def test_java_constants_found_and_non_vacuous() -> None:
    java = _java_constants()
    for name, expected in _FIELDS.items():
        assert name in java, f"{_JAVA_FILE}: constant {name} not found by the parity regex"
        assert java[name] == expected, (
            f"{_JAVA_FILE}: {name} = {java[name]}, but this test's own recorded "
            f"value is {expected} -- update BOTH sides deliberately, this is not "
            "a drift the fix belongs only on one side of"
        )


def test_client_constants_match_the_engine() -> None:
    java = _java_constants()
    client = _client_constants()
    for name in _FIELDS:
        assert name in client, f"nexus.db.t2.http_tuple_store carries no _{name}"
        assert client[name] == java[name], (
            f"{name}: engine={java[name]} client={client[name]} -- these must match"
        )
