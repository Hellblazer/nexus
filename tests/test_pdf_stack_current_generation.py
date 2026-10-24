# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A waiter reads the CURRENT generation, not its own interpreter. nexus-2k66t.4.2.

``pdf_stack.state()`` answers for the running interpreter. A peer's install
builds ANOTHER generation and flips ``current`` to it, so after the install
lock a waiter's ``state()`` still says absent and the waiter used to build a
second 0.8 GB generation. These tests build two real generations on disk (a
receipt each, a ``current`` symlink, an interpreter that finds the stack in the
new one only) and let two actors race through the real lock. Only the uv build
is faked: it writes the new generation and flips ``current``, as the real one
does.
"""
from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from nexus import install_layout, pdf_stack
from nexus.commands import self_cmd
from nexus.pdf_stack import Outcome

_STUBS = ("docling", "mineru", "torch", "torchvision", "pdftext")


def _write_generation(
    tools: Path, name: str, *, extras: list[str], version: str, installer_schema: int,
    source_kind: str = "registry", with_stack: bool = False,
) -> Path:
    gen = tools / name
    (gen / "bin").mkdir(parents=True)
    source = "conexus" if source_kind == "registry" else "/src/nexus"
    receipt = install_layout.Receipt(
        version=version, spec=install_layout.build_spec(source, extras, version),
        source_kind=source_kind, source=source, python="3.12.8",
        base_interpreter="/opt/python/3.12", created_at="2026-10-23T00:00:00Z",
        extras=extras, installer_schema=installer_schema,
    )
    install_layout.receipt_path(gen).write_text(receipt.to_json())
    stubs = gen / "stubs"
    stubs.mkdir()
    if with_stack:
        for mod in _STUBS:
            (stubs / mod).mkdir()
            (stubs / mod / "__init__.py").write_text("")
    python = gen / "bin" / "python"
    # -S: no site-packages, so the dev venv's real docling cannot leak into a
    # generation that is meant not to have the stack; the stubs come in by PYTHONPATH.
    python.write_text(f'#!/bin/sh\nPYTHONPATH="{stubs}" exec "{sys.executable}" -S "$@"\n')
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return gen


def _point_current(tools: Path, gen: Path) -> None:
    tmp = tools / ".current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(gen)
    os.replace(tmp, tools / "current")


@pytest.fixture
def two_generations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """``gen-old`` is what the test process "runs from" (no stack); the fake
    build writes ``gen-new`` (receipt lists pdf, interpreter finds the stack)
    and flips ``current`` to it."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv(pdf_stack.POLICY_ENV, raising=False)
    monkeypatch.delenv(pdf_stack.REEXEC_ENV, raising=False)
    pdf_stack.reset_cache()

    class _Bed:
        pass

    bed = _Bed()
    bed.tools = tools
    bed.old = _write_generation(tools, "gen-old", extras=[], version="1.0.0", installer_schema=1)
    _point_current(tools, bed.old)
    bed.builds = []
    bed.build_seconds = 0.0

    monkeypatch.setattr(self_cmd, "running_generation", lambda: bed.old)
    monkeypatch.setattr(pdf_stack, "_find_spec", lambda _name: False)  # the running interpreter has no stack
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND))
    monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: False)
    monkeypatch.setattr(pdf_stack, "_running_version", lambda: "1.0.0")

    def _perform_self_install(**kw) -> None:
        bed.builds.append(kw)
        time.sleep(bed.build_seconds)
        gen = _write_generation(
            tools, f"gen-new-{len(bed.builds)}", extras=sorted({"pdf", *kw.get("add_extras", ())}),
            version=kw["version"], installer_schema=2, with_stack=True,
        )
        _point_current(tools, gen)

    monkeypatch.setattr(self_cmd, "perform_self_install", _perform_self_install)
    return bed


def test_the_probe_sees_a_stack_in_current_that_the_running_interpreter_lacks(two_generations) -> None:
    assert not pdf_stack.state().present
    assert not pdf_stack.current_generation_has_stack()
    new = _write_generation(
        two_generations.tools, "gen-peer", extras=["pdf"], version="1.0.0", installer_schema=2, with_stack=True,
    )
    _point_current(two_generations.tools, new)
    assert pdf_stack.current_generation_has_stack()
    assert not pdf_stack.state().present, "the running interpreter still cannot see it"


def test_a_receipt_that_lists_pdf_over_an_interpreter_without_it_is_not_the_stack(two_generations) -> None:
    bare = _write_generation(
        two_generations.tools, "gen-bare", extras=["pdf"], version="1.0.0", installer_schema=2, with_stack=False,
    )
    _point_current(two_generations.tools, bare)
    assert not pdf_stack.current_generation_has_stack()


def test_a_second_ensure_from_the_stale_process_reruns_instead_of_building_again(two_generations) -> None:
    """The waiter's path, sequentially: the first call builds and flips; the
    second runs in a process whose interpreter is still gen-old."""
    assert pdf_stack.ensure("nx index pdf") is Outcome.INSTALLED
    assert len(two_generations.builds) == 1
    assert pdf_stack.ensure("nx index pdf") is Outcome.INSTALLED
    assert len(two_generations.builds) == 1, "a peer's generation already holds the stack"


def test_two_concurrent_pdf_indexes_build_one_generation(two_generations) -> None:
    two_generations.build_seconds = 0.4
    outcomes: list[Outcome | BaseException] = []

    def _go() -> None:
        try:
            outcomes.append(pdf_stack.ensure("nx index pdf"))
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            outcomes.append(exc)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert outcomes == [Outcome.INSTALLED, Outcome.INSTALLED]
    assert len(two_generations.builds) == 1


def test_carry_forward_does_not_build_what_a_peer_already_carried(two_generations) -> None:
    assert pdf_stack.carry_forward_pending(), "gen-old is a pre-split receipt without pdf"
    first = pdf_stack.carry_forward()
    assert first is not None and "carried forward into a new generation" in first
    second = pdf_stack.carry_forward()
    assert second is not None and "already carried forward" in second
    assert len(two_generations.builds) == 1


def test_the_install_never_flips_current_back_to_an_older_version(two_generations) -> None:
    newer = _write_generation(
        two_generations.tools, "gen-newer", extras=["local"], version="2.0.0", installer_schema=2,
    )
    _point_current(two_generations.tools, newer)
    assert pdf_stack.ensure("nx index pdf") is Outcome.INSTALLED
    (call,) = two_generations.builds
    assert call["version"] == "2.0.0", "current holds 2.0.0; the stale 1.0.0 process must not pin the build to itself"
    assert set(call["add_extras"]) == {"pdf", "local"}, "current's extras ride along"


def test_a_newer_current_built_from_a_checkout_is_refused_with_the_command(two_generations) -> None:
    newer = _write_generation(
        two_generations.tools, "gen-checkout", extras=[], version="2.0.0", installer_schema=2,
        source_kind="directory",
    )
    _point_current(two_generations.tools, newer)
    with pytest.raises(pdf_stack.PdfStackMissingError, match="NX_REINSTALL_EXTRAS=pdf scripts/reinstall-tool.sh"):
        pdf_stack.ensure("nx index pdf")
    assert two_generations.builds == []


def test_an_older_or_equal_current_keeps_the_running_version_pin(two_generations) -> None:
    assert pdf_stack.ensure("nx index pdf") is Outcome.INSTALLED
    (call,) = two_generations.builds
    assert call["version"] == "1.0.0" and call["add_extras"] == ("pdf",)
