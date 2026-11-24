# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generation's own half of the lease: ``_nx_lease.py`` and its ``.pth``.

Real venvs and real child interpreters throughout. The hook runs inside EVERY
interpreter the generation starts, so the bar is what it must never do: print,
raise, slow the start, or lose its lease to the garbage collector.
"""
from __future__ import annotations

import ast
import errno
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus._install import lease_core
from tests._lease_support import NT, holder, make_generation, venv_python
from tests._module_seam import setattr_in

HOOK = Path(lease_core.__file__).with_name("_nx_lease.py")
POSIX = pytest.mark.skipif(NT, reason="symlinks, execv and flock")


def _site(gen: Path) -> Path:
    found = lease_core.site_packages(gen)
    assert found is not None, f"no site-packages under {gen}"
    return found


def _run(gen: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(venv_python(gen)), *args], capture_output=True, text=True, timeout=60, check=False,
    )


@pytest.fixture
def gen(tmp_path: Path) -> Path:
    return make_generation(tmp_path, "20261101T000000Z")


# ── holding ──────────────────────────────────────────────────────────────────

def test_a_running_interpreter_holds_the_lease_and_its_exit_frees_it(gen: Path) -> None:
    assert lease_core.probe(gen) == "free"
    with holder(venv_python(gen)):
        assert lease_core.probe(gen) == "held"
    assert lease_core.probe(gen) == "free"


def test_the_lease_survives_a_garbage_collection(gen: Path) -> None:
    """A file object in a function local would be closed by the collector and the
    lease silently dropped; the hook keeps a raw descriptor in a module global."""
    code = (
        "import gc, sys; gc.collect(); gc.collect(); "
        "sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
    )
    proc = subprocess.Popen(
        [str(venv_python(gen)), "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert lease_core.probe(gen) == "held"
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


@POSIX
def test_started_through_a_symlink_to_the_generation_it_still_leases_the_real_one(tmp_path: Path, gen: Path) -> None:
    """sys.prefix is the unresolved ``current`` link; the hook resolves it."""
    link = tmp_path / "current"
    link.symlink_to(gen, target_is_directory=True)
    with holder(link / "bin" / "python"):
        assert lease_core.probe(gen) == "held"


@POSIX
def test_a_process_that_execs_the_interpreter_again_is_leased_again(gen: Path) -> None:
    """The descriptor is close-on-exec: the lease drops at the exec and the new
    image's hook takes it again."""
    py = str(venv_python(gen))
    code = (
        "import os, sys; os.execv(sys.executable, [sys.executable, '-c', "
        "\"import sys; sys.stdout.write('ready\\\\n'); sys.stdout.flush(); sys.stdin.read()\"])"
    )
    proc = subprocess.Popen([py, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert lease_core.probe(gen) == "held"
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


def test_dash_I_keeps_the_lease_and_dash_S_does_not(gen: Path) -> None:
    """``-I`` still processes ``.pth``; ``-S`` skips site entirely. Nothing in the
    tree runs a generation interpreter with ``-S`` (pinned below), so this is the
    documented hole, not a path."""
    with holder(venv_python(gen), extra=("-I",)):
        assert lease_core.probe(gen) == "held"
    with holder(venv_python(gen), extra=("-S",)):
        assert lease_core.probe(gen) == "free"


def test_no_generation_interpreter_in_the_tree_runs_with_dash_S() -> None:
    """The documented hole stays closed: no subprocess argv in src/ starts a
    generation's interpreter with -S (a CI script's runner python is not one)."""
    src = Path(lease_core.__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                flags = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if "-S" in flags and any(f in ("-c", "-m") for f in flags):
                    offenders.append(f"{path.relative_to(src)}:{node.lineno}: {flags}")
    assert not offenders, offenders


# ── silence ──────────────────────────────────────────────────────────────────

def _assert_silent(gen: Path) -> None:
    r = _run(gen, "-c", "pass")
    assert (r.returncode, r.stderr, r.stdout) == (0, "", ""), r


def test_a_leased_generation_starts_silently(gen: Path) -> None:
    _assert_silent(gen)


def test_a_missing_hook_module_is_silent(gen: Path) -> None:
    (_site(gen) / "_nx_lease.py").unlink()
    assert (_site(gen) / "_nx_lease.pth").is_file()
    _assert_silent(gen)


def test_a_broken_hook_module_is_silent(gen: Path) -> None:
    (_site(gen) / "_nx_lease.py").write_text("def broken(:\n")
    _assert_silent(gen)
    (_site(gen) / "_nx_lease.py").write_text("raise RuntimeError('boom')\n")
    _assert_silent(gen)


def test_an_unknown_or_empty_lease_file_is_silent_and_takes_nothing(gen: Path) -> None:
    for text in ("", "nx-lease 2\n", "garbage"):
        lease_core.lease_path(gen).write_text(text)
        _assert_silent(gen)
        with holder(venv_python(gen)):
            # The hook took nothing; gc reads the unknown file as an error, which keeps the generation.
            assert lease_core.probe(gen) == "error:version", text


@pytest.mark.skipif(NT or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="root reads any file")
def test_an_unreadable_lease_file_is_silent(gen: Path) -> None:
    lease_core.lease_path(gen).chmod(0)
    try:
        _assert_silent(gen)
    finally:
        lease_core.lease_path(gen).chmod(0o644)


def test_the_lease_is_not_taken_outside_a_leased_generation(tmp_path: Path) -> None:
    plain = make_generation(tmp_path, "20261102T000000Z", leased=False)
    _assert_silent(plain)
    assert not lease_core.lease_path(plain).exists(), "the hook must open O_RDONLY without O_CREAT"


# ── cost and shape ───────────────────────────────────────────────────────────

def test_the_hook_adds_no_module_beyond_itself_and_fcntl(tmp_path: Path, gen: Path) -> None:
    """Startup cost is pinned structurally (a wall-clock bound flakes on a loaded
    host; the measured numbers are on the change's record): the modules a leased
    interpreter has loaded at its first line, against the same interpreter in an
    unleased generation."""
    plain = make_generation(tmp_path, "20261102T000000Z", leased=False)
    code = "import sys; print(sorted(sys.modules))"

    def loaded(g: Path) -> set[str]:
        r = _run(g, "-c", code)
        assert r.returncode == 0, r.stderr
        return set(ast.literal_eval(r.stdout.strip()))

    # encodings.utf_8_sig is site.py reading ANY .pth (setuptools' distutils-precedence.pth
    # already makes every generation pay it); it is not the hook's import.
    assert loaded(gen) - loaded(plain) <= {"_nx_lease", "fcntl", "encodings.utf_8_sig"}


def test_the_hook_imports_only_os_sys_time_and_fcntl() -> None:
    tree = ast.parse(HOOK.read_text())
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            seen.add(node.module or ".")
    assert seen <= {"os", "sys", "time", "fcntl"}, seen


def test_the_pth_is_one_guarded_line_without_a_leading_dot(gen: Path) -> None:
    pth = _site(gen) / "_nx_lease.pth"
    assert not pth.name.startswith("."), "3.13 skips dot-prefixed .pth files"
    text = pth.read_text()
    assert text == lease_core.PTH_TEXT and text.count("\n") == 1
    assert text.startswith("import ") and "except Exception" in text


# ── retry under contention (the hook's own function, driven directly) ────────

def _load_hook(gen: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("_nx_lease_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # takes no lease: the test interpreter's prefix has no .nx-lease
    setattr_in(monkeypatch, mod, "sys.prefix", str(gen))
    return mod


@pytest.mark.skipif(NT, reason="flock")
def test_a_busy_lock_is_retried_briefly_so_a_gc_probe_does_not_leave_it_unleased(
    gen: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_hook(gen, monkeypatch)
    calls: list[int] = []

    def flaky(fd, op):
        calls.append(op)
        if len(calls) <= 3:
            raise BlockingIOError(errno.EWOULDBLOCK, "busy")

    setattr_in(monkeypatch, mod, "fcntl.flock", flaky)
    mod._take()
    try:
        assert mod._FD >= 0 and len(calls) == 4, (mod._FD, calls)
    finally:
        if mod._FD >= 0:
            os.close(mod._FD)


@pytest.mark.skipif(NT, reason="flock")
def test_a_lock_that_stays_busy_gives_up_and_leaks_no_descriptor(gen: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_hook(gen, monkeypatch)

    def always_busy(fd, op):
        raise BlockingIOError(errno.EWOULDBLOCK, "busy")

    setattr_in(monkeypatch, mod, "fcntl.flock", always_busy)
    before = len(os.listdir("/dev/fd"))
    mod._take()
    assert mod._FD == -1
    assert len(os.listdir("/dev/fd")) == before, "the lease file's descriptor leaked"


def test_a_windows_open_refused_by_a_probe_is_retried_and_a_posix_one_is_not(
    gen: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows gc's probe holds the file at share mode 0 for a moment and an
    open that meets it fails with PermissionError; the hook retries as it does for
    the POSIX lock. On POSIX an EACCES is a real refusal and is not retried."""
    mod = _load_hook(gen, monkeypatch)
    real_open = os.open
    calls: list[str] = []

    def refuses_twice(path, flags, *a, **k):
        calls.append(path)
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation")
        return real_open(path, flags, *a, **k)

    setattr_in(monkeypatch, mod, "os.open", refuses_twice)
    setattr_in(monkeypatch, mod, "sys.platform", "win32")
    fd = mod._open(str(lease_core.lease_path(gen)))
    os.close(fd)
    assert len(calls) == 3, calls

    calls.clear()
    setattr_in(monkeypatch, mod, "sys.platform", "linux")
    with pytest.raises(PermissionError):
        mod._open(str(lease_core.lease_path(gen)))
    assert len(calls) == 1, calls
