# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus.types and nexus.errors modules (nexus-gu8)."""
import pytest


class TestSearchResultInTypes:
    def test_importable_from_nexus_types(self):
        from nexus.types import SearchResult  # noqa: F401 — import test

    def test_searchresult_has_required_fields(self):
        from nexus.types import SearchResult
        r = SearchResult(id="x", content="hello", distance=0.1, collection="code__repo")
        assert r.id == "x"
        assert r.content == "hello"
        assert r.distance == 0.1
        assert r.collection == "code__repo"
        assert r.metadata == {}
        assert r.hybrid_score == 0.0

    def test_searchresult_importable_from_types(self):
        """SearchResult importable from nexus.types (canonical path)."""
        from nexus.types import SearchResult  # noqa: F401


class TestErrorHierarchy:
    def test_nexuserror_importable(self):
        from nexus.errors import NexusError  # noqa: F401

    def test_t3_connection_error_importable(self):
        from nexus.errors import T3ConnectionError  # noqa: F401

    def test_indexing_error_importable(self):
        from nexus.errors import IndexingError  # noqa: F401

    def test_credentials_missing_error_importable(self):
        from nexus.errors import CredentialsMissingError  # noqa: F401

    def test_collection_not_found_error_importable(self):
        from nexus.errors import CollectionNotFoundError  # noqa: F401

    def test_all_are_nexuserror_subclasses(self):
        from nexus.errors import (
            NexusError,
            T3ConnectionError,
            IndexingError,
            CredentialsMissingError,
            CollectionNotFoundError,
        )
        for cls in (T3ConnectionError, IndexingError, CredentialsMissingError, CollectionNotFoundError):
            assert issubclass(cls, NexusError), f"{cls.__name__} must subclass NexusError"

    def test_nexuserror_is_exception(self):
        from nexus.errors import NexusError
        assert issubclass(NexusError, Exception)

    def test_credentials_missing_still_importable_from_indexer(self):
        """Backward-compat: indexer re-exports CredentialsMissingError."""
        from nexus.indexer import CredentialsMissingError  # noqa: F401

    def test_credentials_missing_same_object(self):
        from nexus.errors import CredentialsMissingError as E
        from nexus.indexer import CredentialsMissingError as I
        assert E is I
