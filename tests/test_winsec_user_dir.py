# SPDX-License-Identifier: AGPL-3.0-or-later
"""A directory an ELEVATED install creates stays usable by the same user's normal token
(RDR-224, nexus-f9bgu.33, critique S5).

Measured on native Windows 11 (T2 ``nexus_rdr/224-review-p3-fixes-a``): an elevated
``Path.mkdir(mode=0o700)`` carries ACEs for SYSTEM, Administrators and OWNER RIGHTS only,
and the elevated creator owns it as BUILTIN\\Administrators. A LeastPrivilege token (the
Task Scheduler logon task, a plain ``nx`` in a normal shell) has Administrators
deny-only and is not the owner, so it could not list the config dir, list the engine dir,
read or execute ``nexus-service.exe``, or write anything: ``PermissionError`` on every
operation. An explicit, inheritable ACE for the user's own SID
(:func:`nexus._winsec.grant_user_tree_access`) fixes it; ``make_user_dir`` puts it on a
directory it creates, and the engine placement puts it on the engine dir.

Every Windows call is a seam, so these run on every host.
"""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

from nexus import _winsec
from nexus.daemon import binary_install
from nexus.daemon import service_registry as sr

SID = "S-1-5-21-3623811015-3361044348-30300820-1013"


class _Acl:
    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []

    def __call__(self, path: str, sid: str) -> None:
        self.applied.append((path, sid))


class TestMakeUserDir:
    def test_windows_grants_the_user_on_a_directory_it_creates(self, tmp_path: Path) -> None:
        acl = _Acl()
        target = tmp_path / "cfg"
        created = _winsec.make_user_dir(target, platform="win32", sid_lookup=lambda: SID, acl_apply=acl)
        assert created is True and target.is_dir()
        assert acl.applied == [(str(target), SID)]

    def test_a_directory_that_already_exists_is_left_alone(self, tmp_path: Path) -> None:
        # Re-granting would propagate through a whole tree (a PostgreSQL data dir) on every call.
        acl = _Acl()
        (tmp_path / "cfg").mkdir()
        created = _winsec.make_user_dir(tmp_path / "cfg", platform="win32", sid_lookup=lambda: SID, acl_apply=acl)
        assert created is False and acl.applied == []

    def test_posix_is_mkdir_0700_and_no_acl(self, tmp_path: Path) -> None:
        acl = _Acl()
        target = tmp_path / "a" / "cfg"
        assert _winsec.make_user_dir(target, platform="linux", acl_apply=acl) is True
        assert acl.applied == []
        if sys.platform != "win32":  # Windows reports 0o777 for everything; the ACL arm is what matters there
            assert (target.stat().st_mode & 0o777) == 0o700

    def test_parents_are_created_but_only_the_leaf_is_granted(self, tmp_path: Path) -> None:
        acl = _Acl()
        _winsec.make_user_dir(tmp_path / "x" / "y" / "cfg", platform="win32", sid_lookup=lambda: SID, acl_apply=acl)
        assert [p for p, _ in acl.applied] == [str(tmp_path / "x" / "y" / "cfg")]


class TestTheRegistryDirectory:
    def test_the_lease_directory_is_made_through_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        made: list[Path] = []
        def fake(path: Path, **_k: object) -> bool:
            made.append(Path(path))
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        monkeypatch.setattr(sr, "make_user_dir", fake)
        reg = sr.ServiceRegistry(dir=tmp_path / "cfg", tier="storage_service")
        reg.publish("scope", endpoint={"host": "127.0.0.1", "port": 1}, version="v", owner_token="o")
        assert made and set(made) == {tmp_path / "cfg"}

    def test_a_failed_grant_never_stops_the_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(path: Path, **_k: object) -> bool:
            Path(path).mkdir(parents=True, exist_ok=True)
            raise OSError("no ACLs on this volume")

        monkeypatch.setattr(sr, "make_user_dir", refuse)
        reg = sr.ServiceRegistry(dir=tmp_path / "cfg", tier="storage_service")
        reg.publish("scope", endpoint={"host": "127.0.0.1", "port": 1}, version="v", owner_token="o")
        assert reg.discover("scope") is not None  # the lease was written regardless


class TestTheEngineDirectory:
    def _archive(self, tmp_path: Path) -> Path:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            for name in (binary_install.WINDOWS_ENGINE_EXE, *binary_install.WINDOWS_RUNTIME_DLLS):
                data = b"x" + name.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        path = tmp_path / "engine.tar.xz"
        path.write_bytes(buf.getvalue())
        return path

    def test_the_engine_dir_and_the_stage_dir_get_the_users_ace_before_a_byte_is_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        granted: list[tuple[Path, int, bool]] = []
        dest = tmp_path / "cfg" / "service" / binary_install.WINDOWS_ENGINE_EXE

        def grant(path: Path | str, **_k: object) -> None:
            # (what was granted, files already inside it, whether the engine exe is already placed)
            p = Path(path)
            granted.append((p, len(list(p.iterdir())), dest.exists()))

        monkeypatch.setattr(binary_install, "grant_user_tree_access", grant)
        binary_install._place_engine_archive(self._archive(tmp_path), dest, platform="win32")
        assert len(granted) == 2, "the engine dir and the stage dir must both be granted"
        engine_dir, stage_dir = granted
        assert engine_dir[0] == dest.parent and engine_dir[2] is False
        assert stage_dir[0].parent == dest.parent and stage_dir[0].name.startswith(".nx_stage_")
        assert stage_dir[1] == 0, "the stage dir must be granted while still empty so every staged file inherits it"
        assert dest.is_file()  # non-vacuity: the placement itself ran

    def test_a_grant_that_fails_never_stops_the_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_a: object, **_k: object) -> None:
            raise OSError("no ACLs on this volume")

        monkeypatch.setattr(binary_install, "grant_user_tree_access", refuse)
        dest = tmp_path / "cfg" / "service" / binary_install.WINDOWS_ENGINE_EXE
        binary_install._place_engine_archive(self._archive(tmp_path), dest, platform="win32")
        assert dest.is_file()

    def test_posix_placement_applies_no_acl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # grant_user_tree_access is a no-op off Windows; the Windows ACL seam must never run.
        monkeypatch.setattr(
            _winsec, "_windows_grant_user_tree",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("posix has no ACL to grant")),
        )
        dest = tmp_path / "cfg" / "service" / binary_install.WINDOWS_ENGINE_EXE
        binary_install._place_engine_archive(self._archive(tmp_path), dest, platform="linux")
        assert dest.is_file()
