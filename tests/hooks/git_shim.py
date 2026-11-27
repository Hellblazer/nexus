# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recording ``git`` for the settings reader's tests (nexus-sby49.8), and real repositories to read.

``GitShim`` puts a ``git`` script first on ``PATH``. It records every call's argument line, then either runs the real git or,
in a failure *mode*, answers like a git that is broken: ``fail7`` (exit 7 for every call), ``catfail`` (``cat-file`` exits 1),
``sleep`` (it hangs), ``junk`` (exit 0 with output that is not ``cat-file --batch``'s), ``cut`` (a blob header whose body is short). POSIX only: a Windows run would need a ``.cmd`` twin, and the behaviour it pins (how many processes the
reader starts, and what each exit code means) is not platform-specific.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


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


class GitShim:
    def __init__(self, root: Path) -> None:
        self.bin = root / "shim-bin"
        self.bin.mkdir()
        self.log = root / "shim-calls.log"
        self.mode_file = root / "shim-mode"
        self.log.write_text("", encoding="utf-8")
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
            '  cut) printf "491d734467aa3c01755731be31c79b942ca8bd34 blob 99\\n{}\\n"; exit 0 ;;\n'
            "esac\n"
            f'exec "{real_git()}" "$@"\n',
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "GitShim":
        monkeypatch.setenv("PATH", str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
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
