# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the shared conexus/hooks/scripts/_hook_logging.py helper
(nexus-cnzei.2 fix round 2, critic Significant: the per-file
configure_logging(mode="hook") fix was hand-duplicated in rdr_hook.py and
routing/phase_review_close_requires_gate.py with no shared home). Factored
into one module so a THIRD hook script importing nexus.* in the future has
one call to make, and tests/hooks/test_hook_scripts_configure_logging_before_nexus_import.py
can enforce that it does.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "_hook_logging.py"
)


@pytest.fixture()
def hook_logging_module():
    spec = importlib.util.spec_from_file_location("hook_logging_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calls_configure_logging_with_hook_mode(hook_logging_module, monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "nexus.logging_setup.configure_logging",
        lambda mode=None, **kw: calls.append({"mode": mode, **kw}),
    )
    hook_logging_module.configure_hook_logging()
    assert calls == [{"mode": "hook"}]


def test_swallows_a_configure_logging_failure(hook_logging_module, monkeypatch) -> None:
    """THE inner best-effort catch this whole module exists to provide:
    a broken nexus.logging_setup.configure_logging must never propagate
    out of configure_hook_logging()."""
    def boom(mode=None, **kw):
        raise RuntimeError("logging setup broken")

    monkeypatch.setattr("nexus.logging_setup.configure_logging", boom)
    hook_logging_module.configure_hook_logging()  # must not raise


def test_swallows_a_missing_nexus_package(hook_logging_module, monkeypatch) -> None:
    """nexus-4ti7e: an interpreter with no nexus package installed at all
    is a real, observed case for a bare-python3 hook invocation."""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "nexus.logging_setup" or name.startswith("nexus."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    hook_logging_module.configure_hook_logging()  # must not raise
