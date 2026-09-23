# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-r7xao (RDR-205 amendment) -- direct unit coverage of the ledger
projector's per-field size pre-check,
:func:`nexus.hooks.tuple_ledger_project._check_field_size`.

This file used to test ``conexus/hooks/scripts/_tuple_size_limits.py``, the
plugin's stdlib-only mirror of the limits; that mirror was deleted with the
last plugin script that used it (nexus-z9cz2). The wheel projector imports
its constants from :mod:`nexus.db.t2.http_tuple_store`, whose parity with the
engine's ``TupleLimits.java`` is pinned by
``tests/db/test_tuple_size_limits_parity.py``; what stays here is the check's
own logic, including the multibyte case the Python client pins with its own
``test_multibyte_character_counts_bytes_not_length``.
"""
from __future__ import annotations

from nexus.hooks.tuple_ledger_project import _check_field_size


def test_none_value_always_passes() -> None:
    assert _check_field_size("body", None, 0) is None


def test_value_at_the_limit_passes() -> None:
    assert _check_field_size("x", "a" * 10, 10) is None


def test_value_one_byte_over_the_limit_returns_a_reason() -> None:
    reason = _check_field_size("x", "a" * 11, 10)
    assert reason is not None
    assert "field 'x'" in reason
    assert "11 bytes" in reason
    assert "limit of 10 bytes" in reason


def test_multibyte_character_counts_bytes_not_length() -> None:
    """"é" is 2 bytes UTF-8, 1 char -- a length()-based check would halve the
    effective limit and wrongly pass this at the byte boundary, or wrongly
    refuse it well under a char-counted limit. Mirrors the Python client's
    ``test_multibyte_character_counts_bytes_not_length`` exactly."""
    value = "é" * 5  # 10 bytes, 5 chars
    assert _check_field_size("x", value, 10) is None  # exactly at the byte cap
    reason = _check_field_size("x", "é" * 6, 10)  # 12 bytes -- over
    assert reason is not None
    assert "12 bytes" in reason


def test_never_echoes_the_value() -> None:
    reason = _check_field_size("body", "SECRET" * 100, 10)
    assert reason is not None
    assert "SECRET" not in reason
