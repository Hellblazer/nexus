# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 (nexus-uwggq.1.6): ``list_by_collection`` refuses a blank collection name.

The engine's ``GET /list`` treats a present but blank ``collection`` param as absent and answers an unfiltered
page of the tenant, and ``HttpCatalogClient._get`` drops a param equal to ``""`` before it is sent. So a blank
name read as "list this collection" returned other collections' documents, and a destructive caller (``nx catalog
sensitive --collection " "``) then acted on them. The client raises ``ValueError`` before any request, rather
than returning ``[]``, so a destructive caller cannot read the absence as "nothing matched".

The stub replaces ``_get`` and fails the test if it is called: the refusal is before any request.
"""
from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands import catalog as _cat_cmd

# "" (dropped by _get, so no collection param at all), a space, a tab and a newline, and the ideographic space
# (U+3000), which Java's isBlank reads as blank.
BLANKS = ["", " ", "   ", "\t", "\n", "　"]


def _client(monkeypatch: pytest.MonkeyPatch) -> tuple[HttpCatalogClient, list[Any]]:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    calls: list[Any] = []

    def _boom(path: str, **kw: Any) -> Any:
        calls.append((path, kw))
        raise AssertionError(f"no request may be sent for a blank collection: {path} {kw}")

    monkeypatch.setattr(cat, "_get", _boom)
    return cat, calls


@pytest.mark.parametrize("name", BLANKS, ids=[repr(b) for b in BLANKS])
def test_a_blank_name_raises_before_any_request(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cat, calls = _client(monkeypatch)
    with pytest.raises(ValueError, match="physical_collection is blank"):
        cat.list_by_collection(name)
    assert calls == []


@pytest.mark.parametrize("name", BLANKS, ids=[repr(b) for b in BLANKS])
def test_a_blank_name_raises_whatever_the_limit(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The callers that pass ``limit=1_000_000`` (doc_sensitivity, orphan_backfill, enrich) got the whole tenant."""
    cat, calls = _client(monkeypatch)
    with pytest.raises(ValueError, match="physical_collection is blank"):
        cat.list_by_collection(name, limit=1_000_000)
    assert calls == []


def test_a_named_collection_is_still_one_filtered_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen: list[tuple[str, dict]] = []

    def _get(path: str, **kw: Any) -> dict:
        seen.append((path, kw))
        return {"documents": []}

    monkeypatch.setattr(cat, "_get", _get)
    assert cat.list_by_collection("rdr__target", limit=5) == []
    assert seen == [("/list", {"collection": "rdr__target", "limit": 5})]


# ── nx catalog sensitive --collection <blank> ───────────────────────────────


class _MustNotBeUsed:
    """A catalog whose every attribute fails: the refusal comes before any catalog is opened or read."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the catalog must not be used for a blank --collection (touched {name})")


@pytest.mark.parametrize("name", [" ", "   ", "\t", "　"], ids=["space", "spaces", "tab", "ideographic"])
def test_sensitive_refuses_a_blank_collection_and_flags_nothing(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: (_ for _ in ()).throw(
        AssertionError("no catalog may be opened for a blank --collection")))
    monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda: _MustNotBeUsed())
    writes: list[Any] = []
    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", lambda self, *a, **kw: writes.append(a))
    result = CliRunner().invoke(main, ["catalog", "sensitive", "--collection", name])
    assert result.exit_code == 2, result.output
    assert "--collection" in result.output and "blank" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), "a click error, not a traceback"
    assert writes == []


def test_sensitive_refuses_a_blank_collection_even_with_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--path`` alone is a valid target; a blank ``--collection`` beside it must not be read as absent."""
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: (_ for _ in ()).throw(
        AssertionError("no catalog may be opened for a blank --collection")))
    result = CliRunner().invoke(main, ["catalog", "sensitive", "--collection", " ", "--path", "docs/*"])
    assert result.exit_code == 2, result.output
    assert "blank" in result.output


def test_none_is_refused_like_a_blank_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get drops a None param too, so a None name would also have listed the tenant."""
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    monkeypatch.setattr(cat, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    with pytest.raises(ValueError, match="blank"):
        cat.list_by_collection(None)  # type: ignore[arg-type]


def test_sensitive_with_an_empty_collection_keeps_its_old_meaning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare --collection "" still means "not given": with no other target it is the old usage error."""
    result = CliRunner().invoke(main, ["catalog", "sensitive", "--collection", ""])
    assert result.exit_code == 2, result.output
    assert "--collection is blank" not in result.output

