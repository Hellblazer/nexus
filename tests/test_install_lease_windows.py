# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows readings of the generation lease.

``TestInjected`` runs anywhere: ``platform="win32"`` selects the Windows branch,
``_win_open`` is the one seam where ctypes would call ``CreateFileW``, and
:class:`FakeHold` stands in for the ``DELETE_ON_CLOSE`` handle (closing it removes
the lease file, as the kernel does). ``TestRealWindows`` runs only on win32 and is
what proves the kernel's side: an open handle blocks the unlink, a second handle
at share mode 0 reads ``held``, and the reap really deletes everything including
the lease. Run it natively with ``scripts/windows_hand_test.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nexus._install import gc_core, lease_core
from tests._lease_support import blind_snapshot, holder, make_generation, make_tools, venv_python
from tests._module_seam import setattr_in

WIN = "win32"
ERROR_FILE_NOT_FOUND, ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION = 2, 5, 32


class FakeHold:
    """The exclusive handle: closing it deletes the lease file (DELETE_ON_CLOSE)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.path.unlink(missing_ok=True)


def _tree(tmp_path: Path) -> Path:
    gen = tmp_path / "gen-A"
    (gen / "Lib" / "site-packages" / "pkg").mkdir(parents=True)
    (gen / "Lib" / "site-packages" / "pkg" / "m.py").write_text("x")
    (gen / "Scripts").mkdir()
    (gen / "Scripts" / "python.exe").write_bytes(b"MZ")
    (gen / "pyvenv.cfg").write_text("home = C:\\py\n")
    (gen / "nexus-install.json").write_text("{}\n")
    (gen / ".nx-lease").write_text("nx-lease 1\n")
    return gen


class TestInjected:
    def test_the_reap_deletes_everything_but_the_lease_then_releases_it_then_the_directory(self, tmp_path: Path) -> None:
        gen = _tree(tmp_path)
        hold = FakeHold(gen / ".nx-lease")
        seen: dict[str, object] = {}
        real_release = hold.release

        def release() -> None:
            seen["left_when_released"] = sorted(p.name for p in gen.iterdir())
            real_release()

        hold.release = release  # type: ignore[method-assign]
        gone, detail = gc_core._reap_windows_tree(gen, hold)
        assert (gone, detail) == (True, "")
        assert seen["left_when_released"] == [".nx-lease"], seen
        assert not gen.exists()

    def test_the_receipt_is_removed_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _tree(tmp_path)
        first: list[str] = []
        real = os.unlink

        def unlink(path, *a, **k):
            first.append(Path(path).name)
            return real(path, *a, **k)

        setattr_in(monkeypatch, gc_core, "os.unlink", unlink)
        gc_core._reap_windows_tree(gen, FakeHold(gen / ".nx-lease"))
        # Path.unlink goes through os.unlink on POSIX; the receipt must lead, whatever else follows.
        assert first[0] == "nexus-install.json", first

    def test_a_file_that_will_not_go_keeps_the_generation_and_still_releases_the_lease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gen = _tree(tmp_path)
        locked = gen / "Scripts" / "python.exe"
        real = os.unlink

        def unlink(path, *a, **k):
            if Path(path) == locked:
                raise PermissionError(ERROR_SHARING_VIOLATION, "in use", str(path))
            return real(path, *a, **k)

        setattr_in(monkeypatch, gc_core, "os.unlink", unlink)
        hold = FakeHold(gen / ".nx-lease")
        gone, detail = gc_core._reap_windows_tree(gen, hold)
        assert gone is False and "python.exe" in detail, detail
        assert hold.released, "the handle must not outlive the reap"
        assert locked.is_file(), "an in-use file was removed"

    def test_win_open_outcomes_read_as_lease_states(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)

        def raising(code: int):
            def fake(path, delete_on_close):
                err = OSError(13, "x")
                err.winerror = code  # the attribute only exists on Windows; a plain one stands in
                raise err
            return fake

        for code, want in ((ERROR_SHARING_VIOLATION, "held"), (ERROR_FILE_NOT_FOUND, "absent"),
                           (ERROR_ACCESS_DENIED, "error:win5")):
            monkeypatch.setattr(lease_core, "_win_open", raising(code))
            assert lease_core.probe(gen, platform=WIN) == want, code

    def test_a_plain_open_reads_free_and_is_released(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)
        opened: list[bool] = []

        def fake(path, delete_on_close):
            opened.append(delete_on_close)
            return os.open(path, os.O_RDONLY)

        monkeypatch.setattr(lease_core, "_win_open", fake)
        assert lease_core.probe(gen, platform=WIN) == "free"
        assert opened == [False], "the plan-time probe must not ask for DELETE_ON_CLOSE (it would delete the lease of a generation that is kept)"
        state, hold = lease_core.acquire(gen, platform=WIN, delete_on_close=True)
        assert state == "free" and hold is not None and opened == [False, True]
        hold.release()


@pytest.mark.skipif(sys.platform != "win32", reason="the kernel's side of the lease")
class TestRealWindows:
    def test_an_open_handle_blocks_unlinking_the_lease_file(self, tmp_path: Path) -> None:
        """The premise the Windows hook rests on: Python's open() gives no delete
        share, so a process holding the lease open cannot have it removed."""
        path = tmp_path / ".nx-lease"
        path.write_text("nx-lease 1\n")
        fd = os.open(str(path), os.O_RDONLY | os.O_BINARY)
        try:
            with pytest.raises(PermissionError):
                path.unlink()
        finally:
            os.close(fd)
        path.unlink()

    def test_a_live_interpreter_reads_held_and_its_exit_reads_free(self, tmp_path: Path) -> None:
        gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)
        with holder(venv_python(gen)):
            assert lease_core.probe(gen) == "held"
        assert lease_core.probe(gen) == "free"

    def test_plan_keeps_a_leased_generation_the_census_cannot_see(self, tmp_path: Path) -> None:
        tools, (a, *_rest) = make_tools(tmp_path)
        with holder(venv_python(a)):
            plan = gc_core.plan(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
        assert {e.name: act for act, e, _d in plan}[a.name] == "keep"

    def test_the_reap_removes_the_whole_tree_lease_included(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        tools, (a, b, c, d) = make_tools(tmp_path)
        lines = gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
        assert not a.exists() and not b.exists() and not c.exists() and d.exists(), lines
        assert sum(line.startswith("reaped ") for line in lines) == 3, lines

    def test_a_leased_generation_survives_the_reap_intact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        tools, (a, b, c, d) = make_tools(tmp_path)
        with holder(venv_python(a)):
            gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
            assert (a / "pyvenv.cfg").is_file() and venv_python(a).is_file(), "a leased generation was gutted"
        assert not b.exists() and not c.exists()
