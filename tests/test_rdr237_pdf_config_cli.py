# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.2, fix round 1): a ``[pdf]`` integer setting that holds no integer is
reported by name, exit 1, no traceback, on every verb that reads the pdf config.

``nx index pdf`` reads it first, in ``get_pdf_extractor()`` -> ``get_pdf_config()``, before the
MinerU start-of-run check is reached, so a run-level ``MineruBoundsError`` never saw it. The report
is made once, at the CLI group boundary (``_StorageBackendGuardGroup``), so a verb that reads the
config later does not need its own catch.
"""
from __future__ import annotations

from pathlib import Path

import click
import pytest
import yaml
from click.testing import CliRunner

from nexus.cli import main
from nexus.config import get_pdf_config


@pytest.fixture
def home_with_pdf_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway HOME whose global config holds the given ``pdf`` section."""
    home = tmp_path / "home"
    (home / ".config" / "nexus").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")

    def write(**pdf) -> None:
        (home / ".config" / "nexus" / "config.yml").write_text(yaml.safe_dump({"pdf": pdf}))

    return write


def _assert_clean_failure(result, setting: str) -> None:
    assert result.exit_code == 1, result.output
    assert f"pdf.{setting}" in result.output, "the message names the setting"
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, ValueError), (
        f"the PdfConfigError reached the process as {result.exception!r}, not as a CLI error"
    )
    assert isinstance(result.exception, SystemExit), "a ClickException ends the run with exit 1"


@pytest.mark.parametrize("bad", ["soon", "", "2.5", 2.5, True])
def test_nx_index_pdf_names_a_non_integer_timeout_and_exits_1_without_a_traceback(
    tmp_path, home_with_pdf_config, bad,
) -> None:
    home_with_pdf_config(mineru_page_timeout_s=bad)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    result = CliRunner().invoke(main, ["index", "pdf", str(pdf)])

    _assert_clean_failure(result, "mineru_page_timeout_s")
    assert "got" in result.output, "the message says what the value was"


def test_nx_index_pdf_names_a_non_integer_batch_too(tmp_path, home_with_pdf_config) -> None:
    home_with_pdf_config(mineru_page_batch="many")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    result = CliRunner().invoke(main, ["index", "pdf", str(pdf)])

    _assert_clean_failure(result, "mineru_page_batch")


def test_the_report_is_made_at_the_group_for_any_verb_that_reads_the_pdf_config(
    monkeypatch, home_with_pdf_config,
) -> None:
    """``nx dt index``, ``nx dt capture`` and ``nx doctor`` read the config at different depths;
    a probe verb stands for all of them."""
    home_with_pdf_config(mineru_memory_ceiling_mb="lots")

    @click.command("pdf-config-probe")
    def probe() -> None:
        get_pdf_config()

    monkeypatch.setitem(main.commands, "pdf-config-probe", probe)

    result = CliRunner().invoke(main, ["pdf-config-probe"])

    _assert_clean_failure(result, "mineru_memory_ceiling_mb")


def test_valid_settings_pass_through_the_group_untouched(home_with_pdf_config, monkeypatch) -> None:
    home_with_pdf_config(mineru_page_timeout_s="60", mineru_page_batch=4, mineru_memory_ceiling_mb=180.0)
    seen: list = []

    @click.command("pdf-config-probe")
    def probe() -> None:
        cfg = get_pdf_config()
        seen.append((cfg.mineru_page_timeout_s, cfg.mineru_page_batch, cfg.mineru_memory_ceiling_mb))

    monkeypatch.setitem(main.commands, "pdf-config-probe", probe)

    result = CliRunner().invoke(main, ["pdf-config-probe"])

    assert result.exit_code == 0, result.output
    assert seen == [(60, 4, 180)], "numeric strings, ints and integral floats all parse"
