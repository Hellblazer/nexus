# SPDX-License-Identifier: AGPL-3.0-or-later
"""A catalog reader that knows no sensitive document (RDR-243 Phase 1 Steps 4b and 4c).

The document gates (``nexus.doc_sensitivity``) read the sensitivity flag from the catalog. A test
that exercises an extractor, a worker or an enrich verb with a fake LLM has no tenant and no catalog,
and the gate's read would fail closed. ``tests/conftest.py`` puts this reader behind the gates for every
test; a test whose subject is a gate overrides that fixture and reads the real catalog (see
``tests/test_aspect_sensitivity_gates.py``)."""
from __future__ import annotations

from typing import Any


class NoSensitiveDocuments:
    """Every lookup answers "the catalog has no such row", so no document is flagged."""

    def resolve_many(self, doc_ids: list[str]) -> dict[str, Any]:
        return {}

    def by_doc_id(self, doc_id: str) -> None:
        return None

    def resolve(self, tumbler: Any, *, follow_alias: bool = True) -> None:
        return None

    def lookup_doc_id_by_collection_and_path(self, collection: str, source_path: str) -> str:
        return ""

    def by_source_uri(self, uri: str) -> None:
        return None

    def list_by_collection(self, collection: str, *, limit: int | None = None) -> list[Any]:
        return []

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[Any]]:
        return {}

    def close(self) -> None:
        return None
