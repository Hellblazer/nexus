# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit coverage for the shared Claude Code OAuth credential picker
(``tests/e2e/lib/claude_credentials.py``, nexus-galkv.19).

The module lives under ``tests/e2e/lib/``, which is not on ``pythonpath``
(only ``scripts/`` is, per ``pyproject.toml``'s
``[tool.pytest.ini_options]``), so it is loaded by path via
``importlib.util`` — the same pattern ``tests/test_routing_hooks.py`` uses
for ``conexus/hooks/scripts/routing/_lib.py``.

These tests exercise the pure verdict logic only (no real Keychain access,
no subprocess); the picking/enumeration behavior is exercised end to end by
this box's real macOS Keychain via the agent's verify step
(``python3 tests/e2e/lib/claude_credentials.py pick``), not here.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_credentials.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("claude_credentials", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cc():
    return _load_module()


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_verdict_rejects_a_token_less_husk(cc) -> None:
    """The exact shape measured on this box (nexus-galkv.19 / nexus-qs1g6):
    an item whose claudeAiOauth carries neither an access nor a refresh
    token, with expiresAt at its zero default."""
    husk = {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}
    ok, why = cc.verdict(husk)
    assert ok is False
    assert "husk" in why


def test_verdict_rejects_expired_with_no_refresh_token(cc) -> None:
    expired = {
        "claudeAiOauth": {
            "accessToken": "at-stale",
            "refreshToken": "",
            "expiresAt": _now_ms() - 60_000,
        }
    }
    ok, why = cc.verdict(expired)
    assert ok is False
    assert "expired" in why


def test_verdict_accepts_expired_access_token_with_a_refresh_token(cc) -> None:
    """An expired access token is still usable when a refresh token is
    present — the CLI can renew it. This is the case a bare wall-clock
    ``expiresAt > now`` comparison (the pre-fix auth-login.sh cache check)
    could NOT distinguish from a permanently dead credential."""
    renewable = {
        "claudeAiOauth": {
            "accessToken": "at-stale",
            "refreshToken": "rt-live",
            "expiresAt": _now_ms() - 60_000,
        }
    }
    ok, why = cc.verdict(renewable)
    assert ok is True
    assert why == ""


def test_verdict_accepts_a_valid_unexpired_credential(cc) -> None:
    valid = {
        "claudeAiOauth": {
            "accessToken": "at-live",
            "refreshToken": "rt-live",
            "expiresAt": _now_ms() + 3_600_000,
        }
    }
    ok, why = cc.verdict(valid)
    assert ok is True
    assert why == ""


def test_verdict_accepts_a_missing_expiresAt_with_a_live_access_token(cc) -> None:
    """No expiresAt field at all (defaults to 0, the falsy/never-expires
    sentinel this verdict treats as "not proven expired") plus a present
    accessToken is usable."""
    no_expiry = {"claudeAiOauth": {"accessToken": "at-live", "refreshToken": ""}}
    ok, why = cc.verdict(no_expiry)
    assert ok is True
    assert why == ""


def test_verdict_rejects_missing_claudeAiOauth_entirely(cc) -> None:
    ok, why = cc.verdict({})
    assert ok is False
    assert "husk" in why


def test_verdict_rejects_none(cc) -> None:
    ok, why = cc.verdict(None)
    assert ok is False


def test_check_file_accepts_a_usable_credential(cc, tmp_path) -> None:
    valid = tmp_path / "creds.json"
    valid.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "at",
            "refreshToken": "rt",
            "expiresAt": _now_ms() + 3_600_000,
        }
    }))
    assert cc._cmd_check(str(valid)) == 0


def test_check_file_rejects_a_husk(cc, tmp_path) -> None:
    husk = tmp_path / "husk.json"
    husk.write_text('{"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}')
    assert cc._cmd_check(str(husk)) == 1


def test_check_file_rejects_an_unreadable_path(cc, tmp_path) -> None:
    missing = tmp_path / "nope.json"
    assert cc._cmd_check(str(missing)) == 1


def test_check_file_rejects_invalid_json(cc, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert cc._cmd_check(str(bad)) == 1
