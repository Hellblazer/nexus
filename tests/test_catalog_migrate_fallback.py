# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6: ``nx catalog migrate-fallback``.

Walks a fallback collection (``docs__default``, ``knowledge__knowledge``,
etc.) and proposes a per-document target conformant collection. With
``--yes`` performs the catalog-side migration: re-points each
document's ``physical_collection`` and auto-registers the target rows
in the collections projection. Fallback collections are deprecated
when they go to zero rows, never silently nuked (per RDR-101 §"Phase
6").

T3 chunks are NOT moved by this verb. The catalog-side migration is
enough to deprecate the fallback over time; operators repopulate the
target collection by re-running ``nx index`` against the source files,
or operate the existing T3 chunks via ``nx t3 gc`` once they go orphan
(catalog now points elsewhere).

Cross-prefix targets (e.g. docs__default doc with code__... target)
are rejected; the prefix carries the embedding-model contract.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _seed_doc(
    catalog: Catalog,
    *,
    tumbler: str,
    collection: str,
    title: str = "doc",
) -> None:
    catalog._db.execute(
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, title, "", 0, "text", f"/tmp/{tumbler}.md",
            "", collection, 1, "", "", "{}", 0.0, "", "",
        ),
    )
    catalog._db.commit()


# ── Proposal computation ──────────────────────────────────────────────────


def test_dry_run_reports_per_doc_target(catalog, runner):
    """Dry run prints one proposal line per doc; no writes."""
    catalog.register_collection("knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.1", collection="knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.7.3", collection="knowledge__knowledge")

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__knowledge", "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    assert "1.5.1" in result.output
    assert "1.7.3" in result.output
    assert "knowledge__1-5__voyage-context-3__v1" in result.output
    assert "knowledge__1-7__voyage-context-3__v1" in result.output

    # Catalog state unchanged
    rows = catalog._db.execute(
        "SELECT physical_collection FROM documents ORDER BY tumbler"
    ).fetchall()
    assert all(r[0] == "knowledge__knowledge" for r in rows)


def test_apply_repoints_per_doc_and_registers_targets(catalog, runner):
    """``--yes`` re-points each document's physical_collection and
    auto-registers the per-owner targets in the projection.
    """
    catalog.register_collection("knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.1", collection="knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.7.3", collection="knowledge__knowledge")

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__knowledge", "--yes"],
        )
    assert result.exit_code == 0, result.output

    # Documents re-pointed
    by_tumbler = {
        r[0]: r[1] for r in catalog._db.execute(
            "SELECT tumbler, physical_collection FROM documents",
        ).fetchall()
    }
    assert by_tumbler["1.5.1"] == "knowledge__1-5__voyage-context-3__v1"
    assert by_tumbler["1.7.3"] == "knowledge__1-7__voyage-context-3__v1"

    # Targets registered as conformant (legacy_grandfathered=False)
    target_one = catalog.get_collection(
        "knowledge__1-5__voyage-context-3__v1"
    )
    assert target_one is not None
    assert target_one["legacy_grandfathered"] is False
    assert target_one["owner_id"] == "1-5"


def test_apply_supersedes_source_when_emptied(catalog, runner):
    """When migration empties the source, the source row is marked
    superseded_by the (single) target if there is exactly one; or by a
    sentinel meta-collection note if multiple targets received docs.
    """
    catalog.register_collection("knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.1", collection="knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.2", collection="knowledge__knowledge")
    # Two docs, both same owner -> single target -> source can supersede

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__knowledge", "--yes"],
        )

    src = catalog.get_collection("knowledge__knowledge")
    assert src is not None
    assert src["superseded_by"] == "knowledge__1-5__voyage-context-3__v1"


def test_apply_does_not_supersede_when_multiple_targets(catalog, runner):
    """Multiple target collections after migration leaves the source
    NOT superseded (no canonical target to point at). Operator
    deprecates the source manually if appropriate.
    """
    catalog.register_collection("knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.1", collection="knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.7.1", collection="knowledge__knowledge")

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__knowledge", "--yes"],
        )

    src = catalog.get_collection("knowledge__knowledge")
    assert src["superseded_by"] == ""


def test_already_conformant_collection_rejected(catalog, runner):
    """A conformant collection is NOT a fallback; refuse to migrate it."""
    catalog.register_collection(
        "knowledge__1-1__voyage-context-3__v1",
        content_type="knowledge", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__1-1__voyage-context-3__v1",
             "--yes"],
        )
    assert result.exit_code != 0
    assert "not a fallback" in result.output.lower()


def test_unknown_source_rejected(catalog, runner):
    """Source name not in the projection is an error (run backfill first)."""
    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__no-such-name", "--yes"],
        )
    assert result.exit_code != 0
    assert "not registered" in result.output.lower()


def test_empty_source_clean_summary(catalog, runner):
    """Source with zero docs prints a clean zero summary and does nothing."""
    catalog.register_collection("knowledge__knowledge")

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__knowledge", "--yes"],
        )
    assert result.exit_code == 0
    assert "0 doc" in result.output


def test_no_yes_falls_back_to_report_only(catalog, runner):
    catalog.register_collection("knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.1", collection="knowledge__knowledge")

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["catalog", "migrate-fallback", "knowledge__knowledge"],
        )
    assert result.exit_code == 0
    assert "add --yes" in result.output.lower()
    rows = catalog._db.execute(
        "SELECT physical_collection FROM documents WHERE tumbler = ?",
        ("1.5.1",),
    ).fetchone()
    assert rows[0] == "knowledge__knowledge"


def test_owner_with_dot_replaced_by_hyphen_in_target(catalog, runner):
    """Tumbler dots become hyphens in the collection-name segment.

    Owner '1.5' for tumbler '1.5.3' becomes '1-5' in the target name.
    Multi-segment owners like '1.5.2' for nested tumblers preserve the
    internal segment separator (handled by the synthesizer's helper).
    """
    catalog.register_collection("knowledge__knowledge")
    _seed_doc(catalog, tumbler="1.5.42", collection="knowledge__knowledge")

    with patch("nexus.commands.catalog._get_catalog", return_value=catalog):
        runner.invoke(
            main,
            ["catalog", "migrate-fallback",
             "knowledge__knowledge", "--yes"],
        )

    row = catalog._db.execute(
        "SELECT physical_collection FROM documents WHERE tumbler = ?",
        ("1.5.42",),
    ).fetchone()
    assert row[0] == "knowledge__1-5__voyage-context-3__v1"
