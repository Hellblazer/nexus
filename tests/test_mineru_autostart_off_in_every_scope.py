# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite's MinerU kill switch reaches fixtures of every scope (labrun-pex).

``_pin_mineru_autostart_off`` (tests/conftest.py, nexus-1qdb9) is a function-scoped autouse
``monkeypatch.setenv``, and pytest sets up a module- or session-scoped fixture before it. On chas a CI-shaped
shard showed the result: test_pdf_chunker_integration's module fixture ran the real PDFExtractor with
autostart on, and the mineru-api server it started (1.04 GB anon) lived from 366 s to the end of the run,
across the shard's memory peak. The pin is now also set at conftest import, so these scopes see it too."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def _autostart_seen_by_a_session_fixture() -> str | None:
    return os.environ.get("NX_MINERU_AUTOSTART")


@pytest.fixture(scope="module")
def _autostart_seen_by_a_module_fixture() -> str | None:
    return os.environ.get("NX_MINERU_AUTOSTART")


def test_a_module_scoped_fixture_sees_mineru_autostart_off(_autostart_seen_by_a_module_fixture) -> None:
    assert _autostart_seen_by_a_module_fixture == "0"


def test_a_session_scoped_fixture_sees_mineru_autostart_off(_autostart_seen_by_a_session_fixture) -> None:
    assert _autostart_seen_by_a_session_fixture == "0"
