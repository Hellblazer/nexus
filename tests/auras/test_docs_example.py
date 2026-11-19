# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/auras.md "Writing your own": the example a reader copies passes ``nx aura check`` (RDR-252 Step 4 round 1,
bead nexus-igea9.7). Before this, the example passed ``sha`` and ``outcome`` to ``ci_red_notice``, which no longer
declares ``outcome``, so a copied example was refused.

The example is read out of the doc, written beside the SHIPPED ``ci_red_notice.lua`` as ``<aura>.aura.toml``, and run
through the real command against a registry that lists the owner template. No engine, no tenant.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import aura_cmd
from nexus.commands.aura_cmd import aura_group
from nexus.lua_runtime import library

from .test_ci_develop_red import OWNER_TEMPLATE

DOC = Path(__file__).resolve().parents[2] / "docs" / "auras.md"
SHIPPED_PROCEDURE = Path(__file__).resolve().parents[2] / "src" / "nexus" / "procedures" / "ci_red_notice.lua"


def _example() -> str:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Writing your own", 1)[1]
    blocks = re.findall(r"```toml\n(.*?)```", section, flags=re.DOTALL)
    assert blocks, "docs/auras.md 'Writing your own' holds no toml example"
    return blocks[0]


def test_the_writing_your_own_example_passes_nx_aura_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = _example()
    name = re.search(r'^aura = "([^"]+)"', example, flags=re.MULTILINE)
    assert name is not None
    root = tmp_path / "shipped"
    root.mkdir()
    (root / "ci_red_notice.lua").write_bytes(SHIPPED_PROCEDURE.read_bytes())
    (root / f"{name.group(1)}.aura.toml").write_text(example, encoding="utf-8")
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aura_cmd, "_templates", lambda: ([OWNER_TEMPLATE], ""))
    monkeypatch.setattr(aura_cmd, "_group_reader", lambda: (lambda _n: None))
    res = CliRunner().invoke(aura_group, ["check", name.group(1)])
    assert res.exit_code == 0, res.output
    assert f"{name.group(1)} [shipped]: pass" in res.output, res.output
    assert "1 checked, 0 refused" in res.output, res.output
