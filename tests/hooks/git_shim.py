# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recording ``git`` for the settings reader's tests (nexus-sby49.8, nexus-sby49.18), and real repositories to read.

``GitShim`` puts a ``git`` first on ``PATH``. It records every call's argument line, then either runs the real git or,
in a failure *mode*, answers like a git that is broken: ``fail7`` (exit 7 for every call), ``catfail`` (``cat-file`` exits 1),
``sleep`` (it hangs), ``junk`` (exit 0 with output that is not ``cat-file --batch``'s), ``cut`` (a blob header whose body is short).

The shim is spelled once per platform and behaves the same. On POSIX it is a ``/bin/sh`` script. On Windows the code under test
resolves ``git`` only to a real ``.exe`` (``_exec_path.which_off_cwd`` never returns a ``.cmd`` or ``.bat``, and ``CreateProcess``
does not run a script), so the shim is a ``git.exe``: the script-launcher stub that pip's installer prepends to a console script
(``t64.exe``, read from the wheel ``ensurepip`` ships with every CPython), followed by a shebang naming this interpreter and a
zipped ``__main__.py`` that does what the shell script does. Nothing is compiled and nothing is downloaded.
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
_CUT_BLOB = "491d734467aa3c01755731be31c79b942ca8bd34 blob 99"

# The Windows shim's program. ``sys.argv[1:]`` is git's argument list; the paths and the real git are filled in as literals.
_WINDOWS_MAIN = '''\
import subprocess
import sys
import time

LOG, MODE_FILE, REAL_GIT = {log!r}, {mode_file!r}, {real_git!r}
args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\\n")
try:
    with open(MODE_FILE, encoding="utf-8") as f:
        mode = f.read().strip()
except OSError:
    mode = ""
if mode == "fail7":
    sys.exit(7)
if mode == "sleep":
    time.sleep(30)
    sys.exit(0)
if mode == "catfail" and "cat-file" in " ".join(args):
    sys.exit(1)
if mode == "junk":
    sys.stdout.write("nonsense\\n")
    sys.exit(0)
if mode == "cut":
    sys.stdout.write({cut!r} + "\\n{{}}\\n")
    sys.exit(0)
sys.exit(subprocess.call([REAL_GIT, *args]))
'''


def real_git() -> str:
    found = shutil.which("git")
    assert found, "these tests need a git binary on PATH"
    return found


def run_git(repo: "Path | str", *args: str) -> str:
    """Run the real git in *repo* with no user or system config; return stdout. Raises on a non-zero exit."""
    proc = subprocess.run(
        [real_git(), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), capture_output=True, text=True, env={**os.environ, **_ENV}, check=False, encoding="utf-8"
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout


def init_repo(path: Path, files: "dict[str, str] | None" = None, *, commit: bool = True) -> Path:
    """A real repository at *path* with *files* written and, when *commit*, committed. ``.git`` is a real directory."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q")
    for rel, body in (files or {}).items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    if files and commit:
        run_git(path, "add", "--", *files)
        run_git(path, "commit", "-q", "-m", "m")
    return path


def _launcher_stub() -> bytes:
    """The console-script launcher ``.exe`` for this machine's architecture, from the wheel ``ensurepip`` bundles."""
    import ensurepip  # noqa: PLC0415 -- Windows only; the module is in every CPython

    arch = {"AMD64": "t64", "ARM64": "t64-arm", "x86": "t32", "i386": "t32"}.get(platform.machine())
    assert arch, f"no script launcher known for {platform.machine()}"
    wheels = sorted((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
    assert wheels, "ensurepip carries no pip wheel, so there is no launcher stub to build git.exe from"
    with zipfile.ZipFile(wheels[-1]) as z:
        return z.read(f"pip/_vendor/distlib/{arch}.exe")


def _windows_exe(path: Path, main_source: str) -> None:
    """Write *path* as a launcher stub + ``#!<this interpreter>`` + a zip holding *main_source* as ``__main__.py``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", main_source)
    interpreter = sys.executable
    shebang = ('#!"' + interpreter + '"\r\n').encode("utf-8") if " " in interpreter else ("#!" + interpreter + "\r\n").encode("utf-8")
    path.write_bytes(_launcher_stub() + shebang + buf.getvalue())


class GitShim:
    def __init__(self, root: Path) -> None:
        self.bin = root / "shim-bin"
        self.bin.mkdir()
        self.log = root / "shim-calls.log"
        self.mode_file = root / "shim-mode"
        self.log.write_text("", encoding="utf-8")
        if os.name == "nt":
            _windows_exe(
                self.bin / "git.exe",
                _WINDOWS_MAIN.format(log=str(self.log), mode_file=str(self.mode_file), real_git=real_git(), cut=_CUT_BLOB),
            )
            return
        script = self.bin / "git"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.log}"\n'
            f'mode=$(cat "{self.mode_file}" 2>/dev/null)\n'
            'case "$mode" in\n'
            "  fail7) exit 7 ;;\n"
            "  sleep) exec sleep 30 ;;\n"
            '  catfail) case "$*" in *cat-file*) exit 1 ;; esac ;;\n'
            '  junk) echo "nonsense"; exit 0 ;;\n'
            f'  cut) printf "{_CUT_BLOB}\\n{{}}\\n"; exit 0 ;;\n'
            "esac\n"
            f'exec "{real_git()}" "$@"\n',
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def env(self) -> "dict[str, str]":
        """The environment a child process needs to find the shim: its directory first on ``PATH``."""
        return {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "GitShim":
        for k, v in self.env().items():
            monkeypatch.setenv(k, v)
        return self

    def mode(self, name: str) -> None:
        self.mode_file.write_text(name, encoding="utf-8")

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def verbs(self) -> list[str]:
        """The git sub-command of each recorded call (the first argument that is not an option or its value)."""
        out = []
        for line in self.calls():
            words = line.split()
            skip = False
            for w in words:
                if skip:
                    skip = False
                    continue
                if w in ("--git-dir", "-C", "-c"):
                    skip = True
                    continue
                if w.startswith("-"):
                    continue
                out.append(w)
                break
        return out


def path_without_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``PATH`` is one empty directory: ``git`` cannot be started at all."""
    empty = tmp_path / "no-git-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
