# SPDX-License-Identifier: AGPL-3.0-or-later
"""The opener seam of the two mailbox-drain copies (nexus-ec37o).

A drain request that carries a bearer is sent through ``bearer_opener`` (the proxy and CA of
``config.yml``, an HTTPS handler on the bearer context, no redirect followed), not through a bare
``urllib.request.build_opener``: the hook modules no longer call ``build_opener`` themselves, so a
test that swaps ``urllib.request.build_opener`` in the hook module is bypassed and the request
goes to the real network. The seam is ``bearer_opener``: the plugin copy calls it on its
``_endpoint_resolve`` module (``_ep``), the wheel copy imports it from
``nexus.db.t2.bearer_transport`` at call time.
"""
from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest


def _seam_owner(hook: ModuleType) -> ModuleType:
    if hasattr(hook, "_ep"):  # the plugin copy, which cannot import nexus
        return hook._ep
    from nexus.db.t2 import bearer_transport  # noqa: PLC0415 — the wheel copy's source of the opener

    return bearer_transport


def swap_opener(monkeypatch: pytest.MonkeyPatch, hook: ModuleType, make: Callable[[], Any]) -> list[str]:
    """Make *hook*'s bearer requests go through ``make()``'s opener; returns the URLs it was asked for."""
    asked: list[str] = []

    def bearer_opener(url: str, *_config_dir: Any) -> Any:
        asked.append(url)
        return make()

    monkeypatch.setattr(_seam_owner(hook), "bearer_opener", bearer_opener)
    return asked

