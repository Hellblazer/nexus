# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f9bgu.52: on Windows, nx verifies TLS through the OS trust store.

Measured on a clean Windows 11 guest: uv's standalone CPython verifies with
OpenSSL against the Windows ROOT store, which on a fresh machine holds ~17 roots
(the OS fetches the rest on demand only through its own verifier), so nx init
failed CERTIFICATE_VERIFY_FAILED fetching the PG bundle until a PowerShell request
primed the store. ``nexus._use_os_trust_store`` routes ``ssl`` through truststore.
"""
from __future__ import annotations

import ssl
import sys
import types

import pytest

import nexus


def test_posix_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setitem(sys.modules, "truststore", types.SimpleNamespace(inject_into_ssl=lambda: called.append(True)))
    assert nexus._use_os_trust_store(platform="linux") is False
    assert nexus._use_os_trust_store(platform="darwin") is False
    assert called == []


def test_windows_routes_ssl_through_the_os(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setitem(sys.modules, "truststore", types.SimpleNamespace(inject_into_ssl=lambda: called.append(True)))
    assert nexus._use_os_trust_store(platform="win32") is True
    assert called == [True]


def test_a_missing_dependency_falls_back_to_openssl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "truststore", None)  # import raises ImportError
    assert nexus._use_os_trust_store(platform="win32") is False


def test_the_dependency_is_windows_only() -> None:
    import tomllib
    from pathlib import Path

    deps = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["project"]["dependencies"]
    trust = [d for d in deps if d.split(">")[0].split("=")[0].strip() == "truststore"]
    assert trust == ["truststore>=0.10,<0.11; sys_platform == 'win32'"]


@pytest.mark.skipif(sys.platform != "win32", reason="real truststore on a Windows host; the seams above cover the branch everywhere")
class TestRealWindows:
    def test_importing_nexus_puts_ssl_on_the_os_verifier(self) -> None:
        import truststore

        assert ssl.SSLContext is truststore.SSLContext, "nexus import must inject truststore on Windows"
        ctx = ssl.create_default_context()
        assert isinstance(ctx, truststore.SSLContext)
