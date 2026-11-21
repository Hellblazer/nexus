# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.8 (nexus-p2m1x.13), engine 0.1.200: the indexer's legacy migration after a rename
registers the target again only when the row carries a version.

``collection_for`` answers a grandfathered row with a blank ``model_version`` as it is
(``model_version`` None). That row already exists, so the post-rename registration is skipped:
it would otherwise send ``model_version=""``, which engine 0.1.200's upsert refuses (P2.5, D1).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import nexus.indexer as indexer
from nexus.catalog.collection_name import CollectionAttributes, CollectionName
from nexus.db.collection_state import CollectionState

pytestmark = pytest.mark.usefixtures("cloud_mode")

_LEGACY = "code__myproject-cafef00d"
_MODEL = "voyage-code-3"


def _row_attrs(label: str, version: int | None) -> CollectionAttributes:
    return CollectionAttributes(
        collection_id=None, label=label, content_type="code", owner_id="1-1",
        embedding_model=_MODEL, model_version=version, dimension=None, lifecycle_state=None,
    )


def _migrate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target) -> list[dict]:
    """Run the migration for ``code`` with *target* as ``collection_for``'s answer; return the
    kwargs of every ``ensure_collection_registered`` call."""
    registered: list[dict] = []
    monkeypatch.setattr(indexer, "_MIGRATION_CONTENT_TYPES", ("code",))
    monkeypatch.setattr(indexer, "_migration_source_candidates", lambda repo, ct: [_LEGACY])
    monkeypatch.setattr("nexus.repo_identity._repo_identity", lambda r: ("myproject", "cafef00d"))
    monkeypatch.setattr(
        "nexus.db.collection_state.probe_collection_state",
        lambda t3, name: CollectionState.PRESENT if name == _LEGACY else CollectionState.ABSENT,
    )
    monkeypatch.setattr("nexus.collection_rename.rename_collection_data_plane", lambda *a, **k: None)
    monkeypatch.setattr(
        "nexus.corpus.ensure_collection_registered",
        lambda name, *, registrar, kwargs: registered.append({"name": name, **kwargs}),
    )
    cat = MagicMock()
    cat.owner_for_repo.return_value = "1.1"
    cat.collection_for.return_value = target
    cat.get_collection.return_value = {"embedding_model": _MODEL}
    t3 = MagicMock()
    t3.collection_exists.return_value = False
    indexer._migrate_legacy_collections(
        tmp_path, cat=cat, t3_db=t3, registry=MagicMock(), writer=MagicMock(),
    )
    return registered


def test_a_row_with_no_readable_version_is_not_registered_again(monkeypatch, tmp_path) -> None:
    registered = _migrate(monkeypatch, tmp_path, _row_attrs("notes-from-2024", None))
    assert registered == [], "the row exists; a re-registration would send model_version ''"


def test_no_registration_ever_sends_a_blank_version(monkeypatch, tmp_path) -> None:
    for target in (_row_attrs("notes-from-2024", None), _row_attrs("notes-from-2024", 3)):
        for call in _migrate(monkeypatch, tmp_path, target):
            assert call["model_version"] != ""


def test_a_row_with_a_version_is_registered_with_it(monkeypatch, tmp_path) -> None:
    """The control: the skip is for the missing version only."""
    registered = _migrate(monkeypatch, tmp_path, _row_attrs("notes-from-2024", 3))
    assert [c["model_version"] for c in registered] == ["v3"]


def test_a_rendered_name_is_registered_with_its_version(monkeypatch, tmp_path) -> None:
    name = CollectionName(content_type="code", owner_id="1-1", embedding_model=_MODEL, model_version=1)
    registered = _migrate(monkeypatch, tmp_path, name)
    assert [c["model_version"] for c in registered] == ["v1"]
