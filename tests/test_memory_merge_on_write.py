# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for write-time canonical-fact merge (bead nexus-lhxz4).

MemForest-inspired (T3 research-memforest-nexus-leverage-2026-05-27, idea #1):
normalize/merge overlapping content at write time instead of accumulating
near-duplicate entries that later need reactive `memory_consolidate`. The
merge is opt-in (``put_or_merge``), non-destructive (both source texts are
preserved), and keyed on word-set Jaccard against existing *different-title*
entries in the same project.
"""
from __future__ import annotations

from nexus.db.t2 import T2Database


def test_put_or_merge_inserts_when_project_empty(db: T2Database) -> None:
    """No existing entries → plain insert, action='inserted'."""
    row_id, action = db.put_or_merge(
        project="proj", title="a.md", content="alpha beta gamma delta"
    )
    assert action == "inserted"
    assert row_id > 0
    assert len(db.get_all("proj")) == 1


def test_put_or_merge_inserts_when_dissimilar(db: T2Database) -> None:
    """Existing entry on a different topic → insert, two entries remain."""
    db.put(project="proj", title="auth.md", content="authentication security tokens oauth")
    row_id, action = db.put_or_merge(
        project="proj", title="deploy.md",
        content="kubernetes docker containers orchestration",
    )
    assert action == "inserted"
    assert len(db.get_all("proj")) == 2


def test_put_or_merge_merges_high_overlap_into_existing(db: T2Database) -> None:
    """High word-set overlap with an existing different-title entry →
    merge into it, do NOT create the requested title, preserve both texts."""
    keep_id = db.put(
        project="proj", title="search-arch.md",
        content="search engine architecture design patterns optimization caching",
    )
    row_id, action = db.put_or_merge(
        project="proj", title="search-design.md",
        content="search engine architecture design patterns optimization sharding",
        min_similarity=0.5,
    )
    assert action == "merged"
    assert row_id == keep_id  # merged INTO the existing entry
    entries = db.get_all("proj")
    assert len(entries) == 1  # no near-duplicate created
    merged = entries[0]["content"]
    # non-destructive: both the original distinctive token and the new one survive
    assert "caching" in merged
    assert "sharding" in merged
    # requested title was not created as a separate row
    assert {e["title"] for e in entries} == {"search-arch.md"}


def test_put_or_merge_respects_threshold(db: T2Database) -> None:
    """Overlap below min_similarity → insert, not merge."""
    db.put(project="proj", title="a.md", content="search engine architecture design")
    row_id, action = db.put_or_merge(
        project="proj", title="b.md",
        content="kubernetes docker deployment pipeline",
        min_similarity=0.5,
    )
    assert action == "inserted"
    assert len(db.get_all("proj")) == 2


def test_put_or_merge_same_title_is_upsert_not_merge(db: T2Database) -> None:
    """Exact (project, title) collision takes the identity-upsert path,
    never the cross-title merge — content is replaced, one row remains."""
    first_id = db.put(project="proj", title="x.md", content="initial alpha beta gamma")
    row_id, action = db.put_or_merge(
        project="proj", title="x.md", content="updated alpha beta gamma delta",
        min_similarity=0.5,
    )
    assert action == "inserted"
    assert row_id == first_id
    entries = db.get_all("proj")
    assert len(entries) == 1
    assert entries[0]["content"] == "updated alpha beta gamma delta"


def test_put_or_merge_empty_content_inserts(db: T2Database) -> None:
    """Empty/whitespace content has no word-set → plain insert, no merge scan."""
    db.put(project="proj", title="a.md", content="alpha beta gamma")
    row_id, action = db.put_or_merge(project="proj", title="blank.md", content="")
    assert action == "inserted"
    assert len(db.get_all("proj")) == 2
