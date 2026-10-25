# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-p4vhj: the carry-forward checks the Windows VC++ runtime before it says
it is downloading PDF support. On a box without the runtime the build goes slim,
so the only line the operator sees is the slim one."""
from __future__ import annotations

import pytest

from nexus import pdf_stack
from nexus.commands import self_cmd


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(pdf_stack, "_say", lines.append)
    monkeypatch.setattr(pdf_stack, "_running_version", lambda: "9.9.9")
    monkeypatch.setattr(pdf_stack, "_current_generation_receipt", lambda: None)
    monkeypatch.setattr(self_cmd, "perform_self_install", lambda **kw: None)
    return lines


def test_a_carry_forward_on_a_runtime_gap_box_announces_no_download(
    monkeypatch: pytest.MonkeyPatch, said: list[str],
) -> None:
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("none", None))
    pdf_stack._install_generation(carrying=True, notes=[])
    assert said == []


def test_a_carry_forward_with_the_runtime_still_announces_the_download(
    monkeypatch: pytest.MonkeyPatch, said: list[str],
) -> None:
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: None)
    pdf_stack._install_generation(carrying=True, notes=[])
    assert len(said) == 1 and said[0].startswith("Carrying PDF support forward")


def test_the_on_demand_install_keeps_its_notice_whatever_the_gap(
    monkeypatch: pytest.MonkeyPatch, said: list[str],
) -> None:
    """ensure() refuses on a gap before it gets here; this path is not the carry's to silence."""
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("none", None))
    pdf_stack._install_generation()
    assert len(said) == 1 and said[0].startswith("Installing PDF support")
