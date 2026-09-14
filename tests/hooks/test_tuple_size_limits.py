# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-r7xao (RDR-205 amendment) -- ``conexus/hooks/scripts/
_tuple_size_limits.py`` is the stdlib-only, no-``nexus``-import mirror of
the RDR-205 tuple-space size limits, used by ``tuple_ledger_project.py``
and ``mailbox_drain.py`` (neither may import ``nexus``). Parity with the
engine's own ``TupleLimits.java`` and the Python client's constants is
pinned separately by ``tests/db/test_tuple_size_limits_parity.py``; this
file is the direct unit coverage of the module's own logic,
``check_field_size``, mirroring the Python client's
``test_multibyte_character_counts_bytes_not_length`` (CRE minor: this
module previously had no dedicated multibyte test of its own, unlike the
client -- same trivial ``len(value.encode("utf-8"))`` logic, but asymmetric
coverage).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
MODULE_PATH = SCRIPTS_DIR / "_tuple_size_limits.py"


def _load():
    spec = importlib.util.spec_from_file_location("nx_tuple_size_limits", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_none_value_always_passes() -> None:
    sz = _load()
    assert sz.check_field_size("body", None, 0) is None


def test_value_at_the_limit_passes() -> None:
    sz = _load()
    assert sz.check_field_size("x", "a" * 10, 10) is None


def test_value_one_byte_over_the_limit_returns_a_reason() -> None:
    sz = _load()
    reason = sz.check_field_size("x", "a" * 11, 10)
    assert reason is not None
    assert "field 'x'" in reason
    assert "11 bytes" in reason
    assert "limit of 10 bytes" in reason


def test_multibyte_character_counts_bytes_not_length() -> None:
    """"é" is 2 bytes UTF-8, 1 char -- a length()-based check would halve the
    effective limit and wrongly pass this at the byte boundary, or wrongly
    refuse it well under a char-counted limit. Mirrors the Python client's
    ``test_multibyte_character_counts_bytes_not_length`` exactly."""
    sz = _load()
    value = "é" * 5  # 10 bytes, 5 chars
    assert sz.check_field_size("x", value, 10) is None  # exactly at the byte cap
    reason = sz.check_field_size("x", "é" * 6, 10)  # 12 bytes -- over
    assert reason is not None
    assert "12 bytes" in reason


def test_never_echoes_the_value() -> None:
    sz = _load()
    reason = sz.check_field_size("body", "SECRET" * 100, 10)
    assert reason is not None
    assert "SECRET" not in reason


def test_constants_match_the_module_docstring_scope() -> None:
    """This mirror carries the per-field caps only (never the whole-request
    cap): both hooks post per-field data, never construct the raw request
    body byte-for-byte the way the Python client's own pre-send guard
    does, so there is nothing for a whole-request constant to check here."""
    sz = _load()
    assert sz.MAX_BODY_BYTES == 4096
    assert sz.MAX_FIELD_VALUE_BYTES == 256
    assert sz.MAX_SUBSPACE_BYTES == 256
    assert sz.MAX_NONCE_BYTES == 128
    assert sz.MAX_CLAIMANT_BYTES == 128
    assert sz.MAX_CLAIM_ID_BYTES == 128
    assert not hasattr(sz, "MAX_REQUEST_BODY_BYTES")
