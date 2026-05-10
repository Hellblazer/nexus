# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-59vl (GH #667): local-ONNX collections must be named and
metadata-stamped with a local-EF token, not ``voyage-*``. The fix
also makes ``_embedding_fn`` name-aware so a local-mode-built
collection stays queryable after the user adds cloud credentials
(the mode-flip hazard that the original issue surfaced).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    LOCAL_EMBEDDING_MODELS,
    canonical_embedding_model,
    current_local_embedding_model_token,
    effective_embedding_model_for_writes,
    voyage_model_for_collection,
)


# ── Token registry ─────────────────────────────────────────────────────────


class TestCanonicalSet:
    def test_local_tokens_are_in_canonical_set(self) -> None:
        """The catalog guards (``catalog_docs.py``,
        ``collection_name.py``) reject any embedding-model segment
        not in ``CANONICAL_EMBEDDING_MODELS``. Local tokens MUST
        appear there or local-mode catalog registrations will raise.
        """
        assert "minilm-l6-v2-384" in CANONICAL_EMBEDDING_MODELS
        assert "bge-base-en-v15-768" in CANONICAL_EMBEDDING_MODELS
        # And the cloud tokens are still there.
        assert "voyage-context-3" in CANONICAL_EMBEDDING_MODELS
        assert "voyage-code-3" in CANONICAL_EMBEDDING_MODELS

    def test_local_set_is_disjoint_from_cloud_set(self) -> None:
        """A name's segment must unambiguously identify ONE EF; local
        vs cloud must never overlap.
        """
        cloud = {"voyage-context-3", "voyage-code-3"}
        assert LOCAL_EMBEDDING_MODELS.isdisjoint(cloud)


# ── current_local_embedding_model_token ──────────────────────────────────


class TestCurrentLocalToken:
    def test_returns_minilm_when_fastembed_unavailable(self) -> None:
        """Without fastembed the LocalEmbeddingFunction default is
        MiniLM (384d); the token must reflect that exact dim so a
        future fastembed install can't silently re-target writes.
        """
        with patch(
            "nexus.db.local_ef._fastembed_available", return_value=False,
        ):
            token = current_local_embedding_model_token()
        assert token == "minilm-l6-v2-384"

    def test_returns_bge_when_fastembed_available(self) -> None:
        with patch(
            "nexus.db.local_ef._fastembed_available", return_value=True,
        ):
            token = current_local_embedding_model_token()
        assert token == "bge-base-en-v15-768"


# ── canonical_embedding_model + effective_embedding_model_for_writes ─────


class TestCanonicalEmbeddingModelStable:
    """``canonical_embedding_model`` is the schema-level function and
    is INTENTIONALLY mode-agnostic. Always returns the cloud canonical
    token. Existing tests / docs / rendering all depend on this
    contract; the mode-aware variant is a separate function below.
    """

    def test_returns_voyage_code_for_code(self) -> None:
        assert canonical_embedding_model("code") == "voyage-code-3"

    def test_returns_voyage_context_for_docs(self) -> None:
        assert canonical_embedding_model("docs") == "voyage-context-3"
        assert canonical_embedding_model("rdr") == "voyage-context-3"
        assert canonical_embedding_model("knowledge") == "voyage-context-3"

    def test_unknown_content_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown content_type"):
            canonical_embedding_model("nonsense")


class TestEffectiveModelForWritesIsModeAware:
    """nexus-59vl primary fix: the WRITE-PATH function consults
    ``is_local_mode()`` and returns the local-EF token in local mode.
    Pre-fix every write site called ``canonical_embedding_model``
    directly and produced ``...__voyage-*__v1`` names even in local
    mode where the bytes were 384d MiniLM.
    """

    def test_cloud_mode_delegates_to_canonical(self) -> None:
        with patch("nexus.config.is_local_mode", return_value=False):
            assert effective_embedding_model_for_writes("code") == "voyage-code-3"
            assert effective_embedding_model_for_writes("docs") == "voyage-context-3"
            assert effective_embedding_model_for_writes("rdr") == "voyage-context-3"
            assert effective_embedding_model_for_writes("knowledge") == "voyage-context-3"

    def test_local_mode_returns_local_token_for_every_content_type(self) -> None:
        """Reverting the mode-aware branch makes this fail with
        ``voyage-*`` strings.
        """
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.local_ef._fastembed_available", return_value=False,
        ):
            for ct in ("code", "docs", "rdr", "knowledge"):
                assert effective_embedding_model_for_writes(ct) == "minilm-l6-v2-384", (
                    f"local-mode {ct!r} must use the local-EF token"
                )

    def test_unknown_content_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            effective_embedding_model_for_writes("nonsense")


# ── voyage_model_for_collection (now name-aware) ─────────────────────────


class TestNameAwareModelLookup:
    def test_conformant_local_name_returns_local_token(self) -> None:
        """nexus-59vl mode-flip safety: a collection with a local-EF
        token in its name segment returns that token even when
        consulted from cloud mode (no ``is_local_mode()`` here).
        Pre-fix the function unconditionally returned ``voyage-*``
        based on prefix.
        """
        name = "knowledge__foo__minilm-l6-v2-384__v1"
        assert voyage_model_for_collection(name) == "minilm-l6-v2-384"

    def test_conformant_voyage_name_returns_voyage_token(self) -> None:
        name = "knowledge__foo__voyage-context-3__v1"
        assert voyage_model_for_collection(name) == "voyage-context-3"

    def test_legacy_prefix_fallback_unchanged(self) -> None:
        """Pre-RDR-103 collection names (no embedding-model segment)
        still dispatch off the prefix to keep historical reads working.
        """
        assert voyage_model_for_collection("docs__legacy") == "voyage-context-3"
        assert voyage_model_for_collection("knowledge__legacy") == "voyage-context-3"
        assert voyage_model_for_collection("rdr__legacy") == "voyage-context-3"
        assert voyage_model_for_collection("code__legacy") == "voyage-code-3"


# ── _embedding_fn dispatches on the name (mode-flip safety) ──────────────


class TestEmbeddingFnNameAware:
    """The post-fix ``_embedding_fn`` reads the collection name's
    embedding-model segment and builds ``LocalEmbeddingFunction`` for
    local-EF tokens, even when the process is in cloud mode. This is
    the load-bearing guarantee that prevents a local-mode-built
    collection from becoming unqueryable on cloud-credentials add.
    """

    def test_cloud_mode_uses_local_ef_for_local_named_collection(
        self,
    ) -> None:
        """Build a T3Database in cloud mode (no local_mode flag),
        request the EF for a local-named collection, and assert the
        cached EF is a LocalEmbeddingFunction. Pre-fix this always
        returned a VoyageAIEmbeddingFunction and dim-mismatched at
        query time.
        """
        from nexus.db.local_ef import LocalEmbeddingFunction
        from nexus.db.t3 import T3Database

        # Cloud-mode T3Database: bypass the constructor's auto-mode
        # detection by passing a fake voyage_api_key so it's not in
        # local_mode. The _embedding_fn dispatch is what we're
        # testing; a real chroma client isn't needed to inspect the
        # cache.
        t3 = T3Database.__new__(T3Database)
        t3._ef_override = None
        t3._ef_cache = {}
        import threading
        t3._ef_lock = threading.Lock()
        t3._local_mode = False
        t3._voyage_api_key = "test-voyage-key"

        ef = t3._embedding_fn("knowledge__test__minilm-l6-v2-384__v1")
        assert isinstance(ef, LocalEmbeddingFunction)
        assert ef.model_name == "all-MiniLM-L6-v2"
        assert ef.dimensions == 384

    def test_cloud_mode_still_uses_voyage_for_voyage_named_collection(
        self,
    ) -> None:
        """Regression guard: cloud-mode dispatch for a voyage-named
        collection must STILL build VoyageAIEmbeddingFunction, not
        accidentally fall through to the local branch.
        """
        from nexus.db.t3 import T3Database

        t3 = T3Database.__new__(T3Database)
        t3._ef_override = None
        t3._ef_cache = {}
        import threading
        t3._ef_lock = threading.Lock()
        t3._local_mode = False
        t3._voyage_api_key = "test-voyage-key"

        ef = t3._embedding_fn("knowledge__test__voyage-context-3__v1")
        # Voyage EF class name varies across chromadb versions; the
        # robust check is "not LocalEmbeddingFunction".
        from nexus.db.local_ef import LocalEmbeddingFunction
        assert not isinstance(ef, LocalEmbeddingFunction)

    def test_local_mode_uses_local_ef_for_legacy_named_collection(
        self,
    ) -> None:
        """A pre-RDR-103 legacy name (no embedding-model segment)
        in local mode still gets LocalEmbeddingFunction.
        """
        from nexus.db.local_ef import LocalEmbeddingFunction
        from nexus.db.t3 import T3Database

        t3 = T3Database.__new__(T3Database)
        t3._ef_override = None
        t3._ef_cache = {}
        import threading
        t3._ef_lock = threading.Lock()
        t3._local_mode = True
        t3._voyage_api_key = ""

        ef = t3._embedding_fn("knowledge__legacy")
        assert isinstance(ef, LocalEmbeddingFunction)
