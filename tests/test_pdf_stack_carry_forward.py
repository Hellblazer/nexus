# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The upgrade rule in `nx upgrade`: an existing install carries the PDF stack
forward. nexus-2k66t.4.2, Sam 2026-10-23.

The OLD `nx self install` that moves a box to the first post-split release
builds the NEW package, which no longer requires docling or MinerU, and it
cannot know the rule. So the first code of the new release to run (`nx upgrade`,
from the SessionStart lockstep) sees a receipt written before the split and
builds the generation that adds `pdf`. The generation-build half of the rule
(receipt-driven rebuilds, legacy convergence) is in
tests/scripts/test_pdf_stack_install_path.py.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import click
import pytest

from nexus import install_layout, pdf_stack
from nexus.commands import upgrade as upgrade_cmd


def _receipt(extras: list[str], installer_schema: int) -> install_layout.Receipt:
    spec = f"conexus[{','.join(extras)}]" if extras else "conexus"
    return install_layout.Receipt(
        version="", spec=spec, source_kind="registry", source="conexus", python="3.12",
        base_interpreter="/usr/bin", created_at="2026-10-01T00:00:00Z", extras=extras,
        installer_schema=installer_schema,
    )


@pytest.fixture
def bed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class _Bed:
        receipt: install_layout.Receipt | None = _receipt([], 1)
        plan = pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND)
        installs: list[bool] = []
        error: BaseException | None = None

    b = _Bed()
    b.installs = []
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "tools"))

    def _read(_gen):
        if b.receipt is None:
            raise install_layout.InstallLayoutError("no receipt")
        return b.receipt

    monkeypatch.setattr("nexus.install_layout.read_receipt", _read)
    monkeypatch.setattr("nexus.commands.self_cmd.running_generation", lambda: tmp_path / "gen-x")
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: b.plan)

    def _install(*, carrying: bool = False) -> None:
        b.installs.append(carrying)
        if b.error is not None:
            raise b.error

    monkeypatch.setattr(pdf_stack, "_install_generation", _install)
    return b


def test_pending_for_a_pre_split_generation_receipt_without_pdf(bed) -> None:
    assert pdf_stack.carry_forward_pending()


@pytest.mark.parametrize("extras, schema, why", [
    (["pdf"], 1, "already carries it"),
    ([], 2, "a fresh install written by the split-aware installer starts slim"),
    (["local"], 2, "a split-aware install that chose no pdf stays without it"),
])
def test_not_pending_when_the_install_is_not_an_existing_one_without_the_stack(bed, extras, schema, why) -> None:
    bed.receipt = _receipt(extras, schema)
    assert not pdf_stack.carry_forward_pending(), why


def test_not_pending_without_a_readable_receipt(bed) -> None:
    bed.receipt = None
    assert not pdf_stack.carry_forward_pending()


@pytest.mark.parametrize("kind", ["directory", "legacy", "dev"])
def test_not_pending_for_a_layout_the_auto_path_does_not_touch(bed, kind) -> None:
    bed.plan = pdf_stack.InstallPlan(kind, False, "x")
    assert not pdf_stack.carry_forward_pending()


def test_carry_forward_installs_once_and_says_so(bed) -> None:
    line = pdf_stack.carry_forward()
    assert bed.installs == [True]
    assert line is not None and "carried forward" in line


def test_carry_forward_is_silent_and_inert_when_nothing_is_pending(bed) -> None:
    bed.receipt = _receipt([], 2)
    assert pdf_stack.carry_forward() is None
    assert bed.installs == []


def test_a_failed_carry_forward_is_reported_not_raised_and_retried_next_run(bed) -> None:
    bed.error = click.ClickException("generation build failed: dns error")
    line = pdf_stack.carry_forward()
    assert line is not None and "could not be carried forward" in line and pdf_stack.INSTALL_COMMAND in line
    bed.error = None
    assert pdf_stack.carry_forward() is not None  # an interactive run retries at once
    assert bed.installs == [True, True]


def test_the_auto_run_backs_off_after_a_failure_so_an_offline_box_is_not_hammered(bed) -> None:
    bed.error = OSError("offline")
    pdf_stack.carry_forward(auto_mode=True)
    assert bed.installs == [True]
    again = pdf_stack.carry_forward(auto_mode=True)
    assert bed.installs == [True], "a second session start inside the backoff must not fetch again"
    assert again is not None and "deferred" in again
    # past the backoff, it tries again
    sentinel = Path(os.environ["NEXUS_CONFIG_DIR"]) / "pdf-carry-forward-failed"
    old = time.time() - pdf_stack.CARRY_FORWARD_AUTO_BACKOFF_S - 5
    os.utime(sentinel, (old, old))
    bed.error = None
    pdf_stack.carry_forward(auto_mode=True)
    assert bed.installs == [True, True]
    assert not sentinel.exists(), "a success clears the backoff"


# ── the rung in `nx upgrade` ─────────────────────────────────────────────────


def test_nx_upgrade_runs_the_carry_forward_and_reports_it(bed, capsys) -> None:
    upgrade_cmd._converge_pdf_stack(auto_mode=False, skip_t3=False)
    assert bed.installs == [True]
    assert "PDF support: PDF support carried forward" in capsys.readouterr().out


def test_nx_upgrade_auto_logs_instead_of_printing(bed, capsys) -> None:
    upgrade_cmd._converge_pdf_stack(auto_mode=True, skip_t3=False)
    assert bed.installs == [True]
    assert capsys.readouterr().out == ""


def test_skip_t3_skips_the_download(bed) -> None:
    upgrade_cmd._converge_pdf_stack(auto_mode=False, skip_t3=True)
    assert bed.installs == []


def test_a_crash_in_the_rung_never_fails_the_upgrade(bed, monkeypatch) -> None:
    monkeypatch.setattr(pdf_stack, "carry_forward", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    upgrade_cmd._converge_pdf_stack(auto_mode=False, skip_t3=False)  # does not raise


def test_the_carry_runs_before_the_process_cycle_so_processes_cycle_onto_the_carried_generation(bed, monkeypatch) -> None:
    """converge_preconditions restarts the service and MinerU. The carry builds a
    generation and flips `current`; run after the cycle, the processes would be
    started on the slim generation the carry then replaces."""
    order: list[str] = []
    monkeypatch.setattr("nexus.commands.self_cmd.repair_uv_takeover", lambda **kw: order.append("takeover") or [])
    monkeypatch.setattr(
        "nexus.upgrade_ladder.preconditions.converge_preconditions",
        lambda **kw: order.append("process-cycle") or [],
    )
    monkeypatch.setattr(upgrade_cmd, "_converge_vc_runtime", lambda **kw: order.append("vc-runtime"))
    monkeypatch.setattr(pdf_stack, "_install_generation", lambda *, carrying=False: order.append("carry"))
    upgrade_cmd._converge_preconditions(auto_mode=True)
    assert order.index("carry") < order.index("process-cycle"), order
    assert order.index("takeover") < order.index("carry"), order
    assert order.count("carry") == 1, "the carry must not run twice (before and after the cycle)"
