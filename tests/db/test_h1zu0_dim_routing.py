# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h1zu0: the ONE canonical model-token -> pgvector-dim routing table.

``nexus.db.reconcile.dim_for_model_token`` is promoted from the
pre-existing private ``_MODEL_DIMS`` (already documented as mirroring the
Java authority ``PgVectorRepository.MODEL_DIMS``) into a public function so
both the doctor dangling-manifest census (``nexus.health``) and the new
``nx catalog manifest-verify --list`` enumeration route collections to a
dim the exact same way — the fix for the census/enumeration model-token
routing divergence the bead names (client-side; see the module docstring
in ``nexus/db/reconcile.py`` for why an engine-side SQL change was not
chosen).

Unlike ``nexus.corpus``'s ``CANONICAL_EMBEDDING_MODELS``/
``LOCAL_EMBEDDING_MODELS`` pair (a collection-NAMING-policy registry that
deliberately omits the legacy ``voyage-3`` token), this table answers a
storage-ROUTING question and must recognize every token the engine's
``nexus.manifest_orphans(dim)`` IN-lists recognize — ``voyage-3`` included.
"""
from __future__ import annotations

from nexus.db.reconcile import dim_for_model_token


class TestDimForModelToken:
    def test_voyage_code_3_routes_to_1024(self) -> None:
        assert dim_for_model_token("voyage-code-3") == 1024

    def test_voyage_context_3_routes_to_1024(self) -> None:
        assert dim_for_model_token("voyage-context-3") == 1024

    def test_legacy_voyage_3_routes_to_1024(self) -> None:
        """The token corpus.py's CANONICAL_EMBEDDING_MODELS deliberately
        omits — this routing table must still recognize it (it is in the
        engine's manifest_orphans(1024) IN-list)."""
        assert dim_for_model_token("voyage-3") == 1024

    def test_bge_base_en_v15_768_routes_to_768(self) -> None:
        assert dim_for_model_token("bge-base-en-v15-768") == 768

    def test_minilm_l6_v2_384_routes_to_384(self) -> None:
        assert dim_for_model_token("minilm-l6-v2-384") == 384

    def test_unknown_token_returns_none_never_guesses(self) -> None:
        assert dim_for_model_token("some-future-model-9000") is None

    def test_empty_token_returns_none(self) -> None:
        assert dim_for_model_token("") is None
