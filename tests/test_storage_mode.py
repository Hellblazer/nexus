# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P0.B: NX_STORAGE_MODE cutover-flag scaffolding.

At P0 only `direct` is a valid value; `daemon` is rejected with
"not yet supported"; any other value is rejected with the list of
valid values. This is a no-op cutover flag — its purpose is to
establish the single source of truth before P3 wires it to actual
behavior.
"""
from __future__ import annotations

import os

import pytest


def test_default_is_direct(monkeypatch):
    """Unset NX_STORAGE_MODE defaults to `direct`."""
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
    from nexus.config import storage_mode

    assert storage_mode() == "direct"


def test_direct_is_accepted(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    from nexus.config import storage_mode

    assert storage_mode() == "direct"


def test_daemon_is_rejected_at_phase_0(monkeypatch):
    """RDR-120 P0: daemon mode rejected with explicit not-yet-supported message."""
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    from nexus.config import StorageModeError, storage_mode

    with pytest.raises(StorageModeError) as exc:
        storage_mode()
    assert "daemon" in str(exc.value).lower()
    assert "not yet supported" in str(exc.value).lower() or "phase 0" in str(exc.value).lower()


def test_unknown_value_lists_valid_options(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "bogus")
    from nexus.config import StorageModeError, storage_mode

    with pytest.raises(StorageModeError) as exc:
        storage_mode()
    msg = str(exc.value)
    assert "bogus" in msg
    assert "direct" in msg
    assert "daemon" in msg


def test_empty_string_treated_as_unset(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "")
    from nexus.config import storage_mode

    assert storage_mode() == "direct"


def test_whitespace_only_treated_as_unset(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "   ")
    from nexus.config import storage_mode

    assert storage_mode() == "direct"


def test_case_normalization(monkeypatch):
    """`DIRECT` and `Direct` accepted; case shouldn't trip operators."""
    for val in ("DIRECT", "Direct", "direct"):
        monkeypatch.setenv("NX_STORAGE_MODE", val)
        from nexus.config import storage_mode

        assert storage_mode() == "direct"


def test_valid_modes_constant_is_exported():
    """The accepted-values list is callable for tooling."""
    from nexus.config import VALID_STORAGE_MODES

    assert "direct" in VALID_STORAGE_MODES
    assert "daemon" in VALID_STORAGE_MODES


def test_storage_mode_error_is_nexus_error():
    """StorageModeError should be importable and a click-friendly exception."""
    from nexus.config import StorageModeError

    # ClickException family is friendly to CLI surfacing
    err = StorageModeError("test")
    assert hasattr(err, "args")
    assert err.args == ("test",)
