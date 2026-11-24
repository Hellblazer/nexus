# SPDX-License-Identifier: AGPL-3.0-or-later
"""Both generation builders write the lease, in order, before the receipt.

``install_generation.sh`` (POSIX) and ``generation_core.build_generation`` (the
Windows twin, injected-platform here) each call ``lease_core`` to write the hook
module, the ``.pth``, then ``.nx-lease``, and then verify with a real child. The
staged installer (``target_installer``) must carry the two new files, or the
build silently falls back to the old installer and produces an unleased tree.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from nexus import target_installer
from nexus._install import generation_core as gen_core
from nexus._install import lease_core
from tests._lease_support import NT, make_generation, venv_python
from tests._module_seam import setattr_in

INSTALL = Path(lease_core.__file__).resolve().parent
HOOK_SRC = INSTALL / "_nx_lease.py"


# ── lease_core.install_lease ─────────────────────────────────────────────────

def test_the_three_files_are_written_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gen = tmp_path / "gen"
    venv.EnvBuilder(with_pip=False, symlinks=not NT).create(gen)
    order: list[str] = []
    real = lease_core._write_atomic

    def recording(path: Path, data: bytes) -> None:
        order.append(path.name)
        real(path, data)

    monkeypatch.setattr(lease_core, "_write_atomic", recording)
    lease_core.install_lease(gen)
    assert order == ["_nx_lease.py", "_nx_lease.pth", ".nx-lease"], order


def test_a_failure_before_the_lease_file_leaves_the_generation_unleased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lease file is what gc keys on and it goes last: a build killed after the
    hook or the .pth is an unleased tree, never a leased one with no hook."""
    gen = tmp_path / "gen"
    venv.EnvBuilder(with_pip=False, symlinks=not NT).create(gen)
    real = lease_core._write_atomic

    def dies_on_pth(path: Path, data: bytes) -> None:
        if path.name == "_nx_lease.pth":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(lease_core, "_write_atomic", dies_on_pth)
    with pytest.raises(OSError):
        lease_core.install_lease(gen)
    assert lease_core.probe(gen) == "absent"


def test_the_lease_file_names_its_version_on_the_first_line(tmp_path: Path) -> None:
    gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)
    assert lease_core.lease_path(gen).read_text().splitlines()[0] == "nx-lease 1"


def test_the_installed_hook_is_the_shipped_hook_byte_for_byte(tmp_path: Path) -> None:
    gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)
    assert (lease_core.site_packages(gen) / "_nx_lease.py").read_bytes() == HOOK_SRC.read_bytes()


def test_a_generation_with_no_site_packages_is_reported_not_leased(tmp_path: Path) -> None:
    gen = tmp_path / "gen"
    gen.mkdir()
    lines = lease_core.install_and_verify(gen)
    assert any("WARNING" in line and "site-packages" in line for line in lines), lines
    assert lease_core.probe(gen) == "absent"


def test_the_self_test_reads_held_for_a_leased_generation(tmp_path: Path) -> None:
    gen = make_generation(tmp_path, "20261101T000000Z", receipt=False)
    assert lease_core.self_test(gen) == "held"


def test_the_self_test_warns_when_the_hook_is_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken hook is invisible by design (silent, and every lease reads free), so
    the builder's own check is the only place it can be seen."""
    broken = tmp_path / "broken_hook.py"
    broken.write_text("raise RuntimeError('x')\n")
    monkeypatch.setattr(lease_core, "hook_source", lambda: broken)
    gen = tmp_path / "gen"
    venv.EnvBuilder(with_pip=False, symlinks=not NT).create(gen)
    lines = lease_core.install_and_verify(gen)
    assert any("WARNING" in line and "free" in line for line in lines), lines
    assert not any("lease: verified" in line for line in lines), lines


def test_a_good_build_says_the_lease_is_verified(tmp_path: Path) -> None:
    gen = tmp_path / "gen"
    venv.EnvBuilder(with_pip=False, symlinks=not NT).create(gen)
    lines = lease_core.install_and_verify(gen)
    assert any("lease: verified" in line for line in lines), lines
    assert not any("WARNING" in line for line in lines), lines


def test_a_real_uv_generation_leases_from_its_own_interpreter(tmp_path: Path) -> None:
    """The .pth on a venv uv built, not one the test made: it survives, stays
    silent and takes the lease."""
    uv = shutil.which("uv")
    assert uv, "uv is the project's installer and must be on PATH here"
    gen = tmp_path / "gen-real"
    subprocess.run(
        [uv, "venv", "--allow-existing", "--python", sys.executable, str(gen)],
        check=True, capture_output=True, timeout=120,
    )
    lines = lease_core.install_and_verify(gen)
    assert any("lease: verified" in line for line in lines), lines
    r = subprocess.run([str(venv_python(gen)), "-c", "pass"], capture_output=True, text=True, check=False)
    assert (r.returncode, r.stderr) == (0, "")


# ── the POSIX builder ────────────────────────────────────────────────────────

SH = INSTALL / "install_generation.sh"


def _fake_uv(bindir: Path) -> None:
    """``uv venv`` makes a real venv; ``uv pip install`` does nothing (no network)."""
    bindir.mkdir()
    script = bindir / "uv"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then\n'
        '  for last; do :; done\n'
        f'  exec "{sys.executable}" -m venv --without-pip "$last"\n'
        "fi\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    py3 = bindir / "python3"
    if not py3.exists():
        py3.symlink_to(sys.executable)


@pytest.mark.skipif(NT, reason="the POSIX builder is bash")
def test_the_shell_builder_writes_the_lease_before_the_receipt(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _fake_uv(bindir)
    tools = tmp_path / "tools"
    tools.mkdir()
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path),
        "NX_TOOLS_DIR": str(tools),
    }
    r = subprocess.run(
        ["bash", str(SH), "--source", "conexus", "--version", "1.0.0"],
        capture_output=True, text=True, env=env, check=False, timeout=180,
    )
    assert r.returncode == 0, r.stderr
    gen = Path(r.stdout.strip())
    assert gen.parent == tools and (gen / "nexus-install.json").is_file()
    site = lease_core.site_packages(gen)
    assert (site / "_nx_lease.py").read_bytes() == HOOK_SRC.read_bytes()
    assert (site / "_nx_lease.pth").is_file() and lease_core.lease_path(gen).is_file()
    assert "lease: verified" in r.stderr, r.stderr
    assert lease_core.probe(gen) == "free"


def test_the_shell_builder_installs_the_lease_before_it_renders_the_receipt() -> None:
    text = SH.read_text()
    lease = text.index("lease_core.py")
    assert lease < text.index("nx_render_receipt"), "the lease must be written before the receipt"
    assert lease > text.rindex('uv pip install --python "$GEN/bin/python"'), "after the packages, so the hook sees a finished site-packages"


def test_every_file_the_shell_builder_reads_beside_itself_is_staged() -> None:
    needed = set(re.findall(r'\$_here/([A-Za-z0-9_.]+)', SH.read_text()))
    needed |= {"layout.sh", "layout_core.py", "_nx_lease.py"}  # sourced, dispatched, and read by lease_core
    assert "lease_core.py" in needed, "the builder no longer calls lease_core"
    missing = {n for n in needed if n not in target_installer.POSIX_FILES}
    assert not missing, f"install_generation.sh needs {sorted(missing)} but POSIX_FILES does not stage them"


# ── the staged installer lists the new files ─────────────────────────────────

@pytest.mark.parametrize("files", ["POSIX_FILES", "WINDOWS_FILES"])
def test_the_staged_installer_carries_the_lease_files(files: str) -> None:
    listed = getattr(target_installer, files)
    for name in ("lease_core.py", "_nx_lease.py"):
        assert name in listed, f"{name} missing from target_installer.{files}"
        assert (INSTALL / name).is_file()


# ── the Windows builder, injected-platform ───────────────────────────────────

def _fake_run(argv, **_kw):
    argv = ["uv", *argv[1:]] if Path(argv[0]).stem.lower() == "uv" else argv  # a Windows host resolves the head
    if argv[:2] == ["uv", "venv"]:
        gen = Path(argv[-1])
        (gen / "Scripts").mkdir(parents=True, exist_ok=True)
        (gen / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        (gen / "pyvenv.cfg").write_text("home = C:\\py\nversion = 3.12.8\n")
    elif argv[:3] == ["uv", "pip", "install"]:
        python = Path(argv[argv.index("--python") + 1])
        python.write_text("#!/bin/sh\nprintf 'nx\\n'\n")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        (python.parent / "nx.exe").write_bytes(b"x")
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.mark.skipif(NT, reason="the injected interpreter is a #!/bin/sh stub; the native build is test_install_generation_real_windows")
def test_the_python_builder_writes_the_lease_before_the_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    events: list[str] = []
    real_write = lease_core._write_atomic
    monkeypatch.setattr(lease_core, "_write_atomic", lambda p, d: (events.append(p.name), real_write(p, d)))
    real_replace = os.replace
    setattr_in(
        monkeypatch, gen_core, "os.replace",
        lambda s, d, *a, **k: (events.append("receipt") if Path(d).name == "nexus-install.json" else None, real_replace(s, d, *a, **k))[1],
    )
    gen = gen_core.build_generation(
        "conexus", version="1.0.0", tools=tools, run=_fake_run, platform="win32", now=lambda: 1_790_000_000.0,
    )
    assert events == ["_nx_lease.py", "_nx_lease.pth", ".nx-lease", "receipt"], events
    assert (lease_core.site_packages(gen) / "_nx_lease.py").read_bytes() == HOOK_SRC.read_bytes()


def test_a_build_killed_before_the_receipt_leaves_no_tree(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()

    def dies(argv, **kw):
        if argv[:3] == ["uv", "pip", "install"]:
            raise OSError("killed")
        return _fake_run(argv, **kw)

    with pytest.raises(gen_core.GenerationError):
        gen_core.build_generation("conexus", version="1.0.0", tools=tools, run=dies, platform="win32")
    assert list(tools.iterdir()) == []


@pytest.mark.skipif(NT, reason="the POSIX builder is bash")
def test_a_staged_installer_without_lease_core_still_builds_and_says_it_is_unleased(tmp_path: Path) -> None:
    """The first upgrade through an older running nexus stages the installer with
    ITS file list, which does not carry lease_core.py: the build must not fail."""
    stage = tmp_path / "stage"
    stage.mkdir()
    for name in ("install_generation.sh", "layout.sh", "layout_core.py", "overrides.txt"):
        shutil.copy(INSTALL / name, stage / name)
    bindir = tmp_path / "bin"
    _fake_uv(bindir)
    (tmp_path / "tools").mkdir()
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path),
        "NX_TOOLS_DIR": str(tmp_path / "tools"),
    }
    r = subprocess.run(
        ["bash", str(stage / "install_generation.sh"), "--source", "conexus", "--version", "1.0.0"],
        capture_output=True, text=True, env=env, check=False, timeout=180,
    )
    assert r.returncode == 0, r.stderr
    gen = Path(r.stdout.strip())
    assert (gen / "nexus-install.json").is_file()
    assert "is not leased" in r.stderr, r.stderr
    assert lease_core.probe(gen) == "absent"


@pytest.mark.skipif(NT, reason="the injected interpreter is a #!/bin/sh stub")
def test_the_python_builder_survives_a_missing_lease_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def gone():
        raise ImportError("cannot load lease_core.py")

    monkeypatch.setattr(gen_core, "_lease", gone)
    tools = tmp_path / "tools"
    tools.mkdir()
    gen = gen_core.build_generation(
        "conexus", version="1.0.0", tools=tools, run=_fake_run, platform="win32", now=lambda: 1_790_000_000.0,
    )
    assert (gen / "nexus-install.json").is_file()
    assert "is not leased" in capsys.readouterr().err
