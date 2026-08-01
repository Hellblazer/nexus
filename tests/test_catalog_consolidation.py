# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# nexus-i711w terminal deletion: TestMergeCorpus (4 tests) and
# TestConsolidateCommand (1 test) retired WITH
# nexus.catalog.consolidation / the `nx catalog consolidate` verb —
# deep-maintenance merge over the local event log, no service
# equivalent. The by_corpus read contract survives below.

from tests._catalog_fixture_ops import ActiveCatalog


def _make_catalog() -> ActiveCatalog:
    return ActiveCatalog()


class TestByCorpus:
    def test_by_corpus(self):
        cat = _make_catalog()
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Paper A", content_type="paper", corpus="ml",
                     physical_collection="docs__La")
        cat.register(owner, "Paper B", content_type="paper", corpus="ml",
                     physical_collection="docs__Lb")
        cat.register(owner, "Paper C", content_type="paper", corpus="systems",
                     physical_collection="docs__Lc")
        results = cat.by_corpus("ml")
        assert len(results) == 2

    def test_by_corpus_empty(self):
        cat = _make_catalog()
        assert cat.by_corpus("nonexistent") == []
