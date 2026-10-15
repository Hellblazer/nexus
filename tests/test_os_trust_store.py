# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f9bgu.52: on Windows, nx verifies TLS through the OS trust store.

Measured on a clean Windows 11 guest: uv's standalone CPython verifies with
OpenSSL against the Windows ROOT store, which on a fresh machine holds ~17 roots
(the OS fetches the rest on demand only through its own verifier), so nx init
failed CERTIFICATE_VERIFY_FAILED fetching the PG bundle until a PowerShell request
primed the store. ``nexus._use_os_trust_store`` routes ``ssl`` through truststore.
"""
from __future__ import annotations

import platform
import ssl
import subprocess
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


class _WinVer(tuple):
    """``sys.getwindowsversion()``'s shape: a tuple whose first three items are
    major, minor, build, plus ``platform_version`` (kernel32.dll's version)."""

    platform_version: tuple[int, int, int]

    def __new__(cls, real: tuple[int, int, int], kernel32: tuple[int, int, int] | None):
        self = super().__new__(cls, (*real, 0, ""))
        self.platform_version = kernel32  # type: ignore[assignment]
        return self


#: Windows 11 25H2, as measured on qwentescence 2026-10-14: WMI, the registry and
#: ``ver`` say 10.0.26200, kernel32.dll says 26100 (an enablement-package build).
_ENABLEMENT = _WinVer((10, 0, 26200), (10, 0, 26100))


class _FakePlatform:
    """The shape of the stdlib ``platform`` the priming relies on: ``uname()``
    reaches WMI through the module-level ``_wmi_query`` and, when WMI fails, the
    ``ver`` command through ``_syscmd_ver``, both looked up at call time, and
    caches in ``_uname_cache``. Records what ``uname()`` saw while it ran."""

    def __init__(self) -> None:
        self._uname_cache: tuple[str, ...] | None = None
        self.seen: list[str] = []
        self._wmi_query = self._real_wmi
        self._syscmd_ver = self._real_ver

    def _real_wmi(self, *_keys: str):
        self.seen.append("wmi")
        return iter(("10.0.26200", "1", "Multiprocessor Free", "0", "0"))

    def _real_ver(self, system: str = "", release: str = "", version: str = ""):
        self.seen.append("ver")
        return "Microsoft Windows", "", "10.0.26200.9550"

    def uname(self):
        if self._uname_cache is not None:
            return self._uname_cache
        try:
            version = next(self._wmi_query("OS", "Version"))
        except OSError:
            self.seen.append("wmi-refused")
            version = self._syscmd_ver()[2]
        self._uname_cache = ("Windows", "box", "11", version, "AMD64")
        return self._uname_cache


def test_priming_fills_the_uname_cache_without_wmi_or_ver() -> None:
    fake = _FakePlatform()
    real_wmi, real_ver = fake._wmi_query, fake._syscmd_ver
    assert nexus._prime_platform_without_wmi(platform="win32", platform_module=fake, winver=_ENABLEMENT) is True
    assert fake._uname_cache == ("Windows", "box", "11", "10.0.26200", "AMD64"), "the version WMI would have given"
    assert fake.seen == ["wmi-refused"], "uname() must neither query WMI nor run the ver command"
    assert fake._wmi_query == real_wmi and fake._syscmd_ver == real_ver, "both must be restored"
    fake.uname()
    assert fake.seen == ["wmi-refused"], "a later system()/machine() reads the cache"


def test_the_version_is_the_real_build_not_kernel32s() -> None:
    assert nexus._windows_version(_ENABLEMENT) == "10.0.26200"


def test_an_interpreter_in_compatibility_mode_falls_back_to_kernel32() -> None:
    # Without a Windows 10 manifest GetVersionEx says 6.2; kernel32's version is then the better answer.
    assert nexus._windows_version(_WinVer((6, 2, 9200), (10, 0, 26100))) == "10.0.26100"


def test_a_missing_kernel32_version_uses_the_reported_one() -> None:
    assert nexus._windows_version(_WinVer((10, 0, 26300), None)) == "10.0.26300"


def test_priming_leaves_posix_alone() -> None:
    fake = _FakePlatform()
    assert nexus._prime_platform_without_wmi(platform="linux", platform_module=fake, winver=_ENABLEMENT) is False
    assert nexus._prime_platform_without_wmi(platform="darwin", platform_module=fake, winver=_ENABLEMENT) is False
    assert fake._uname_cache is None and fake.seen == []


def test_priming_keeps_a_cache_that_is_already_there() -> None:
    fake = _FakePlatform()
    fake._uname_cache = ("Windows", "box", "11", "10.0.22631", "AMD64")
    assert nexus._prime_platform_without_wmi(platform="win32", platform_module=fake, winver=_ENABLEMENT) is True
    assert fake._uname_cache[3] == "10.0.22631" and fake.seen == []


def test_priming_touches_nothing_when_platform_lacks_the_hooks() -> None:
    fake = _FakePlatform()
    del fake._syscmd_ver  # a Python whose platform module no longer has it
    assert nexus._prime_platform_without_wmi(platform="win32", platform_module=fake, winver=_ENABLEMENT) is False
    assert fake._uname_cache is None and fake.seen == []


def test_priming_touches_nothing_without_a_version_to_offer() -> None:
    fake = _FakePlatform()
    assert nexus._prime_platform_without_wmi(platform="win32", platform_module=fake, winver=("broken",)) is False
    assert fake._uname_cache is None and fake.seen == []


def test_priming_restores_and_clears_when_uname_fails() -> None:
    fake = _FakePlatform()
    real_wmi, real_ver = fake._wmi_query, fake._syscmd_ver
    ran: list[bool] = []

    def boom():
        ran.append(True)
        fake._uname_cache = ("partial",)
        raise RuntimeError("uname broke")

    fake.uname = boom
    assert nexus._prime_platform_without_wmi(platform="win32", platform_module=fake, winver=_ENABLEMENT) is False
    assert ran == [True], "uname() must have been reached, or this test proves nothing"
    assert fake._uname_cache is None, "a failed priming must leave platform to compute uname itself"
    assert fake._wmi_query == real_wmi and fake._syscmd_ver == real_ver


def test_windows_primes_before_truststore_is_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(nexus, "_prime_platform_without_wmi", lambda **_k: order.append("prime") or True)
    monkeypatch.setitem(sys.modules, "truststore", types.SimpleNamespace(inject_into_ssl=lambda: order.append("inject")))
    assert nexus._use_os_trust_store(platform="win32") is True
    assert order == ["prime", "inject"]


def test_posix_does_not_prime(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(nexus, "_prime_platform_without_wmi", lambda **_k: order.append("prime") or True)
    assert nexus._use_os_trust_store(platform="linux") is False
    assert order == []


def test_the_dependency_is_windows_only() -> None:
    import tomllib
    from pathlib import Path

    deps = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    trust = [d for d in deps if d.split(">")[0].split("=")[0].strip() == "truststore"]
    assert trust == ["truststore>=0.10,<0.11; sys_platform == 'win32'"]


@pytest.mark.skipif(sys.platform != "win32", reason="real truststore on a Windows host; the seams above cover the branch everywhere")
class TestRealWindows:
    def test_importing_nexus_puts_ssl_on_the_os_verifier(self) -> None:
        import truststore

        assert ssl.SSLContext is truststore.SSLContext, "nexus import must inject truststore on Windows"
        ctx = ssl.create_default_context()
        assert isinstance(ctx, truststore.SSLContext)

    def test_importing_nexus_makes_no_wmi_query(self) -> None:
        # nexus-f9bgu.55: CPython 3.12's _wmi crashes the process (0xC000070A) when a
        # query times out, and truststore's import queried WMI in every nx process.
        code = (
            "import sys\n"
            "hits = []\n"
            "sys.addaudithook(lambda e, a: hits.append(a) if e == '_wmi.exec_query' else None)\n"
            "import nexus, platform\n"
            "platform.system(); platform.machine(); platform.release()\n"
            "print(len(hits))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
        assert out.stdout.strip() == "0", out.stdout + out.stderr

    def test_the_primed_uname_matches_the_wmi_answer(self) -> None:
        code = "import platform; u = platform.uname(); print(u.system, u.release, u.version, u.machine)"
        wmi = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
        primed = platform.uname()  # this process imported nexus, so its cache was primed
        assert f"{primed.system} {primed.release} {primed.version} {primed.machine}" == wmi.stdout.strip()
