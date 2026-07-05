# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-7yfe6: a transient upsert 5xx (gateway 504 / pool 503 / 502) on the
direct prose/PDF upsert path must DEFER the file (return 0 → staleness retries),
not propagate — so one gateway blip does not fail (and, under concurrency, hang)
the whole index run. Permanent errors still propagate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db.http_vector_client import VectorServiceError
from nexus.indexer import _contain_transient_upsert

_FILE = Path("/repo/doc.md")


def test_success_returns_value() -> None:
    assert _contain_transient_upsert(lambda: 7, _FILE) == 7


@pytest.mark.parametrize("code", [502, 503, 504])
def test_transient_5xx_deferred_returns_zero(code: int) -> None:
    def boom() -> int:
        raise VectorServiceError(f"gateway said {code}", code=code)

    # Deferred, not raised: 0 chunks written this run, staleness retries next run.
    assert _contain_transient_upsert(boom, _FILE) == 0


@pytest.mark.parametrize("code", [400, 401, 404, 422, 500])
def test_permanent_error_propagates(code: int) -> None:
    def boom() -> int:
        raise VectorServiceError(f"hard error {code}", code=code)

    with pytest.raises(VectorServiceError):
        _contain_transient_upsert(boom, _FILE)


def test_transport_error_code_none_propagates() -> None:
    # code=None (transport-level failure) is NOT a transient gateway 5xx → raise.
    def boom() -> int:
        raise VectorServiceError("connection reset", code=None)

    with pytest.raises(VectorServiceError):
        _contain_transient_upsert(boom, _FILE)


def test_non_vector_error_propagates() -> None:
    # A non-VectorServiceError (e.g. a real bug) must never be swallowed.
    def boom() -> int:
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        _contain_transient_upsert(boom, _FILE)
