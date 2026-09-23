# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :func:`nexus._hook_runtime._io.configure_hook_logging`, the
logging bridge a hook verb calls before anything that logs through
structlog, so those lines go to stderr and the hook log instead of the
hook's stdout decision channel (nexus-cnzei.2).

These pins were written against the plugin-resident
``conexus/hooks/scripts/_hook_logging.py`` helper; that copy was deleted at
nexus-z9cz2 and the same contract lives in the wheel.
"""
from __future__ import annotations

import pytest

from nexus._hook_runtime._io import configure_hook_logging


def test_calls_configure_logging_with_hook_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "nexus.logging_setup.configure_logging",
        lambda mode=None, **kw: calls.append({"mode": mode, **kw}),
    )
    configure_hook_logging()
    assert calls == [{"mode": "hook"}]


def test_swallows_a_configure_logging_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The best-effort catch: a broken
    nexus.logging_setup.configure_logging must never propagate out of
    configure_hook_logging()."""
    def boom(mode=None, **kw):
        raise RuntimeError("logging setup broken")

    monkeypatch.setattr("nexus.logging_setup.configure_logging", boom)
    configure_hook_logging()  # must not raise


def test_swallows_an_unimportable_logging_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-4ti7e: an interpreter that cannot import
    ``nexus.logging_setup`` is a real, observed case; the hook must still
    run."""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "nexus.logging_setup":
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    configure_hook_logging()  # must not raise
