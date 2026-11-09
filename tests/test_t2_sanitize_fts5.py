# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_sanitize_fts5`` is a pure function of its query text and reaches no storage, so
these cases live apart from ``tests/test_t2.py``, whose module mark gives every test
there a tenant. The sanitizer's engine-backed cases stay in that file."""
from __future__ import annotations

import pytest

from nexus.db.t2 import _sanitize_fts5


@pytest.mark.parametrize("input_q,expected", [
    ("hello world", "hello world"),
    ("verification-probe", '"verification-probe"'),
    ("phase:1", '"phase:1"'),
    ("foo bar-baz", 'foo "bar-baz"'),
    ('say"hello', '"say""hello"'),
])
def test_sanitize_fts5(input_q: str, expected: str) -> None:
    assert _sanitize_fts5(input_q) == expected
