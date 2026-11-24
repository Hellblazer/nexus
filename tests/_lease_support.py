# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers the generation-lease tests share (real venvs, real child interpreters).

A "generation" here is a real ``python -m venv`` tree (no pip), so ``sys.prefix``,
``site-packages`` and the ``.pth`` machinery are the interpreter's own, not a
hand-made imitation. A "holder" is a real child process started from that
generation's interpreter.
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from nexus._install import layout_core as lc
from nexus._install import lease_core

NT = sys.platform == "win32"
STAMPS = ("20261101T000000Z", "20261102T000000Z", "20261103T000000Z", "20261104T000000Z")

#: A process table that lists only gc's own pid: the census cannot see any child.
def blind_snapshot() -> str:
    return f"{os.getpid()} python gc_core.py\n"


def venv_python(gen: Path) -> Path:
    return gen / ("Scripts/python.exe" if NT else "bin/python")


def make_generation(tools: Path, stamp: str, *, leased: bool = True, receipt: bool = True) -> Path:
    """A receipted ``gen-<stamp>`` that is a real venv, carrying the lease hook."""
    gen = tools / f"gen-{stamp}"
    venv.EnvBuilder(with_pip=False, symlinks=not NT).create(gen)
    if leased:
        lease_core.install_lease(gen)
    if receipt:
        r = lc.Receipt(
            version="1.0.0", spec="conexus==1.0.0", source_kind="registry", source="conexus",
            python="3.12.0", base_interpreter=str(Path(sys.base_prefix)),
            created_at="2026-01-01T00:00:00Z",
        )
        (gen / lc.RECEIPT_NAME).write_text(r.to_json())
    return gen


def make_tools(tmp_path: Path, *, leased: bool = True) -> tuple[Path, list[Path]]:
    tools = tmp_path / "tools"
    tools.mkdir()
    return tools, [make_generation(tools, s, leased=leased) for s in STAMPS]


@contextmanager
def holder(python: Path | str, *, extra: tuple[str, ...] = ()) -> Iterator[subprocess.Popen]:
    """A live interpreter started from *python*; it reports ready and blocks on stdin."""
    code = "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
    proc = subprocess.Popen(
        [str(python), *extra, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
        yield proc
    finally:
        stop(proc)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
