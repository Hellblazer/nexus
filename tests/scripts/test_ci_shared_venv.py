# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dvk18 (CI F5): scripts/ci_shared_venv.sh against a temp lab cache root.

The script builds a dependencies-only venv once per key under ``$NX_LAB_CACHE_ROOT/nexus/venvs``
and layers a thin per-job venv on it. These tests run the REAL script and the REAL ``uv`` over
a three-package fixture project (a project, a runtime dependency, a dev-group dependency) whose
build backend lives in the package directory, so nothing is downloaded: the fixture's ``uv lock``,
``uv sync`` and editable install all run offline.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script; the pool is Linux")

REPO = Path(__file__).resolve().parents[2]
# NX_CI_SHARED_VENV_SCRIPT points the suite at another copy of the script (red/green comparisons
# against an older revision); unset, it is the repo's.
SCRIPT = Path(os.environ.get("NX_CI_SHARED_VENV_SCRIPT") or REPO / "scripts" / "ci_shared_venv.sh")

# A PEP 517/660 backend small enough to read in one go: wheels hold the package files, the
# console scripts from [project.scripts], and one optional .pth line (to prove that a .pth file
# inside the SHARED site-packages still runs through the thin venv).
_BACKEND = textwrap.dedent(
    '''
    import base64, hashlib, os, tomllib, zipfile

    def _cfg():
        with open("pyproject.toml", "rb") as f:
            return tomllib.load(f)

    def _wheel(wheel_directory, editable):
        cfg = _cfg()
        proj = cfg["project"]
        name, version = proj["name"], proj["version"]
        dist = f"{name}-{version}"
        files = {}
        meta = f"Metadata-Version: 2.1\\nName: {name}\\nVersion: {version}\\n"
        for dep in proj.get("dependencies", []):
            meta += f"Requires-Dist: {dep}\\n"
        files[f"{dist}.dist-info/METADATA"] = meta
        files[f"{dist}.dist-info/WHEEL"] = (
            "Wheel-Version: 1.0\\nGenerator: tinybackend\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
        )
        scripts = proj.get("scripts", {})
        if scripts:
            files[f"{dist}.dist-info/entry_points.txt"] = (
                "[console_scripts]\\n" + "".join(f"{k} = {v}\\n" for k, v in scripts.items())
            )
        pkg = cfg["tool"]["tinybackend"]["package"]
        root = os.path.abspath(os.path.dirname(pkg))
        if editable:
            files[f"{name}.pth"] = os.path.abspath(os.path.dirname(pkg)) + "\\n"
        else:
            for dirpath, _, names in os.walk(pkg):
                if "__pycache__" in dirpath:
                    continue
                for n in names:
                    p = os.path.join(dirpath, n)
                    with open(p) as f:
                        files[p.replace(os.sep, "/")] = f.read()
        pth = cfg["tool"]["tinybackend"].get("pth")
        if pth:
            files[f"{name}_marker.pth"] = pth + "\\n"
        record = []
        for fname, text in files.items():
            digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
            record.append(f"{fname},sha256={digest},{len(text.encode())}")
        record.append(f"{dist}.dist-info/RECORD,,")
        files[f"{dist}.dist-info/RECORD"] = "\\n".join(record) + "\\n"
        out = f"{dist}-py3-none-any.whl"
        with zipfile.ZipFile(os.path.join(wheel_directory, out), "w") as z:
            for fname, text in files.items():
                z.writestr(fname, text)
        return out

    def get_requires_for_build_wheel(config_settings=None): return []
    def get_requires_for_build_editable(config_settings=None): return []
    def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
        return _wheel(wheel_directory, False)
    def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
        return _wheel(wheel_directory, True)
    '''
)

_BUILD_SYSTEM = '[build-system]\nrequires = []\nbuild-backend = "tinybackend"\nbackend-path = ["."]\n'


def _write_package(root: Path, name: str, *, deps: str = "", scripts: str = "", extra: str = "", pth: str = "",
                   body: str = "") -> None:
    pkg = root / name
    (pkg / name).mkdir(parents=True)
    (pkg / "tinybackend.py").write_text(_BACKEND, encoding="utf-8")
    (pkg / name / "__init__.py").write_text(
        body or f"def main():\n    print('{name} ok')\n", encoding="utf-8"
    )
    tool = f'[tool.tinybackend]\npackage = "{name}"\n' + (f"pth = {pth!r}\n" if pth else "")
    (pkg / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        f"dependencies = [{deps}]\n{scripts}\n{_BUILD_SYSTEM}\n{tool}\n{extra}",
        encoding="utf-8",
    )


@pytest.fixture(scope="session")
def project_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A locked fixture project: fxproj -> fxdep (runtime), dev group -> fxdev. Offline."""
    base = tmp_path_factory.mktemp("f5-template")
    _write_package(base, "fxdep", scripts='[project.scripts]\nfxdep-tool = "fxdep:main"\n',
                   pth="import os; os.environ['FXDEP_PTH'] = '1'")
    _write_package(base, "fxdev", scripts='[project.scripts]\nfxdev-tool = "fxdev:main"\n')
    _write_package(
        base, "fxproj", deps='"fxdep"', scripts='[project.scripts]\nfxproj = "fxproj:main"\n',
        extra='[dependency-groups]\ndev = ["fxdev"]\n\n[tool.uv.sources]\n'
              'fxdep = { path = "../fxdep" }\nfxdev = { path = "../fxdev" }\n',
    )
    env = _uv_env(base / "uv-cache")
    subprocess.run(["uv", "lock", "--offline"], cwd=base / "fxproj", env=env, check=True,
                   capture_output=True, text=True)
    assert (base / "fxproj" / "uv.lock").is_file()
    return base


def _uv_env(cache: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("UV_", "VIRTUAL_ENV", "GITHUB_", "NX_", "RUNNER_", "PYTHONPYCACHEPREFIX"))}
    env.update(UV_CACHE_DIR=str(cache), UV_OFFLINE="1", UV_PYTHON=sys._base_executable,
               UV_PYTHON_DOWNLOADS="never", NX_NO_TELEMETRY="1")
    return env


def _clean_env() -> dict[str, str]:
    """The inherited environment minus everything uv reads (UV_*, VIRTUAL_ENV): a suite run through
    the thin venv itself (UV_PROJECT_ENVIRONMENT=<thin>, UV_NO_SYNC=1) must not change what any
    subprocess here sees. Each test then sets only what it means to."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "VIRTUAL_ENV"))}


class Job:
    """One CI job's view: a project checkout, a workspace, GITHUB_ENV/OUTPUT files, a lab root."""

    def __init__(self, tmp: Path, template: Path, name: str, root: Path | None) -> None:
        self.dir = tmp / name
        shutil.copytree(template, self.dir, ignore=shutil.ignore_patterns("uv-cache", ".venv", "__pycache__"))
        self.project = self.dir / "fxproj"
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()
        self.disk = self.dir / "labdisk"          # labrun F1's LAB_DISK_DIR
        self.disk.mkdir()
        self.github_env = self.dir / "github_env"
        self.github_output = self.dir / "github_output"
        self.github_env.touch()
        self.github_output.touch()
        self.env = _uv_env(tmp / "uv-cache")
        self.env.update(RUNNER_WORKSPACE=str(self.workspace), GITHUB_ENV=str(self.github_env),
                        GITHUB_OUTPUT=str(self.github_output))
        if root is not None:
            self.env["NX_LAB_CACHE_ROOT"] = str(root)
        # F5 is for tmpfs-workspace jobs only (labrun F1)
        self.env.update(LAB_WS="tmpfs", LAB_DISK_DIR=str(self.disk))
        # waiting/heartbeat knobs: a quick poll, a 1 s heartbeat, and a dead-after that a loaded test
        # host cannot reach by accident (planted dead locks are an hour old)
        self.env.update(NX_SHARED_VENV_WAIT="0", NX_SHARED_VENV_HEARTBEAT="1", NX_SHARED_VENV_DEAD="20",
                        NX_SHARED_VENV_POLL="1")

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=self.project, env=env or self.env,
                              capture_output=True, text=True, timeout=300)

    @property
    def outputs(self) -> dict[str, str]:
        """GITHUB_OUTPUT as written by the script. GITHUB_ENV must stay empty: see ``env_untouched``."""
        return dict(line.split("=", 1) for line in self.github_output.read_text().splitlines() if "=" in line)

    @property
    def used(self) -> bool:
        return self.outputs["used"] == "true"

    def env_untouched(self) -> bool:
        return self.github_env.read_text() == ""

    @property
    def thin(self) -> Path:
        return self.workspace / "nx-thin-venv"


@pytest.fixture
def lab_root(tmp_path: Path):  # noqa: ANN201
    root = tmp_path / "lab-cache"
    root.mkdir()
    yield root
    # published entries are read-only by design; pytest's own tmp cleanup cannot delete them
    subprocess.run(["chmod", "-R", "u+w", str(root)], check=False, env=_clean_env())


@pytest.fixture
def job(tmp_path: Path, project_template: Path, lab_root: Path) -> Job:
    return Job(tmp_path, project_template, "job0", lab_root)


def _venvs(root: Path) -> Path:
    return root / "nexus" / "venvs"


def entries(root: Path) -> list[Path]:
    """Published entries: every directory whose name is not a script-private dot name."""
    venvs = _venvs(root)
    return sorted(p for p in venvs.iterdir() if not p.name.startswith(".")) if venvs.is_dir() else []


def _dots(root: Path, prefix: str) -> list[Path]:
    venvs = _venvs(root)
    return sorted(p for p in venvs.iterdir() if p.name.startswith(prefix)) if venvs.is_dir() else []


def tmp_dirs(root: Path) -> list[Path]:
    return _dots(root, ".tmp.")


def lock_dirs(root: Path) -> list[Path]:
    return _dots(root, ".lock.")


def tree_state(path: Path) -> dict[str, tuple[int, int, int]]:
    """(mode, size, mtime_ns) of everything under ``path``, links not followed."""
    out = {}
    for p in sorted(path.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(path))] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def writable_bits(path: Path) -> bool:
    return bool(path.lstat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_cold_build_publishes_a_complete_read_only_entry(job: Job, lab_root: Path) -> None:
    res = job.run("--group", "dev", "--smoke-import", "fxproj")
    assert res.returncode == 0, res.stdout + res.stderr
    [entry] = entries(lab_root)
    assert (entry / "COMPLETE").read_text().strip() == entry.name
    assert len(entry.name) == 32
    assert (entry / "last-used").is_file()
    assert tmp_dirs(lab_root) == []
    # the dependency tree is read-only: no file or directory under venv/ carries a write bit
    writable = [str(p) for p in [entry / "venv", *(entry / "venv").rglob("*")]
                if not p.is_symlink() and writable_bits(p)]
    assert writable == []
    # dev group included (fxdev), runtime dep included (fxdep), the project itself NOT (--no-install-project)
    site = next((entry / "venv" / "lib").glob("python3.*/site-packages"))
    assert (site / "fxdep").is_dir() and (site / "fxdev").is_dir()
    assert not (site / "fxproj").exists()
    # precompiled at build: the pyc is there before any job runs
    assert list((site / "fxdep").rglob("*.pyc"))
    assert job.used
    assert job.outputs == {"used": "true", "venv": str(job.thin), "key": entry.name}
    # a job-wide UV_PROJECT_ENVIRONMENT would pull other projects' `uv run` into this venv
    assert job.env_untouched(), "the script wrote GITHUB_ENV"


def test_warm_reuse_writes_only_last_used(job: Job, lab_root: Path, tmp_path: Path, project_template: Path) -> None:
    assert job.run().returncode == 0
    [entry] = entries(lab_root)
    before = tree_state(entry)
    before.pop("last-used")
    second = Job(tmp_path, project_template, "job1", lab_root)
    res = second.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "reusing the published entry" in res.stdout
    assert "building shared venv" not in res.stdout
    after = tree_state(entry)
    assert after.pop("last-used")  # touched, still there
    assert after == before, "a warm reuse changed something besides last-used"
    assert tmp_dirs(lab_root) == []
    assert second.outputs["key"] == entry.name


def test_thin_venv_imports_dependencies_from_shared_and_the_project_from_the_checkout(job: Job, lab_root: Path) -> None:
    assert job.run().returncode == 0
    [entry] = entries(lab_root)
    py = job.thin / "bin" / "python"
    out = subprocess.run(
        [str(py), "-c",
         "import os, fxdep, fxdev, fxproj, importlib.metadata as m;"
         "print(fxdep.__file__); print(fxdev.__file__); print(fxproj.__file__);"
         "print(os.environ.get('FXDEP_PTH')); print(m.version('fxproj'), m.version('fxdep'))"],
        capture_output=True, text=True, env={**job.env, "PYTHONPATH": ""}, check=True,
    ).stdout.split("\n")
    assert out[0].startswith(str(entry / "venv")), out
    assert out[1].startswith(str(entry / "venv")), out
    assert out[2].startswith(str(job.project)), out          # editable: the checkout's source
    assert out[3] == "1", "a .pth file inside the shared site-packages did not run"
    assert out[4] == "0.1.0 0.1.0"
    # the thin venv itself holds no dependency copy
    thin_site = next((job.thin / "lib").glob("python3.*/site-packages"))
    assert not (thin_site / "fxdep").exists()


def test_console_scripts_work_from_the_thin_venv(job: Job, lab_root: Path) -> None:
    assert job.run().returncode == 0
    for tool, text in (("fxproj", "fxproj ok"), ("fxdep-tool", "fxdep ok"), ("fxdev-tool", "fxdev ok")):
        res = subprocess.run([str(job.thin / "bin" / tool)], capture_output=True, text=True, env=job.env)
        assert res.returncode == 0 and res.stdout.strip() == text, (tool, res.stdout, res.stderr)
    # no script carries the dead build path of the renamed tmp dir
    for script in (job.thin / "bin").iterdir():
        if script.is_file() and not script.is_symlink():
            assert b".tmp." not in script.read_bytes(), script


def test_uv_run_in_the_thin_venv_does_not_install_anything(job: Job, lab_root: Path) -> None:
    """The CI shape: UV_PROJECT_ENVIRONMENT=<thin> + UV_NO_SYNC=1, then `uv run <console script>`."""
    assert job.run().returncode == 0
    env = {**job.env, "UV_PROJECT_ENVIRONMENT": job.outputs["venv"], "UV_NO_SYNC": "1"}
    env.pop("UV_OFFLINE")  # a sync attempt would need the network; no sync means none is needed
    before = tree_state(job.thin)
    res = subprocess.run(["uv", "run", "fxdep-tool"], cwd=job.project, env=env, capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout.strip() == "fxdep ok", (res.stdout, res.stderr)
    res = subprocess.run(["uv", "run", "python", "-c", "import fxdev"], cwd=job.project, env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    after = tree_state(job.thin)
    def stable(t: dict) -> dict:
        return {k: v for k, v in t.items() if "__pycache__" not in k}

    assert stable(after) == stable(before), "uv run changed the thin venv"


def _build_count(outs: list[str]) -> int:
    return sum(len(re.findall(r"(?m)^building shared venv ", o)) for o in outs)


def _spawn(job: "Job", env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(["bash", str(SCRIPT)], cwd=job.project, env=env or job.env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def _prime(tmp_path: Path, template: Path, lab_root: Path) -> tuple[str, Path]:
    """Build the fixture's entry once, then move it out of sight: returns (key, the hidden entry)."""
    probe = Job(tmp_path, template, "probe", lab_root)
    res = probe.run()
    assert res.returncode == 0 and probe.used, res.stdout + res.stderr
    [entry] = entries(lab_root)
    hidden = lab_root / "nexus" / "hidden"
    entry.rename(hidden)
    return entry.name, hidden


def _plant_lock(lab_root: Path, key: str, *, age_s: float, owner: str = "ghost.1.1", tmp: Path | None = None) -> Path:
    lock = _venvs(lab_root) / f".lock.{key}"
    lock.mkdir()
    (lock / "owner").write_text(owner + "\n")
    hb = lock / "heartbeat"
    hb.touch()
    if tmp is not None:
        (lock / "tmp").write_text(str(tmp) + "\n")
    then = time.time() - age_s
    os.utime(hb, (then, then))
    os.utime(lock, (then, then))
    return lock


class Beat(threading.Thread):
    """A live lock holder's heartbeat: refreshes ``<lock>/heartbeat`` until stopped."""

    def __init__(self, lock: Path) -> None:
        super().__init__(daemon=True)
        self.lock = lock
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                (self.lock / "heartbeat").touch()
            except OSError:
                pass
            self._stop_evt.wait(0.3)

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(5)


def test_concurrent_jobs_result_in_exactly_one_build(tmp_path: Path, project_template: Path, lab_root: Path) -> None:
    jobs = [Job(tmp_path, project_template, f"c{i}", lab_root) for i in range(4)]
    for j in jobs:
        j.env["NX_SHARED_VENV_WAIT"] = "300"
    procs = [_spawn(j) for j in jobs]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], outs
    [entry] = entries(lab_root)
    assert tmp_dirs(lab_root) == [] and lock_dirs(lab_root) == [], "a job left partial state behind"
    for j in jobs:
        assert j.used and j.outputs["key"] == entry.name
        res = subprocess.run([str(j.thin / "bin" / "fxdep-tool")], capture_output=True, text=True, env=_clean_env())
        assert res.stdout.strip() == "fxdep ok"
    # ONE build, not one winner among several builders. The message is a joined string, not
    # the list: pytest truncates a list repr, which hid the second build's job (nexus-6xlce).
    full = "\n".join(f"--- job c{i} ---\n{o}" for i, o in enumerate(outs))
    assert _build_count(outs) == 1, full
    assert sum("published shared venv" in o for o in outs) == 1, full


def test_waiters_retake_the_lock_when_the_holder_never_publishes(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """A holder that fails without publishing frees the lock: three waiters must not all build."""
    key, hidden = _prime(tmp_path, project_template, lab_root)
    lock = _plant_lock(lab_root, key, age_s=0)
    beat = Beat(lock)
    beat.start()
    jobs = [Job(tmp_path, project_template, f"w{i}", lab_root) for i in range(3)]
    for j in jobs:
        j.env["NX_SHARED_VENV_WAIT"] = "300"
    procs = [_spawn(j) for j in jobs]
    time.sleep(3)
    assert all(p.poll() is None for p in procs), "a waiter did not wait for the live lock"
    beat.stop()
    shutil.rmtree(lock)                      # the holder failed and released
    outs = [p.communicate(timeout=300)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    assert all(j.used for j in jobs)
    assert _build_count(outs) == 1, outs
    [entry] = entries(lab_root)
    assert entry.name == key
    assert lock_dirs(lab_root) == [] and tmp_dirs(lab_root) == []


def test_a_live_lock_is_waited_for_and_its_entry_is_taken(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    key, hidden = _prime(tmp_path, project_template, lab_root)
    lock = _plant_lock(lab_root, key, age_s=0)
    beat = Beat(lock)
    beat.start()
    waiter = Job(tmp_path, project_template, "waiter", lab_root)
    waiter.env["NX_SHARED_VENV_WAIT"] = "60"
    proc = _spawn(waiter)
    time.sleep(3)
    assert proc.poll() is None, "the waiter did not wait for a live holder"
    hidden.rename(_venvs(lab_root) / key)       # the holder publishes ...
    beat.stop()
    shutil.rmtree(lock)                         # ... and releases
    out = proc.communicate(timeout=120)[0]
    assert proc.returncode == 0, out
    assert "waiting up to 60s" in out, out
    assert _build_count([out]) == 0 and "taking it over" not in out, out
    assert waiter.used and waiter.outputs["key"] == key


def test_a_dead_lock_is_taken_over_without_waiting_out_the_limit(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    key, hidden = _prime(tmp_path, project_template, lab_root)
    subprocess.run(["chmod", "-R", "u+w", str(hidden)], check=True, env=_clean_env())
    shutil.rmtree(hidden)                       # nothing published: the dead holder never finished
    ghost_tmp = _venvs(lab_root) / f".tmp.{key}.ghost.AAAAAA"
    (ghost_tmp / "venv").mkdir(parents=True)
    (ghost_tmp / "venv" / "partial").write_text("x")
    _plant_lock(lab_root, key, age_s=3600, tmp=ghost_tmp)
    waiter = Job(tmp_path, project_template, "waiter", lab_root)
    waiter.env["NX_SHARED_VENV_WAIT"] = "300"
    t0 = time.monotonic()
    res = waiter.run()
    elapsed = time.monotonic() - t0
    assert res.returncode == 0, res.stdout + res.stderr
    assert elapsed < 120, f"took {elapsed:.0f}s: waited on a dead lock"
    assert "taking it over" in res.stdout, res.stdout
    assert not ghost_tmp.exists(), "the dead builder's tmp dir was left behind"
    assert lock_dirs(lab_root) == [] and _dots(lab_root, ".lock-dead.") == []
    assert waiter.used and [e.name for e in entries(lab_root)] == [key]


def test_a_sigkilled_builder_goes_stale_and_the_next_job_takes_over(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """End to end ghost builder: a `uv sync` that never returns, the script SIGKILLed past every trap."""
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    stop = tmp_path / "stop-the-fake-sync"
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = sync ]; then\n'
        f"  while [ ! -e {stop} ]; do sleep 0.2; done\n"
        "  exit 1\n"
        "fi\n"
        f'exec {real_uv} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    ghost = Job(tmp_path, project_template, "ghost", lab_root)
    proc = _spawn(ghost, {**ghost.env, "PATH": f"{shim_dir}:{os.environ['PATH']}"})
    try:
        deadline = time.monotonic() + 120
        locks: list[Path] = []
        while time.monotonic() < deadline and not (locks := lock_dirs(lab_root)):
            time.sleep(0.2)
        assert locks, "the builder never took the lock"
        [lock] = locks
        # the heartbeat is refreshed while it builds
        seen = set()
        for _ in range(12):
            seen.add((lock / "heartbeat").stat().st_mtime_ns)
            time.sleep(0.5)
        assert len(seen) >= 2, "the build lock's heartbeat was not refreshed"
        assert tmp_dirs(lab_root), "the builder's tmp dir should exist mid-build"
        proc.kill()                                  # SIGKILL: no trap runs
        proc.wait(timeout=30)
        time.sleep(3.5)                              # the orphan heartbeat loop must notice
        last = (lock / "heartbeat").stat().st_mtime_ns
        time.sleep(2.5)
        assert (lock / "heartbeat").stat().st_mtime_ns == last, "the heartbeat outlived its SIGKILLed builder"
        assert lock.exists() and tmp_dirs(lab_root), "the ghost's state should still be there"
    finally:
        stop.touch()
        if proc.poll() is None:
            proc.kill()
    nxt = Job(tmp_path, project_template, "next", lab_root)
    nxt.env.update(NX_SHARED_VENV_WAIT="300", NX_SHARED_VENV_DEAD="3")
    res = nxt.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "taking it over" in res.stdout, res.stdout
    assert nxt.used
    assert tmp_dirs(lab_root) == [] and lock_dirs(lab_root) == [], "the ghost's tmp dir or lock survived"


def test_the_heartbeat_outlives_a_lock_moved_aside_and_put_back(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """take_over_lock moves a lock aside to measure its age and puts a live one back. The holder's
    heartbeat must keep refreshing the restored lock, or it goes stale in NX_SHARED_VENV_DEAD and a
    second job builds; it stops only for a lock naming another owner (nexus-6xlce)."""
    real_uv = shutil.which("uv")
    assert real_uv
    stop = tmp_path / "stop-the-fake-sync"
    shim = _path_shim(tmp_path, "uv",
                      'if [ "$1" = sync ]; then\n'
                      f"  while [ ! -e {stop} ]; do sleep 0.2; done\n"
                      "  exit 1\n"
                      "fi\n"
                      f'exec {real_uv} "$@"\n')
    builder = Job(tmp_path, project_template, "builder", lab_root)
    proc = _spawn(builder, {**builder.env, **shim})

    def mtime(lock: Path) -> int:
        return (lock / "heartbeat").stat().st_mtime_ns

    try:
        deadline = time.monotonic() + 120
        locks: list[Path] = []
        while time.monotonic() < deadline:
            locks = [lk for lk in lock_dirs(lab_root) if (lk / "owner").exists() and (lk / "owner").read_text()]
            if locks:
                break
            time.sleep(0.2)
        assert locks, "the builder never took the lock"
        [lock] = locks
        aside = lock.with_name(".lock-dead.measuring")
        lock.rename(aside)
        time.sleep(2.5)                    # one-second beats: at least two find no lock
        aside.rename(lock)
        before = mtime(lock)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and mtime(lock) == before:
            time.sleep(0.2)
        assert mtime(lock) != before, "the heartbeat stopped while its lock was aside and never refreshed it again"
        (lock / "owner").write_text("someone-else\n")
        time.sleep(4)                      # the next beat sees the foreign owner and ends the loop
        last = mtime(lock)
        time.sleep(2.5)
        assert mtime(lock) == last, "the heartbeat refreshed a lock that names another owner"
    finally:
        stop.touch()
        try:
            proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
    assert proc.returncode == 0
    assert (lock / "owner").read_text() == "someone-else\n", "release_lock removed a lock it does not own"


def test_a_builder_that_loses_the_publish_race_uses_the_winner_and_cleans_up(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """Deterministic: a `uv` shim takes the outer builder's lock away (as a take-over of a merely slow
    holder would) and runs a complete second job to publication before the outer `uv sync` returns,
    so the outer rename finds its target taken."""
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    marker = tmp_path / "raced"
    inner = Job(tmp_path, project_template, "inner", lab_root)
    inner_env = " ".join(f"{k}={v!r}" for k, v in inner.env.items()
                         if k.startswith(("NX_", "UV_", "LAB_", "RUNNER_", "GITHUB_")))
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = sync ] && [ ! -e {marker} ]; then\n'
        f"  : > {marker}\n"
        f"  rm -rf {_venvs(lab_root)}/.lock.*\n"
        f"  (cd {inner.project} && env {inner_env} bash {SCRIPT}) >&2 || exit 99\n"
        "fi\n"
        f'exec {real_uv} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    outer = Job(tmp_path, project_template, "outer", lab_root)
    env = {**outer.env, "PATH": f"{shim_dir}:{os.environ['PATH']}"}
    res = outer.run(env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert marker.exists(), "the shim never ran the inner builder"
    assert "lost the publish race" in res.stdout, res.stdout
    [entry] = entries(lab_root)
    assert tmp_dirs(lab_root) == [] and lock_dirs(lab_root) == []
    assert outer.used and inner.used
    assert outer.outputs["key"] == inner.outputs["key"] == entry.name
    res = subprocess.run([str(outer.thin / "bin" / "fxdep-tool")], capture_output=True, text=True, env=_clean_env())
    assert res.stdout.strip() == "fxdep ok"


@pytest.mark.parametrize("variant", ["unset", "missing", "unwritable", "no-workspace", "no-lock", "no-uv"])
def test_fallbacks_exit_zero_having_done_nothing(job: Job, lab_root: Path, tmp_path: Path, variant: str) -> None:
    env = dict(job.env)
    if variant == "unset":
        env.pop("NX_LAB_CACHE_ROOT")
    elif variant == "missing":
        env["NX_LAB_CACHE_ROOT"] = str(tmp_path / "does-not-exist")
    elif variant == "unwritable":
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions; the CI pool user is not root")
        lab_root.chmod(0o500)
    elif variant == "no-workspace":
        env.pop("RUNNER_WORKSPACE")
    elif variant == "no-lock":
        (job.project / "uv.lock").unlink()
    elif variant == "no-uv":
        bash = shutil.which("bash")
        assert bash
        empty = tmp_path / "emptybin"
        empty.mkdir()
        mkdir = shutil.which("mkdir")
        assert mkdir
        (empty / "mkdir").symlink_to(mkdir)   # the script's only external command before it looks for uv
        env["PATH"] = str(empty)
        res = subprocess.run([bash, str(SCRIPT)], cwd=job.project, env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "uv is not on PATH" in res.stdout
        assert job.env_untouched() and not job.used
        assert job.outputs["venv"] == str(job.disk / "venv")
        return
    try:
        res = job.run(env=env)
    finally:
        lab_root.chmod(0o700)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "shared venv not used" in res.stdout, res.stdout
    assert job.env_untouched(), "a fallback must not touch GITHUB_ENV"
    assert not job.used
    assert job.outputs["venv"] == str(job.disk / "venv")
    assert not job.thin.exists()
    assert entries(lab_root) == [] and tmp_dirs(lab_root) == [] and lock_dirs(lab_root) == []


def test_a_failed_build_falls_back_and_leaves_no_partial_state(job: Job, lab_root: Path) -> None:
    (job.project / "uv.lock").write_text("not a lockfile\n")
    res = job.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "::warning::shared venv failed" in res.stdout, res.stdout
    assert job.env_untouched() and not job.used
    assert entries(lab_root) == [] and tmp_dirs(lab_root) == []
    assert not job.thin.exists()


def _key_of(stdout: str) -> str:
    """The key a run used or tried to build, from its log (a failed build prints no entry)."""
    m = re.search(r"building shared venv ([0-9a-f]{32}) |shared venv ([0-9a-f]{32}): reusing", stdout)
    assert m, stdout
    return m.group(1) or m.group(2)


def _uv_probe_shim(tmp_path: Path) -> Path:
    """A `uv` that rewrites the interpreter probe's python-version / platform lines from
    FAKE_PYVER / FAKE_PLATFORM, so a test can vary the two inputs it has no second interpreter for."""
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "probe-shim"
    shim_dir.mkdir()
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = run ] && [[ " $* " == *" --no-project "* ]]; then\n'
        f'  out="$({real_uv} "$@")" || exit $?\n'
        '  printf \'%s\\n\' "$out" | awk -v pv="${FAKE_PYVER:-}" -v pl="${FAKE_PLATFORM:-}" '
        "'NR==2 && pv != \"\" {print pv; next} NR==3 && pl != \"\" {print pl; next} {print}'\n"
        "  exit 0\n"
        "fi\n"
        f'exec {real_uv} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim_dir


def test_key_depends_on_dependencies_python_platform_and_groups_but_not_the_root_version(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    shim_dir = _uv_probe_shim(tmp_path)

    def run(name: str, *args: str, extra: dict[str, str] | None = None, edit=None) -> tuple[Job, subprocess.CompletedProcess[str]]:  # noqa: ANN001
        j = Job(tmp_path, project_template, name, lab_root)
        if edit is not None:
            lock = j.project / "uv.lock"
            lock.write_text(edit(lock.read_text()))
        env = {**j.env, "PATH": f"{shim_dir}:{os.environ['PATH']}", **(extra or {})}
        res = j.run(*args, env=env)
        assert res.returncode == 0, res.stdout + res.stderr
        return j, res

    base, res = run("base", "--group", "dev")
    base_key = _key_of(res.stdout)
    assert base.used and [e.name for e in entries(lab_root)] == [base_key]

    # the default group set is dev: same key, served from the entry
    _, res = run("default-groups")
    assert _key_of(res.stdout) == base_key and "reusing the published entry" in res.stdout

    # a release bump: only the root project's own version line moves in uv.lock
    def bump_root(text: str) -> str:
        out = re.sub(r'(name = "fxproj"\nversion = )"0\.1\.0"', r'\1"0.2.0"', text)
        assert out != text, "the fixture lock has no fxproj version line to bump"
        return out

    j, res = run("root-version", edit=bump_root)
    assert _key_of(res.stdout) == base_key and "reusing the published entry" in res.stdout
    assert "0.2.0" in (j.project / "uv.lock").read_text()

    # python and platform are inputs of the key
    _, res = run("pyver", extra={"FAKE_PYVER": "3.99.1"})
    py_key = _key_of(res.stdout)
    _, res = run("platform", extra={"FAKE_PLATFORM": "fakeos-1-x86_64"})
    plat_key = _key_of(res.stdout)
    assert len({base_key, py_key, plat_key}) == 3, (base_key, py_key, plat_key)

    # a change to a dependency's own lock entry is a new key; the build may or may not succeed offline
    # (the lock then disagrees with the path source), the key is what is under test
    def bump_dep(text: str) -> str:
        out = re.sub(r'(name = "fxdep"\nversion = )"0\.1\.0"', r'\1"0.1.1"', text)
        assert out != text, "the fixture lock has no fxdep version line to bump"
        return out

    _, res = run("dep", edit=bump_dep)
    dep_key = _key_of(res.stdout)
    assert dep_key not in {base_key, py_key, plat_key}
    assert "reusing the published entry" not in res.stdout

    # a different group set is a different key; fxproj defines no `nonexistent` group, so uv refuses
    # it and the job falls back, publishing nothing under the new key
    j, res = run("groups", "--group", "nonexistent")
    assert not j.used and _key_of(res.stdout) not in {base_key, py_key, plat_key, dep_key}
    assert "reusing the published entry" not in res.stdout


def test_a_tmpfs_workspace_job_that_falls_back_syncs_into_the_disk_dir(job: Job, tmp_path: Path) -> None:
    """labrun F1: LAB_WS=tmpfs + LAB_DISK_DIR. Without F5 the venv must not be synced into the tmpfs."""
    disk = tmp_path / "labdisk"
    disk.mkdir()
    env = {**job.env, "LAB_WS": "tmpfs", "LAB_DISK_DIR": str(disk)}
    env.pop("NX_LAB_CACHE_ROOT")
    res = job.run(env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert job.outputs == {"used": "false", "venv": str(disk / "venv")}
    assert job.env_untouched()


@pytest.mark.parametrize("disk", ["unset", "missing"])
def test_a_tmpfs_job_without_a_usable_disk_dir_warns_and_uses_the_default(job: Job, tmp_path: Path, disk: str) -> None:
    env = {**job.env, "LAB_WS": "tmpfs"}
    env.pop("NX_LAB_CACHE_ROOT")
    env.pop("LAB_DISK_DIR")
    if disk == "missing":
        env["LAB_DISK_DIR"] = str(tmp_path / "gone")
    res = job.run(env=env)
    assert res.returncode == 0
    assert "::warning::LAB_WS=tmpfs but LAB_DISK_DIR is unusable" in res.stderr, res.stderr
    assert job.outputs["venv"] == str(job.project / ".venv")


def test_a_tmpfs_job_that_uses_the_shared_venv_gets_the_thin_venv_not_the_disk_dir(job: Job, tmp_path: Path) -> None:
    disk = tmp_path / "labdisk"
    disk.mkdir()
    res = job.run(env={**job.env, "LAB_WS": "tmpfs", "LAB_DISK_DIR": str(disk)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert job.outputs["venv"] == str(job.thin) and job.used
    assert not (disk / "venv").exists()


def test_empty_uv_env_values_are_what_the_workflow_must_never_pass() -> None:
    """The reason `venv` is never empty and the workflow passes UV_NO_SYNC=0, not an empty string."""
    env = _clean_env()
    res = subprocess.run(["uv", "run", "--no-project", "python", "-c", "pass"], env={**env, "UV_NO_SYNC": ""},
                         capture_output=True, text=True)
    # uv words the refusal differently across versions ("not a boolean", "expected a boolish value").
    assert res.returncode != 0 and ("not a boolean" in res.stderr or "boolish" in res.stderr), (
        "uv now accepts an empty UV_NO_SYNC; relax the rule"
    )
    res = subprocess.run(["uv", "run", "--no-project", "python", "-c", "pass"], env={**env, "UV_NO_SYNC": "0"},
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ---- the step never fails the shard (nexus-dvk18 fix round) -----------------------------------


def _path_shim(tmp_path: Path, name: str, body: str) -> dict[str, str]:
    """An env whose PATH starts with a one-command shim directory; ``body`` is the shim's sh text."""
    shim_dir = tmp_path / f"shim-{name}"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / name
    shim.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    shim.chmod(0o755)
    return {"PATH": f"{shim_dir}:{os.environ['PATH']}"}


def _assert_fell_back_cleanly(job: Job, res: subprocess.CompletedProcess[str], lab_root: Path) -> None:
    assert res.returncode == 0, res.stdout + res.stderr
    assert not job.used and job.outputs == {"used": "false", "venv": str(job.disk / "venv")}, job.outputs
    assert job.env_untouched()
    assert not job.thin.exists()
    assert entries(lab_root) == [] and lock_dirs(lab_root) == []


def test_an_unwritable_venvs_dir_after_the_preconditions_pass_still_exits_zero(
    job: Job, lab_root: Path, tmp_path: Path
) -> None:
    """The preconditions check -w on venvs/; it can still turn unwritable (quota, remount) before the build."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions; the CI pool user is not root")
    real_uv = shutil.which("uv")
    assert real_uv
    venvs = _venvs(lab_root)
    shim = _path_shim(tmp_path, "uv",
                      f'if [ "$1" = run ]; then {real_uv} "$@"; rc=$?; chmod 500 {venvs}; exit $rc; fi\n'
                      f'exec {real_uv} "$@"\n')
    try:
        res = job.run(env={**job.env, **shim})
    finally:
        venvs.chmod(0o700)
    _assert_fell_back_cleanly(job, res, lab_root)
    assert "::warning::" in res.stdout, res.stdout


def test_a_failing_mktemp_falls_back_and_never_touches_another_builders_dir(
    job: Job, lab_root: Path, tmp_path: Path
) -> None:
    other = _venvs(lab_root) / ".tmp.0123456789abcdef0123456789abcdef.otherhost.AAAAAA"
    (other / "venv").mkdir(parents=True)
    (other / "venv" / "partial").write_text("another builder's work")
    shim = _path_shim(tmp_path, "mktemp", "exit 1\n")
    res = job.run(env={**job.env, **shim})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not job.used and job.outputs["venv"] == str(job.disk / "venv")
    assert "cannot create a build directory" in res.stdout, res.stdout
    assert (other / "venv" / "partial").read_text() == "another builder's work"
    assert lock_dirs(lab_root) == [] and not job.thin.exists()


def test_a_lock_it_cannot_write_into_is_removed_not_left_ownerless(
    job: Job, lab_root: Path, tmp_path: Path
) -> None:
    """A lock made by mkdir whose heartbeat or owner write fails is this job's to remove: left
    behind without an owner, release_lock would skip it and every other job wait it out (nexus-6xlce)."""
    real_touch = shutil.which("touch")
    assert real_touch
    shim = _path_shim(tmp_path, "touch", 'case "$*" in */heartbeat) exit 1 ;; esac\n'
                      f'exec {real_touch} "$@"\n')
    res = job.run(env={**job.env, **shim})
    _assert_fell_back_cleanly(job, res, lab_root)
    assert "cannot write into the build lock" in res.stdout, res.stdout


def test_a_failing_chmod_of_the_built_venv_falls_back_and_leaves_nothing(
    job: Job, lab_root: Path, tmp_path: Path
) -> None:
    real_chmod = shutil.which("chmod")
    assert real_chmod
    shim = _path_shim(tmp_path, "chmod", f'[ "$1 $2" = "-R a-w" ] && exit 1\nexec {real_chmod} "$@"\n')
    res = job.run(env={**job.env, **shim})
    _assert_fell_back_cleanly(job, res, lab_root)
    assert tmp_dirs(lab_root) == []
    assert "cannot make" in res.stdout, res.stdout


# ---- F1 gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize("ws", ["unset", "disk"])
def test_f5_is_used_only_when_the_workspace_is_tmpfs(job: Job, lab_root: Path, ws: str) -> None:
    env = dict(job.env)
    env.pop("LAB_WS")
    env.pop("LAB_DISK_DIR")
    if ws == "disk":
        env["LAB_WS"] = "disk"
    res = job.run(env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "LAB_WS is not tmpfs" in res.stdout, res.stdout
    assert job.outputs == {"used": "false", "venv": str(job.project / ".venv")}
    assert entries(lab_root) == [] and lock_dirs(lab_root) == [] and not job.thin.exists()


# ---- entry health ----------------------------------------------------------------------------


def _make_debris_entry(lab_root: Path, key: str) -> Path:
    debris = _venvs(lab_root) / key
    (debris / "venv" / "lib").mkdir(parents=True)
    (debris / "venv" / "lib" / "half").write_text("an interrupted prune")
    (debris / "venv" / "lib" / "half").chmod(0o444)
    (debris / "venv" / "lib").chmod(0o555)
    return debris


def test_a_key_dir_without_complete_is_debris_removed_and_rebuilt(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    key, hidden = _prime(tmp_path, project_template, lab_root)
    subprocess.run(["chmod", "-R", "u+w", str(hidden)], check=True, env=_clean_env())
    shutil.rmtree(hidden)
    _make_debris_entry(lab_root, key)
    job = Job(tmp_path, project_template, "heal", lab_root)
    res = job.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "removing incomplete entry" in res.stdout, res.stdout
    assert job.used and job.outputs["key"] == key
    [entry] = entries(lab_root)
    assert (entry / "COMPLETE").read_text().strip() == key and not (entry / "venv" / "lib" / "half").exists()
    assert lock_dirs(lab_root) == [] and tmp_dirs(lab_root) == []


def _unhealthy(entry: Path) -> None:
    """Strip the dependency metadata from an entry's site-packages: a half-pruned tree."""
    subprocess.run(["chmod", "-R", "u+w", str(entry)], check=True, env=_clean_env())
    for d in (entry / "venv").rglob("*.dist-info"):
        shutil.rmtree(d)


def test_an_entry_that_fails_validation_is_renamed_aside_once_and_rebuilt(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    first = Job(tmp_path, project_template, "first", lab_root)
    assert first.run().returncode == 0
    [entry] = entries(lab_root)
    key = entry.name
    _unhealthy(entry)
    os.utime(entry / "last-used", (0, 0))
    second = Job(tmp_path, project_template, "second", lab_root)
    res = second.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "failed validation" in res.stdout and "rebuilding once" in res.stdout, res.stdout
    [aside] = _dots(lab_root, f".gone.{key}.")
    # last-used is touched BEFORE validating, so a concurrent prune sees the aside entry as young
    assert (aside / "last-used").stat().st_mtime > time.time() - 600
    assert second.used and second.outputs["key"] == key
    [entry] = entries(lab_root)
    assert list((entry / "venv").rglob("*.dist-info")), "the rebuilt entry has no dependencies"
    assert _build_count([res.stdout]) == 1


def test_an_entry_replaced_after_validation_is_not_moved_aside(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """Another job moved the unhealthy entry aside and republished a healthy one between this job's
    validation and its rename. A `date` shim (the aside name is taken right before the rename) makes
    that swap; the rename must leave the new entry alone, and the job use it unbuilt (nexus-6xlce)."""
    key, hidden = _prime(tmp_path, project_template, lab_root)
    venvs = _venvs(lab_root)
    entry = venvs / key
    subprocess.run(["cp", "-a", str(hidden), str(entry)], check=True, env=_clean_env())
    _unhealthy(entry)
    real_date = shutil.which("date")
    assert real_date
    marker = tmp_path / "swapped"
    other_aside = venvs / f".gone.{key}.other-job"
    shim = _path_shim(tmp_path, "date",
                      f'if [ "$1" = +%s ] && [ ! -e {marker} ]; then\n'
                      f"  : > {marker}\n"
                      f"  mv {entry} {other_aside} && mv {hidden} {entry} || exit 99\n"
                      "fi\n"
                      f'exec {real_date} "$@"\n')
    job = Job(tmp_path, project_template, "job", lab_root)
    res = job.run(env={**job.env, **shim})
    assert res.returncode == 0, res.stdout + res.stderr
    assert marker.exists(), "the shim never swapped the entry"
    assert "failed validation" in res.stdout, res.stdout
    assert _build_count([res.stdout]) == 0, res.stdout
    assert job.used and job.outputs["key"] == key
    assert entries(lab_root) == [entry] and list((entry / "venv").rglob("*.dist-info"))
    assert _dots(lab_root, f".gone.{key}.") == [other_aside], "the republished entry was moved aside"


def test_an_entry_that_is_unhealthy_after_its_rebuild_falls_back_instead_of_looping(
    job: Job, lab_root: Path, tmp_path: Path
) -> None:
    real_uv = shutil.which("uv")
    assert real_uv
    # a `uv sync` that builds an empty venv: every build is unhealthy
    shim = _path_shim(tmp_path, "uv",
                      'if [ "$1" = sync ]; then\n'
                      f'  exec {real_uv} venv --quiet --python "$UV_PYTHON" "$UV_PROJECT_ENVIRONMENT"\n'
                      "fi\n"
                      f'exec {real_uv} "$@"\n')
    res = job.run(env={**job.env, **shim})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not job.used and job.outputs["venv"] == str(job.disk / "venv")
    assert "unhealthy as well" in res.stdout, res.stdout
    assert _build_count([res.stdout]) == 2, "one rebuild, then the fallback"
    assert len(_dots(lab_root, ".gone.")) == 1 and lock_dirs(lab_root) == [] and not job.thin.exists()


def test_a_shared_entry_whose_base_interpreter_is_gone_is_still_used(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """setup-uv installs the base python under a per-job RUNNER_TEMP; a later job finds the entry's
    bin/python dangling. The thin venv does not need it."""
    first = Job(tmp_path, project_template, "first", lab_root)
    assert first.run().returncode == 0
    [entry] = entries(lab_root)
    bindir = entry / "venv" / "bin"
    bindir.chmod(0o755)
    gone = []
    for p in bindir.iterdir():
        if p.is_symlink() and p.name.startswith("python"):
            p.unlink()
            p.symlink_to(tmp_path / "no-such-runner-temp" / "python")
            gone.append(p.name)
    bindir.chmod(0o555)
    assert gone, "the fixture entry has no python symlink to break"
    second = Job(tmp_path, project_template, "second", lab_root)
    res = second.run("--smoke-import", "fxproj")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "reusing the published entry" in res.stdout and second.used, res.stdout
    assert second.outputs["key"] == entry.name
    out = subprocess.run([str(second.thin / "bin" / "python"), "-c", "import fxdep, fxdev; print(fxdep.__file__)"],
                         capture_output=True, text=True, env={**second.env, "PYTHONPATH": ""}, check=True).stdout
    assert out.startswith(str(entry / "venv")), out
    res = subprocess.run([str(second.thin / "bin" / "fxdep-tool")], capture_output=True, text=True, env=_clean_env())
    assert res.stdout.strip() == "fxdep ok", res


def test_non_script_executables_of_the_shared_bin_are_linked_into_the_thin_bin(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """ruff (dev group) is a native binary: no shebang to rewrite, but `uv run ruff` must find it."""
    first = Job(tmp_path, project_template, "first", lab_root)
    assert first.run().returncode == 0
    [entry] = entries(lab_root)
    bindir = entry / "venv" / "bin"
    true_bin = shutil.which("true")
    assert true_bin
    bindir.chmod(0o755)
    native = bindir / "fxnative"
    shutil.copy(true_bin, native)
    native.chmod(0o755)
    bindir.chmod(0o555)
    second = Job(tmp_path, project_template, "second", lab_root)
    assert second.run().returncode == 0 and second.used
    link = second.thin / "bin" / "fxnative"
    assert link.is_symlink() and link.resolve() == native.resolve()
    assert subprocess.run([str(link)], check=False, env=_clean_env()).returncode == 0
    # scripts are still copied and rewritten, not linked
    assert not (second.thin / "bin" / "fxdep-tool").is_symlink()
    # every executable of the shared bin (python and activate scripts aside) landed in the thin bin
    missing = [p.name for p in bindir.iterdir()
               if p.is_file() and not p.is_symlink() and not p.name.lower().startswith(("activate", "python", "deactivate"))
               and not (second.thin / "bin" / p.name).exists()]
    assert missing == []


# ---- stale sweep -----------------------------------------------------------------------------


def test_the_12h_sweep_removes_old_debris_and_spares_live_state(job: Job, lab_root: Path) -> None:
    venvs = _venvs(lab_root)
    venvs.mkdir(parents=True)
    thirteen_h = time.time() - 13 * 3600
    old_names = [".tmp.aaaa.host.AAAAAA", ".gone.bbbb.1700000000", ".lock.cccc", ".lock-dead.dddd.me"]
    live_names = [".tmp.eeee.host.BBBBBB", ".gone.ffff.1700000001", ".lock.gggg", ".lock-dead.hhhh.me"]
    for n in old_names + live_names:
        (venvs / n / "venv" / "sub").mkdir(parents=True)
        (venvs / n / "venv" / "sub" / "f").write_text("x")
        (venvs / n / "venv" / "sub" / "f").chmod(0o444)
        (venvs / n / "venv" / "sub").chmod(0o555)      # a published-style read-only tree
    old_entry = venvs / ("0" * 32)                    # a published entry idle for a month: the sweep is not prune
    old_entry.mkdir()
    (old_entry / "COMPLETE").write_text("0" * 32 + "\n")
    eleven_h = time.time() - 11 * 3600                # just inside the window: a long build's, spared
    for n in old_names:
        os.utime(venvs / n, (thirteen_h, thirteen_h))
    for n in live_names[:2]:
        os.utime(venvs / n, (eleven_h, eleven_h))
    os.utime(old_entry, (thirteen_h - 30 * 86400,) * 2)
    res = job.run()                                   # a cold build runs the sweep
    assert res.returncode == 0, res.stdout + res.stderr
    assert job.used
    for n in old_names:
        assert not (venvs / n).exists(), f"{n} is older than 12 h and should have been swept"
    for n in live_names:
        assert (venvs / n).exists(), f"{n} is young and may be a live builder's: it must be spared"
    assert old_entry.exists(), "the sweep must never touch a published entry"


def test_the_exit_trap_is_a_catch_all_for_a_command_nothing_guards(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """Every step the script knows can fail is routed to warn_fallback; this fails one that is not
    (`date` in the move-aside name, reached only for an unhealthy entry) and still expects exit 0,
    used=false, the fallback venv, and nothing left behind."""
    first = Job(tmp_path, project_template, "first", lab_root)
    assert first.run().returncode == 0
    [entry] = entries(lab_root)
    _unhealthy(entry)
    second = Job(tmp_path, project_template, "second", lab_root)
    res = second.run(env={**second.env, **_path_shim(tmp_path, "date", "exit 1\n")})
    assert res.returncode == 0, res.stdout + res.stderr
    assert second.outputs == {"used": "false", "venv": str(second.disk / "venv")}, second.outputs
    assert "ended early" in res.stdout, res.stdout
    assert second.env_untouched() and not second.thin.exists() and lock_dirs(lab_root) == []
