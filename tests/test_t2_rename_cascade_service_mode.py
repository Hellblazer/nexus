# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests: _rename_collection_cascade_locked routes each store through its
store's rename_collection() (RDR-152 nexus-gmiaf.16).

Design: patch all stores on T2Database to spy objects and assert that
rename_collection was called on each with (old=..., new=...) and that the
returned counts surface per store.

HISTORY. The per-store isolation classes (one store on service, the rest on
their SQLite arms) died in nexus-i711w Stage 2 sub-stage A with the SQLite
stores; the "no raw SQLite UPDATE" non-mutation twin and the module's
global-=sqlite pin died with the =sqlite opt-out itself (RDR-158 P3,
nexus-7bomn) — the cascade is a pure HTTP fan-out with no connection to a
local file, so there is no SQLite arm left to prove unreached. The surviving
routing contract is pinned below and by TestCascadeOrchestration in
tests/test_collection_rename.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


#: Minimum env required so T2Database's Http* store constructors don't raise.
#: The stores are replaced with spies immediately after construction, so
#: these values never need to point at a real service.
_SVC_ENV: dict[str, str] = {
    "NX_SERVICE_PORT": "9999",
    "NX_SERVICE_TOKEN": "test-token",
}


class _SpyStore:
    """Drop-in spy: records calls to rename_collection and returns a fixed count."""

    def __init__(self, return_value: Any = 1) -> None:
        self.calls: list[dict[str, str]] = []
        self._return_value = return_value

    def rename_collection(self, *, old: str, new: str) -> Any:
        self.calls.append({"old": old, "new": new})
        return self._return_value

    def close(self) -> None:
        pass


class _SpyTelemetry(_SpyStore):
    """Telemetry spy returns a dict (search_telemetry + hook_failures)."""

    def rename_collection(self, *, old: str, new: str) -> dict[str, int]:  # type: ignore[override]
        self.calls.append({"old": old, "new": new})
        return {"search_telemetry": 2, "hook_failures": 0}


class TestCascadeAllStoresServiceMode:
    """Every store's rename_collection is called and its count surfaces."""

    def test_all_stores_routed_in_full_service_mode(self, tmp_path: Path) -> None:
        spy_chash = _SpyStore()
        spy_aspects = _SpyStore()
        spy_queue = _SpyStore()
        spy_highlights = _SpyStore()
        spy_taxonomy = MagicMock()
        spy_taxonomy.rename_collection.return_value = {
            "topics": 1, "assignments": 0, "meta": 0
        }
        spy_telemetry = _SpyTelemetry()

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, _SVC_ENV):
            with T2Database(tmp_path / "t2.db") as db:
                db.chash_index = spy_chash  # type: ignore[assignment]
                db.document_aspects = spy_aspects  # type: ignore[assignment]
                db.aspect_queue = spy_queue  # type: ignore[assignment]
                db.document_highlights = spy_highlights  # type: ignore[assignment]
                db.taxonomy = spy_taxonomy  # type: ignore[assignment]
                db.telemetry = spy_telemetry  # type: ignore[assignment]

                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        expected_call = [{"old": "code__old", "new": "code__new"}]

        assert spy_chash.calls == expected_call, f"chash: {spy_chash.calls}"
        assert spy_aspects.calls == expected_call, f"aspects: {spy_aspects.calls}"
        assert spy_queue.calls == expected_call, f"queue: {spy_queue.calls}"
        assert spy_highlights.calls == expected_call, f"highlights: {spy_highlights.calls}"
        spy_taxonomy.rename_collection.assert_called_once_with("code__old", "code__new")
        assert spy_telemetry.calls == expected_call, f"telemetry: {spy_telemetry.calls}"

        # Verify counts are correctly populated from each store's return value
        assert counts["chash"] == 1
        assert counts["aspects"] == 1
        assert counts["aspect_queue"] == 1
        assert counts["highlights"] == 1
        assert counts["tax_topics"] == 1
        assert counts["search_telemetry"] == 2
        assert counts["hook_failures"] == 0
