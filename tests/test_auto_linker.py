# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.auto_linker import LinkContext, auto_link, read_link_contexts


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return cat


class TestAutoLink:
    def test_link_context_creates_relates_link(self, tmp_path):
        """Seeding one LinkContext with a valid tumbler creates a relates link."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target_t = cat.register(owner, "Target Doc", content_type="knowledge")

        ctx = LinkContext(target_tumbler=str(target_t), link_type="relates")
        count = auto_link(cat, source_t, [ctx])

        assert count == 1
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 1
        assert links[0].created_by == "auto-linker"

    def test_no_link_context_no_crash(self, tmp_path):
        """Empty context list produces no links and no exception."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")

        count = auto_link(cat, source_t, [])

        assert count == 0
        links = cat.links_from(source_t)
        assert links == []

    def test_nonexistent_tumbler_graceful_skip(self, tmp_path):
        """A LinkContext referencing a non-existent tumbler is silently skipped."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")

        ctx = LinkContext(target_tumbler="99.99.99", link_type="relates")
        count = auto_link(cat, source_t, [ctx])

        assert count == 0
        links = cat.links_from(source_t)
        assert links == []

    def test_multiple_contexts_create_multiple_links(self, tmp_path):
        """Two LinkContext objects each create one link — two total."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target1_t = cat.register(owner, "Target One", content_type="knowledge")
        target2_t = cat.register(owner, "Target Two", content_type="knowledge")

        contexts = [
            LinkContext(target_tumbler=str(target1_t), link_type="relates"),
            LinkContext(target_tumbler=str(target2_t), link_type="relates"),
        ]
        count = auto_link(cat, source_t, contexts)

        assert count == 2
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 2

    def test_single_entry_multiple_targets(self):
        """read_link_contexts() flattens a targets array into multiple LinkContext objects."""
        entries = [
            {
                "targets": [
                    {"target_tumbler": "1.1.1", "link_type": "relates"},
                    {"target_tumbler": "1.1.2", "link_type": "implements"},
                ]
            }
        ]
        contexts = read_link_contexts(entries)
        assert len(contexts) == 2
        assert contexts[0].target_tumbler == "1.1.1"
        assert contexts[0].link_type == "relates"
        assert contexts[1].target_tumbler == "1.1.2"
        assert contexts[1].link_type == "implements"

    def test_idempotent_no_duplicate(self, tmp_path):
        """Calling auto_link twice with the same inputs creates only one link."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target_t = cat.register(owner, "Target Doc", content_type="knowledge")

        ctx = LinkContext(target_tumbler=str(target_t), link_type="relates")
        auto_link(cat, source_t, [ctx])
        count2 = auto_link(cat, source_t, [ctx])

        assert count2 == 0
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 1

    def test_created_by_auto_linker(self, tmp_path):
        """Every link created by auto_link has created_by='auto-linker'."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target1_t = cat.register(owner, "Target One", content_type="knowledge")
        target2_t = cat.register(owner, "Target Two", content_type="knowledge")

        contexts = [
            LinkContext(target_tumbler=str(target1_t), link_type="relates"),
            LinkContext(target_tumbler=str(target2_t), link_type="implements"),
        ]
        auto_link(cat, source_t, contexts)

        links = cat.links_from(source_t)
        assert len(links) == 2
        for link in links:
            assert link.created_by == "auto-linker"
