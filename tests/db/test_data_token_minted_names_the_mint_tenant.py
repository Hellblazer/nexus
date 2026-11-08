# SPDX-License-Identifier: AGPL-3.0-or-later
"""``data_token_minted`` names the tenant the token was minted for, not only the lease key.

With ``mint_tenant`` configured the mint body carries that tenant, while the caller's lease key stays the default
tenant. The event logged only the lease key, so a gate-xr789 sweep read ``tenant=default`` on 2026-11-07 (bob)
while the data matched gate-xr789 exactly.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from nexus.db import data_token
from nexus.db.data_token import DataTokenManager

URL = "http://127.0.0.1:9999"


@pytest.fixture(autouse=True)
def info_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


def _manager(cfg: Path, mint_tenant: str, seen: list[dict]) -> DataTokenManager:
    def poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        seen.append(body)
        return 200, {"data_token": "tok", "expires_in_seconds": 300}, {}

    return DataTokenManager(
        poster=poster, mint_credential=lambda: "mintcred", mint_tenant=lambda: mint_tenant,
        config_dir=cfg, sleep=lambda s: None,
    )


def _minted(captured: list[dict[str, Any]]) -> dict[str, Any]:
    (event,) = [e for e in captured if e["event"] == "data_token_minted"]
    return event


def test_a_configured_mint_tenant_is_named_on_the_minted_event(tmp_path: Path) -> None:
    seen: list[dict] = []
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path, "gate-xr789", seen).bearer_for(URL, "default")
    event = _minted(captured)
    assert seen and seen[0].get("tenant") == "gate-xr789", "the mint body carries the configured tenant"
    assert event["minted_for"] == "gate-xr789"
    assert event["tenant"] == "default", "the lease key is still logged as before"


def test_without_a_mint_tenant_the_event_names_the_callers_tenant(tmp_path: Path) -> None:
    seen: list[dict] = []
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path, "", seen).bearer_for(URL, "default")
    assert _minted(captured)["minted_for"] == "default"


def test_the_unguarded_mint_path_names_the_mint_tenant_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The path taken when the mint lock cannot be opened logs its own data_token_minted."""
    def refuse(*a: Any, **k: Any) -> int:
        raise PermissionError("no lock")

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", refuse)
    seen: list[dict] = []
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path, "gate-xr789", seen).bearer_for(URL, "default")
    assert any(e["event"] == "data_token_mint_lock_unavailable" for e in captured)
    assert _minted(captured)["minted_for"] == "gate-xr789"
