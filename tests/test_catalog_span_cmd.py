# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 3: ``nx catalog span`` through CliRunner (the catalog is a fake reader)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.dchash import parse_dchash_span, text_hash
from nexus.commands.catalog import catalog
from tests.catalog.test_dchash_mint import TUMBLER, FakeCatalog, doc_catalog, make_entry

BODY = "Intro line.\nthe passage we quote and more.\nEnd.\n"


def run(cat: FakeCatalog, *args: str):
    with patch("nexus.commands.catalog._get_catalog", return_value=cat):
        return CliRunner().invoke(catalog, ["span", *args])


@pytest.fixture
def cat(tmp_path: Path) -> FakeCatalog:
    return doc_catalog(tmp_path, BODY.encode())


def test_help_lists_span() -> None:
    result = CliRunner().invoke(catalog, ["--help"])
    assert result.exit_code == 0
    assert any(line.split()[:1] == ["span"] for line in result.output.splitlines())


def test_span_help_recommends_passage_and_code_points() -> None:
    result = CliRunner().invoke(catalog, ["span", "--help"])
    assert result.exit_code == 0
    assert "--passage" in result.output and "code point" in result.output


def test_passage_prints_one_parseable_line(cat: FakeCatalog) -> None:
    result = run(cat, TUMBLER, "--passage", "the passage we quote")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 1
    h, start, end = parse_dchash_span(lines[0])
    assert h == text_hash(BODY)
    assert BODY[start:end] == "the passage we quote"


def test_offsets_mode_prints_same_span(cat: FakeCatalog) -> None:
    by_passage = run(cat, TUMBLER, "--passage", "the passage we quote").output
    _h, start, end = parse_dchash_span(by_passage.strip())
    by_offsets = run(cat, TUMBLER, "--start", str(start), "--end", str(end))
    assert by_offsets.exit_code == 0, by_offsets.output
    assert by_offsets.output == by_passage


@pytest.mark.parametrize(
    "args",
    [
        ["--passage", "x", "--start", "0", "--end", "1"],
        ["--passage", "x", "--start", "0"],
        [],
        ["--start", "0"],
        ["--end", "3"],
    ],
)
def test_mode_combinations_are_usage_errors(cat: FakeCatalog, args: list[str]) -> None:
    result = run(cat, TUMBLER, *args)
    assert result.exit_code == 2
    assert "Usage" in result.output or "usage" in result.output.lower()


def test_empty_passage_is_a_usage_or_click_error(cat: FakeCatalog) -> None:
    result = run(cat, TUMBLER, "--passage", "")
    assert result.exit_code != 0
    assert "empty" in result.output


def test_refused_mint_exits_1_with_message(cat: FakeCatalog) -> None:
    result = run(cat, TUMBLER, "--passage", "not in the document")
    assert result.exit_code == 1
    assert "passage not found" in result.output


def test_range_refusal_exits_1(cat: FakeCatalog) -> None:
    result = run(cat, TUMBLER, "--start", "5", "--end", "5")
    assert result.exit_code == 1
    assert "5" in result.output and str(len(BODY)) in result.output


def test_pdf_refused_exits_1(tmp_path: Path) -> None:
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF \xff\xfe")
    cat = FakeCatalog(make_entry(file_path=str(f), content_type="paper"))
    result = run(cat, TUMBLER, "--start", "0", "--end", "1")
    assert result.exit_code == 1
    assert f"{TUMBLER} is a PDF" in result.output


def test_invalid_utf8_exits_1_with_message(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"\xff\xfe bad")
    result = run(cat, TUMBLER, "--start", "0", "--end", "1")
    assert result.exit_code == 1
    assert "UTF-8" in result.output and TUMBLER in result.output


def test_missing_source_file_exits_1(tmp_path: Path) -> None:
    cat = FakeCatalog(make_entry(file_path=str(tmp_path / "gone.md")))
    result = run(cat, TUMBLER, "--start", "0", "--end", "1")
    assert result.exit_code == 1
    assert "Traceback" not in result.output
