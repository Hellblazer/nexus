# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1668: ``resolve(corpus=...)`` answers for a collection content type, and fails loud on an unknown value.

The catalog is an in-process fake that records which reads were made, so the
no-walk rule (RDR-241: only ``by_corpus`` and ``list_collections``) is asserted
too. No test here provisions a tenant.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.lookup.resolve_collections import (
    UnknownCorpusError,
    resolve_collections,
)
from nexus.commands.catalog import catalog
from nexus.corpus import CONTENT_TYPES
from nexus.mcp import catalog as mcp_catalog
from nexus.mcp._tool_failure import is_failure

RDR_A = "rdr__1-48__bge-base-en-v15-768__v1"
RDR_B = "rdr__1-9__bge-base-en-v15-768__v1"
CODE_A = "code__1-48__bge-base-en-v15-768__v1"
LABELLED = "docs__1-2__bge-base-en-v15-768__v1"


def _entry(physical_collection: str, tumbler: str = "1.2.1") -> SimpleNamespace:
    return SimpleNamespace(physical_collection=physical_collection, tumbler=tumbler)


class FakeCatalog:
    """Just the reads ``resolve`` may make; anything else is an AttributeError (the no-walk rule)."""

    def __init__(self, *, labels=None, collections=None) -> None:
        self.labels: dict[str, list[SimpleNamespace]] = labels or {}
        self.collections: list[dict] = collections or []
        self.calls: list[tuple] = []

    def resolve(self, tumbler) -> SimpleNamespace | None:
        self.calls.append(("resolve", str(tumbler)))
        return {"1.2.1": _entry(LABELLED)}.get(str(tumbler))

    def by_owner(self, owner) -> list[SimpleNamespace]:
        self.calls.append(("by_owner", str(owner)))
        return [_entry(CODE_A)] if str(owner) == "1.48" else []

    def by_corpus(self, corpus: str) -> list[SimpleNamespace]:
        self.calls.append(("by_corpus", corpus))
        return list(self.labels.get(corpus, []))

    def list_collections(self, lifecycle_state=None, content_type=None) -> list[dict]:
        self.calls.append(("list_collections", lifecycle_state, content_type))
        return [
            c for c in self.collections
            if (content_type is None or c["content_type"] == content_type)
            and (lifecycle_state is None or c["lifecycle_state"] == lifecycle_state)
        ]


def _row(name: str, content_type: str, lifecycle_state: str = "live") -> dict:
    return {"name": name, "content_type": content_type, "lifecycle_state": lifecycle_state}


@pytest.fixture
def cat() -> FakeCatalog:
    return FakeCatalog(
        labels={"papers": [_entry(LABELLED), _entry(LABELLED, "1.2.2"), _entry("")]},
        collections=[
            _row(RDR_A, "rdr"),
            _row(RDR_B, "rdr"),
            _row("quarantine-" + RDR_A, "rdr", "quarantine"),
            _row(CODE_A, "code"),
        ],
    )


# ── the shared function ─────────────────────────────────────────────────────


def test_label_path_matches_documents_labelled_corpus(cat) -> None:
    assert resolve_collections(cat, corpus="papers") == [LABELLED]


def test_content_type_returns_live_collections_of_that_type(cat) -> None:
    assert resolve_collections(cat, corpus="rdr") == sorted([RDR_A, RDR_B])
    assert ("list_collections", "live", "rdr") in cat.calls


def test_content_type_and_label_are_unioned_sorted_and_distinct() -> None:
    labelled = FakeCatalog(
        labels={"rdr": [_entry(RDR_B), _entry("code__x__m__v1")]},
        collections=[_row(RDR_A, "rdr"), _row(RDR_B, "rdr")],
    )
    assert resolve_collections(labelled, corpus="rdr") == sorted([RDR_A, RDR_B, "code__x__m__v1"])


def test_content_type_with_no_collections_is_a_true_empty() -> None:
    assert resolve_collections(FakeCatalog(), corpus="knowledge") == []


@pytest.mark.parametrize("ct", CONTENT_TYPES)
def test_every_content_type_is_accepted(ct: str) -> None:
    assert resolve_collections(FakeCatalog(), corpus=ct) == []


def test_unknown_value_raises_naming_value_and_valid_types(cat) -> None:
    with pytest.raises(UnknownCorpusError) as ei:
        resolve_collections(cat, corpus="rdrs")
    text = str(ei.value)
    assert "'rdrs'" in text
    assert all(ct in text for ct in CONTENT_TYPES)
    assert "matched exactly" in text


def test_tumbler_and_owner_still_resolve_and_union_with_corpus(cat) -> None:
    assert resolve_collections(cat, tumbler="1.2.1") == [LABELLED]
    assert resolve_collections(cat, owner="1.48") == [CODE_A]
    assert resolve_collections(cat, tumbler="1.2.1", owner="1.48", corpus="rdr") == sorted(
        [LABELLED, CODE_A, RDR_A, RDR_B]
    )


def test_bad_tumbler_is_parsed_before_any_lookup(cat) -> None:
    with pytest.raises(ValueError, match="not a dotted tumbler"):
        resolve_collections(cat, tumbler="1-2188")
    assert cat.calls == []


def test_only_by_corpus_and_list_collections_are_read_for_corpus(cat) -> None:
    resolve_collections(cat, corpus="rdr")
    assert {c[0] for c in cat.calls} <= {"by_corpus", "list_collections"}


# ── the MCP tool ────────────────────────────────────────────────────────────


@pytest.fixture
def mcp_with(cat):
    with patch.object(mcp_catalog, "_require_catalog", return_value=(cat, None)):
        yield cat


def test_mcp_content_type_returns_collections(mcp_with) -> None:
    result = mcp_catalog.catalog_resolve(corpus="rdr")
    assert result == sorted([RDR_A, RDR_B])
    assert not is_failure(result)


def test_mcp_label_still_matches(mcp_with) -> None:
    assert mcp_catalog.catalog_resolve(corpus="papers") == [LABELLED]


def test_mcp_unknown_value_is_a_marked_failure(mcp_with) -> None:
    result = mcp_catalog.catalog_resolve(corpus="nope")
    assert is_failure(result)
    assert len(result) == 1 and result[0].startswith("Error: ")
    assert "'nope'" in result[0] and "rdr" in result[0] and "matched exactly" in result[0]


def test_mcp_empty_content_type_is_not_a_failure() -> None:
    with patch.object(mcp_catalog, "_require_catalog", return_value=(FakeCatalog(), None)):
        result = mcp_catalog.catalog_resolve(corpus="docs")
    assert result == [] and not is_failure(result)


def test_mcp_corpus_field_description_says_what_it_matches() -> None:
    tool = mcp_catalog.mcp._tool_manager._tools["resolve"]
    desc = tool.parameters["properties"]["corpus"]["description"]
    assert "content type" in desc and "label" in desc


# ── the CLI ─────────────────────────────────────────────────────────────────


def _run(cat, *args: str):
    with patch("nexus.commands.catalog._get_catalog", return_value=cat):
        return CliRunner().invoke(catalog, ["resolve", *args])


def test_cli_registered_in_catalog_help() -> None:
    result = CliRunner().invoke(catalog, ["--help"])
    assert any(line.split()[:1] == ["resolve"] for line in result.output.splitlines())


def test_cli_prints_one_collection_per_line(cat) -> None:
    result = _run(cat, "--corpus", "rdr")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == sorted([RDR_A, RDR_B])


def test_cli_json(cat) -> None:
    result = _run(cat, "--corpus", "rdr", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == sorted([RDR_A, RDR_B])


def test_cli_label_path(cat) -> None:
    result = _run(cat, "--corpus", "papers")
    assert result.exit_code == 0 and result.output.splitlines() == [LABELLED]


def test_cli_unknown_value_exits_1_with_text(cat) -> None:
    result = _run(cat, "--corpus", "nope")
    assert result.exit_code == 1
    assert "'nope'" in result.output and "matched exactly" in result.output


def test_cli_empty_content_type_exits_0_empty() -> None:
    result = _run(FakeCatalog(), "--corpus", "code")
    assert result.exit_code == 0 and result.output == ""
    assert json.loads(_run(FakeCatalog(), "--corpus", "code", "--json").stdout) == []


def test_cli_needs_an_argument(cat) -> None:
    assert _run(cat).exit_code == 2


def test_cli_bad_tumbler_exits_1(cat) -> None:
    result = _run(cat, "--tumbler", "1-2188")
    assert result.exit_code == 1 and "not a dotted tumbler" in result.output
