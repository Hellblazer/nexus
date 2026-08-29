# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-mlu3k: ``nx enrich aspects-without-catalog`` buckets the engine's
read-only census of aspect rows no live catalog document claims.

The CLI half is exercised against canned engine rows (the bucketing is the
verb's own logic); the wire half — ``GET
/v1/aspects/list_without_catalog_document`` through
``HttpDocumentAspectsStore.list_without_catalog_document`` — is exercised in
``TestWire`` against the real engine substrate.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.commands.enrich import enrich
from nexus.db.t2 import T2Database
from nexus.db.t2 import http_document_aspects_store as aspects_mod
from tests._catalog_fixture_ops import ActiveCatalog

_ROWS = [
    {"collection": "code__mtest__bge-base-en-v15-768__v1", "source_path": "src/a.py",
     "source_uri": "file:///nowhere/src/a.py", "extracted_at": "2026-06-03T10:00:00Z",
     "model_version": "v2", "extractor_name": "scholarly-paper", "confidence": 0.9,
     "tombstoned_match": False},
    {"collection": "code__mtest__bge-base-en-v15-768__v1", "source_path": "src/b.py",
     "source_uri": "file:///nowhere/src/b.py", "extracted_at": "2026-07-14T10:00:00Z",
     "model_version": "v2", "extractor_name": "scholarly-paper", "confidence": 0.9,
     "tombstoned_match": True},
    {"collection": "knowledge__k__bge-base-en-v15-768__v1", "source_path": "note",
     "source_uri": "chroma://knowledge__k/note", "extracted_at": "2026-07-14T11:00:00Z",
     "model_version": "v1", "extractor_name": "scholarly-paper", "confidence": 0.8,
     "tombstoned_match": False},
    {"collection": "knowledge__k__bge-base-en-v15-768__v1", "source_path": "bare",
     "source_uri": "some/relative/path.md", "extracted_at": None,
     "model_version": "v1", "extractor_name": "frontmatter", "confidence": 0.7,
     "tombstoned_match": False},
]


@pytest.fixture
def _canned_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch the store method the verb calls; the engine route is covered by TestWire."""
    calls: list[dict] = []

    def _fake(self, limit=None, offset=0):
        calls.append({"limit": limit, "offset": offset})
        return [dict(r) for r in _ROWS]

    monkeypatch.setattr(aspects_mod.HttpDocumentAspectsStore, "list_without_catalog_document", _fake)
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
    return calls


def test_buckets_by_scheme_tombstone_era_and_extractor(_canned_rows) -> None:
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["total"] == 4
    assert report["by_scheme"] == {"file": 2, "chroma": 1, "none": 1}
    assert report["tombstoned_match"] == 1
    assert report["by_era"] == {"2026-06": 1, "2026-07": 2, "unknown": 1}
    assert report["by_extractor"] == {"scholarly-paper@v2": 2, "scholarly-paper@v1": 1, "frontmatter@v1": 1}
    assert report["file_rows"] == 2
    assert report["file_rows_missing_on_disk"] == 2  # /nowhere does not exist
    assert report["file_rows_basename_registered_elsewhere"] is None  # not asked for
    assert _canned_rows == [{"limit": 300, "offset": 0}]  # one full page asked; short page ends it


def test_human_report_names_the_buckets_and_flags(_canned_rows) -> None:
    result = CliRunner().invoke(enrich, ["aspects-without-catalog"])
    assert result.exit_code == 0, result.output
    assert "Aspect rows with no live catalog document: 4 (tombstoned match: 1)" in result.output
    assert "by scheme: chroma=1, file=2, none=1" in result.output
    assert "file:// rows: 2, missing on this box: 2" in result.output
    assert "[tombstoned-match, missing-on-disk]" in result.output


def test_limit_is_passed_through(_canned_rows) -> None:
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--limit", "50", "--json"])
    assert result.exit_code == 0, result.output
    assert _canned_rows == [{"limit": 50, "offset": 0}]


def test_pages_through_the_engine_ceiling_until_a_short_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The engine clamps every call to 300 rows; the verb must keep asking with
    offset until a short page, or an 846-row census would report 300."""
    calls: list[dict] = []

    def _paged(self, limit=None, offset=0):
        calls.append({"limit": limit, "offset": offset})
        pool = [dict(_ROWS[0], source_uri=f"file:///nowhere/{i}.py") for i in range(304)]
        return pool[offset:offset + limit]

    monkeypatch.setattr(aspects_mod.HttpDocumentAspectsStore, "list_without_catalog_document", _paged)
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["total"] == 304
    assert calls == [{"limit": 300, "offset": 0}, {"limit": 300, "offset": 300}]

    calls.clear()
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--json", "--limit", "302"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["total"] == 302
    assert calls == [{"limit": 300, "offset": 0}, {"limit": 2, "offset": 300}]


def _raise(exc: Exception):
    def _boom(self, limit=None, offset=0):
        raise exc
    return _boom


def test_older_engine_without_the_route_is_exit_2_not_an_empty_census(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exc = httpx.HTTPStatusError(
        "HTTP 404: not found", request=MagicMock(), response=MagicMock(status_code=404),
    )
    monkeypatch.setattr(aspects_mod.HttpDocumentAspectsStore, "list_without_catalog_document", _raise(exc))
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
    result = CliRunner().invoke(enrich, ["aspects-without-catalog"])
    assert result.exit_code == 2, result.output
    assert "no /v1/aspects/list_without_catalog_document route" in result.output
    assert "Unverifiable is not empty" in result.output


def test_non_404_http_error_is_a_loud_failure_not_the_missing_route_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A WAF 403 (the edge's KnownBadInputs rule) is not 'deploy a newer engine'."""
    exc = httpx.HTTPStatusError(
        "HTTP 403: forbidden", request=MagicMock(), response=MagicMock(status_code=403),
    )
    monkeypatch.setattr(aspects_mod.HttpDocumentAspectsStore, "list_without_catalog_document", _raise(exc))
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
    result = CliRunner().invoke(enrich, ["aspects-without-catalog"])
    assert result.exit_code == 1, result.output
    assert "aspects census failed" in result.output
    assert "no /v1/aspects/list_without_catalog_document route" not in result.output


def test_unreachable_engine_is_a_clean_failure_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exc = httpx.ConnectError("connection refused", request=MagicMock())
    monkeypatch.setattr(aspects_mod.HttpDocumentAspectsStore, "list_without_catalog_document", _raise(exc))
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
    result = CliRunner().invoke(enrich, ["aspects-without-catalog"])
    assert result.exit_code == 1, result.output
    assert "engine unreachable" in result.output
    assert "Traceback" not in result.output


class TestWire:
    """The engine route through the client store, against the real substrate."""

    def test_unclaimed_row_is_listed_and_claimed_row_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
        cat = ActiveCatalog()
        owner = cat.register_owner("mlu3k", "repo", repo_hash="abcd1234", repo_root="/tmp/mlu3k")
        claimed_uri = "file:///tmp/mlu3k/claimed.md"
        cat.register(
            owner, "Claimed", content_type="docs",
            physical_collection="docs__mlu3k__bge-base-en-v15-768__v1",
            file_path="claimed.md", source_uri=claimed_uri,
        )
        with T2Database(tmp_path / "t2.db") as db:
            for name, uri in (("claimed.md", claimed_uri), ("orphan.md", "file:///tmp/mlu3k/orphan.md")):
                assert db.document_aspects.upsert(AspectRecord(
                    collection="docs__mlu3k__bge-base-en-v15-768__v1", source_path=name,
                    problem_formulation="p", proposed_method="m",
                    experimental_datasets=[], experimental_baselines=[],
                    experimental_results="r", confidence=0.9,
                    extracted_at="2026-08-01T00:00:00Z", model_version="v2",
                    extractor_name="scholarly-paper", source_uri=uri,
                ))
            rows = list(db.document_aspects.iter_without_catalog_document())
        uris = {r["source_uri"] for r in rows}
        assert "file:///tmp/mlu3k/orphan.md" in uris
        assert claimed_uri not in uris
        orphan = next(r for r in rows if r["source_uri"] == "file:///tmp/mlu3k/orphan.md")
        assert orphan["tombstoned_match"] is False
        assert orphan["extractor_name"] == "scholarly-paper"
