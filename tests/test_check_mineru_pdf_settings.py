# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tr1rj.27: ``nx doctor --check-mineru`` names a bad ``[pdf]`` integer setting.

The mode is exclusive: it returns before the default sweep, so that sweep's "PDF settings" row
never runs. Reading the server URL reads the same section and raises ``PdfConfigError``; the
mode swallowed it, printed "no mineru-api server configured; subprocess mode in use" and exited 0,
which told an operator with a broken setting that nothing was wrong. These tests run the real
verb through the real config loader against a private ``NEXUS_CONFIG_DIR``; only what would reach
a real MinerU install (the stack probe, the imports, the child probe, cv2) is stubbed. Nothing
here touches T3 or any service.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

pytestmark = pytest.mark.usefixtures("no_mineru_child_probe")  # nexus-vyy65: no real MinerU child import here

_NOT_CONFIGURED = "no mineru-api server configured"


@pytest.fixture
def check_mineru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run ``nx doctor --check-mineru``; the returned function takes the ``[pdf]`` section."""
    import nexus.commands.doctor as doctor
    import nexus.config as config
    from nexus import pdf_stack
    from nexus.cli import main

    cfg = tmp_path / "nxcfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    # MinerU "installed and importing", cv2 loading, no weights (so no real parse): the rows
    # before the server line pass without a MinerU install on the host.
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", "stubbed"))
    fake = types.ModuleType("mineru.cli.common")
    fake.do_parse = lambda *a, **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", fake)
    monkeypatch.setattr(doctor, "_probe_cv2", lambda timeout_s=None: (None, "stub"))
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: False)
    monkeypatch.setattr(config, "_read_live_mineru_port", lambda: None)
    monkeypatch.setattr(config, "_read_live_mineru_port_full", lambda: (None, False))

    def run(**pdf):
        if pdf:
            (cfg / "config.yml").write_text(yaml.safe_dump({"pdf": pdf}))
        return CliRunner().invoke(main, ["doctor", "--check-mineru"])

    return run


@pytest.mark.parametrize("key,bad", [
    ("mineru_page_timeout_s", "soon"),
    ("mineru_page_batch", "many"),
    ("mineru_memory_ceiling_mb", "lots"),
])
def test_a_non_integer_pdf_setting_is_red_names_the_key_and_exits_nonzero(check_mineru, key, bad) -> None:
    result = check_mineru(**{key: bad})

    assert result.exit_code != 0, result.output
    row = next((ln for ln in result.output.splitlines() if "PDF settings" in ln), "")
    assert "✗" in row, result.output
    assert f"pdf.{key} must be an integer" in row
    assert f"nx config set pdf.{key}=<integer>" in result.output
    assert _NOT_CONFIGURED not in result.output


def test_usable_pdf_settings_are_not_reported_red_and_the_mode_still_exits_zero(check_mineru) -> None:
    """Control: the same verb on good settings reaches the server line and reports no PDF settings row."""
    result = check_mineru(mineru_page_timeout_s="60", mineru_page_batch=4)

    assert result.exit_code == 0, result.output
    assert "PDF settings" not in result.output
    assert _NOT_CONFIGURED in result.output
