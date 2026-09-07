# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-204 Phase 1 item 10 (nexus-ft04v.34).

``_CatalogBackedRegistry.update`` used to hardcode
``embedding_model="voyage-context-3"`` in its ``register_collection``
call. On a local-mode install (profile bge-768) that registration
starts 422ing the moment the engine enforces profile-vs-write-model
agreement (nexus-ft04v.8). The fix routes the model through
``nexus.corpus.effective_embedding_model_for_writes(ct)`` instead,
which already agrees with the engine's profile by construction.

These tests assert the adapter sends WHATEVER
``effective_embedding_model_for_writes`` returns for the derived
content type — not a second hardcoded literal — under both local and
cloud mode, and that a plain fake writer (no profile knowledge, i.e.
a pre-Phase-1 engine double) still succeeds.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.commands.index import _CatalogBackedRegistry
from nexus.corpus import effective_embedding_model_for_writes


def _adapter(tmp_path: Path) -> tuple[_CatalogBackedRegistry, MagicMock]:
    cat = MagicMock()
    cat.ensure_owner_for_repo.return_value = "1.1"
    adapter = _CatalogBackedRegistry(
        cat=cat, registry_path=tmp_path / "repos.json",
    )
    return adapter, cat


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "myrepo"
    r.mkdir()
    return r


def test_local_mode_sends_bge_not_voyage_context_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local mode (service-vector, the 6.0+ default): the registration
    must send the bge token the engine actually embeds with, never the
    voyage-context-3 literal the pre-fix code hardcoded."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    adapter, cat = _adapter(tmp_path)
    repo = _repo(tmp_path)
    new_name = "docs__myrepo-1-1__bge-base-en-v15-768__v1"

    ok = adapter.update(repo, docs_collection=new_name)

    assert ok is True
    assert cat.register_collection.called
    sent_model = cat.register_collection.call_args.kwargs["embedding_model"]
    assert sent_model != "voyage-context-3"
    assert sent_model == effective_embedding_model_for_writes("docs")


@pytest.mark.parametrize(
    ("content_type", "collection_prefix"),
    [
        ("docs", "docs"),
        ("code", "code"),
    ],
)
def test_cloud_mode_sends_content_type_appropriate_voyage_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    collection_prefix: str,
) -> None:
    """Cloud mode: docs -> voyage-context-3, code -> voyage-code-3."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    adapter, cat = _adapter(tmp_path)
    repo = _repo(tmp_path)
    new_name = f"{collection_prefix}__myrepo-1-1__voyage-placeholder__v1"

    ok = adapter.update(repo, docs_collection=new_name)

    assert ok is True
    sent_model = cat.register_collection.call_args.kwargs["embedding_model"]
    assert sent_model == effective_embedding_model_for_writes(content_type)


def test_sent_model_equals_effective_embedding_model_for_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The value sent to register_collection must equal what
    effective_embedding_model_for_writes(ct) returns for the SAME ct
    under the SAME patched mode — asserted against the function
    itself, not a second hardcoded expectation, per the bead's test
    spec."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    adapter, cat = _adapter(tmp_path)
    repo = _repo(tmp_path)
    ct = "knowledge"
    new_name = f"{ct}__myrepo-1-1__bge-base-en-v15-768__v1"

    adapter.update(repo, docs_collection=new_name)

    expected = effective_embedding_model_for_writes(ct)
    sent_model = cat.register_collection.call_args.kwargs["embedding_model"]
    assert sent_model == expected


def test_plain_fake_writer_with_no_profile_knowledge_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against a writer double with no profile-aware validation (the
    pre-Phase-1 engine shape — it accepts whatever embedding_model is
    handed to it), the registration path still succeeds end to end."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    adapter, cat = _adapter(tmp_path)
    repo = _repo(tmp_path)
    new_name = "docs__myrepo-1-1__voyage-context-3__v1"

    ok = adapter.update(repo, docs_collection=new_name)

    assert ok is True
    assert cat.register_collection.called
