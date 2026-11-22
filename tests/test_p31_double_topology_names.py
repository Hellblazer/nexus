# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): the client-embedding double names its collections under the tenant's profile model.

A run on the double (``NX_STORAGE_BACKEND_VECTORS`` opted out) used to mint ``minilm-l6-v2-384`` names, which the
engine's profile check refuses at registration. ``double_topology_mints_profile_model`` makes the names carry the
profile's model so the registration passes the check as it stands. Needs no engine: the profile read and the registrar
are fakes.
"""
from __future__ import annotations

import pytest

from nexus import corpus
from nexus.corpus import EmbeddingProfileMismatchError

BGE = "bge-base-en-v15-768"


class _Registrar:
    def __init__(self) -> None:
        self.registered: list[tuple[str, dict]] = []

    def get_collection(self, name):  # noqa: ANN001, ANN201
        return None

    def register_collection(self, name, **kwargs):  # noqa: ANN001, ANN201
        self.registered.append((name, kwargs))

    def close(self) -> None:
        pass


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "local")
    monkeypatch.setattr("nexus.db.local_ef.local_model_token", lambda *a, **k: "minilm-l6-v2-384")
    corpus._REGISTERED_COLLECTIONS.clear()


def test_the_double_without_the_fixture_mints_the_local_embedders_token_the_profile_refuses(double, monkeypatch):
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda ct: BGE)
    name = "code__r-1234abcd__minilm-l6-v2-384__v1"

    with pytest.raises(EmbeddingProfileMismatchError):
        corpus.ensure_collection_registered(name, registrar=_Registrar)


def test_the_fixture_makes_the_double_name_under_the_profile_model_and_the_check_passes(
    double, double_topology_mints_profile_model, monkeypatch,
):
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda ct: BGE)
    registrar = _Registrar()

    assert corpus.effective_embedding_model_for_writes("code") == BGE
    corpus.ensure_collection_registered("code__r-1234abcd__bge-base-en-v15-768__v1", registrar=lambda: registrar)

    ((name, kwargs),) = registrar.registered
    assert kwargs["embedding_model"] == BGE and name.startswith("code__")


def test_the_check_is_not_loosened_a_name_off_the_profile_is_still_refused(
    double, double_topology_mints_profile_model, monkeypatch,
):
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda ct: BGE)

    with pytest.raises(EmbeddingProfileMismatchError):
        corpus.ensure_collection_registered(
            "code__r-1234abcd__minilm-l6-v2-384__v1",
            registrar=_Registrar, kwargs={**corpus.collection_registration_kwargs("code__r-1234abcd__x__v1"),
                                          "embedding_model": "minilm-l6-v2-384"},
        )


def test_a_tenant_with_no_profile_row_gets_the_engines_tier_one_token(
    double, double_topology_mints_profile_model, monkeypatch,
):
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda ct: None)

    assert corpus.effective_embedding_model_for_writes("docs") == BGE


def test_service_mode_does_not_read_the_profile_to_name_a_collection(
    double_topology_mints_profile_model, monkeypatch,
):
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")

    def _never(ct):  # noqa: ANN001, ANN202
        raise AssertionError("service mode derived a name from the engine's profile")

    monkeypatch.setattr(corpus, "_profile_model_for_content_type", _never)

    assert corpus.effective_embedding_model_for_writes("code") == BGE
