# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 (nexus-25wlq): what the clean Win11 guest walk of 7.74.0 found in
``nx uninstall`` (T2 ``nexus_rdr/224-win-release-guest-walk``).

1. The engine's DJL tokenizer cache (``~/.djl.ai/tokenizers``) outlived an
   uninstall on every platform. The supervisor now points DJL under
   ``~/.cache/nexus`` so the existing cache removal covers it; the legacy
   directory is DJL's SHARED default, so uninstall names it and never removes it.
2. On Windows the last engine run's ``%TEMP%\\onnxruntime-java<n>`` directory
   survived (the engine's own boot sweep never runs again). ``--remove-data``
   removes exactly what ``OrtTempSweep`` would, after a confirmed stop, and the
   sentinel tests below plant everything near that predicate that must survive.
3. The preview named a POSIX ``nexus-t2.service`` on Windows, and said
   "Re-run with confirm=true" (the MCP wording) to a CLI user who needs ``--yes``.

Platform is injected through ``_autostart_platform``; the unit manager is a fake,
so every Windows arm runs on every host.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

#: The darwin arms these tests run read os.getuid(), absent on Windows (see conftest).
pytestmark = pytest.mark.usefixtures("launchd_uid")

from nexus.cli import main
from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer
from nexus.db import onnx_model_root as omr

ORT = installer.ORT_TEMP_DIR_PREFIX
COMPLETE = ("onnxruntime.dll", "onnxruntime4j_jni.dll", "onnxruntime_providers_shared.dll")


def _ok_manager(argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake home, config dir and temp dir; a stopped stack; no real unit manager."""
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "Temp"
    temp.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(omr.ENV_MODEL_DIR, raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])
    monkeypatch.setattr(installer, "run_bounded", _ok_manager)
    monkeypatch.setattr(installer, "_windows_task_registered", lambda: False)
    monkeypatch.setattr(installer, "_probe_survivors", lambda *, tier: ())
    monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))
    monkeypatch.setattr(installer, "_ort_temp_root", lambda: temp)

    def platform(name: str) -> None:
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: name)

    return SimpleNamespace(home=home, temp=temp, platform=platform, tmp=tmp_path)


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _ort_dir(root: Path, name: str, *, files: tuple[str, ...] = COMPLETE, age_s: float = 3600) -> Path:
    d = root / name
    d.mkdir()
    for f in files:
        (d / f).write_bytes(b"MZ")
        _age(d / f, age_s)
    _age(d, age_s)
    return d


# ── the preview names what this platform would remove ───────────────────────


class TestPreview:
    def test_windows_preview_names_the_task_not_a_posix_unit(self, box) -> None:
        box.platform("win32")
        report = installer.uninstall_daemon(confirm=False)
        assert "nexus-t2.service" not in report.message, report.message
        assert "T2" not in report.message, report.message
        assert "NexusStorageService" in report.message, report.message
        assert str(report.service_unit_dest) in report.message, report.message

    @pytest.mark.parametrize(("platform", "t2_name"), [
        ("darwin", "com.nexus.t2.plist"), ("linux", "nexus-t2.service"),
    ])
    def test_posix_preview_still_names_the_units_it_would_remove(
        self, platform: str, t2_name: str, box,
    ) -> None:
        box.platform(platform)
        report = installer.uninstall_daemon(confirm=False)
        assert t2_name in report.message, report.message

    def test_windows_confirmed_summary_does_not_name_the_posix_t2_unit(self, box) -> None:
        box.platform("win32")
        report = installer.uninstall_daemon(confirm=True)
        assert "T2" not in report.message, report.message
        assert "service autostart unit" in report.message

    def test_the_mcp_wording_is_unchanged(self, box) -> None:
        box.platform("linux")
        assert "Re-run with confirm=true to proceed" in installer.uninstall_daemon(confirm=False).message
        withdata = installer.uninstall_daemon(confirm=False, remove_data=True).message
        assert "remove_data=true is set" in withdata

    def test_the_cli_preview_says_yes_not_confirm(self, box) -> None:
        box.platform("linux")
        msg = installer.uninstall_daemon(confirm=False, remove_data=True, cli=True).message
        assert "confirm=true" not in msg and "remove_data=true" not in msg, msg
        assert "--remove-data" in msg and "DELETES" in msg, msg

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    def test_nx_uninstall_never_tells_a_cli_user_to_pass_confirm(
        self, platform: str, box, monkeypatch,
    ) -> None:
        """Through the real verb, with a local footprint present so the
        preview path runs (without one `uninstall_daemon` is never called and
        the CLI's own closing line would satisfy the assertion by itself)."""
        from nexus.commands import uninstall as uninstall_cmd_mod

        box.platform(platform)
        monkeypatch.setattr(uninstall_cmd_mod, "_local_service_present", lambda: True)
        res = CliRunner().invoke(main, ["uninstall", "--remove-data"])
        assert res.exit_code == 0, res.output
        assert "confirm=true" not in res.output, res.output
        assert "remove_data=true" not in res.output, res.output
        # The preview's own tail, not the CLI's closing "Re-run with --yes".
        assert "(--remove-data is set: this DELETES your notes and search index)." in res.output, res.output
        assert "Re-run with confirm" not in res.output, res.output


# ── DJL's tokenizer cache ───────────────────────────────────────────────────


class TestDjlCache:
    def test_the_engine_is_pointed_under_the_nexus_cache(self, box) -> None:
        env: dict[str, str] = {}
        omr.apply_djl_cache_env(env)
        assert env[omr.ENV_DJL_CACHE_DIR] == str(box.home / ".cache" / "nexus" / "djl")
        assert omr.djl_cache_root().is_relative_to(omr.nexus_cache_root()), (
            "uninstall removes nexus_cache_root(); the DJL cache must be inside it"
        )

    @pytest.mark.parametrize("name", [omr.ENV_DJL_CACHE_DIR, omr.ENV_DJL_ENGINE_CACHE_DIR])
    def test_an_operators_own_djl_directory_wins(self, name: str) -> None:
        env = {name: "/their/djl"}
        omr.apply_djl_cache_env(env)
        assert env == {name: "/their/djl"}

    def test_a_blank_value_is_unset(self, box) -> None:
        env = {omr.ENV_DJL_CACHE_DIR: "  ", omr.ENV_DJL_ENGINE_CACHE_DIR: ""}
        omr.apply_djl_cache_env(env)
        assert env[omr.ENV_DJL_CACHE_DIR] == str(omr.djl_cache_root())

    def test_remove_data_removes_the_djl_cache_under_the_nexus_cache(self, box) -> None:
        box.platform("linux")
        lib = omr.djl_cache_root() / "tokenizers" / "0.30.0-0.21-linux" / "libtokenizers.so"
        lib.parent.mkdir(parents=True)
        lib.write_bytes(b"\x7fELF")
        installer.uninstall_daemon(confirm=True, remove_data=True)
        assert not omr.nexus_cache_root().exists()

    def test_the_shared_legacy_cache_is_reported_and_never_removed(self, box) -> None:
        """Sentinel: ~/.djl.ai belongs to every DJL program on the machine."""
        box.platform("linux")
        legacy = box.home / ".djl.ai" / "tokenizers" / "0.30.0-0.21-linux" / "libtokenizers.so"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"\x7fELF")
        foreign = box.home / ".djl.ai" / "cache" / "repo" / "model.bin"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"someone else's model")

        report = installer.uninstall_daemon(confirm=True, remove_data=True)

        assert legacy.read_bytes() == b"\x7fELF"
        assert foreign.read_bytes() == b"someone else's model"
        named = [w for w in report.warnings if str(omr.legacy_djl_tokenizer_cache()) in w]
        assert named and "left" in named[0], report.warnings

    def test_the_legacy_cache_is_silent_when_absent_and_without_remove_data(self, box) -> None:
        box.platform("linux")
        assert not [w for w in installer.uninstall_daemon(confirm=True, remove_data=True).warnings
                    if ".djl.ai" in w]
        legacy = box.home / ".djl.ai" / "tokenizers"
        legacy.mkdir(parents=True)
        assert not [w for w in installer.uninstall_daemon(confirm=True).warnings if ".djl.ai" in w]
        assert legacy.is_dir()

    def test_the_preview_says_the_legacy_cache_is_kept(self, box) -> None:
        box.platform("linux")
        (box.home / ".djl.ai" / "tokenizers").mkdir(parents=True)
        msg = installer.uninstall_daemon(confirm=False, remove_data=True).message
        assert str(omr.legacy_djl_tokenizer_cache()) in msg and "kept" in msg, msg


# ── the last engine run's onnxruntime-java directory ────────────────────────


def _sweep(root: Path, **kw) -> tuple[list[Path], list[str]]:
    return installer.sweep_ort_temp_dirs(root, **kw)


class TestOrtTempSweepPredicate:
    """The predicate is ``OrtTempSweep.sweepOne`` ported rule for rule."""

    def test_removes_a_finished_runs_directory_and_nothing_near_it(self, tmp_path: Path) -> None:
        victim = _ort_dir(tmp_path, f"{ORT}8812345")
        biggest = _ort_dir(tmp_path, f"{ORT}{'9' * 20}")  # Long.toUnsignedString tops out at 20 digits
        foreign_target = tmp_path / "elsewhere"
        foreign_target.mkdir()
        (foreign_target / "keep.txt").write_text("user data")
        sentinels = {
            "unrelated dir": _ort_dir(tmp_path, "pip-build-xyz", files=("a.txt",)),
            "prefix not at start": _ort_dir(tmp_path, f"my-{ORT}1"),
            "wrong case": _ort_dir(tmp_path, "ONNXRUNTIME-JAVA1"),
        }
        # Narrower than the engine's predicate on purpose: the engine would take these
        # (any name starting with the prefix), a user-run uninstall must not. Each holds
        # both loaded libraries and is old, so only the NAME keeps it.
        sentinels |= {
            "user backup": _ort_dir(tmp_path, f"{ORT}-backup"),
            "bare prefix": _ort_dir(tmp_path, ORT),
            "digits then text": _ort_dir(tmp_path, f"{ORT}123old"),
            "21 digits": _ort_dir(tmp_path, f"{ORT}{'9' * 21}"),
        }
        (tmp_path / f"{ORT}.txt").write_text("a FILE with the prefix")
        nested = _ort_dir(tmp_path, f"{ORT}7")
        (nested / "sub").mkdir()
        (nested / "sub" / "deep.dll").write_bytes(b"x")
        _age(nested, 3600)

        removed, notes = _sweep(tmp_path)

        assert sorted(removed) == sorted([victim, biggest])
        assert not victim.exists() and not biggest.exists()
        for label, d in sentinels.items():
            assert d.is_dir() and (d / next(iter(os.listdir(d)))).exists(), label
        assert (tmp_path / f"{ORT}.txt").read_text() == "a FILE with the prefix"
        assert (nested / "sub" / "deep.dll").exists(), "a directory holding a subdirectory is not ORT's"
        assert (foreign_target / "keep.txt").read_text() == "user data"
        assert any(str(nested) in n for n in notes), notes

    def test_a_symbolic_link_is_never_followed(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("user data")
        link = tmp_path / f"{ORT}55"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:  # Windows without SeCreateSymbolicLinkPrivilege (WinError 1314)
            if sys.platform != "win32":
                raise
            pytest.skip(
                "a directory symlink needs SeCreateSymbolicLinkPrivilege, which this Windows "
                f"account lacks ({exc})"
            )
        removed, notes = _sweep(tmp_path)
        assert removed == [] and link.is_symlink()
        assert (target / "keep.txt").read_text() == "user data"
        assert any(str(link) in n for n in notes), notes

    @pytest.mark.skipif(sys.platform != "win32", reason="a directory junction exists only on Windows")
    def test_a_real_junction_is_never_followed(self, tmp_path: Path) -> None:
        """`_winapi.CreateJunction` needs no privilege; the sweep must leave both
        the junction and its target alone (the injected-lstat test above only
        simulates the reparse tag)."""
        import _winapi

        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "onnxruntime.dll").write_bytes(b"user data")
        _age(target / "onnxruntime.dll", 3600)
        junction = tmp_path / f"{ORT}77"
        _winapi.CreateJunction(str(target), str(junction))

        removed, notes = _sweep(tmp_path)

        assert removed == []
        assert (target / "onnxruntime.dll").read_bytes() == b"user data", "the target must survive"
        assert any(str(junction) in n for n in notes), notes

    def test_a_junction_reads_as_foreign(self, tmp_path: Path) -> None:
        """Windows reports a junction as a directory whose lstat carries a reparse
        tag; presented here through the injected lstat on any host."""
        d = _ort_dir(tmp_path, f"{ORT}3")
        real = os.lstat

        def lstat(p):
            st = real(p)
            if Path(p) == d:
                return SimpleNamespace(st_mode=st.st_mode, st_mtime=st.st_mtime, st_reparse_tag=0xA0000003)
            return st

        calls: list[str] = []
        removed, _ = _sweep(tmp_path, lstat=lstat, unlink=lambda p: calls.append(str(p)))
        assert removed == [] and calls == [] and d.is_dir()

    def test_a_reparse_attribute_on_a_file_reads_as_foreign(self, tmp_path: Path) -> None:
        d = _ort_dir(tmp_path, f"{ORT}4")
        real = os.lstat

        def lstat(p):
            st = real(p)
            if Path(p).name == "onnxruntime.dll":
                return SimpleNamespace(
                    st_mode=st.st_mode, st_mtime=st.st_mtime, st_file_attributes=0x400,
                )
            return st

        calls: list[str] = []
        removed, _ = _sweep(tmp_path, lstat=lstat, unlink=lambda p: calls.append(str(p)))
        assert removed == [] and calls == [] and (d / "onnxruntime.dll").exists()

    def test_a_directory_touched_inside_the_complete_bound_is_left(self, tmp_path: Path) -> None:
        d = _ort_dir(tmp_path, f"{ORT}5", age_s=0.0)
        removed, notes = _sweep(tmp_path)
        assert removed == [] and d.is_dir()
        assert any("recent" in n for n in notes), notes

    def test_a_complete_directory_older_than_the_short_bound_goes(self, tmp_path: Path) -> None:
        d = _ort_dir(tmp_path, f"{ORT}6", age_s=1.0)
        removed, _ = _sweep(tmp_path)
        assert removed == [d]

    def test_an_incomplete_directory_is_left_for_thirty_seconds(self, tmp_path: Path) -> None:
        young = _ort_dir(tmp_path, f"{ORT}10", files=("onnxruntime.dll",), age_s=10)
        old = _ort_dir(tmp_path, f"{ORT}11", files=("onnxruntime.dll",), age_s=60)
        removed, _ = _sweep(tmp_path)
        assert removed == [old] and young.is_dir()

    def test_a_directory_whose_loaded_library_is_mapped_is_left_whole(self, tmp_path: Path) -> None:
        """A live engine's DLLs refuse deletion: nothing in the directory is deleted,
        the providers library ORT never loads included."""
        d = _ort_dir(tmp_path, f"{ORT}12")
        tried: list[str] = []

        def unlink(p):
            tried.append(Path(p).name)
            raise PermissionError(13, "in use", str(p))

        removed, notes = _sweep(tmp_path, unlink=unlink)

        assert removed == [] and sorted(os.listdir(d)) == sorted(COMPLETE)
        assert tried == ["onnxruntime.dll"], "the loaded libraries are tried first, and one refusal ends it"
        assert any("in use" in n for n in notes), notes

    def test_a_later_failure_leaves_a_partial_directory_and_says_so(self, tmp_path: Path) -> None:
        d = _ort_dir(tmp_path, f"{ORT}13")
        real = os.unlink

        def unlink(p):
            if Path(p).name == "onnxruntime_providers_shared.dll":
                raise PermissionError(13, "locked", str(p))
            real(p)

        removed, notes = _sweep(tmp_path, unlink=unlink)
        assert removed == [] and d.is_dir()
        assert any(str(d) in n and "could not" in n for n in notes), notes

    def test_a_missing_temp_root_is_not_an_error(self, tmp_path: Path) -> None:
        assert _sweep(tmp_path / "absent") == ([], [])


class TestOrtTempSweepInUninstall:
    def _sentinels(self, box) -> dict[str, Path]:
        keep = box.temp / "someone-elses"
        keep.mkdir()
        (keep / "doc.txt").write_text("not ours")
        miss = _ort_dir(box.temp, "pytest-of-user", files=("x.log",))
        return {"dir": keep, "other": miss}

    def test_windows_remove_data_removes_exactly_the_ort_directories(self, box) -> None:
        box.platform("win32")
        victims = [_ort_dir(box.temp, f"{ORT}111"), _ort_dir(box.temp, f"{ORT}222")]
        young = _ort_dir(box.temp, f"{ORT}333", age_s=0.0)
        sentinels = self._sentinels(box)

        report = installer.uninstall_daemon(confirm=True, remove_data=True)

        assert all(not v.exists() for v in victims)
        assert young.is_dir(), "a directory an engine may be filling is left"
        assert (sentinels["dir"] / "doc.txt").read_text() == "not ours"
        assert (sentinels["other"] / "x.log").exists()
        assert report.ort_temp_dirs_removed == tuple(sorted(victims))
        assert "2 onnxruntime-java temp dir(s) removed" in report.message, report.message

    def test_planted_foreign_files_survive_the_whole_uninstall(self, box) -> None:
        """The sentinel the bead asks for: a foreign file in ~/.djl.ai and a
        non-matching %TEMP% directory both survive a full Windows uninstall."""
        box.platform("win32")
        djl = box.home / ".djl.ai" / "tokenizers" / "other-app" / "tokenizers.dll"
        djl.parent.mkdir(parents=True)
        djl.write_bytes(b"MZ")
        near_miss = box.temp / "onnxruntime"  # not the engine's prefix
        near_miss.mkdir()
        (near_miss / "onnxruntime.dll").write_bytes(b"MZ")
        _age(near_miss, 3600)
        victim = _ort_dir(box.temp, f"{ORT}999")

        installer.uninstall_daemon(confirm=True, remove_data=True)

        assert djl.read_bytes() == b"MZ"
        assert (near_miss / "onnxruntime.dll").read_bytes() == b"MZ"
        assert not victim.exists()

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_posix_never_sweeps_the_temp_directory(self, platform: str, box) -> None:
        """deleteOnExit works off Windows, and the engine's sweep is Windows-only."""
        box.platform(platform)
        d = _ort_dir(box.temp, f"{ORT}1")
        installer.uninstall_daemon(confirm=True, remove_data=True)
        assert d.is_dir()

    def test_without_remove_data_nothing_is_swept(self, box) -> None:
        box.platform("win32")
        d = _ort_dir(box.temp, f"{ORT}1")
        installer.uninstall_daemon(confirm=True)
        assert d.is_dir()

    def test_a_dry_run_removes_nothing_and_names_the_sweep(self, box) -> None:
        box.platform("win32")
        d = _ort_dir(box.temp, f"{ORT}1")
        report = installer.uninstall_daemon(confirm=False, remove_data=True)
        assert d.is_dir()
        assert str(box.temp) in report.message and ORT in report.message, report.message

    def test_a_stop_that_is_not_confirmed_leaves_the_directories(self, box, monkeypatch) -> None:
        box.platform("win32")
        d = _ort_dir(box.temp, f"{ORT}1")
        monkeypatch.setattr(
            installer, "_service_stack_confirmed_stopped", lambda ok: (False, ("a survivor",)),
        )
        report = installer.uninstall_daemon(confirm=True, remove_data=True)
        assert d.is_dir()
        assert any(ORT in w and "kept" in w for w in report.warnings), report.warnings

    def test_a_live_engines_directory_is_reported_not_removed(self, box, monkeypatch) -> None:
        box.platform("win32")
        d = _ort_dir(box.temp, f"{ORT}1")

        def refuse(p):
            raise PermissionError(13, "in use", str(p))

        real = installer.sweep_ort_temp_dirs
        monkeypatch.setattr(
            installer, "sweep_ort_temp_dirs", lambda root, **kw: real(root, unlink=refuse, **kw),
        )
        report = installer.uninstall_daemon(confirm=True, remove_data=True)
        assert sorted(os.listdir(d)) == sorted(COMPLETE)
        assert any("in use" in w for w in report.warnings), report.warnings


class TestOrtTempRoot:
    def test_windows_reads_the_variables_the_jvm_does_in_the_same_order(self, box, monkeypatch) -> None:
        """java.io.tmpdir on Windows is GetTempPath: TMP, then TEMP, then USERPROFILE."""
        monkeypatch.undo()  # the box fixture stubs _ort_temp_root; use the real one
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "win32")
        for name, value in (("TMP", "C:\\a"), ("TEMP", "C:\\b"), ("USERPROFILE", "C:\\c")):
            monkeypatch.setenv(name, value)
        assert installer._ort_temp_root() == Path("C:\\a")
        monkeypatch.setenv("TMP", " ")
        assert installer._ort_temp_root() == Path("C:\\b")
        monkeypatch.delenv("TEMP")
        assert installer._ort_temp_root() == Path("C:\\c")

    def test_predicate_constants_match_the_engines(self) -> None:
        """Tie the Python port to the Java source so a change there fails here."""
        src = (Path(__file__).resolve().parents[2] / "service" / "src" / "main" / "java" / "dev" / "nexus"
               / "service" / "vectors" / "OrtTempSweep.java").read_text()
        assert f'DIR_PREFIX = "{installer.ORT_TEMP_DIR_PREFIX}"' in src
        for lib in installer.ORT_LOADED_LIBS:
            assert f'"{lib}"' in src
        assert "Duration.ofMillis(250)" in src and installer.ORT_COMPLETE_MIN_AGE_S == 0.25
        assert "Duration.ofSeconds(30)" in src and installer.ORT_INCOMPLETE_MIN_AGE_S == 30.0

