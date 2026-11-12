# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/ci_install_ripgrep.sh: ripgrep for CI without depending on apt.

On 2026-11-11 GitHub-hosted ubuntu-latest could not resolve the Ubuntu mirrors, so a bare
``sudo apt-get install -y ripgrep`` failed the integration and docs-reader jobs and pytest-gate with
them (CI 43033435294). The script uses an rg already on PATH, else the pinned GitHub release with its
SHA-256 checked, and only then apt, with retries. These tests drive it with stub curl and sudo."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_install_ripgrep.sh"


def _tarball(tmp: Path, target: str) -> tuple[Path, str]:
    """A release-shaped tarball holding a fake rg, and its sha256."""
    rg = b"#!/bin/sh\necho 'ripgrep 14.1.1 (fake)'\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"ripgrep-14.1.1-{target}/rg")
        info.size, info.mode = len(rg), 0o755
        tf.addfile(info, io.BytesIO(rg))
    path = tmp / "rg.tar.gz"
    path.write_bytes(buf.getvalue())
    return path, hashlib.sha256(buf.getvalue()).hexdigest()


#: The real tools the script and the stubs need. The test's PATH is ONLY the sandbox bin dir: links to
#: these plus the stubs, so a host rg (CI's runners have /usr/bin/rg) can never satisfy the first branch.
_REAL_TOOLS = ("sh", "bash", "tar", "gzip", "sha256sum", "shasum", "perl", "cut", "mkdir", "uname", "sleep", "cat", "cp")


def _stubs(tmp: Path, *, curl_ok: bool, tar_path: Path | None, apt_ok: bool) -> Path:
    bindir = tmp / "bin"
    bindir.mkdir()
    for tool in _REAL_TOOLS:
        real = shutil.which(tool)
        if real:
            (bindir / tool).symlink_to(real)
    curl = bindir / "curl"
    if curl_ok and tar_path is not None:
        curl.write_text(f'#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && {{ cp "{tar_path}" "$2"; exit 0; }}; shift; done\nexit 22\n')
    else:
        curl.write_text("#!/bin/sh\necho 'curl: (6) Could not resolve host' >&2\nexit 6\n")
    sudo = bindir / "sudo"
    sudo.write_text(f'#!/bin/sh\necho "$*" >> "{tmp}/sudo.log"\n' + ("exit 0\n" if apt_ok else "exit 100\n"))
    for f in (curl, sudo):
        f.chmod(0o755)
    return bindir


def _run(tmp: Path, bindir: Path, *, sha: str, extra_path: str = "") -> subprocess.CompletedProcess[str]:
    gh_path = tmp / "github_path"
    gh_path.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{extra_path}" if extra_path else str(bindir),
        "RUNNER_TEMP": str(tmp / "runner_temp"),
        "GITHUB_PATH": str(gh_path),
        "NX_RG_SHA256_X86_64": sha,
        "NX_RG_SHA256_AARCH64": sha,
        "NX_RG_APT_RETRY_SLEEP": "0",
    }
    (tmp / "runner_temp").mkdir(exist_ok=True)
    assert not (bindir / "rg").exists()
    return subprocess.run([str(bindir / "bash"), str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def _target() -> str:
    return "aarch64-unknown-linux-gnu" if os.uname().machine in ("aarch64", "arm64") else "x86_64-unknown-linux-musl"


def test_an_rg_already_on_path_is_used_and_nothing_is_fetched(tmp_path: Path) -> None:
    have = tmp_path / "have"
    have.mkdir()
    (have / "rg").write_text("#!/bin/sh\necho ripgrep\n")
    (have / "rg").chmod(0o755)
    bindir = _stubs(tmp_path, curl_ok=False, tar_path=None, apt_ok=False)
    proc = _run(tmp_path, bindir, sha="0" * 64, extra_path=str(have))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "already on PATH" in proc.stdout
    assert not (tmp_path / "sudo.log").exists()


def test_the_pinned_release_is_installed_when_apt_is_down(tmp_path: Path) -> None:
    tar, sha = _tarball(tmp_path, _target())
    bindir = _stubs(tmp_path, curl_ok=True, tar_path=tar, apt_ok=False)
    proc = _run(tmp_path, bindir, sha=sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    added = (tmp_path / "github_path").read_text().strip()
    assert added and Path(added, "rg").is_file()
    assert not (tmp_path / "sudo.log").exists()


def test_a_checksum_mismatch_is_never_installed_and_apt_is_tried(tmp_path: Path) -> None:
    tar, _ = _tarball(tmp_path, _target())
    bindir = _stubs(tmp_path, curl_ok=True, tar_path=tar, apt_ok=True)
    proc = _run(tmp_path, bindir, sha="f" * 64)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checksum" in proc.stdout + proc.stderr
    assert (tmp_path / "github_path").read_text().strip() == ""
    assert "apt-get install -y ripgrep" in (tmp_path / "sudo.log").read_text()


def test_apt_is_retried_then_the_step_fails_loud(tmp_path: Path) -> None:
    bindir = _stubs(tmp_path, curl_ok=False, tar_path=None, apt_ok=False)
    proc = _run(tmp_path, bindir, sha="0" * 64)
    assert proc.returncode != 0
    assert (tmp_path / "sudo.log").read_text().count("apt-get install -y ripgrep") == 3
    assert "ripgrep could not be installed" in proc.stdout + proc.stderr


def test_every_workflow_ripgrep_install_goes_through_the_script() -> None:
    bare, via_script = [], 0
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            for s in job.get("steps", []):
                run = s.get("run", "")
                if "apt-get install -y ripgrep" in run:
                    bare.append(wf.name)
                via_script += "scripts/ci_install_ripgrep.sh" in run
    assert not bare, f"a bare apt install of ripgrep is back: {bare}"
    assert via_script == 4, via_script  # ci.yml x3 (test, docs readers, integration) + the nightly
