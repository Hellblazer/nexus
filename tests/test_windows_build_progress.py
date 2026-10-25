# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows generation build shows progress (nexus-2k66t.4.7).

The POSIX build streams uv's stderr (``self_cmd._run_build``); the Windows one,
``generation_core.build_generation``, captured everything until uv exited, so
the on-demand PDF install (a ~370 MB fetch) sat silent for minutes after its
notice line. With ``progress=True`` the build now copies uv's stderr through as
it arrives and prints a heartbeat line whenever uv has been silent for
``HEARTBEAT_S`` (uv writes no progress bar when its stderr is a pipe, so the
copy alone can stay silent through a long download).

Real child processes here (``sys.executable``), so the streaming and the
heartbeat are observed, not asserted against a stand-in.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nexus._install import generation_core as gen_core
from nexus.install_layout import Receipt
from nexus.commands import self_cmd
from tests._module_seam import setattr_in
from tests.test_install_generation_windows import WIN, FakeUv, SymlinkOps


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_progress_copies_stderr_through_as_it_arrives(capsys: pytest.CaptureFixture[str]) -> None:
    out = gen_core._run(
        _child("import sys; sys.stderr.write('Downloading torch\\n'); print('stdout-line')"),
        None, "uv pip install", progress=True,
    )
    assert out.strip() == "stdout-line"
    assert "Downloading torch" in capsys.readouterr().err


def test_progress_prints_a_heartbeat_while_the_child_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(gen_core, "HEARTBEAT_S", 0.2)
    gen_core._run(_child("import time; time.sleep(1.1)"), None, "uv pip install", progress=True)
    err = capsys.readouterr().err
    beats = [ln for ln in err.splitlines() if "uv pip install still running" in ln]
    assert len(beats) >= 2, err
    assert "no progress when its output is not a terminal" in beats[0]


def test_a_child_that_keeps_talking_gets_no_heartbeat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # 2 s, not 0.5: the clock starts before the child's interpreter does, and a
    # loaded CI box can spend over half a second on that alone.
    monkeypatch.setattr(gen_core, "HEARTBEAT_S", 2.0)
    code = "import sys,time\nfor i in range(8):\n    sys.stderr.write('chunk %d\\n' % i); sys.stderr.flush(); time.sleep(0.1)"
    gen_core._run(_child(code), None, "uv pip install", progress=True)
    assert "still running" not in capsys.readouterr().err


def test_a_failed_progress_run_raises_with_the_childs_stderr() -> None:
    with pytest.raises(gen_core.GenerationError) as exc:
        gen_core._run(
            _child("import sys; sys.stderr.write('dns error: failed to lookup address\\n'); sys.exit(3)"),
            None, "uv pip install", progress=True,
        )
    assert "dns error: failed to lookup address" in str(exc.value)


def test_without_progress_nothing_is_streamed_and_failures_read_the_same(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(gen_core.GenerationError) as exc:
        gen_core._run(
            _child("import sys; sys.stderr.write('boom\\n'); sys.exit(3)"), None, "uv pip install",
        )
    assert "boom" in str(exc.value)
    assert capsys.readouterr().err == ""


# ── the request reaches the Windows build ────────────────────────────────────


@pytest.fixture
def spy_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    core = self_cmd._generation()
    seen: list[dict] = []
    generation = tmp_path / "tools" / "gen-x"

    def _fake_build(source, **kw):
        seen.append(kw)
        # A built generation carries its receipt, and the flip step reads it first (nexus-lvvhm).
        generation.mkdir(parents=True, exist_ok=True)
        (generation / "nexus-install.json").write_text(Receipt(
            version="7.0.0", spec="conexus", source_kind="registry", source="conexus", python="3.12.8",
            base_interpreter="/py", created_at="2026-10-24T00:00:00Z", extras=[],
        ).to_json())
        return generation

    monkeypatch.setattr(core, "build_generation", _fake_build)
    monkeypatch.setattr(self_cmd, "_windows_flip_and_path", lambda *a, **k: None)
    monkeypatch.setattr(self_cmd, "_plat", lambda: "win32")
    return seen


def test_a_progress_request_reaches_the_windows_builder(spy_build, tmp_path: Path) -> None:
    build = self_cmd._WindowsBuild("conexus", ["pdf"], "7.0.0")
    self_cmd._build_flip_shims(
        build, install_dir=tmp_path, tools=tmp_path / "tools", bin_dir=tmp_path / "bin", progress=True,
    )
    assert spy_build[0]["progress"] is True


def test_a_plain_request_stays_quiet_on_windows(spy_build, tmp_path: Path) -> None:
    build = self_cmd._WindowsBuild("conexus", [], "7.0.0")
    self_cmd._build_flip_shims(build, install_dir=tmp_path, tools=tmp_path / "tools", bin_dir=tmp_path / "bin")
    assert spy_build[0]["progress"] is False


def test_progress_with_the_run_seam_builds_as_before(tmp_path: Path) -> None:
    """The seam wins over streaming: a fake ``run`` is called with capture options."""
    calls: list[list[str]] = []

    def _run(argv, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    assert gen_core._run(["uv", "venv"], _run, "uv venv", progress=True) == "ok"
    assert calls and calls[0][-1] == "venv"


# ── the heartbeat keeps its cadence (nexus-2k66t.4.7 fix round) ───────────────


class _FakeProc:
    """A process the test drives by hand: real pipes, a clock-honouring ``wait``."""

    def __init__(self) -> None:
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()
        self.stdout = os.fdopen(r_out, "r", encoding="utf-8")
        self.stderr = os.fdopen(r_err, "r", encoding="utf-8")
        self.out_w = os.fdopen(w_out, "w", encoding="utf-8")
        self.err_w = os.fdopen(w_err, "w", encoding="utf-8")
        self.returncode: int | None = None
        self.killed = False
        self._done = threading.Event()

    def wait(self, timeout: float | None = None) -> int | None:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code
        self.out_w.close()
        self.err_w.close()
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)


class _StderrClock:
    """Stands in for ``sys.stderr`` and timestamps every write."""

    def __init__(self) -> None:
        self.events: list[float] = []

    def write(self, text: str) -> int:
        self.events.append(time.monotonic())
        return len(text)

    def flush(self) -> None:
        pass


def test_silence_never_exceeds_one_heartbeat_window_when_output_lands_just_after_a_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed-cadence wait checks at T, 2T, 3T; output at T+0.06 made the next
    heartbeat wait until 3T, so the operator saw ~2T of silence. The wait now
    covers only what is LEFT of the window."""
    hb = 1.0
    monkeypatch.setattr(gen_core, "HEARTBEAT_S", hb)
    clock = _StderrClock()
    monkeypatch.setattr(sys, "stderr", clock)
    proc = _FakeProc()
    end: list[float] = []

    def _drive() -> None:
        time.sleep(hb + 0.06)  # just after the first would-be tick
        proc.err_w.write("Downloading torch\n")
        proc.err_w.flush()
        time.sleep(2.4)
        end.append(time.monotonic())
        proc.finish()

    start = time.monotonic()
    driver = threading.Thread(target=_drive)
    driver.start()
    gen_core._stream_until_exit(proc, ["uv"], "uv pip install")
    driver.join()
    events = [start, *clock.events, end[0]]
    gaps = [b - a for a, b in zip(events, events[1:], strict=False)]
    assert max(gaps) <= 1.5 * hb, f"longest silence {max(gaps):.2f}s of a {hb}s window; gaps {gaps}"
    assert len(clock.events) >= 4, "the chunk plus a heartbeat per window"


# ── a dead stderr, a Ctrl-C, big output ──────────────────────────────────────


def _within(seconds: float, fn):
    """Run *fn* on a thread; fail (not hang) when it has not returned in *seconds*."""
    box: dict = {}

    def _go() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- reported to the caller
            box["error"] = exc

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "the call hung (a full pipe nobody drains)"
    if "error" in box:
        raise box["error"]
    return box["value"]


class _BrokenStderr:
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


_NOISY = (
    "import sys\n"
    "for _ in range(3000): sys.stderr.write('e' * 99 + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.stdout.write('o' * 300_000 + '\\n'); sys.stdout.flush()\n"
)


def _closed_stderr():
    import io

    f = io.StringIO()
    f.close()
    return f


@pytest.mark.parametrize("dead", [None, _BrokenStderr(), _closed_stderr()], ids=["none", "broken-pipe", "closed"])
def test_a_dead_stderr_does_not_stall_uv_or_replace_the_builds_result(
    monkeypatch: pytest.MonkeyPatch, dead,
) -> None:
    """The tee used to be unguarded: a write error killed the reader thread, uv
    then blocked on a full pipe, and what the operator got was a hang, never the
    build's own error. ``sys.stderr`` is None under pythonw."""
    monkeypatch.setattr(sys, "stderr", dead)
    out = _within(30, lambda: gen_core._run(_child(_NOISY), None, "uv pip install", progress=True))
    assert len(out.strip()) == 300_000

    failing = _NOISY + "sys.stderr.write('the real reason\\n'); sys.exit(3)\n"
    with pytest.raises(gen_core.GenerationError) as exc:
        _within(30, lambda: gen_core._run(_child(failing), None, "uv pip install", progress=True))
    assert "the real reason" in str(exc.value)
    assert "could not run" not in str(exc.value)


def test_a_dead_stderr_does_not_break_the_heartbeat_emitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    gen_core._emit(["a line"])  # does not raise
    monkeypatch.setattr(sys, "stderr", None)
    gen_core._emit(["a line"])


def test_more_than_a_pipe_buffer_on_both_pipes_does_not_deadlock(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _within(30, lambda: gen_core._run(_child(_NOISY), None, "uv pip install", progress=True))
    assert len(out.strip()) == 300_000
    assert capsys.readouterr().err.count("e") >= 297_000


def test_the_posix_build_drain_also_survives_a_dead_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """``self_cmd._run_build`` has the same tee-then-drain shape."""
    monkeypatch.setattr(sys, "stderr", None)
    done = _within(30, lambda: self_cmd._run_build(_child(_NOISY + "sys.exit(0)"), progress=True))
    assert done.returncode == 0 and len(done.stderr.replace("\n", "")) == 297_000


class _InterruptingProc:
    """Wraps a real process; ``wait`` raises KeyboardInterrupt *times* times first."""

    def __init__(self, proc: "subprocess.Popen[str]", times: int) -> None:
        self._proc = proc
        self._left = times
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    @property
    def returncode(self):
        return self._proc.returncode

    def wait(self, timeout: float | None = None):
        if self._left > 0:
            self._left -= 1
            raise KeyboardInterrupt
        return self._proc.wait(timeout)

    def kill(self) -> None:
        self._proc.kill()


def _sleeper() -> "subprocess.Popen[str]":
    return subprocess.Popen(  # noqa: S603
        _child("import time; time.sleep(120)"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
    )


@pytest.mark.parametrize("interrupts", [1, 2], ids=["ctrl-c", "second-ctrl-c-during-the-post-kill-wait"])
def test_ctrl_c_kills_the_child_closes_its_pipes_and_propagates(interrupts: int) -> None:
    real = _sleeper()
    try:
        with pytest.raises(KeyboardInterrupt):
            gen_core._stream_until_exit(_InterruptingProc(real, interrupts), ["uv"], "uv pip install")
        assert real.wait(timeout=10) is not None, "the child must be dead"
        assert real.returncode != 0
        assert real.stdout.closed and real.stderr.closed
    finally:
        if real.poll() is None:
            real.kill()
            real.wait()


@pytest.mark.parametrize("code", ["pass", "import sys; sys.stderr.write('bad\\n'); sys.exit(2)"], ids=["ok", "failed"])
def test_the_pipes_are_closed_after_a_normal_run(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    made: list = []
    real_popen = subprocess.Popen

    def _spy(*a, **k):
        p = real_popen(*a, **k)
        made.append(p)
        return p

    setattr_in(monkeypatch, gen_core, "subprocess.Popen", _spy)
    try:
        gen_core._run(_child(code), None, "uv pip install", progress=True)
    except gen_core.GenerationError:
        pass
    assert made and made[0].stdout.closed and made[0].stderr.closed


def test_a_popen_failure_with_progress_is_a_generation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise PermissionError(13, "Access is denied")

    setattr_in(monkeypatch, gen_core, "subprocess.Popen", _boom)
    with pytest.raises(gen_core.GenerationError, match="could not run"):
        gen_core._run(_child("pass"), None, "uv pip install", progress=True)


# ── every inner call of a build carries the request ──────────────────────────


@pytest.fixture
def spy_run(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[list[str], bool]] = []
    real = gen_core._run

    def _spy(argv, run, what, *, progress=False):
        seen.append((list(argv), progress))
        return real(argv, run, what, progress=progress)

    monkeypatch.setattr(gen_core, "_run", _spy)
    monkeypatch.delenv("NX_INSTALL_UNPINNED", raising=False)
    return seen


def _builds(seen):
    return [(argv, p) for argv, p in seen if argv[:2] == ["uv", "venv"] or argv[:3] == ["uv", "pip", "install"]]


def test_build_generation_hands_progress_to_every_uv_call(spy_run, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    gen_core.build_generation(
        "conexus", tools=tools, run=FakeUv(), platform=WIN, now=lambda: 1_790_000_000.0, progress=True,
    )
    calls = _builds(spy_run)
    # venv, the --no-deps fetch for the shipped constraints, the pinned install
    assert len(calls) == 3, calls
    assert all(p is True for _argv, p in calls), calls


def test_build_generation_stays_quiet_without_the_request(spy_run, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    gen_core.build_generation("conexus", tools=tools, run=FakeUv(), platform=WIN, now=lambda: 1_790_000_000.0)
    assert [p for _argv, p in _builds(spy_run)] == [False, False, False]


def test_an_explicit_constraints_file_path_hands_progress_to_its_calls_too(spy_run, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    cons = tmp_path / "c.txt"
    cons.write_text("mcp==1.2.3\n")
    gen_core.build_generation(
        "conexus", tools=tools, run=FakeUv(), platform=WIN, constraints=str(cons),
        now=lambda: 1_790_000_000.0, progress=True,
    )
    calls = _builds(spy_run)
    assert len(calls) == 2 and all(p is True for _a, p in calls), calls


def test_migrate_legacy_hands_progress_to_the_build(spy_run, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    legacy = tmp_path / "uvtools" / "conexus"
    (legacy / "Scripts").mkdir(parents=True)
    (legacy / "Scripts" / "nx.exe").write_bytes(b"uv's launcher")
    (legacy / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "conexus", extras = ["local"] }]\n')
    store = gen_core.FileUserPath(tmp_path / "userpath.txt")
    store.write("C:\\Windows;C:\\uvbin", 2)
    gen_core.migrate_legacy(
        "conexus", legacy_venv=legacy, tools=tools, run=FakeUv(), ops=SymlinkOps(), platform=WIN,
        store=store, uv_bin="C:\\uvbin", environ={}, progress=True,
    )
    calls = _builds(spy_run)
    assert len(calls) == 3 and all(p is True for _a, p in calls), calls


# ── the callers a person waits on ask for it ─────────────────────────────────


def test_nx_self_install_asks_for_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """install_cmd serves install.ps1's bootstrap, the setup tool's build and a
    person at a prompt; none of them should sit through a silent Windows build."""
    from click.testing import CliRunner

    seen: list[dict] = []
    monkeypatch.setattr(self_cmd, "perform_self_install", lambda **kw: seen.append(kw))
    result = CliRunner().invoke(self_cmd.install_cmd, [])
    assert result.exit_code == 0, result.output
    assert seen and seen[0]["progress"] is True


def test_perform_self_install_forwards_progress_into_the_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(self_cmd, "_bootstrap_install", lambda *a, **kw: seen.append(kw))
    self_cmd.perform_self_install(bootstrap=True, progress=True)
    assert seen and seen[0]["progress"] is True


def test_the_windows_legacy_convergence_asks_for_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    core = self_cmd._generation()
    seen: list[dict] = []

    def _fake(source, **kw):
        seen.append(kw)
        return tmp_path / "gen-x"

    monkeypatch.setattr(core, "migrate_legacy", _fake)
    monkeypatch.setattr(self_cmd, "_plat", lambda: "win32")
    monkeypatch.setattr(self_cmd, "_retarget_autostart_task", lambda generation: None)
    self_cmd._windows_converge_legacy_install(tmp_path, version=None, dry_run=False)
    assert seen and seen[0]["progress"] is True


def _legacy_converge_seen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, gap) -> list[dict]:
    from nexus import pdf_stack

    core = self_cmd._generation()
    seen: list[dict] = []

    def _fake(source, *, without_pdf=False, **kw):
        seen.append({"without_pdf": without_pdf, **kw})
        return tmp_path / "gen-x"

    monkeypatch.setattr(core, "migrate_legacy", _fake)
    monkeypatch.setattr(self_cmd, "_plat", lambda: "win32")
    monkeypatch.setattr(self_cmd, "_retarget_autostart_task", lambda generation: None)
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: gap)
    self_cmd._windows_converge_legacy_install(tmp_path, version=None, dry_run=False)
    return seen


def test_the_windows_legacy_convergence_leaves_pdf_out_and_says_so_on_a_runtime_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A legacy tree predates the PDF split, so migrate_legacy adds pdf; on a machine that cannot
    load the stack it is told not to, and one line says so (nexus-vyy65)."""
    seen = _legacy_converge_seen(monkeypatch, tmp_path, gap=("none", None))
    assert len(seen) == 1 and seen[0]["without_pdf"] is True
    out = capsys.readouterr().out
    assert out.count("Built without PDF support") == 1 and "aka.ms/vs/17" in out


def test_the_windows_legacy_convergence_adds_pdf_as_before_without_a_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _legacy_converge_seen(monkeypatch, tmp_path, gap=None)
    assert len(seen) == 1 and seen[0]["without_pdf"] is False
    assert "PDF support" not in capsys.readouterr().out


@pytest.mark.parametrize("progress", [True, False])
def test_a_failed_streamed_build_repeats_only_the_tail_of_uv_output(progress: bool, monkeypatch, tmp_path) -> None:
    """A streamed build already showed uv's output on stderr, so its failure message repeats only
    the last lines; a captured build shows all of it (nexus-2k66t.4.7 confirmation pass)."""
    import subprocess

    import click

    from nexus.commands import self_cmd

    lines = [f"uv line {i}" for i in range(40)]
    monkeypatch.setattr(
        self_cmd, "_run_build",
        lambda argv, *, progress: subprocess.CompletedProcess(argv, 1, "", "\n".join(lines)),
    )
    with pytest.raises(click.ClickException) as ei:
        self_cmd._build_flip_shims(
            ["bash", "install_generation.sh"], install_dir=tmp_path, tools=tmp_path, bin_dir=tmp_path,
            progress=progress,
        )
    message = ei.value.message
    assert "uv line 39" in message
    assert ("uv line 0\n" in message) is (not progress), message
