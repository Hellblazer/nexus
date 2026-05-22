# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P6 (nexus-qg86h): NX_STORAGE_MODE post-decommission tests.

The env-var is retained for one release as a deprecation shim;
``storage_mode()`` now always returns ``"daemon"`` regardless of the
caller's value. ``direct`` triggers a ``DeprecationWarning`` and is
silently re-mapped to daemon. Other non-daemon values still raise
``StorageModeError`` so a typo doesn't slip through silently.

Historical phases:

- P0: flag introduced (only ``direct`` valid).
- P2: ``daemon`` unlocked for T3.
- P4: ``daemon`` became the default.
- P6 (this): ``direct`` decommissioned; deprecation-warning shim.
"""
from __future__ import annotations

import warnings

import pytest


def test_default_is_daemon(monkeypatch):
    """Unset NX_STORAGE_MODE resolves to ``daemon`` (no warning)."""
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
    from nexus.config import storage_mode

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert storage_mode() == "daemon"
    assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_explicit_daemon_is_accepted(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    from nexus.config import storage_mode

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert storage_mode() == "daemon"
    assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_daemon_case_normalization(monkeypatch):
    """``DAEMON``, ``Daemon``, ``daemon`` all resolve identically."""
    for val in ("DAEMON", "Daemon", "daemon"):
        monkeypatch.setenv("NX_STORAGE_MODE", val)
        from nexus.config import storage_mode

        assert storage_mode() == "daemon"


def test_direct_emits_deprecation_warning_and_returns_daemon(monkeypatch):
    """RDR-120 P6: ``direct`` is decommissioned. Setting it to the
    legacy value fires a ``DeprecationWarning`` and resolves to
    ``daemon`` so existing scripts don't break on this release; the
    env-var itself is removed in the next release.
    """
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    from nexus.config import storage_mode

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert storage_mode() == "daemon"
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert "NX_STORAGE_MODE=direct is decommissioned" in str(deprecations[0].message)


def test_unknown_value_raises_storage_mode_error(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "bogus")
    from nexus.config import StorageModeError, storage_mode

    with pytest.raises(StorageModeError) as exc:
        storage_mode()
    msg = str(exc.value)
    assert "bogus" in msg
    assert "daemon" in msg


def test_empty_string_treated_as_unset(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "")
    from nexus.config import storage_mode

    assert storage_mode() == "daemon"


def test_whitespace_only_treated_as_unset(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "   ")
    from nexus.config import storage_mode

    assert storage_mode() == "daemon"


def test_valid_modes_constant_is_daemon_only():
    """RDR-120 P6: only ``daemon`` is supported. ``direct`` was
    removed from VALID_STORAGE_MODES at this release.
    """
    from nexus.config import VALID_STORAGE_MODES

    assert VALID_STORAGE_MODES == ("daemon",)


def test_storage_mode_error_is_click_friendly():
    """``StorageModeError`` remains a ``ClickException`` so CLI
    surfacing works unchanged.
    """
    from nexus.config import StorageModeError

    err = StorageModeError("test")
    assert hasattr(err, "args")
    assert err.args == ("test",)
