# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P2: NX_STORAGE_MODE cutover-flag (T3 daemon mode unlocked).

P0 introduced the flag as a no-op (only `direct` valid, `daemon`
rejected with "not yet supported"). P2 (nexus-ut8zy) unblocks the
`daemon` branch so ``make_t3()`` can dispatch through the daemon
``T3Client`` in local + daemon mode.

Validation matrix:

- unset / empty / whitespace -> "direct" (default)
- "direct" (any case) -> "direct"
- "daemon" (any case) -> "daemon"  (P2 onward)
- anything else -> StorageModeError naming the bad value
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


def test_daemon_is_accepted_at_phase_2(monkeypatch):
    """RDR-120 P2 (nexus-ut8zy): daemon mode is valid for T3 dispatch.

    Pre-P2 this asserted rejection with "not yet supported"; that
    behaviour shipped from P0 through 4.33.1. The P2 cutover flipped
    the rejection to acceptance so make_t3() can route through the
    T3 daemon.
    """
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    from nexus.config import storage_mode

    assert storage_mode() == "daemon"


def test_daemon_case_normalization(monkeypatch):
    """`DAEMON` and `Daemon` accepted; case shouldn't trip operators."""
    for val in ("DAEMON", "Daemon", "daemon"):
        monkeypatch.setenv("NX_STORAGE_MODE", val)
        from nexus.config import storage_mode

        assert storage_mode() == "daemon"


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
