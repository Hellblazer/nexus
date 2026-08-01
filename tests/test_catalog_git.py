# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# nexus-i711w terminal deletion: TestInit / TestIsInitialized / TestSync /
# TestPull (12 tests) retired WITH nexus.catalog.catalog — git-backed JSONL
# Catalog init/sync/pull/rebuild has no service-substrate equivalent.


class TestCatalogPath:
    def test_env_override(self, tmp_path, monkeypatch):
        from nexus.config import catalog_path
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "custom"))
        assert catalog_path() == tmp_path / "custom"

    def test_default_path(self, monkeypatch):
        from nexus.config import catalog_path
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        result = catalog_path()
        assert str(result).endswith("nexus/catalog")
