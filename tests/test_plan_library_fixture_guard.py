# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixture rows must never reach a real install's plan library.

On 2026-08-21 a reproduction script for a seed-collision bug constructed a
bare HttpPlanLibrary() outside pytest. That resolves the machine's own
service lease — the developer's REAL library — so it created two fixture
rows there, and the bug it was reproducing destroyed one of them. The
survivor was found only because the new disk-vs-live parity check reported
it as an orphan.

pytest itself was never the hole: the autouse substrate fixture points
every test at a hermetic engine with a per-test tenant. The hole is
everything that is NOT pytest, which is precisely where ad-hoc repro
scripts live.
"""
from __future__ import annotations

import pytest

from nexus.db.t2.http_plan_library import _refuse_test_fixture_write_to_production


def test_fixture_tagged_write_is_refused_without_an_explicit_substrate(monkeypatch):
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    with pytest.raises(RuntimeError, match="test-fixture"):
        _refuse_test_fixture_write_to_production("builtin-template,test-fixture")


def test_fixture_tagged_write_is_allowed_against_an_explicit_substrate(monkeypatch):
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:1")
    _refuse_test_fixture_write_to_production("builtin-template,test-fixture")


def test_untagged_writes_are_never_touched(monkeypatch):
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    _refuse_test_fixture_write_to_production("builtin-template")
    _refuse_test_fixture_write_to_production("")


def test_tag_is_matched_as_a_comma_token(monkeypatch):
    """Not a substring — 'not-test-fixtures' is a different tag."""
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    _refuse_test_fixture_write_to_production("not-test-fixtures")
