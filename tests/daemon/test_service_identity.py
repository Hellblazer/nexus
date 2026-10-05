# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ONE service-identity derivation (RDR-224 Gap 3, nexus-f9bgu.16).

``service_identity()`` is the scope key every service-lease / lock / endpoint
name is built from. POSIX keeps ``str(os.getuid())`` byte for byte (every
existing lease file ``storage_service_addr.<uid>`` stays where it is); Windows
uses the user's SID. The platform and both lookups are injected, so the Windows
branch runs here on every host and never skip-passes.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

from nexus.daemon import service_registry as sr
from nexus.daemon.service_registry import ServiceIdentityError, ServiceRegistry, service_identity

REPO = Path(__file__).parent.parent.parent
PLUGIN_SCRIPT = REPO / "conexus" / "hooks" / "scripts" / "_endpoint_resolve.py"

SID = "S-1-5-21-3623811015-3361044348-30300820-1013"


def _boom() -> str:
    raise AssertionError("the other platform's lookup must not run")


def _no_uid() -> int:
    raise AssertionError("getuid must not run on Windows (it does not exist there)")


class TestPosix:
    @pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
    def test_is_str_of_getuid_exactly(self, platform: str) -> None:
        assert service_identity(platform=platform, getuid=lambda: 501, sid_lookup=_boom) == "501"

    def test_root_uid_zero_is_not_dropped(self) -> None:
        assert service_identity(platform="linux", getuid=lambda: 0, sid_lookup=_boom) == "0"

    @pytest.mark.skipif(sys.platform == "win32", reason="os.getuid is POSIX-only; the injected tests above cover the branch")
    def test_default_equals_the_historical_expression(self) -> None:
        assert service_identity() == str(os.getuid())


class TestWindows:
    def test_is_the_sid_and_the_sid_branch_ran(self) -> None:
        calls: list[int] = []

        def lookup() -> str:
            calls.append(1)
            return SID

        got = service_identity(platform="win32", getuid=_no_uid, sid_lookup=lookup)
        assert got == SID
        # Non-vacuity: the Windows branch really ran, and produced a SID, not a uid-shaped value.
        assert calls == [1]
        assert got.startswith("S-1-5-") and not got.isdigit()

    @pytest.mark.parametrize(
        "bad",
        ["", "alice", "DOMAIN\\alice", "S-1-5-21-1/../x", "S-1-", "s-1-5-21-1", "S-1-5-21-1 ", "S-1-5-21-1\n", "C:\\x"],
    )
    def test_a_value_that_is_not_a_sid_is_refused(self, bad: str) -> None:
        with pytest.raises(ServiceIdentityError):
            service_identity(platform="win32", getuid=_no_uid, sid_lookup=lambda: bad)

    def test_a_failed_lookup_fails_loud_never_falls_back_to_a_name(self) -> None:
        def lookup() -> str:
            raise OSError("token query failed")

        with pytest.raises(ServiceIdentityError, match="token query failed"):
            service_identity(platform="win32", getuid=_no_uid, sid_lookup=lookup)

    def test_the_real_lookup_is_unreachable_off_windows_and_says_so(self) -> None:
        if sys.platform == "win32":
            assert re.fullmatch(r"S-1-\d+(-\d+)+", sr._windows_user_sid())
        else:
            with pytest.raises(OSError):
                sr._windows_user_sid()

    def test_the_value_is_a_safe_lease_file_and_lock_name(self, tmp_path: Path) -> None:
        """The identity becomes ``storage_service_addr.<id>`` and ``..._elect.<id>.lock``: a real round trip."""
        ident = service_identity(platform="win32", getuid=_no_uid, sid_lookup=lambda: SID)
        reg = ServiceRegistry(dir=tmp_path, tier="storage_service")
        reg.publish(ident, endpoint={"host": "127.0.0.1", "port": 1, "token": "t"}, version="v", owner_token="o")
        assert (tmp_path / f"storage_service_addr.{SID}").is_file()
        record = reg.discover(ident)
        assert record is not None and record.scope_key == SID
        assert re.fullmatch(r"[A-Za-z0-9._-]+", ident)


class TestPluginMirror:
    """``conexus/hooks/scripts/_endpoint_resolve.py`` is stdlib-only (it must not import nexus), so it carries a
    mirror. A hand-kept mirror drifts (nexus-aginu); these tests make drift red."""

    @staticmethod
    def _load():
        spec = importlib.util.spec_from_file_location("_endpoint_resolve_under_test", PLUGIN_SCRIPT)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(spec.name, None)
        return mod

    def test_same_answer_on_both_platforms(self) -> None:
        mirror = self._load()
        assert mirror.service_identity(platform="linux", getuid=lambda: 501, sid_lookup=_boom) == "501"
        got = mirror.service_identity(platform="win32", getuid=_no_uid, sid_lookup=lambda: SID)
        assert got == SID == service_identity(platform="win32", getuid=_no_uid, sid_lookup=lambda: SID)

    def test_same_refusals(self) -> None:
        mirror = self._load()
        for bad in ("", "alice", "DOMAIN\\alice", "S-1-5-21-1\n"):
            with pytest.raises(Exception) as exc:  # noqa: PT011 -- the mirror owns its own error class
                mirror.service_identity(platform="win32", getuid=_no_uid, sid_lookup=lambda b=bad: b)
            assert type(exc.value).__name__ == "ServiceIdentityError"

    def test_the_lease_path_uses_the_identity(self, tmp_path: Path) -> None:
        mirror = self._load()
        want = f"storage_service_addr.{service_identity()}"
        if sys.platform == "win32":
            assert mirror.storage_service_lease_path(tmp_path).name == want
        else:
            assert mirror.storage_service_lease_path(tmp_path) == tmp_path / want

    def test_the_two_sid_lookups_are_the_same_code(self) -> None:
        """The ctypes lookup cannot run off Windows, so its parity is structural: identical ASTs, and the
        validation pattern is the same string."""

        def func(path: Path, name: str) -> ast.FunctionDef:
            tree = ast.parse(path.read_text())
            hits = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
            assert len(hits) == 1, f"{path} must define exactly one {name}"
            return hits[0]

        def dump(fn: ast.FunctionDef) -> str:
            fn = ast.parse(ast.unparse(fn)).body[0]  # type: ignore[assignment]
            fn.body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]  # drop docstring
            return ast.dump(fn)

        real = func(Path(sr.__file__), "_windows_user_sid")
        mirror = func(PLUGIN_SCRIPT, "_windows_user_sid")
        assert dump(real) == dump(mirror)
        # Non-vacuity: the compared bodies really are the Windows token lookup.
        assert "ConvertSidToStringSidW" in ast.unparse(real) and "ConvertSidToStringSidW" in ast.unparse(mirror)
        src_real, src_mirror = Path(sr.__file__).read_text(), PLUGIN_SCRIPT.read_text()
        pattern = re.search(r'_SID_PATTERN = re\.compile\((r"[^"]+")\)', src_real)
        assert pattern and f"_SID_PATTERN = re.compile({pattern.group(1)})" in src_mirror
