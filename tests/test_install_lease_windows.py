# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows readings of the generation lease.

``TestInjected`` runs anywhere: ``platform="win32"`` selects the Windows branch,
``_win_open`` is the one seam where ctypes would call ``CreateFileW``, and
:class:`FakeHold` stands in for the delete-sharing handle (closing it removes
nothing). ``TestRealWindows`` runs only on win32 and is
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
from tests._lease_support import blind_snapshot, holder, make_generation, make_tools, venv_python, wait_until_free
from tests._module_seam import setattr_in

WIN = "win32"
ERROR_FILE_NOT_FOUND, ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION = 2, 5, 32


class FakeHold:
    """The exclusive handle (share mode FILE_SHARE_DELETE only): closing it removes
    nothing, so a reap that stops early leaves the lease file in place. The reap
    itself unlinks the lease while it still holds the handle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False

    def release(self) -> None:
        self.released = True


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
    def test_the_reap_deletes_everything_then_the_lease_while_holding_it_then_the_directory(self, tmp_path: Path) -> None:
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
        assert seen["left_when_released"] == [], "the lease file goes while the handle is still held, before the release"
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
        assert (gen / ".nx-lease").is_file(), (
            "a reap that stops early must leave the survivor leased: the lease file goes only on a complete reap"
        )

    def test_a_receipt_that_will_not_go_leaves_the_lease(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _tree(tmp_path)
        real = os.unlink

        def unlink(path, *a, **k):
            if Path(path).name == "nexus-install.json":
                raise PermissionError(ERROR_SHARING_VIOLATION, "in use", str(path))
            return real(path, *a, **k)

        setattr_in(monkeypatch, gc_core, "os.unlink", unlink)
        hold = FakeHold(gen / ".nx-lease")
        gone, detail = gc_core._reap_windows_tree(gen, hold)
        assert gone is False and "receipt" in detail, detail
        assert hold.released and (gen / ".nx-lease").is_file()

    def test_a_link_inside_the_tree_is_removed_as_a_link_and_never_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A directory that reads as a junction (``isjunction`` True, ``is_symlink`` False,
        as on Windows) is removed as a link: what it names survives."""
        gen = _tree(tmp_path)
        sentinel = tmp_path / "sentinel"
        sentinel.mkdir()
        (sentinel / "precious.txt").write_text("keep")
        (gen / "Lib" / "junction").symlink_to(sentinel, target_is_directory=True)
        # os.path.islink would catch a symlink; make the injected platform see it the way
        # Windows sees a junction instead, so only layout.is_link recognises it.
        real_islink = os.path.islink
        setattr_in(monkeypatch, gc_core, "os.path.islink", lambda p: False if Path(p).name == "junction" else real_islink(p), raising=False)
        setattr_in(monkeypatch, "nexus._install.layout_core", "os.path.isjunction", lambda p: Path(p).name == "junction", raising=False)
        gone, detail = gc_core._reap_windows_tree(gen, FakeHold(gen / ".nx-lease"), platform=WIN)
        assert (sentinel / "precious.txt").read_text() == "keep", "the reap followed a link out of the tree"
        assert gone, detail

    def test_the_hold_open_asks_for_delete_sharing_not_delete_on_close(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)
        seen: list[bool] = []
        monkeypatch.setattr(lease_core, "_win_open", lambda path, for_delete: (seen.append(for_delete), os.open(path, os.O_RDONLY))[1])
        state, hold = lease_core.acquire(gen, platform=WIN, for_delete=True)
        assert state == "free" and seen == [True]
        hold.release()
        assert lease_core.lease_path(gen).is_file(), "closing the hold must not delete the lease"

    def test_a_stat_error_that_is_not_absence_is_an_error_not_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)

        def denied(path, *a, **k):
            raise PermissionError(13, "access denied", str(path))

        setattr_in(monkeypatch, lease_core, "os.stat", denied)
        assert lease_core.probe(gen, platform=WIN) == "error:EACCES"

    def test_a_missing_or_nondirectory_parent_is_absent(self, tmp_path: Path) -> None:
        assert lease_core.probe(tmp_path / "no-such-gen", platform=WIN) == "absent"
        (tmp_path / "file").write_text("x")
        assert lease_core.probe(tmp_path / "file", platform=WIN) == "absent"

    def test_win_open_outcomes_read_as_lease_states(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)

        def raising(code: int):
            def fake(path, for_delete):
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

        def fake(path, for_delete):
            opened.append(for_delete)
            return os.open(path, os.O_RDONLY)

        monkeypatch.setattr(lease_core, "_win_open", fake)
        assert lease_core.probe(gen, platform=WIN) == "free"
        assert opened == [False], "the plan-time probe must not ask for delete access (it would take the lease away from a generation that is kept)"
        state, hold = lease_core.acquire(gen, platform=WIN, for_delete=True)
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
        assert wait_until_free(gen) == "free"

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

    def test_a_junction_inside_a_generation_is_removed_as_a_link_and_its_target_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``os.path.islink`` is False for a junction, and a walk that treats it as a directory
        empties what it names (data outside the tree). The reap must remove the junction only."""
        import _winapi  # noqa: PLC0415 -- exists on Windows only

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        tools, (a, b, c, d) = make_tools(tmp_path)
        sentinel = tmp_path / "sentinel"
        (sentinel / "sub").mkdir(parents=True)
        (sentinel / "sub" / "precious.txt").write_text("keep")
        (sentinel / "top.txt").write_text("keep")
        _winapi.CreateJunction(str(sentinel), str(a / "Lib" / "site-packages" / "linked"))
        assert (a / "Lib" / "site-packages" / "linked" / "top.txt").is_file(), "the junction does not resolve"
        lines = gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
        assert not a.exists(), lines
        assert (sentinel / "top.txt").read_text() == "keep" and (sentinel / "sub" / "precious.txt").read_text() == "keep", (
            "the reap followed a junction out of the generation and deleted what it names"
        )

    def test_a_reap_that_cannot_finish_leaves_the_survivor_leased(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A file held open without delete sharing (an unleased process's, here) stops the delete.
        The generation survives incomplete; its lease file must survive with it."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        tools, (a, *_rest) = make_tools(tmp_path)
        victim = a / "Lib" / "site-packages" / "held.py"
        victim.write_text("x")
        fd = os.open(str(victim), os.O_RDONLY | os.O_BINARY)
        try:
            lines = gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
        finally:
            os.close(fd)
        assert a.is_dir() and victim.is_file(), lines
        assert lease_core.lease_path(a).is_file(), "the failed reap took the lease with it"
        assert lease_core.probe(a) == "free"
