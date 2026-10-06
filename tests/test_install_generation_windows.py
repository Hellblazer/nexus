# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows generation installer, ``_install/generation_core.py``
(RDR-224, nexus-f9bgu.47).

Injected-platform tests: ``platform="win32"`` selects the Windows reading, a
fake ``uv`` stands in for the real one, and :class:`SymlinkOps` makes a symlink
where Windows makes a junction. They use ``#!/bin/sh`` interpreter stubs and
symlinks, so they run on POSIX hosts and are NOT part of the Windows rehearsal's
test set. What an injected seam cannot prove (real junctions, a running
launcher, a file held open) is ``tests/test_install_generation_real_windows.py``.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from nexus._install import gc_core
from nexus._install import generation_core as gen_core
from nexus._install import layout_core as lc

WIN = "win32"


class SymlinkOps(gen_core.LinkOps):
    """Where Windows makes a junction, a symlink; the logic above it is the same."""

    def create(self, target: str, link: Path) -> None:
        os.symlink(target, link, target_is_directory=True)


class FakeUv:
    """Answers ``uv venv`` / ``uv pip install`` / ``uv tool dir`` the way the real
    one leaves a tree: a pyvenv.cfg, and a Windows ``Scripts`` directory with an
    interpreter stub that declares the console scripts."""

    def __init__(
        self, *, scripts=("nx", "nx-mcp"), home="C:\\py\\cpython-3.12", cfg_version="",
        fail_install: str | None = None, tool_dir: Path | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.scripts = scripts
        self.home = home
        self.cfg_version = cfg_version
        self.fail_install = fail_install
        self.tool_dir = tool_dir

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[:3] == ["uv", "tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.tool_dir}\n", "")
        if argv[:2] == ["uv", "venv"]:
            gen = Path(argv[-1])
            (gen / "Scripts").mkdir(parents=True, exist_ok=True)
            cfg = f"home = {self.home}\nversion_info = 3.12.8\n"
            if self.cfg_version:
                cfg += f"version = {self.cfg_version}\n"
            if self.home:
                (gen / "pyvenv.cfg").write_text(cfg)
            else:
                (gen / "pyvenv.cfg").write_text("version_info = 3.12.8\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["uv", "pip", "install"]:
            if self.fail_install is not None:
                return subprocess.CompletedProcess(argv, 1, "", self.fail_install)
            python = Path(argv[argv.index("--python") + 1])
            scripts = python.parent
            python.write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(self.scripts) + "\n")
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            for name in self.scripts:
                (scripts / f"{name}.exe").write_bytes(f"launcher:{name}".encode())
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    root = tmp_path / "tools"
    root.mkdir()
    return root


def _build(tools: Path, uv: FakeUv, **kw) -> Path:
    kw.setdefault("source", "conexus")
    return gen_core.build_generation(
        tools=tools, run=uv, platform=WIN, now=kw.pop("now", lambda: 1_790_000_000.0), **kw,
    )


def _gen(tools: Path, name: str) -> Path:
    gen = tools / name
    (gen / "Scripts").mkdir(parents=True)
    return gen


class TestBuildGeneration:
    def test_builds_a_venv_at_its_final_path_and_writes_the_receipt_last(self, tools: Path) -> None:
        uv = FakeUv()
        gen = _build(tools, uv, version="7.1.0", extras=["local"])
        assert gen.parent == tools and gen.name.startswith("gen-")
        receipt = lc.read_receipt(gen)
        assert receipt.spec == "conexus[local]==7.1.0"
        assert receipt.source_kind == "registry" and receipt.source == "conexus"
        assert receipt.extras == ["local"] and receipt.version == "7.1.0"
        assert receipt.base_interpreter == "C:\\py\\cpython-3.12"
        assert lc.list_generations(tools=tools) == [gen]

    def test_the_commands_name_the_windows_interpreter_and_the_packaged_overrides(self, tools: Path) -> None:
        uv = FakeUv()
        gen = _build(tools, uv, python_version="3.12")
        venv, install = uv.calls
        assert venv == ["uv", "venv", "--allow-existing", "--python", "3.12", str(gen)]
        assert install[:3] == ["uv", "pip", "install"]
        assert install[install.index("--python") + 1] == str(gen / "Scripts" / "python.exe")
        overrides = Path(install[install.index("--overrides") + 1])
        assert overrides.name == "overrides.txt" and overrides.is_file()
        assert install[-1] == "conexus"
        assert "--torch-backend" not in install  # Linux-only

    def test_constraints_are_passed_through(self, tools: Path, tmp_path: Path) -> None:
        pins = tmp_path / "pins.txt"
        pins.write_text("x==1\n")
        uv = FakeUv()
        _build(tools, uv, constraints=str(pins))
        install = uv.calls[1]
        assert install[install.index("--constraints") + 1] == str(pins)

    def test_the_building_marker_is_left_in_place(self, tools: Path) -> None:
        gen = _build(tools, FakeUv())
        assert (gen / lc.BUILDING_MARKER_NAME).is_file()

    def test_python_full_comes_from_the_version_line_else_the_requested_version(self, tools: Path) -> None:
        assert lc.read_receipt(_build(tools, FakeUv())).python == "3.12"
        assert lc.read_receipt(_build(tools, FakeUv(cfg_version="3.12.8"), now=lambda: 1_790_000_100.0)).python == "3.12.8"

    def test_two_builds_in_one_second_never_share_a_tree(self, tools: Path) -> None:
        first = _build(tools, FakeUv())
        (first / "fingerprint").write_text("first")
        second = _build(tools, FakeUv())
        third = _build(tools, FakeUv())
        assert len({first, second, third}) == 3
        assert second.name == first.name + "a" and third.name == first.name + "b"
        assert (first / "fingerprint").read_text() == "first"

    def test_a_failed_install_raises_with_uvs_words_and_leaves_no_tree(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="No solution found"):
            _build(tools, FakeUv(fail_install="x No solution found when resolving"))
        assert list(tools.iterdir()) == []

    def test_a_missing_uv_says_so_and_leaves_no_tree(self, tools: Path) -> None:
        def no_uv(argv, **_kw):
            raise FileNotFoundError(2, "not found")

        with pytest.raises(gen_core.GenerationError, match="uv was not found"):
            gen_core.build_generation("conexus", tools=tools, run=no_uv, platform=WIN)
        assert list(tools.iterdir()) == []

    def test_a_venv_with_no_home_line_is_refused_and_removed(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="no 'home ='"):
            _build(tools, FakeUv(home=""))
        assert list(tools.iterdir()) == []

    def test_a_missing_constraints_file_is_refused_before_a_tree_is_claimed(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="constraints"):
            _build(tools, FakeUv(), constraints=str(tools / "nope.txt"))
        assert list(tools.iterdir()) == []

    def test_an_empty_source_is_refused(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="--source is required"):
            gen_core.build_generation("", tools=tools, run=FakeUv(), platform=WIN)

    def test_a_directory_source_is_made_absolute_and_recorded(
        self, tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.chdir(checkout)
        gen = _build(tools, FakeUv(), source=".")
        receipt = lc.read_receipt(gen)
        assert receipt.source_kind == "directory"
        assert receipt.source == str(checkout.resolve()) and receipt.spec == str(checkout.resolve())

    def test_a_backslash_source_is_a_directory_not_a_registry_name(self, tools: Path) -> None:
        """Without the nt-only source_kind rule ``C:\\src\\nexus`` is a registry
        spec and uv is asked to resolve it as a package name."""
        uv = FakeUv()
        with pytest.raises(gen_core.GenerationError, match="directory source does not exist"):
            _build(tools, uv, source="C:\\src\\nexus")
        assert uv.calls == []
        assert list(tools.iterdir()) == []


class TestFlipAndRollback:
    def _flip(self, gen: Path, tools: Path) -> None:
        gen_core.flip_current(gen, tools, ops=SymlinkOps(), platform="linux")

    def test_the_first_flip_points_current_and_records_no_previous(self, tools: Path) -> None:
        gen = _gen(tools, "gen-A")
        self._flip(gen, tools)
        assert os.readlink(tools / "current") == str(gen)
        assert not os.path.lexists(tools / "previous")

    def test_the_second_flip_records_the_outgoing_generation_as_previous(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        assert os.readlink(tools / "current") == str(b)
        assert os.readlink(tools / "previous") == str(a)
        assert sorted(p.name for p in tools.iterdir()) == ["current", "gen-A", "gen-B", "previous"]

    def test_flipping_to_the_generation_already_current_leaves_previous_alone(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        self._flip(b, tools)
        assert os.readlink(tools / "previous") == str(a)

    def test_rollback_is_itself_reversible(self, tools: Path) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")
        assert os.readlink(tools / "current") == str(a)
        assert os.readlink(tools / "previous") == str(b)
        gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")
        assert os.readlink(tools / "current") == str(b)

    def test_rollback_with_nothing_recorded_or_a_reaped_previous_refuses(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="nothing to roll back"):
            gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        self._flip(b, tools)
        shutil.rmtree(a)
        with pytest.raises(gen_core.GenerationError, match="previous generation is gone"):
            gen_core.rollback_current(tools, ops=SymlinkOps(), platform="linux")

    def test_a_relative_or_missing_target_is_refused(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="absolute"):
            gen_core.flip_current("gen-A", tools, ops=SymlinkOps())
        with pytest.raises(gen_core.GenerationError, match="not a directory"):
            gen_core.flip_current(tools / "gen-nope", tools, ops=SymlinkOps())

    def test_a_real_directory_named_current_is_never_replaced(self, tools: Path) -> None:
        (tools / "current").mkdir()
        (tools / "current" / "keep").write_text("k")
        with pytest.raises(gen_core.GenerationError, match="not a link"):
            self._flip(_gen(tools, "gen-A"), tools)
        assert (tools / "current" / "keep").read_text() == "k"

    def test_a_failed_second_rename_restores_the_original_pointer(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        self._flip(a, tools)
        real = os.rename

        def rename(src, dst, *args, **kw):
            if Path(src).name.startswith(".current.new."):
                raise PermissionError(5, "denied")
            return real(src, dst, *args, **kw)

        monkeypatch.setattr(os, "rename", rename)
        with pytest.raises(gen_core.GenerationError, match="could not repoint"):
            self._flip(b, tools)
        assert os.readlink(tools / "current") == str(a)
        assert [p.name for p in tools.iterdir() if p.name.startswith(".")] == []

    def test_litter_from_an_interrupted_swap_is_swept(self, tools: Path) -> None:
        a = _gen(tools, "gen-A")
        os.symlink(str(a), tools / ".current.new.999")
        os.symlink(str(a), tools / ".current.old.999")
        self._flip(a, tools)
        assert [p.name for p in tools.iterdir() if p.name.startswith(".")] == []

    def test_the_pointer_comparison_is_the_windows_key_not_the_string(
        self, tools: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flipping to the generation current already names, spelt in another
        case, must not rewrite previous to name that same generation."""
        monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)
        a, b = _gen(tools, "gen-A"), _gen(tools, "gen-B")
        gen_core.flip_current(a, tools, ops=SymlinkOps(), platform=WIN)
        gen_core.flip_current(b, tools, ops=SymlinkOps(), platform=WIN)
        assert os.readlink(tools / "previous") == str(a)


class TestRegisterLegacy:
    def test_registers_idempotently(self, tools: Path, tmp_path: Path) -> None:
        legacy = tmp_path / "uv" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        pointer = gen_core.register_legacy(legacy, tools, ops=SymlinkOps(), platform="linux")
        assert pointer == tools / "gen-legacy-uv-tool"
        assert os.readlink(pointer) == str(legacy)
        before = os.lstat(pointer).st_ino
        gen_core.register_legacy(legacy, tools, ops=SymlinkOps(), platform="linux")
        assert os.lstat(pointer).st_ino == before

    def test_refreshes_a_pointer_that_names_something_else(self, tools: Path, tmp_path: Path) -> None:
        old, new = tmp_path / "old", tmp_path / "new"
        old.mkdir()
        new.mkdir()
        gen_core.register_legacy(old, tools, ops=SymlinkOps(), platform="linux")
        gen_core.register_legacy(new, tools, ops=SymlinkOps(), platform="linux")
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(new)
        assert old.is_dir()

    def test_a_relative_path_is_refused(self, tools: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="absolute"):
            gen_core.register_legacy("uv/conexus", tools, ops=SymlinkOps())


class TestLegacyExtras:
    def _receipt(self, tmp_path: Path, body: str) -> Path:
        (tmp_path / "uv-receipt.toml").write_text(body)
        return tmp_path

    def test_reads_sorted_extras_and_drops_mineru(self, tmp_path: Path) -> None:
        legacy = self._receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "conexus", extras = [\n  "voyage",\n  "local",\n"mineru" ] }]\n',
        )
        assert gen_core.legacy_extras(legacy) == ["local", "voyage"]

    def test_no_receipt_or_no_extras_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert gen_core.legacy_extras(tmp_path) == []
        assert gen_core.legacy_extras(self._receipt(tmp_path, '[tool]\nrequirements = [{ name = "conexus" }]\n')) == []


class TestMigrateLegacy:
    def _legacy(self, tmp_path: Path, extras: str = '"local", "mineru"') -> Path:
        legacy = tmp_path / "uvtools" / "conexus"
        (legacy / "Scripts").mkdir(parents=True)
        (legacy / "Scripts" / "nx.exe").write_bytes(b"uv's launcher")
        (legacy / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "conexus", extras = [{extras}] }}]\n'
        )
        return legacy

    def test_builds_flips_shims_and_registers_without_touching_the_legacy_tree(
        self, tools: Path, tmp_path: Path,
    ) -> None:
        legacy = self._legacy(tmp_path)
        bin_dir = tmp_path / "bin"
        gen = gen_core.migrate_legacy(
            "conexus", legacy_venv=legacy, tools=tools, bin_dir=bin_dir,
            run=FakeUv(), ops=SymlinkOps(), platform=WIN,
        )
        assert gen is not None and lc.read_receipt(gen).extras == ["local"]
        assert os.readlink(tools / "current") == str(gen)
        assert (bin_dir / "nx.exe").read_bytes() == b"launcher:nx"
        assert lc.read_shim_record(bin_dir).keys() == {"nx", "nx-mcp"}
        assert os.readlink(tools / "gen-legacy-uv-tool") == str(legacy)
        # Never uninstalled, never reaped: every holder keeps its tree.
        assert (legacy / "Scripts" / "nx.exe").read_bytes() == b"uv's launcher"

    def test_no_legacy_tree_is_a_clean_no_op(self, tools: Path, tmp_path: Path) -> None:
        uv = FakeUv()
        got = gen_core.migrate_legacy(
            "conexus", legacy_venv=tmp_path / "absent", tools=tools, run=uv, platform=WIN,
        )
        assert got is None and uv.calls == [] and list(tools.iterdir()) == []

    def test_the_legacy_dir_is_asked_of_uv_when_not_given(self, tools: Path, tmp_path: Path) -> None:
        self._legacy(tmp_path)
        uv = FakeUv(tool_dir=tmp_path / "uvtools")
        gen = gen_core.migrate_legacy(
            "conexus", tools=tools, bin_dir=tmp_path / "bin", run=uv, ops=SymlinkOps(), platform=WIN,
        )
        assert gen is not None
        assert uv.calls[0] == ["uv", "tool", "dir"]

    def test_an_unresolvable_uv_is_an_error_not_a_no_op(self, tools: Path) -> None:
        def broken(argv, **_kw):
            return subprocess.CompletedProcess(argv, 1, "", "uv exploded")

        with pytest.raises(gen_core.GenerationError, match="uv exploded"):
            gen_core.migrate_legacy("conexus", tools=tools, run=broken, platform=WIN)

    def test_a_missing_source_is_refused(self, tools: Path, tmp_path: Path) -> None:
        with pytest.raises(gen_core.GenerationError, match="--source is required"):
            gen_core.migrate_legacy("", legacy_venv=self._legacy(tmp_path), tools=tools, platform=WIN)


class TestReap:
    def test_reaps_through_gc_core_with_rule_d(self, tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)
        gens = [_gen(tools, f"gen-{n}") for n in "ABCD"]
        for gen in gens:
            (gen / "nexus-install.json").write_text("{}\n")
            os.utime(gen, (1_000_000_000, 1_000_000_000))
            for sub in gen.rglob("*"):
                os.utime(sub, (1_000_000_000, 1_000_000_000))
        monkeypatch.setattr(gc_core._census(), "ps_snapshot", lambda **_k: "")
        lines = gen_core.reap(tools, keep=1, self_generation=gens[0], dry_run=True, platform=WIN)
        would = {Path(line.split(" ", 2)[2]).name for line in lines if line.startswith("would reap")}
        assert would == {"gen-B", "gen-C"}, lines

    def test_keep_zero_is_refused_on_stderr_and_deletes_nothing(
        self, tools: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        gen = _gen(tools, "gen-A")
        assert gen_core.reap(tools, keep=0, platform=WIN) == []
        assert "would retain no generations" in capsys.readouterr().err
        assert gen.exists()
