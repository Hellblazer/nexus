# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-uwggq.2.2: ``meta_at_least`` is the one integer lower-bound rule, and ``nx catalog gc`` uses it.

Pure rule table (no engine) plus a ``gc`` run over a mocked catalog, where one document's ``miss_count`` is
``"2.9"``: before this change ``int("2.9")`` raised ``ValueError`` and the command failed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.meta_filter import meta_at_least
from nexus.cli import main

_ABSENT = object()


@pytest.mark.parametrize(
    ("value", "n", "expected"),
    [
        (2, 2, True),
        (3, 2, True),
        ("3", 2, True),
        ("0003", 2, True),
        (-1, -2, True),
        ("-1", -2, True),
        (1, 2, False),
        (-3, -2, False),
        (3.0, 2, False),  # "3.0": a decimal point
        (2.9, 2, False),
        ("2.9", 2, False),
        ("abc", 2, False),
        (True, 0, False),  # "true"
        (False, 0, False),
        (None, 0, False),
        ("", 0, False),
        (" 3", 2, False),
        ("3 ", 2, False),
        ("3\n", 2, False),  # a bare re.match with $ would accept this
        ("+3", 2, False),
        ("1e3", 2, False),
        (_ABSENT, 0, False),
    ],
    ids=repr,
)
def test_the_rule_table(value, n, expected) -> None:
    meta = {} if value is _ABSENT else {"k": value}
    assert meta_at_least(meta, "k", n) is expected


def test_a_key_default_is_not_consulted() -> None:
    # extraction_source reads "file" for equality; a bound has no such default.
    assert meta_at_least({}, "extraction_source", 0) is False


@pytest.mark.parametrize("meta", [None, "miss_count=3", ["miss_count"], 3])
def test_a_non_mapping_meta_matches_nothing(meta) -> None:
    assert meta_at_least(meta, "miss_count", 0) is False


def _entry(tumbler: str, miss_count) -> SimpleNamespace:
    return SimpleNamespace(
        tumbler=tumbler, title=f"doc {tumbler}", file_path=f"/x/{tumbler}.md",
        meta={"miss_count": miss_count}, sensitive=False, alias_of="",
    )


def test_gc_skips_a_document_whose_miss_count_is_not_an_integer() -> None:
    cat = MagicMock(spec=HttpCatalogClient)
    cat.all_documents.return_value = [
        _entry("1.1.1", "2.9"), _entry("1.1.2", 2), _entry("1.1.3", 2.0), _entry("1.1.4", 1),
    ]
    with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
            patch("nexus.commands.catalog._get_catalog_writer",
                  return_value=MagicMock(spec=list(CATALOG_WRITE_OPS))), \
            patch("nexus.sensitive_output.entries_flagged", return_value=set()):
        result = CliRunner().invoke(main, ["catalog", "gc"])

    assert result.exit_code == 0, result.output
    assert "Found 1 orphan entry" in result.output, result.output
    assert "1.1.2" in result.output
    for skipped in ("1.1.1", "1.1.3", "1.1.4"):
        assert skipped not in result.output, result.output
