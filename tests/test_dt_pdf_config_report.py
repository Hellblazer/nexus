# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-489n1): ``nx dt index`` and ``nx dt capture`` report a bad ``[pdf]`` setting.

``tests/test_rdr237_pdf_config_cli.py`` stands a probe verb in for them. These drive the real
verbs through the real config loader: exit 1, the setting named, and no record touched (nothing
indexed, nothing captured into DEVONthink) when the setting cannot be used. Both the no-flag path
(the extractor comes from ``pdf.extractor``, which reads the whole ``[pdf]`` section) and the
``--extractor mineru`` path (the MinerU bounds gate, ahead of the stack gate) are covered.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def pdf_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".config" / "nexus").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)

    def write(**pdf) -> None:
        (home / ".config" / "nexus" / "config.yml").write_text(yaml.safe_dump({"pdf": pdf}))

    return write


@pytest.fixture
def touched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Stubs DEVONthink and the indexer; the list records every call that reached either."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    calls: list[str] = []
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page", lambda url, **kw: calls.append("capture") or "U")
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [("U", str(pdf))])
    monkeypatch.setattr("nexus.commands.dt._index_record", lambda uuid, path, **kw: (calls.append("index") or True, 1))
    monkeypatch.setattr("nexus.pdf_stack.ensure_or_exit", lambda *a, **kw: calls.append("stack gate"))
    return calls


_VERBS = {
    "index": ["dt", "index", "--uuid", "U"],
    "capture": ["dt", "capture", "https://example.com", "--type", "pdf"],
}


@pytest.mark.parametrize("verb", sorted(_VERBS))
@pytest.mark.parametrize("flags", [[], ["--extractor", "mineru"]], ids=["setting", "flag"])
@pytest.mark.parametrize("key,bad", [
    ("mineru_page_timeout_s", "soon"),
    ("mineru_page_batch", "many"),
])
def test_a_bad_time_limit_is_named_and_no_record_is_touched(pdf_config, touched, verb, flags, key, bad) -> None:
    pdf_config(**{key: bad})

    result = CliRunner().invoke(main, [*_VERBS[verb], *flags])

    assert result.exit_code == 1, result.output
    assert f"pdf.{key} must be an integer" in result.output
    assert "Traceback" not in result.output
    assert touched == [], f"a record was touched before the setting was reported: {touched}"


@pytest.mark.parametrize("verb", sorted(_VERBS))
def test_a_bad_memory_ceiling_is_named_when_the_extractor_comes_from_the_setting(pdf_config, touched, verb) -> None:
    pdf_config(mineru_memory_ceiling_mb="lots")

    result = CliRunner().invoke(main, _VERBS[verb])

    assert result.exit_code == 1, result.output
    assert "pdf.mineru_memory_ceiling_mb must be an integer" in result.output
    assert "nx config set pdf.mineru_memory_ceiling_mb=<integer>" in result.output
    assert touched == []


@pytest.mark.parametrize("verb", sorted(_VERBS))
def test_a_product_above_the_bound_is_reported_with_both_settings_and_no_record_is_touched(
    pdf_config, touched, verb,
) -> None:
    pdf_config(mineru_page_timeout_s=100, mineru_page_batch=4)

    result = CliRunner().invoke(main, [*_VERBS[verb], "--extractor", "mineru"])

    assert result.exit_code == 1, result.output
    assert "pdf.mineru_page_timeout_s (100 s) x pdf.mineru_page_batch (4)" in result.output
    assert touched == []


@pytest.mark.parametrize("verb", sorted(_VERBS))
def test_usable_settings_reach_the_stack_gate_and_the_record(pdf_config, touched, verb) -> None:
    """Non-vacuity: the stubs let a good configuration through to the indexer."""
    pdf_config(mineru_page_timeout_s="60", mineru_page_batch=4)

    result = CliRunner().invoke(main, [*_VERBS[verb], "--extractor", "mineru"])

    assert result.exit_code == 0, result.output
    assert "index" in touched and "stack gate" in touched
