# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows readings of ``gc_core`` (RDR-224, nexus-f9bgu.47).

``platform="win32"`` is injected, so these run on macOS. Real symlinks stand in
for junctions where only the pointer's existence matters; where the Windows
reading of "is this a pointer" is the point, ``os.path.isjunction`` and
``os.readlink`` are patched to answer as a junction does (``is_symlink()``
False, ``isjunction()`` True, a ``\\\\?\\``-prefixed target). The locked-file
case on a real Windows kernel is ``TestRealWindows`` in
``tests/test_install_generation_windows.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus._install import gc_core

WIN = "win32"
RECEIPT = "nexus-install.json"
EMPTY_PS = ""


@pytest.fixture(autouse=True)
def _identity_realpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """compare_key resolves paths; on a macOS host that would case-fold through
    the real filesystem. Identity keeps the Windows reading under test."""
    monkeypatch.setattr(os.path, "realpath", lambda s, **_kw: s)


def _gen(tools: Path, name: str, *, receipt: bool = True) -> Path:
    gen = tools / name
    (gen / "Scripts").mkdir(parents=True)
    (gen / "pyvenv.cfg").write_text("home = C:\\py\nversion = 3.12.8\n")
    if receipt:
        (gen / RECEIPT).write_text("{}\n")
    return gen


def _ages(*paths: Path) -> None:
    """Old enough that no build-in-progress grace applies."""
    old = 1_000_000_000
    for path in paths:
        for sub in [path, *path.rglob("*")]:
            os.utime(sub, (old, old), follow_symlinks=False)


def _plan(tools: Path, **kw):
    return gc_core.plan(tools, snapshot=EMPTY_PS, platform=WIN, **kw)


def _actions(plan) -> dict[str, str]:
    return {entry.name: action for action, entry, _detail in plan}


class TestRulesCompareFoldedPaths:
    def test_rule_d_protects_the_running_generation_spelt_another_way(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        gens = [_gen(tools, f"gen-{n}") for n in "ABCDE"]
        _ages(*gens)
        # The running installer's generation, as sys.prefix can spell it: other
        # case and the extended prefix. String equality read this as "some other
        # tree" and reaped it.
        spelt = "\\\\?\\" + str(gens[0]).upper()
        plan = _plan(tools, keep=2, self_generation=spelt)
        assert _actions(plan)["gen-A"] == "skip", plan
        assert plan[0][2] == "protected pointer"

    def test_without_rule_d_the_oldest_is_reaped(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        gens = [_gen(tools, f"gen-{n}") for n in "ABCDE"]
        _ages(*gens)
        assert _actions(_plan(tools, keep=2))["gen-A"] == "reap"

    def test_a_junction_pointer_protects_current_and_previous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools = tmp_path / "tools"
        gens = {n: _gen(tools, f"gen-{n}") for n in "ABCDE"}
        _ages(*gens.values())
        # `current` and `previous` as Windows junctions: plain directories to
        # is_symlink(), links to isjunction(), prefixed targets from readlink.
        (tools / "current").mkdir()
        (tools / "previous").mkdir()
        targets = {"current": gens["A"], "previous": gens["B"]}
        monkeypatch.setattr(os.path, "isjunction", lambda p: Path(p).name in targets, raising=False)
        real_readlink = os.readlink
        monkeypatch.setattr(
            os, "readlink",
            lambda p, **kw: "\\\\?\\" + str(targets[Path(p).name]) if Path(p).name in targets
            else real_readlink(p, **kw),
        )
        actions = _actions(_plan(tools, keep=1))
        assert actions["gen-A"] == "skip" and actions["gen-B"] == "skip"
        assert actions["gen-C"] == "reap" and actions["gen-D"] == "reap"

    def test_a_junction_named_gen_something_is_never_treated_as_a_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """is_symlink() is False for a junction, so a receipt-less junction
        looked like a plain wreckage directory. It must be a pointer."""
        tools = tmp_path / "tools"
        victim = tmp_path / "unrelated"
        victim.mkdir()
        (victim / "precious.txt").write_text("data")
        tools.mkdir()
        rogue = tools / "gen-rogue"
        rogue.mkdir()  # stands in for the junction
        monkeypatch.setattr(os.path, "isjunction", lambda p: Path(p).name == "gen-rogue", raising=False)
        monkeypatch.setattr(os, "readlink", lambda p, **kw: "\\\\?\\" + str(victim))
        errors: list[str] = []
        lines = gc_core._reap(rogue, errors.append, WIN)
        assert lines == []
        assert any("unrecognised generation symlink" in e for e in errors)
        assert (victim / "precious.txt").read_text() == "data"


class TestWindowsReap:
    def _tree(self, tmp_path: Path) -> tuple[Path, Path]:
        tools = tmp_path / "tools"
        gen = _gen(tools, "gen-A")
        (gen / "Lib").mkdir()
        (gen / "Lib" / "x.py").write_text("x")
        return tools, gen

    def test_a_free_generation_is_reaped_and_is_gone(self, tmp_path: Path) -> None:
        _tools, gen = self._tree(tmp_path)
        lines = gc_core._reap(gen, lambda _m: None, WIN)
        assert lines == [f"reaped {gen}"]
        assert not gen.exists()

    def test_the_receipt_goes_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _tools, gen = self._tree(tmp_path)
        seen: dict[str, bool] = {}

        def spy(path, on_error):
            seen["receipt_present"] = (path / RECEIPT).exists()
            seen["dir_present"] = path.exists()

        monkeypatch.setattr(gc_core, "_rmtree", spy)
        gc_core._reap(gen, lambda _m: None, WIN)
        assert seen == {"receipt_present": False, "dir_present": True}

    def test_a_locked_file_keeps_the_generation_and_never_says_reaped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tools, gen = self._tree(tmp_path)
        locked = gen / "Lib" / "x.py"

        def partial(path, on_error):
            # What rmtree does against an open file: everything else goes, the
            # locked file and its parents stay, the handler hears about it.
            (path / "pyvenv.cfg").unlink()
            on_error(os.unlink, str(locked), PermissionError(32, "in use"))

        # The handler retries a PermissionError once; make the retry fail too.
        real_unlink = os.unlink

        def unlink(p, *a, **kw):
            if str(p) == str(locked):
                raise PermissionError(32, "in use")
            return real_unlink(p, *a, **kw)

        monkeypatch.setattr(gc_core, "_rmtree", partial)
        monkeypatch.setattr(os, "unlink", unlink)
        lines = gc_core._reap(gen, lambda _m: None, WIN)
        assert len(lines) == 1
        assert lines[0].startswith(f"kept {gen}: in use"), lines
        assert "reaped" not in lines[0]
        assert gen.exists() and locked.exists()
        # The receipt went first, so the husk is no longer a generation.
        assert not (gen / RECEIPT).exists()

    def test_a_receipt_that_cannot_be_removed_keeps_the_tree_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tools, gen = self._tree(tmp_path)
        real_unlink = Path.unlink

        def unlink(self, *a, **kw):
            if self.name == RECEIPT:
                raise PermissionError(5, "denied")
            return real_unlink(self, *a, **kw)

        called: list[Path] = []
        monkeypatch.setattr(Path, "unlink", unlink)
        monkeypatch.setattr(gc_core, "_rmtree", lambda path, cb: called.append(path))
        lines = gc_core._reap(gen, lambda _m: None, WIN)
        assert lines[0].startswith(f"kept {gen}: in use")
        assert called == []
        assert (gen / "Lib" / "x.py").exists()

    def test_the_handler_retries_a_read_only_file_once(self, tmp_path: Path) -> None:
        target = tmp_path / "ro.txt"
        target.write_text("x")
        failures: list = []
        handler = gc_core._collecting_handler(failures)
        handler(os.unlink, str(target), PermissionError(5, "read-only"))
        assert failures == []
        assert not target.exists()

    def test_the_handler_records_what_still_fails(self, tmp_path: Path) -> None:
        failures: list = []
        handler = gc_core._collecting_handler(failures)

        def always(_p):
            raise PermissionError(32, "in use")

        handler(always, str(tmp_path), PermissionError(32, "in use"))
        handler(os.rmdir, str(tmp_path / "x"), OSError(39, "not empty"))
        assert [p for p, _e in failures] == [str(tmp_path), str(tmp_path / "x")]


class TestWindowsLegacyLedger:
    def _legacy(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        tools = tmp_path / "tools"
        tools.mkdir()
        venv = tmp_path / "uvtools" / "conexus"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = C:\\py\n")
        (venv / "Lib").mkdir()
        (venv / "Lib" / "m.py").write_text("m")
        pointer = tools / "gen-legacy-uv-tool"
        pointer.symlink_to(venv)
        return tools, venv, pointer

    def test_reaps_the_real_tree_then_the_pointer(self, tmp_path: Path) -> None:
        _tools, venv, pointer = self._legacy(tmp_path)
        errs: list[str] = []
        lines = gc_core._reap(pointer, errs.append, WIN)
        assert lines == [f"reaped {pointer}"]
        assert not venv.exists() and not pointer.is_symlink()
        assert any("reaped the legacy uv tree" in e for e in errs)

    def test_a_locked_legacy_tree_keeps_tree_and_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tools, venv, pointer = self._legacy(tmp_path)

        def locked(path, on_error):
            on_error(os.unlink, str(path / "Lib" / "m.py"), OSError(32, "in use"))

        monkeypatch.setattr(gc_core, "_rmtree", locked)
        lines = gc_core._reap(pointer, lambda _m: None, WIN)
        assert lines[0].startswith(f"kept {pointer}: in use"), lines
        assert venv.is_dir() and pointer.is_symlink()

    def test_a_pointer_at_a_non_venv_unlinks_only_the_pointer(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        (home / "keep.txt").write_text("k")
        pointer = tools / "gen-legacy-uv-tool"
        pointer.symlink_to(home)
        errs: list[str] = []
        gc_core._reap(pointer, errs.append, WIN)
        assert (home / "keep.txt").exists()
        assert not pointer.is_symlink()


def test_the_full_sweep_on_windows_keeps_held_and_protected(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    gens = [_gen(tools, f"gen-{n}") for n in "ABCD"]
    _ages(*gens)
    snapshot = f"4242 {str(gens[1]).replace(os.sep, '/')}/Scripts/python.exe -m nexus\n"
    lines = gc_core.gc_generations(
        tools, keep=1, self_generation=str(gens[0]), snapshot=snapshot, platform=WIN,
    )
    reaped = {Path(line.split(" ", 1)[1]).name for line in lines if line.startswith("reaped")}
    assert reaped == {"gen-C"}, lines
    assert any(line.startswith("kept") and "gen-B" in line and "4242" in line for line in lines)
    assert gens[0].exists() and gens[1].exists() and gens[3].exists()
    assert not gens[2].exists()
