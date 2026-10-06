# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-only credential files on Windows (RDR-224, nexus-f9bgu.22).

On Windows ``os.chmod`` restricts nothing and ``os.stat`` says ``0o666`` for
every file, so the POSIX ``0600`` write and the mode-bit read check both fail
silently there. ``nexus._winsec`` replaces them with a protected one-ACE DACL
(write side) and a DACL read (check side). The platform and every Windows call
are seams, so the Windows branch runs on every host; ``TestRealWindows`` runs
the real ctypes calls and is skipped off Windows, with a non-vacuity assert
once it runs.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from nexus import _winsec
from nexus._winsec import ensure_owner_only, open_private, owner_only_problem, restrict_to_owner

REPO = Path(__file__).parent.parent
PLUGIN_SCRIPT = REPO / "conexus" / "hooks" / "scripts" / "_endpoint_resolve.py"

SID = "S-1-5-21-3623811015-3361044348-30300820-1013"
OTHER = "S-1-5-21-3623811015-3361044348-30300820-1099"
SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
OWNER_RIGHTS = "S-1-3-4"


def _boom(*_a: object, **_k: object) -> None:
    raise AssertionError("the other platform's call must not run")


class FakeAcl:
    """A path -> grantee list store standing in for NTFS. ``apply`` is what
    ``SetNamedSecurityInfoW`` does here (one grantee, nothing inherited); the
    ``inherited`` list is what a fresh file in a user profile carries."""

    def __init__(self, inherited: list[str] | None = None) -> None:
        self.inherited = inherited if inherited is not None else [SID, SYSTEM, ADMINS, OTHER]
        self.acls: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str, int]] = []

    def apply(self, path: str, sid: str) -> None:
        self.calls.append((path, sid, os.path.getsize(path)))  # size at call time: before any secret byte
        self.acls[path] = [sid]

    def trustees(self, path: str) -> list[str] | None:
        return self.acls.get(path, list(self.inherited))


@pytest.fixture
def win_kw() -> dict[str, object]:
    """Keyword seams that put every helper on its Windows branch."""
    fake = FakeAcl()
    return {"platform": "win32", "sid_lookup": lambda: SID, "acl_apply": fake.apply, "_fake": fake}


def _kw(win_kw: dict[str, object], *names: str) -> dict[str, object]:
    return {k: win_kw[k] for k in names}


class TestOpenPrivateWritesTheBytesItIsGiven:
    """RDR-224, nexus-f9bgu.44. ``os.open`` on Windows opens in TEXT mode unless
    ``O_BINARY`` is set, and ``os.write`` then turns every ``\\n`` byte into
    ``\\r\\n``. ``open_private`` hands callers a raw descriptor they write
    bytes to (lease records, the appliance handoff), so a file it makes must hold
    exactly those bytes."""

    def test_o_binary_is_passed_where_the_platform_defines_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[int] = []
        real_open = os.open
        fake_o_binary = 0x8000  # Windows' value; a bit POSIX never sets

        def spy(path, flags, mode=0o777):
            seen.append(flags)
            return real_open(path, flags & ~fake_o_binary, mode)

        monkeypatch.setattr(os, "O_BINARY", fake_o_binary, raising=False)
        monkeypatch.setattr(os, "open", spy)
        fd = open_private(tmp_path / "t", os.O_CREAT | os.O_WRONLY, platform="linux", acl_apply=_boom)
        os.close(fd)
        assert seen and seen[0] & fake_o_binary

    def test_a_newline_byte_is_one_byte_on_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "t"
        fd = open_private(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, b"a\nb\n")
        finally:
            os.close(fd)
        assert path.read_bytes() == b"a\nb\n"


class TestPosix:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="asserts the 0o600 file mode os.open sets; Windows ignores them (st_mode reads 0o666 for every file), and its owner-only "
        "check is the DACL, covered by the Windows classes below",
    )
    def test_open_private_is_the_old_os_open_0600(self, tmp_path: Path) -> None:
        old = os.umask(0)
        try:
            fd = open_private(tmp_path / "t", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, platform="linux", acl_apply=_boom)
        finally:
            os.umask(old)
        os.close(fd)
        assert stat.S_IMODE((tmp_path / "t").stat().st_mode) == 0o600

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="asserts a chmod 0o600 result; Windows ignores them (st_mode reads 0o666 for every file), and its owner-only "
        "check is the DACL, covered by the Windows classes below",
    )
    def test_restrict_to_owner_is_chmod_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "t"
        path.write_text("x")
        path.chmod(0o644)
        restrict_to_owner(path, platform="linux", sid_lookup=_boom, acl_apply=_boom)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
    def test_owner_only_modes_are_fine(self, mode: int) -> None:
        assert owner_only_problem("p", stat.S_IFREG | mode, platform="darwin", sid_lookup=_boom, trustees_lookup=_boom) is None

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666])
    def test_a_group_or_other_bit_is_a_problem_naming_the_mode(self, mode: int) -> None:
        problem = owner_only_problem("p", stat.S_IFREG | mode, platform="linux", sid_lookup=_boom, trustees_lookup=_boom)
        assert problem == f"group/other-accessible (mode {oct(mode)})"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="builds a loose file with chmod 0o644; Windows ignores them (st_mode reads 0o666 for every file), and its owner-only "
        "check is the DACL, covered by the Windows classes below",
    )
    def test_ensure_owner_only_tightens_a_loose_file_and_leaves_a_tight_one(self, tmp_path: Path) -> None:
        path = tmp_path / "t"
        path.write_text("x")
        path.chmod(0o644)
        ensure_owner_only(path, platform="linux")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o400)
        ensure_owner_only(path, platform="linux")
        assert stat.S_IMODE(path.stat().st_mode) == 0o400, "a file with no group/other bit is not touched"


class TestWindowsWrite:
    def test_the_acl_goes_on_while_the_file_is_still_empty(self, tmp_path: Path, win_kw: dict[str, object]) -> None:
        fake: FakeAcl = win_kw["_fake"]  # type: ignore[assignment]
        path = tmp_path / "secret"
        fd = open_private(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, **_kw(win_kw, "platform", "sid_lookup", "acl_apply"))
        os.write(fd, b"hunter2")
        os.close(fd)
        assert fake.calls == [(str(path), SID, 0)], "exactly one ACL call, for the current SID, before the first write"
        assert path.read_text() == "hunter2"
        assert fake.acls[str(path)] == [SID]

    def test_a_failed_acl_closes_the_descriptor_removes_the_empty_file_and_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "secret"
        seen: list[int] = []

        def failing(p: str, sid: str) -> None:
            seen.append(1)
            raise PermissionError("ACLs not supported on this volume")

        with pytest.raises(PermissionError):
            open_private(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, platform="win32", sid_lookup=lambda: SID, acl_apply=failing)
        assert seen == [1]
        assert not path.exists(), "an empty credential file the failed call made must not be left behind"

    def test_a_failed_acl_never_deletes_a_file_that_already_had_content(self, tmp_path: Path) -> None:
        path = tmp_path / "existing"
        path.write_text("keep me")

        def failing(p: str, sid: str) -> None:
            raise PermissionError("no")

        with pytest.raises(PermissionError):
            open_private(path, os.O_CREAT | os.O_WRONLY, platform="win32", sid_lookup=lambda: SID, acl_apply=failing)
        assert path.read_text() == "keep me"

    def test_the_sid_lookup_failing_is_a_failed_write_not_an_unprotected_one(self, tmp_path: Path) -> None:
        def no_sid() -> str:
            raise OSError("no token")

        with pytest.raises(OSError):
            open_private(tmp_path / "s", os.O_CREAT | os.O_WRONLY, platform="win32", sid_lookup=no_sid, acl_apply=_boom)
        assert not (tmp_path / "s").exists()

    def test_restrict_to_owner_applies_the_acl_for_the_sid(self, tmp_path: Path, win_kw: dict[str, object]) -> None:
        fake: FakeAcl = win_kw["_fake"]  # type: ignore[assignment]
        path = tmp_path / "t"
        path.write_text("x")
        restrict_to_owner(path, **_kw(win_kw, "platform", "sid_lookup", "acl_apply"))
        assert fake.acls == {str(path): [SID]}

    def test_the_descriptor_is_one_protected_full_control_ace_for_the_sid(self) -> None:
        sddl = _winsec._OWNER_ONLY_SDDL.format(sid=SID)
        assert sddl == f"D:P(A;;FA;;;{SID})"
        assert sddl.startswith("D:P("), "P = protected: nothing inherited from the directory"
        assert sddl.count("(A;") == 1, "one ACE"


class TestWindowsRead:
    def test_st_mode_is_ignored_the_acl_decides(self) -> None:
        # Windows reports 0o666 for every file; that must not read as "world-writable".
        problem = owner_only_problem(
            "p", stat.S_IFREG | 0o666, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p: [SID],
        )
        assert problem is None

    @pytest.mark.parametrize(
        "trustees",
        [[SID], [SID, SYSTEM], [SID, SYSTEM, ADMINS], [SYSTEM, ADMINS], [SYSTEM, ADMINS, OWNER_RIGHTS], []],
    )
    def test_the_user_system_admins_and_owner_rights_are_accepted(self, trustees: list[str]) -> None:
        assert owner_only_problem(
            "p", 0o666, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p: trustees,
        ) is None

    def test_another_account_is_a_problem_and_is_named(self) -> None:
        problem = owner_only_problem(
            "p", 0o600, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p: [SID, OTHER],
        )
        assert problem is not None and OTHER in problem and SID not in problem

    @pytest.mark.parametrize("everyone", ["S-1-1-0", "S-1-5-32-545", "S-1-5-11"])
    def test_everyone_users_and_authenticated_users_are_problems(self, everyone: str) -> None:
        assert owner_only_problem(
            "p", 0o600, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p: [SID, everyone],
        ) is not None

    def test_no_dacl_at_all_is_a_problem(self) -> None:
        problem = owner_only_problem("p", 0o600, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p: None)
        assert problem is not None and "no access list" in problem

    def test_an_unreadable_acl_fails_closed(self) -> None:
        def unreadable(p: str) -> list[str] | None:
            raise OSError("access denied")

        problem = owner_only_problem("p", 0o600, platform="win32", sid_lookup=lambda: SID, trustees_lookup=unreadable)
        assert problem is not None and "cannot be read" in problem

    def test_an_unreadable_sid_fails_closed(self) -> None:
        def no_sid() -> str:
            raise OSError("no token")

        problem = owner_only_problem("p", 0o600, platform="win32", sid_lookup=no_sid, trustees_lookup=lambda p: [SID])
        assert problem is not None

    def test_an_ace_the_walk_cannot_read_is_a_problem(self) -> None:
        # _windows_dacl_trustees reports an unknown ACE type as "<ace-type-N>"; it is not a known grantee.
        assert owner_only_problem(
            "p", 0o600, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p: [SID, "<ace-type-5>"],
        ) is not None


class TestWriteThenRead:
    """The loop the bead exists for: a file this module writes passes this module's check, and a file it
    did not write (inheriting the profile's ACL) does not."""

    def test_written_file_is_owner_only_and_an_inherited_one_is_not(self, tmp_path: Path, win_kw: dict[str, object]) -> None:
        fake: FakeAcl = win_kw["_fake"]  # type: ignore[assignment]
        written = tmp_path / "written"
        fd = open_private(written, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, **_kw(win_kw, "platform", "sid_lookup", "acl_apply"))
        os.close(fd)
        stranger = tmp_path / "inherited"
        stranger.write_text("x")
        read_kw = {"platform": "win32", "sid_lookup": lambda: SID, "trustees_lookup": fake.trustees}
        assert owner_only_problem(written, 0o666, **read_kw) is None  # type: ignore[arg-type]
        assert owner_only_problem(stranger, 0o666, **read_kw) is not None  # type: ignore[arg-type]

    def test_ensure_owner_only_tightens_the_inherited_file_and_skips_the_tight_one(
        self, tmp_path: Path, win_kw: dict[str, object],
    ) -> None:
        fake: FakeAcl = win_kw["_fake"]  # type: ignore[assignment]
        path = tmp_path / "f"
        path.write_text("x")
        kw = {"platform": "win32", "sid_lookup": lambda: SID, "trustees_lookup": fake.trustees, "acl_apply": fake.apply}
        ensure_owner_only(path, **kw)  # type: ignore[arg-type]
        assert fake.acls[str(path)] == [SID]
        before = list(fake.calls)
        ensure_owner_only(path, **kw)  # type: ignore[arg-type]
        assert fake.calls == before, "an already owner-only file is left alone"


# ── every credential site, on the Windows branch ────────────────────────────


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> FakeAcl:
    """Put the real credential writers on the Windows branch of ``nexus._winsec``."""
    fake = FakeAcl()
    monkeypatch.setattr(_winsec, "_is_windows", lambda platform: True)
    monkeypatch.setattr(_winsec, "_windows_user_sid", lambda: SID)
    monkeypatch.setattr(_winsec, "_windows_set_owner_only_dacl", fake.apply)
    monkeypatch.setattr(_winsec, "_windows_dacl_trustees", fake.trustees)
    # Windows has no os.chmod effect worth the name and no os.fchmod at all.
    monkeypatch.delattr(os, "fchmod", raising=False)
    return fake


def _assert_acl_before_secret(fake: FakeAcl, directory: Path, secret: str, *, minimum: int = 1) -> None:
    assert len(fake.calls) >= minimum, "the site never asked for an ACL (vacuous run)"
    for path, sid, size in fake.calls:
        assert sid == SID
        assert Path(path).parent == directory, f"the ACL went on {path}, not a file in {directory}"
        assert size == 0, f"{path} already held {size} bytes when its ACL was applied"
    assert any(secret in p.read_text() for p in directory.iterdir() if p.is_file()), "the secret never reached disk"


class TestEveryCredentialSite:
    def test_config_set_credential(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: FakeAcl) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        from nexus.config import set_credential

        set_credential("voyage_api_key", "pa55-secret")
        _assert_acl_before_secret(windows, tmp_path, "pa55-secret")

    def test_config_unset_credential(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: FakeAcl) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        from nexus.config import set_credential, unset_credential

        set_credential("voyage_api_key", "k1")
        set_credential("service_token", "pa55-secret")
        windows.calls.clear()
        assert unset_credential("voyage_api_key")
        _assert_acl_before_secret(windows, tmp_path, "pa55-secret")

    def test_config_set_config_value_dotted_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: FakeAcl) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        from nexus.config import set_config_value

        set_config_value("pdf.extractor", "mineru-secret-ish")
        _assert_acl_before_secret(windows, tmp_path, "mineru-secret-ish")

    def test_pg_credentials_file_and_both_backfills(self, tmp_path: Path, windows: FakeAcl) -> None:
        from nexus.db import pg_provision as pg

        creds = tmp_path / "pg_credentials"
        pg._write_credentials(creds, tmp_path / "pgdata", 5432, "adm1n-pw", "svc-pw", "svc-token-secret", "diag-pw")
        _assert_acl_before_secret(windows, tmp_path, "svc-token-secret")
        # The backfills rewrite the file from a temp file; each must protect that temp file too.
        stripped = "\n".join(
            line for line in creds.read_text().splitlines()
            if not line.startswith(("NX_SERVICE_TOKEN", "NX_DB_DIAG"))
        ) + "\n"
        creds.write_text(stripped)
        windows.calls.clear()
        pg._persist_service_token(creds, "backfilled-token-secret")
        _assert_acl_before_secret(windows, tmp_path, "backfilled-token-secret")
        windows.calls.clear()
        pg._persist_diag_credentials(creds, "diag-backfill-secret")
        _assert_acl_before_secret(windows, tmp_path, "diag-backfill-secret")

    def test_data_token_lease(self, tmp_path: Path, windows: FakeAcl) -> None:
        from nexus.db.data_token import DataTokenManager, _CachedToken

        mgr = DataTokenManager(
            clock=lambda: 1_000_000.0, poster=lambda *a, **k: (200, {}), mint_credential=lambda: "c",
            mint_tenant=lambda: "", config_dir=tmp_path, wall_clock=lambda: 1_000.0,
        )
        mgr._write_lease("http://127.0.0.1:1234", "default", _CachedToken("data-token-secret", 1_000_000.0, 1_000_300.0, 300.0))
        _assert_acl_before_secret(windows, tmp_path, "data-token-secret")

    def test_t1_session_token_lease(self, tmp_path: Path, windows: FakeAcl) -> None:
        from nexus.db.t1 import publish_t1_session_lease

        publish_t1_session_lease("sess-1", "t1-token-secret", tmp_path)
        _assert_acl_before_secret(windows, tmp_path, "t1-token-secret")

    def test_service_registry_lease_record(self, tmp_path: Path, windows: FakeAcl) -> None:
        from nexus.daemon.service_registry import ServiceRegistry

        reg = ServiceRegistry(dir=tmp_path, tier="storage_service")
        reg.publish("scope-1", endpoint={"host": "127.0.0.1", "port": 1, "token": "lease-token-secret"}, version="1", owner_token="o")
        _assert_acl_before_secret(windows, tmp_path, "lease-token-secret")

    def test_appliance_handoff_file_and_its_tightening(self, tmp_path: Path, windows: FakeAcl) -> None:
        from nexus.daemon import appliance_handoff as ah

        target = tmp_path / "endpoint.json"
        ah._atomic_write(target, json.dumps({"mint_token": "appliance-secret"}).encode())
        _assert_acl_before_secret(windows, tmp_path, "appliance-secret")
        # A credential file already on disk with an open ACL is tightened, not trusted.
        stray = tmp_path / "appliance_mint_credential"
        stray.write_text("mint_token=appliance-secret\nmint_tenant=t\n")
        windows.calls.clear()
        ah._enforce_0600(stray)
        assert [Path(p) for p, _s, _z in windows.calls] == [stray]
        assert windows.acls[str(stray)] == [SID]


# ── readers ─────────────────────────────────────────────────────────────────


def _load_mirror():
    spec = importlib.util.spec_from_file_location("_endpoint_resolve_winsec_test", PLUGIN_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _live_lease(path: Path, token: str) -> None:
    path.write_text(json.dumps({
        "status": "live", "heartbeat_epoch": time.time(), "ttl": 60.0, "endpoint": {"token": token},
    }))
    path.chmod(0o666)  # Windows says 0o666 for everything; the reader must not care


class TestReadersUseTheAclOnWindows:
    def test_the_plugin_mirror_accepts_an_owner_only_lease_and_refuses_an_open_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mirror = _load_mirror()
        monkeypatch.setattr(mirror, "_is_windows", lambda platform: True)
        monkeypatch.setattr(mirror, "_windows_user_sid", lambda: SID)
        # After the stand-ins: the lease NAME is the identity, which on a real
        # Windows host would otherwise be the host's own SID and not the one
        # the reader below sees.
        lease = mirror.storage_service_lease_path(tmp_path)
        _live_lease(lease, "bearer-1")
        monkeypatch.setattr(mirror, "_windows_dacl_trustees", lambda p: [SID])
        assert mirror.read_local_supervisor_token(tmp_path) == "bearer-1"
        monkeypatch.setattr(mirror, "_windows_dacl_trustees", lambda p: [SID, OTHER])
        with pytest.raises(mirror.EndpointUnresolvable, match="accessible to other accounts"):
            mirror.read_local_supervisor_token(tmp_path)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="builds a group-readable lease with chmod 0o644; Windows ignores them (st_mode reads 0o666 for every file), and its owner-only "
        "check is the DACL, covered by the Windows classes below",
    )
    def test_the_plugin_mirror_still_refuses_a_group_readable_lease_on_posix(self, tmp_path: Path) -> None:
        mirror = _load_mirror()
        lease = mirror.storage_service_lease_path(tmp_path)
        _live_lease(lease, "bearer-1")
        lease.chmod(0o644)
        with pytest.raises(mirror.EndpointUnresolvable, match=r"group/other-accessible \(mode 0o644\)"):
            mirror.read_local_supervisor_token(tmp_path)
        lease.chmod(0o600)
        assert mirror.read_local_supervisor_token(tmp_path) == "bearer-1"

    def test_the_mailbox_drain_reader_checks_the_acl_not_the_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.hooks import mailbox_drain as md

        lease = tmp_path / f"storage_service_addr.{_identity()}"
        lease.write_text("{}")
        lease.chmod(0o666)
        monkeypatch.setattr(_winsec, "_is_windows", lambda platform: True)
        monkeypatch.setattr(_winsec, "_windows_user_sid", lambda: SID)
        monkeypatch.setattr(_winsec, "_windows_dacl_trustees", lambda p: [SID, OTHER])
        with pytest.raises(md._Skip, match="accessible to other accounts"):
            md._read_local_supervisor_token(tmp_path)
        # Owner-only ACL on the same 0o666 file: the ACL gate passes, so the next gate (liveness) is what speaks.
        monkeypatch.setattr(_winsec, "_windows_dacl_trustees", lambda p: [SID])
        with pytest.raises(md._Skip, match="not live or is stale|malformed|corrupt|no token"):
            md._read_local_supervisor_token(tmp_path)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="builds a group-readable lease with chmod 0o644; Windows ignores them (st_mode reads 0o666 for every file), and its owner-only "
        "check is the DACL, covered by the Windows classes below",
    )
    def test_the_mailbox_drain_reader_still_refuses_a_group_readable_lease_on_posix(self, tmp_path: Path) -> None:
        from nexus.hooks import mailbox_drain as md

        lease = tmp_path / f"storage_service_addr.{_identity()}"
        lease.write_text("{}")
        lease.chmod(0o644)
        with pytest.raises(md._Skip, match=r"group/other-accessible \(mode 0o644\)"):
            md._read_local_supervisor_token(tmp_path)


def _identity() -> str:
    from nexus.daemon.service_registry import service_identity

    return service_identity()


# ── the stdlib mirror ───────────────────────────────────────────────────────


def _func(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text())
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, f"{path} must define exactly one {name}"
    return hits[0]


def _dump(fn: ast.FunctionDef) -> str:
    fn = ast.parse(ast.unparse(fn)).body[0]  # type: ignore[assignment]
    fn.body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]  # drop docstring
    return ast.dump(fn)


class TestMirrorParity:
    """``_endpoint_resolve.py`` cannot import nexus, so the reader's check is restated there. The ctypes walk
    cannot run off Windows, so its parity is structural: identical ASTs."""

    @pytest.mark.parametrize(
        "name", ["_is_windows", "_windows_dacl_trustees", "owner_only_problem", "_win_libs"],
    )
    def test_same_code(self, name: str) -> None:
        real, mirror = _func(Path(_winsec.__file__), name), _func(PLUGIN_SCRIPT, name)
        assert _dump(real) == _dump(mirror)

    def test_non_vacuity_the_compared_bodies_are_the_dacl_walk_and_the_check(self) -> None:
        walk = ast.unparse(_func(PLUGIN_SCRIPT, "_windows_dacl_trustees"))
        assert "GetNamedSecurityInfoW" in walk and "GetAce" in walk and "ConvertSidToStringSidW" in walk
        check = ast.unparse(_func(PLUGIN_SCRIPT, "owner_only_problem"))
        assert "_SID_SYSTEM" in check and "_SID_ADMINISTRATORS" in check and "_SID_OWNER_RIGHTS" in check

    def test_the_tolerated_sids_are_the_same_strings(self) -> None:
        mirror = _load_mirror()
        assert (mirror._SID_SYSTEM, mirror._SID_ADMINISTRATORS, mirror._SID_OWNER_RIGHTS) == (
            _winsec._SID_SYSTEM, _winsec._SID_ADMINISTRATORS, _winsec._SID_OWNER_RIGHTS,
        )

    def test_the_mirror_behaves_like_the_module(self) -> None:
        mirror = _load_mirror()
        cases = [([SID], None), ([SID, SYSTEM, ADMINS], None), ([SID, OTHER], "accessible to other accounts"), (None, "no access list")]
        for trustees, want in cases:
            for impl in (owner_only_problem, mirror.owner_only_problem):
                got = impl("p", 0o666, platform="win32", sid_lookup=lambda: SID, trustees_lookup=lambda p, t=trustees: t)
                assert (got is None) if want is None else (got is not None and want in got)
        for impl in (owner_only_problem, mirror.owner_only_problem):
            assert impl("p", stat.S_IFREG | 0o644, platform="linux") == "group/other-accessible (mode 0o644)"


# ── the real thing, on Windows only ─────────────────────────────────────────


# ── the DACL seam (nexus-f9bgu.33, review: falsifiability) ───────────────────


class TestTheDaclIsProtected:
    """``SetNamedSecurityInfoW`` honours only ``PROTECTED_DACL_SECURITY_INFORMATION``
    (the flag), never the descriptor's own ``P`` control bit. An owner-only DACL
    applied with ``protected=False`` leaves inheritance ON, so a credential file
    inherits its directory's ACEs, and the SDDL-string test alone cannot tell:
    only the seam-level ``protected`` argument can."""

    def _record(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
        calls: list[tuple[str, str, bool]] = []
        monkeypatch.setattr(
            _winsec, "_windows_set_dacl",
            lambda path, sddl, *, protected: calls.append((path, sddl, protected)),
        )
        return calls

    def test_the_owner_only_dacl_is_applied_protected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        _winsec._windows_set_owner_only_dacl("C:/secret", SID)
        assert calls == [("C:/secret", f"D:P(A;;FA;;;{SID})", True)]

    def test_the_user_tree_grant_keeps_inheritance_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The contrast case: the two seams differ, so a swap of the flags is caught.
        calls = self._record(monkeypatch)
        _winsec._windows_grant_user_tree("C:/dir", SID)
        assert calls == [("C:/dir", f"D:(A;OICI;FA;;;{SID})", False)]

    def test_open_private_reaches_the_protected_seam(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        fd = open_private(tmp_path / "f", os.O_CREAT | os.O_WRONLY, platform="win32", sid_lookup=lambda: SID)
        os.close(fd)
        assert [c[2] for c in calls] == [True], "open_private must route through the protected owner-only DACL"


class _FakeFn:
    argtypes = None
    restype = None


class _FakeDll:
    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        log.append(name)
        self._fns: dict[str, _FakeFn] = {}

    def __getattr__(self, attr: str) -> _FakeFn:
        return self._fns.setdefault(attr, _FakeFn())


def _clean_caches(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    monkeypatch.setattr(module, "_WIN_LIBS", [])
    monkeypatch.setattr(module, "_SID_CACHE", [])


class TestPerProcessCaches:
    """The SID and the kernel32/advapi32 bindings are built once per process, in
    the module and in the plugin's stdlib mirror (nexus-f9bgu.33, review m11)."""

    @pytest.mark.parametrize("which", ["module", "mirror"])
    def test_the_libraries_load_once_and_carry_every_signature(
        self, which: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mod = _winsec if which == "module" else _load_mirror()
        _clean_caches(monkeypatch, mod)
        loads: list[str] = []

        def loader(name: str) -> _FakeDll:
            return _FakeDll(name, loads)

        first = mod._win_libs(loader=loader)
        second = mod._win_libs(loader=loader)
        assert first is second
        assert loads == ["advapi32", "kernel32"], "the second call must not load anything"
        advapi32 = first[0]
        # Non-vacuity: the signatures every caller relies on were set, not just the DLL loaded.
        for fn in ("OpenProcessToken", "GetNamedSecurityInfoW", "SetNamedSecurityInfoW", "GetAce"):
            assert getattr(advapi32, fn).argtypes is not None, fn

    @pytest.mark.parametrize("which", ["module", "mirror"])
    def test_a_cached_sid_is_returned_without_touching_a_dll(
        self, which: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mod = _winsec if which == "module" else _load_mirror()
        _clean_caches(monkeypatch, mod)
        if sys.platform != "win32":
            with pytest.raises(OSError):  # non-vacuity: an empty cache off Windows has no answer
                mod._windows_user_sid()
        mod._SID_CACHE.append(SID)
        monkeypatch.setattr(mod, "_win_libs", _boom)
        assert mod._windows_user_sid() == SID

    @pytest.mark.parametrize("which", ["module", "mirror"])
    def test_nothing_is_cached_off_windows(self, which: str, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _winsec if which == "module" else _load_mirror()
        _clean_caches(monkeypatch, mod)
        if sys.platform != "win32":
            with pytest.raises(OSError):
                mod._win_libs()
            assert mod._WIN_LIBS == []


USER = "S-1-5-21-1-2-3-1001"


class TestUserDirProblem:
    """nexus-f9bgu.50: can a NON-elevated token of this user use the directory?"""

    def _ask(self, trustees: list[str] | None, owner: str = "S-1-5-32-544") -> str | None:
        return _winsec.user_dir_problem(
            "C:/cfg", platform="win32", sid_lookup=lambda: USER,
            trustees_lookup=lambda _p: trustees, owner_lookup=lambda _p: owner,
        )

    def test_posix_is_never_a_problem(self) -> None:
        assert _winsec.user_dir_problem("/tmp/x", platform="linux") is None

    def test_an_elevated_mkdir_0o700_is_refused_naming_what_it_grants(self) -> None:
        # Measured shape: SYSTEM, Administrators, OWNER RIGHTS, owned by Administrators.
        reason = self._ask(["S-1-5-18", "S-1-5-32-544", "S-1-3-4"])
        assert reason is not None and reason.startswith("grants only")
        assert USER in reason and "S-1-5-32-544" in reason

    def test_owner_rights_grants_when_this_user_owns_it(self) -> None:
        # A non-elevated mkdir(0o700): same ACEs, but the user is the owner.
        assert self._ask(["S-1-5-18", "S-1-5-32-544", "S-1-3-4"], owner=USER) is None

    def test_owner_without_an_owner_rights_ace_is_not_enough(self) -> None:
        assert self._ask(["S-1-5-18", "S-1-5-32-544"], owner=USER) is not None

    @pytest.mark.parametrize("sid", [USER, "S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-4"])
    def test_an_ace_for_the_user_or_a_group_every_token_carries_grants(self, sid: str) -> None:
        assert self._ask(["S-1-5-18", "S-1-5-32-544", sid]) is None

    def test_a_null_dacl_grants_everyone(self) -> None:
        assert self._ask(None) is None

    def test_an_unreadable_acl_is_reported_not_passed(self) -> None:
        def boom(_p: str) -> list[str]:
            raise OSError("access denied")

        reason = _winsec.user_dir_problem("C:/cfg", platform="win32", sid_lookup=lambda: USER, trustees_lookup=boom)
        assert reason is not None and "cannot be read" in reason


@pytest.mark.skipif(sys.platform != "win32", reason="real advapi32 calls; the seams above cover the branch everywhere")
class TestRealWindows:
    def test_a_dir_make_user_dir_creates_is_usable_by_a_non_elevated_token(self, tmp_path: Path) -> None:
        # nexus-f9bgu.50: the real DACL and owner readers agree with make_user_dir.
        target = tmp_path / "cfg"
        assert _winsec.make_user_dir(target)
        assert _winsec.user_dir_problem(target) is None
        assert _winsec._windows_owner_sid(str(target)).startswith("S-1-5-")  # non-vacuity: a real owner SID

    def test_the_sid_and_the_bindings_are_read_once_then_served_from_the_cache(self) -> None:
        _winsec._SID_CACHE.clear()
        _winsec._WIN_LIBS.clear()
        first = _winsec._windows_user_sid()
        assert first.startswith("S-1-5-21-") or first.startswith("S-1-5-")  # non-vacuity: a real SID
        libs = _winsec._win_libs()
        assert _winsec._windows_user_sid() == first and _winsec._SID_CACHE == [first]
        assert _winsec._win_libs() is libs


    def test_open_private_leaves_exactly_the_current_user_and_the_check_agrees(self, tmp_path: Path) -> None:
        path = tmp_path / "secret"
        fd = open_private(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        os.write(fd, b"hunter2")
        os.close(fd)
        trustees = _winsec._windows_dacl_trustees(str(path))
        assert trustees == [_winsec._windows_user_sid()], "the DACL must hold one ACE, for the current user"
        assert owner_only_problem(path, path.stat().st_mode) is None
        assert path.read_bytes() == b"hunter2", "the owner can still read what it wrote"

    def test_a_default_file_is_not_owner_only_so_the_check_is_not_vacuous(self, tmp_path: Path) -> None:
        # A fresh file inherits its directory's ACL, which on a stock Windows profile adds SYSTEM and
        # Administrators; restrict_to_owner must then change what the walk reports.
        path = tmp_path / "plain"
        path.write_text("x")
        before = _winsec._windows_dacl_trustees(str(path))
        assert before is not None and len(before) >= 1
        restrict_to_owner(path)
        after = _winsec._windows_dacl_trustees(str(path))
        assert after == [_winsec._windows_user_sid()]
