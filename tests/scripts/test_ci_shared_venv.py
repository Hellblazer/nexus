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
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script; the pool is Linux")

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci_shared_venv.sh"

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


class Job:
    """One CI job's view: a project checkout, a workspace, GITHUB_ENV/OUTPUT files, a lab root."""

    def __init__(self, tmp: Path, template: Path, name: str, root: Path | None) -> None:
        self.dir = tmp / name
        shutil.copytree(template, self.dir, ignore=shutil.ignore_patterns("uv-cache", ".venv", "__pycache__"))
        self.project = self.dir / "fxproj"
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()
        self.github_env = self.dir / "github_env"
        self.github_output = self.dir / "github_output"
        self.github_env.touch()
        self.github_output.touch()
        self.env = _uv_env(tmp / "uv-cache")
        self.env.update(RUNNER_WORKSPACE=str(self.workspace), GITHUB_ENV=str(self.github_env),
                        GITHUB_OUTPUT=str(self.github_output))
        if root is not None:
            self.env["NX_LAB_CACHE_ROOT"] = str(root)
        self.env["NX_SHARED_VENV_WAIT"] = "0"

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
    subprocess.run(["chmod", "-R", "u+w", str(root)], check=False)


@pytest.fixture
def job(tmp_path: Path, project_template: Path, lab_root: Path) -> Job:
    return Job(tmp_path, project_template, "job0", lab_root)


def entries(root: Path) -> list[Path]:
    venvs = root / "nexus" / "venvs"
    return sorted(p for p in venvs.iterdir() if not p.name.startswith(".tmp.")) if venvs.is_dir() else []


def tmp_dirs(root: Path) -> list[Path]:
    venvs = root / "nexus" / "venvs"
    return sorted(p for p in venvs.iterdir() if p.name.startswith(".tmp.")) if venvs.is_dir() else []


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


def test_two_concurrent_builders_end_on_one_published_entry(tmp_path: Path, project_template: Path, lab_root: Path) -> None:
    jobs = [Job(tmp_path, project_template, f"c{i}", lab_root) for i in range(3)]
    procs = [subprocess.Popen(["bash", str(SCRIPT)], cwd=j.project, env=j.env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) for j in jobs]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    [entry] = entries(lab_root)
    assert tmp_dirs(lab_root) == [], "a loser left partial state behind"
    for j in jobs:
        assert j.used and j.outputs["key"] == entry.name
        res = subprocess.run([str(j.thin / "bin" / "fxdep-tool")], capture_output=True, text=True)
        assert res.stdout.strip() == "fxdep ok"
    # exactly one process published
    assert sum("published shared venv" in o for o in outs) == 1, outs


def test_a_builder_that_loses_the_publish_race_uses_the_winner_and_cleans_up(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """Deterministic: a `uv` shim runs a complete second job to publication before the outer
    builder's `uv sync` returns, so the outer rename finds its target taken."""
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    marker = tmp_path / "raced"
    inner = Job(tmp_path, project_template, "inner", lab_root)
    inner_env = " ".join(f"{k}={v!r}" for k, v in inner.env.items() if k in
                         ("NX_LAB_CACHE_ROOT", "RUNNER_WORKSPACE", "GITHUB_ENV", "GITHUB_OUTPUT", "UV_CACHE_DIR",
                          "UV_OFFLINE", "UV_PYTHON", "UV_PYTHON_DOWNLOADS", "NX_SHARED_VENV_WAIT"))
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = sync ] && [ ! -e {marker} ]; then\n'
        f"  : > {marker}\n"
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
    assert tmp_dirs(lab_root) == []
    assert outer.used and inner.used
    assert outer.outputs["key"] == inner.outputs["key"] == entry.name
    res = subprocess.run([str(outer.thin / "bin" / "fxdep-tool")], capture_output=True, text=True)
    assert res.stdout.strip() == "fxdep ok"


def test_a_waiter_takes_the_entry_a_concurrent_builder_publishes(
    tmp_path: Path, project_template: Path, lab_root: Path
) -> None:
    """A fresh .tmp.<key>.* dir means someone is building; with a wait allowed we take their result."""
    probe = Job(tmp_path, project_template, "probe", lab_root)
    assert probe.run().returncode == 0
    [entry] = entries(lab_root)
    # hide the entry, plant a "builder in progress", then publish it back after a moment
    hidden = lab_root / "nexus" / "hidden"
    entry.rename(hidden)
    busy = lab_root / "nexus" / "venvs" / f".tmp.{entry.name}.otherhost.1"
    busy.mkdir()
    waiter = Job(tmp_path, project_template, "waiter", lab_root)
    waiter.env["NX_SHARED_VENV_WAIT"] = "60"
    proc = subprocess.Popen(["bash", str(SCRIPT)], cwd=waiter.project, env=waiter.env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    import time
    time.sleep(2)
    hidden.rename(entry)
    out = proc.communicate(timeout=120)[0]
    assert proc.returncode == 0, out
    assert "waiting up to 60s" in out and "(groups:" not in out, out
    assert waiter.used
    busy.rmdir()


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
        assert job.outputs["venv"] == str(job.project / ".venv")
        return
    try:
        res = job.run(env=env)
    finally:
        lab_root.chmod(0o700)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "shared venv not used" in res.stdout, res.stdout
    assert job.env_untouched(), "a fallback must not touch GITHUB_ENV"
    assert not job.used
    assert job.outputs["venv"] == str(job.project / ".venv")
    assert not job.thin.exists()
    assert entries(lab_root) == [] and tmp_dirs(lab_root) == []


def test_a_failed_build_falls_back_and_leaves_no_partial_state(job: Job, lab_root: Path) -> None:
    (job.project / "uv.lock").write_text("not a lockfile\n")
    res = job.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "::warning::shared venv failed" in res.stdout, res.stdout
    assert job.env_untouched() and not job.used
    assert entries(lab_root) == [] and tmp_dirs(lab_root) == []
    assert not job.thin.exists()


def test_key_depends_on_lock_python_and_groups(tmp_path: Path, project_template: Path, lab_root: Path) -> None:
    a = Job(tmp_path, project_template, "ka", lab_root)
    assert a.run("--group", "dev").returncode == 0
    b = Job(tmp_path, project_template, "kb", lab_root)
    assert b.run().returncode == 0                       # default group is dev: same key
    assert len(entries(lab_root)) == 1
    c = Job(tmp_path, project_template, "kc", lab_root)
    # a different group set is a different entry; fxproj defines no `extra` group, so uv refuses it
    # and the job falls back, publishing nothing. The key change itself is what is under test: the
    # build was attempted under a new key (the failure message names no entry), not served the old one.
    res = c.run("--group", "nonexistent")
    assert res.returncode == 0
    assert "reusing the published entry" not in res.stdout
    assert not c.used
    d = Job(tmp_path, project_template, "kd", lab_root)
    (d.project / "uv.lock").write_text((d.project / "uv.lock").read_text() + "\n# changed\n")
    res = d.run()
    assert res.returncode == 0 and "reusing the published entry" not in res.stdout
    assert len(entries(lab_root)) == 2, "a different uv.lock must publish a different key"


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
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    res = subprocess.run(["uv", "run", "--no-project", "python", "-c", "pass"], env={**env, "UV_NO_SYNC": ""},
                         capture_output=True, text=True)
    assert res.returncode != 0 and "not a boolean" in res.stderr, "uv now accepts an empty UV_NO_SYNC; relax the rule"
    res = subprocess.run(["uv", "run", "--no-project", "python", "-c", "pass"], env={**env, "UV_NO_SYNC": "0"},
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
