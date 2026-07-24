# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for L1 context cache generator (RDR-072)."""
from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture()
def db(tmp_path: Path) -> T2Database:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


#: Monotonic source-ids for the fidelity-import seeding leg — unique across
#: the module so parent/child links stay valid within any one test's tenant.
#: Starts at 1e9: import_topic preserves the given id VERBATIM without
#: advancing the engine's topics id sequence, so low imported ids on the
#: fresh session PG would sit exactly in the path of a later tenant's
#: sequence-issued INSERT (persist_rebuild) and 409 on the PK — observed
#: order-dependently against test_t2_concurrency's rebuild loop.
from tests.conftest import next_import_seed_id  # session-unique import ids (see conftest note)


def _seed_topics(taxonomy: Any, rows: list[dict[str, Any]]) -> list[int]:
    """Seed ``topics`` rows on either substrate (RDR-155 P4b P0a').

    Raw-SQLite leg keeps the historical INSERT (dies with the twin at the
    flip); the service leg routes through the fidelity-import surface
    ``import_topic`` which preserves label/collection/doc_count/created_at/
    review_status verbatim. Returns the topic ids in row order so callers
    can link children via ``parent_id``.
    """
    from nexus.db.storage_mode import has_raw_access

    ids: list[int] = []
    if has_raw_access(taxonomy):
        for r in rows:
            cur = taxonomy.conn.execute(
                "INSERT INTO topics "
                "(label, parent_id, collection, doc_count, created_at, review_status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    r["label"],
                    r.get("parent_id"),
                    r["collection"],
                    r["doc_count"],
                    r.get("created_at", "2026-01-01T00:00:00Z"),
                    r.get("review_status", "accepted"),
                ),
            )
            ids.append(cur.lastrowid)
        taxonomy.conn.commit()
    else:
        for r in rows:
            ids.append(taxonomy.import_topic(
                src_id=next_import_seed_id(),
                label=r["label"],
                parent_id=r.get("parent_id"),
                collection=r["collection"],
                centroid_hash=None,
                doc_count=r["doc_count"],
                created_at=r.get("created_at", "2026-01-01T00:00:00Z"),
                review_status=r.get("review_status", "accepted"),
                terms=None,
            ))
    return ids


class TestGenerateContextL1:
    """Core L1 cache generation from taxonomy topics."""

    def test_with_topics(self, db: T2Database, tmp_path: Path) -> None:
        """Generates grouped topic map from taxonomy."""
        from nexus.context import generate_context_l1

        # Seed topics across collections
        _seed_topics(db.taxonomy, [
            {"label": "GPU Kernels", "collection": "code__art", "doc_count": 100},
            {"label": "HTTP Handlers", "collection": "code__nexus", "doc_count": 50},
            {"label": "BFT Consensus", "collection": "knowledge__delos", "doc_count": 80},
            {"label": "PDF Extraction", "collection": "docs__nexus", "doc_count": 60},
            {"label": "Catalog Design", "collection": "rdr__nexus", "doc_count": 40},
        ])

        out = tmp_path / "context_l1.txt"
        result = generate_context_l1(db.taxonomy, output_path=out)

        assert result == out
        assert out.exists()
        content = out.read_text()
        assert "GPU Kernels" in content
        assert "BFT Consensus" in content
        assert "code:" in content
        assert "knowledge:" in content

    def test_empty_taxonomy(self, db: T2Database, tmp_path: Path) -> None:
        """No topics produces no file."""
        from nexus.context import generate_context_l1

        out = tmp_path / "context_l1.txt"
        result = generate_context_l1(db.taxonomy, output_path=out)

        assert result is None
        assert not out.exists()

    def test_groups_by_prefix(self, db: T2Database, tmp_path: Path) -> None:
        """Topics grouped by collection prefix (code/docs/knowledge/rdr)."""
        from nexus.context import generate_context_l1

        _seed_topics(db.taxonomy, [
            {"label": "Topic A", "collection": "code__repo1", "doc_count": 10},
            {"label": "Topic B", "collection": "docs__repo1", "doc_count": 20},
            {"label": "Topic C", "collection": "knowledge__kb", "doc_count": 30},
            {"label": "Topic D", "collection": "rdr__repo1", "doc_count": 40},
        ])

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        assert "code:" in content
        assert "docs:" in content
        assert "knowledge:" in content
        assert "rdr:" in content

    def test_top5_per_prefix(self, db: T2Database, tmp_path: Path) -> None:
        """Only top 5 topics per prefix by doc_count."""
        from nexus.context import generate_context_l1

        _seed_topics(db.taxonomy, [
            {"label": f"Code Topic {i}", "collection": "code__repo",
             "doc_count": (8 - i) * 10}
            for i in range(8)
        ])

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        # Should have topics 0-4 (top 5 by doc_count), not 5-7
        assert "Code Topic 0" in content
        assert "Code Topic 4" in content
        assert "Code Topic 5" not in content

    def test_no_readside_dedup_after_rdr154(self, db: T2Database, tmp_path: Path) -> None:
        """RDR-154 P0 (nexus-i7ivk): the nexus-9iw41 read-side dedup is retired.

        doc_count is now trigger-maintained (the topic_assignments statement-level
        trigger is the sole writer), so ``generate_context_l1`` no longer masks
        same-label root-topic rows. Same-label rows pass through verbatim (subject
        only to the ``_TOPICS_PER_PREFIX`` cap). Any residual duplicate-topic-row
        state is a separate clustering concern (nexus-9iw41), now surfaced rather
        than papered over at read time.

        RDR-170: the inserted rows use DISTINCT collections (same ``docs__``
        prefix, same label) rather than five byte-identical rows. The slcn7
        migration (``introduced=5.10.7``) now enforces a partial unique index on
        ``topics(collection, label) WHERE parent_id IS NULL`` — un-dormanted by
        RDR-170 — so byte-identical root rows can no longer exist. Distinct
        collections keep all five rows valid, in the one ``docs`` prefix bucket,
        and with the same label so the read path is still exercised for non-
        collapse (the original five-identical-rows shape only ever materialised
        because slcn7 was dormant; that was the bug RDR-170 fixed).
        """
        from nexus.context import _TOPICS_PER_PREFIX
        from nexus.context import generate_context_l1

        _seed_topics(db.taxonomy, [
            {"label": "Phantom Duplicate", "collection": f"docs__phantom{i}",
             "doc_count": 144, "created_at": "2026-05-28T00:00:00Z"}
            for i in range(5)
        ])

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        # No read-side collapse: identical rows appear up to the per-prefix cap.
        # (Assumes _TOPICS_PER_PREFIX >= 5 so all 5 inserted rows are emitted;
        # if the cap dropped below 5 this would silently under-count.)
        assert _TOPICS_PER_PREFIX >= 5
        assert content.count("Phantom Duplicate") == _TOPICS_PER_PREFIX

    def test_dedups_preserves_same_label_distinct_count(
        self, db: T2Database, tmp_path: Path,
    ) -> None:
        """Dedup must NOT collapse same-label rows with different doc_counts.

        Same label across two collections with different counts is a
        legitimate case (e.g. ``Project knowledge findings`` at 596 in
        ``docs__1-1`` vs 144 in some other live collection). The dedup
        key includes ``doc_count`` so both survive.
        """
        from nexus.context import generate_context_l1

        _seed_topics(db.taxonomy, [
            {"label": "Shared Label", "collection": "docs__a", "doc_count": 100,
             "created_at": "2026-05-28T00:00:00Z"},
            {"label": "Shared Label", "collection": "docs__b", "doc_count": 50,
             "created_at": "2026-05-28T00:00:00Z"},
        ])

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        # Both rows survive because doc_count differs.
        assert content.count("Shared Label") == 2

    def test_excludes_child_topics(self, db: T2Database, tmp_path: Path) -> None:
        """Only root topics (parent_id IS NULL), not children from split."""
        from nexus.context import generate_context_l1

        [root_id] = _seed_topics(db.taxonomy, [
            {"label": "Root Topic", "collection": "code__repo", "doc_count": 100},
        ])
        _seed_topics(db.taxonomy, [
            {"label": "Child Topic", "collection": "code__repo", "doc_count": 50,
             "parent_id": root_id},
        ])

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        assert "Root Topic" in content
        assert "Child Topic" not in content

    def test_atomic_write(self, db: T2Database, tmp_path: Path) -> None:
        """Cache file is written atomically (no partial reads)."""
        from nexus.context import generate_context_l1

        _seed_topics(db.taxonomy, [
            {"label": "Test Topic", "collection": "code__repo", "doc_count": 10},
        ])

        out = tmp_path / "context_l1.txt"
        # Write initial content
        out.write_text("old content")

        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        # Should have new content, not partial or old
        assert "Test Topic" in content
        assert "old content" not in content


    def test_repo_scoped(self, db: T2Database, tmp_path: Path) -> None:
        """Only includes collections registered to the specified repo."""
        from unittest.mock import patch

        from nexus.context import generate_context_l1

        # Topics from two different repos
        _seed_topics(db.taxonomy, [
            {"label": "My Repo Topic", "collection": "code__myrepo-abc123",
             "doc_count": 100},
            {"label": "Other Repo Topic", "collection": "code__other-def456",
             "doc_count": 50},
        ])

        # Mock registry to return myrepo collections only
        mock_entry = {"collection": "code__myrepo-abc123", "docs_collection": "docs__myrepo-abc123"}
        with patch("nexus.context._repo_collections", return_value={"code__myrepo-abc123", "docs__myrepo-abc123", "rdr__myrepo-abc123"}):
            out = tmp_path / "context_l1.txt"
            generate_context_l1(db.taxonomy, output_path=out, repo_path=Path("/fake/myrepo"))
            content = out.read_text()

        assert "My Repo Topic" in content
        assert "Other Repo Topic" not in content


class TestSubagentHookInjection:
    """Verify the SubagentStart hook expands the L1 context cache."""

    HOOK_PATH = Path(__file__).parent.parent / "conexus" / "hooks" / "scripts" / "subagent-start.sh"

    def test_hook_emits_knowledge_map(self, tmp_path: Path) -> None:
        """SubagentStart hook outputs the cached knowledge map content."""
        import subprocess

        # Write a fake context cache
        context_dir = tmp_path / ".config" / "nexus" / "context"
        context_dir.mkdir(parents=True)
        # The hook uses $(pwd -P) to compute the repo hash — we override HOME
        # so it reads from our temp dir, and run from a known cwd so the hash
        # is deterministic.
        repo_dir = tmp_path / "fakerepo"
        repo_dir.mkdir()

        # Compute expected filename the same way the hook does
        import hashlib
        repo_hash = hashlib.sha1(str(repo_dir.resolve()).encode()).hexdigest()[:8]
        cache_file = context_dir / f"fakerepo-{repo_hash}.txt"
        cache_file.write_text("## Knowledge Map\n\ncode: Test Topic (42)\n")

        result = subprocess.run(
            ["/bin/bash", str(self.HOOK_PATH)],  # /bin/bash 3.2 — homebrew bash 5.3 deadlocks on the NXTOOLS heredoc
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
            env={**__import__("os").environ, "HOME": str(tmp_path)},
        )

        assert "## Knowledge Map" in result.stdout, (
            f"Hook did not emit Knowledge Map. stdout={result.stdout[:500]}"
        )
        assert "Test Topic (42)" in result.stdout

    def test_hook_silent_when_no_cache(self, tmp_path: Path) -> None:
        """Hook doesn't error when no cache file exists."""
        import subprocess

        repo_dir = tmp_path / "norepo"
        repo_dir.mkdir()

        result = subprocess.run(
            ["/bin/bash", str(self.HOOK_PATH)],  # /bin/bash 3.2 — homebrew bash 5.3 deadlocks on the NXTOOLS heredoc
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
            env={**__import__("os").environ, "HOME": str(tmp_path)},
        )

        assert result.returncode == 0
        assert "Knowledge Map" not in result.stdout

    def test_hook_falls_back_to_global(self, tmp_path: Path) -> None:
        """Hook uses global context_l1.txt when no per-repo file exists."""
        import subprocess

        # Write only the global fallback
        config_dir = tmp_path / ".config" / "nexus"
        config_dir.mkdir(parents=True)
        (config_dir / "context_l1.txt").write_text(
            "## Knowledge Map\n\ncode: Global Topic (99)\n"
        )

        repo_dir = tmp_path / "noperrepo"
        repo_dir.mkdir()

        result = subprocess.run(
            ["/bin/bash", str(self.HOOK_PATH)],  # /bin/bash 3.2 — homebrew bash 5.3 deadlocks on the NXTOOLS heredoc
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
            env={**__import__("os").environ, "HOME": str(tmp_path)},
        )

        assert "Global Topic (99)" in result.stdout


class TestSessionHookInjection:
    """Verify the SessionStart hook reads the L1 context cache."""

    HOOK_PATH = Path(__file__).parent.parent / "conexus" / "hooks" / "scripts" / "session_start_hook.py"

    def test_hook_emits_knowledge_map(self, tmp_path: Path) -> None:
        """SessionStart hook outputs the cached knowledge map content."""
        import hashlib
        import subprocess

        repo_dir = tmp_path / "fakerepo"
        repo_dir.mkdir()

        context_dir = tmp_path / ".config" / "nexus" / "context"
        context_dir.mkdir(parents=True)
        repo_hash = hashlib.sha1(str(repo_dir.resolve()).encode()).hexdigest()[:8]
        cache_file = context_dir / f"fakerepo-{repo_hash}.txt"
        cache_file.write_text("## Knowledge Map\n\ncode: Session Topic (77)\n")

        result = subprocess.run(
            ["python3", str(self.HOOK_PATH)],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
            env={**__import__("os").environ, "HOME": str(tmp_path)},
        )

        assert "Session Topic (77)" in result.stdout, (
            f"SessionStart hook did not emit Knowledge Map. stdout={result.stdout[:500]}"
        )


class TestRefreshContextL1:
    """Convenience wrapper that opens T2 and delegates."""

    def test_refresh(self, tmp_path: Path) -> None:
        """refresh_context_l1 opens DB, generates, returns path."""
        from nexus.context import refresh_context_l1

        db_path = tmp_path / "memory.db"
        out = tmp_path / "context_l1.txt"

        # Create DB with a topic
        with T2Database(db_path) as db:
            _seed_topics(db.taxonomy, [
                {"label": "Refresh Test", "collection": "code__repo", "doc_count": 10},
            ])

        result = refresh_context_l1(db_path=db_path, output_path=out)
        assert result == out
        assert "Refresh Test" in out.read_text()


class TestServiceModeL1(object):
    """nexus-azss4: generate_context_l1 must work against an
    HttpTaxonomyStore-shaped handle (no raw .conn/._lock) — the raw-handle
    AttributeError was swallowed at both call sites, leaving every
    service-mode box's SessionStart Knowledge Map permanently stale."""

    class _FakeHttpTaxonomy:
        """HttpTaxonomyStore-shaped: public API only, no .conn/._lock."""

        def __init__(self, rows: list[dict]) -> None:
            self._rows = rows
            self.calls: list[int | None] = []

        def get_topics(self, *, parent_id: int | None = None) -> list[dict]:
            self.calls.append(parent_id)
            assert parent_id is None
            return list(self._rows)

    def test_service_handle_generates_l1(self, tmp_path: Path) -> None:
        from nexus.context import generate_context_l1

        fake = self._FakeHttpTaxonomy([
            {"collection": "code__nexus__m__v1", "label": "Routing", "doc_count": 9},
            {"collection": "docs__nexus__m__v1", "label": "Guides", "doc_count": 4},
        ])
        out = generate_context_l1(fake, output_path=tmp_path / "l1.md")
        assert out is not None
        text = out.read_text()
        assert "Routing" in text and "Guides" in text
        assert fake.calls == [None]

    def test_service_handle_empty_topics_returns_none(self, tmp_path: Path) -> None:
        from nexus.context import generate_context_l1

        fake = self._FakeHttpTaxonomy([])
        assert generate_context_l1(fake, output_path=tmp_path / "l1.md") is None

    def test_service_rows_sorted_client_side(self, tmp_path: Path) -> None:
        """Top-N selection depends on doc_count DESC ordering — the client
        must not trust wire ordering."""
        from nexus.context import generate_context_l1

        fake = self._FakeHttpTaxonomy([
            {"collection": f"code__nexus__m__v1", "label": f"T{i}", "doc_count": i}
            for i in range(1, 8)  # ascending — wrong order on the wire
        ])
        out = generate_context_l1(fake, output_path=tmp_path / "l1.md")
        text = out.read_text()
        assert "T7" in text  # highest doc_count survives the top-5 cut
        assert "T1" not in text and "T2" not in text  # lowest two cut
