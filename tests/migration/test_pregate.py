# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P1d.T (nexus-ue6g7.11) — the per-collection-model pre-gate +
idempotent provisioning + fresh-user no-op.

RDR-159 §Approach P1 + §"Pre-gate tests". Before any ETL call, the migration
classifies every data-bearing collection's embedding model against the LIVE
service ``EmbedderRouter`` registry (the ``/version`` handshake's
``embedding_models``), NOT a static onnx-vs-voyage assumption. The live set is a
belt-and-suspenders confirmation over the P0 pure ``wired_models`` function — it
falls back to the pure deployment-mode floor only when the service is
unreachable, never to something weaker.

Gate behaviours (locked):

* (a) unsupported model (bge-768) → BLOCK with a re-index diagnostic listing the
  affected collections, BEFORE any ETL call;
* (b) voyage-model collection while the service has no voyage embedder wired →
  HARD-FAIL before any ETL (credential diagnostic);
* (c) onnx-384 + no voyage key → PROCEEDS (onnx is wired in every mode);
* (d) MIXED store → the gate fires only on the voyage subset; onnx collections
  pass.

Plus: idempotent service-stack provisioning (no-op when already up); a fresh
user with no Chroma footprint → whole flow is a no-op success.

The pre-gate consumes a ``WiredModelSource`` by injection, so these tests pin
the gate logic without a running service.
"""
from __future__ import annotations

import pytest

from nexus.migration.detection import CollectionClassification
from nexus.migration.pregate import (
    LiveServiceWiredModels,
    ModelPreGateBlocked,
    WiredModelSource,
    assert_models_supported,
    ensure_service_stack,
    is_fresh_user,
    resolve_wired_models,
)

_ONNX = "minilm-l6-v2-384"
_VOYAGE = "voyage-context-3"
_BGE = "bge-base-en-v15-768"

_WIRED_ONNX_ONLY = frozenset({_ONNX})
_WIRED_WITH_VOYAGE = frozenset({_ONNX, _VOYAGE, "voyage-code-3", "voyage-3"})


class _FixedSource:
    """A ``WiredModelSource`` returning a fixed set (or None = unreachable)."""

    def __init__(self, wired: frozenset[str] | None) -> None:
        self._wired = wired

    def wired_models(self) -> frozenset[str] | None:
        return self._wired


def _cls(
    collection: str, model: str | None, *, has_data: bool = True, leg: str = "local"
) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model=model,
        dim=None,
        support="unsupported",  # stored value is IGNORED — gate re-resolves live
        source_count=1 if has_data else 0,
        has_data=has_data,
        reason="",
    )


# --------------------------------------------------------------------------
# resolve_wired_models — live when reachable, pure floor when not
# --------------------------------------------------------------------------


def test_resolve_uses_live_set_when_reachable() -> None:
    got = resolve_wired_models(
        _FixedSource(_WIRED_WITH_VOYAGE), voyage_key_present=False
    )
    assert got == _WIRED_WITH_VOYAGE  # live wins over the client-side key flag


def test_resolve_falls_back_to_pure_floor_when_unreachable() -> None:
    # Live source returns None (service down) → pure deployment-mode floor.
    assert resolve_wired_models(_FixedSource(None), voyage_key_present=False) == frozenset(
        {_ONNX}
    )
    assert resolve_wired_models(_FixedSource(None), voyage_key_present=True) == frozenset(
        {_ONNX, _VOYAGE, "voyage-code-3", "voyage-3"}
    )


# --------------------------------------------------------------------------
# Gate behaviours (a)-(d)
# --------------------------------------------------------------------------


def test_b_voyage_collection_blocks_when_service_has_no_voyage() -> None:
    etl_called = False
    classifications = [_cls("knowledge__art__voyage-context-3__v1", _VOYAGE)]
    with pytest.raises(ModelPreGateBlocked) as exc:
        assert_models_supported(
            classifications,
            voyage_key_present=False,
            source=_FixedSource(_WIRED_ONNX_ONLY),
        )
        etl_called = True  # unreachable — gate raised first
    assert etl_called is False  # BLOCK is BEFORE any ETL call
    assert exc.value.collections == ["knowledge__art__voyage-context-3__v1"]
    assert "NX_VOYAGE_API_KEY" in str(exc.value)


def test_c_onnx_collection_proceeds_without_key() -> None:
    classifications = [_cls("code__nexus__minilm-l6-v2-384__v1", _ONNX)]
    # Must NOT raise — onnx is wired in every mode.
    assert_models_supported(
        classifications,
        voyage_key_present=False,
        source=_FixedSource(_WIRED_ONNX_ONLY),
    )


def test_a_unsupported_bge_blocks_with_reindex_diagnostic() -> None:
    classifications = [_cls("docs__legacy__bge-base-en-v15-768__v1", _BGE)]
    with pytest.raises(ModelPreGateBlocked) as exc:
        assert_models_supported(
            classifications,
            voyage_key_present=True,
            source=_FixedSource(_WIRED_WITH_VOYAGE),
        )
    assert exc.value.collections == ["docs__legacy__bge-base-en-v15-768__v1"]
    assert "re-index" in str(exc.value).lower()
    assert "NX_VOYAGE_API_KEY" not in str(exc.value)  # not a credential issue


def test_d_mixed_store_blocks_only_voyage_subset() -> None:
    classifications = [
        _cls("code__nexus__minilm-l6-v2-384__v1", _ONNX),  # supported
        _cls("knowledge__art__voyage-context-3__v1", _VOYAGE),  # unsupported here
        _cls("docs__x__bge-base-en-v15-768__v1", _BGE),  # unsupported (re-index)
    ]
    with pytest.raises(ModelPreGateBlocked) as exc:
        assert_models_supported(
            classifications,
            voyage_key_present=False,
            source=_FixedSource(_WIRED_ONNX_ONLY),
        )
    blocked = exc.value.collections
    assert "code__nexus__minilm-l6-v2-384__v1" not in blocked  # onnx proceeds
    assert "knowledge__art__voyage-context-3__v1" in blocked
    assert "docs__x__bge-base-en-v15-768__v1" in blocked
    assert len(blocked) == 2


def test_empty_collections_are_not_gated() -> None:
    # A voyage collection with NO data must not block — nothing to migrate.
    classifications = [
        _cls("knowledge__art__voyage-context-3__v1", _VOYAGE, has_data=False)
    ]
    assert_models_supported(
        classifications,
        voyage_key_present=False,
        source=_FixedSource(_WIRED_ONNX_ONLY),
    )


def test_all_supported_mixed_proceeds() -> None:
    classifications = [
        _cls("code__nexus__minilm-l6-v2-384__v1", _ONNX),
        _cls("knowledge__art__voyage-context-3__v1", _VOYAGE),
    ]
    # Service HAS voyage wired → both supported → no raise.
    assert_models_supported(
        classifications,
        voyage_key_present=True,
        source=_FixedSource(_WIRED_WITH_VOYAGE),
    )


# --------------------------------------------------------------------------
# LiveServiceWiredModels — unreachable degrades to None (→ pure floor)
# --------------------------------------------------------------------------


def test_live_source_returns_none_when_service_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> tuple[str, int, str]:
        raise RuntimeError("no service lease")

    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_config", _boom
    )
    assert LiveServiceWiredModels().wired_models() is None


def test_live_source_parses_version_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_config",
        lambda: ("127.0.0.1", 9999, "tok"),
    )
    monkeypatch.setattr(
        "nexus.daemon.jar_lifecycle.fetch_service_version",
        lambda host, port: {"embedding_mode": "cloud", "embedding_models": [_ONNX, _VOYAGE]},
    )
    assert LiveServiceWiredModels().wired_models() == frozenset({_ONNX, _VOYAGE})


# --------------------------------------------------------------------------
# Idempotent provisioning
# --------------------------------------------------------------------------


def test_ensure_service_stack_noop_when_up() -> None:
    started = []
    changed = ensure_service_stack(
        is_up=lambda: True, start=lambda: started.append(1)
    )
    assert changed is False
    assert started == []  # never started a stack that was already up


def test_ensure_service_stack_starts_when_down() -> None:
    started = []
    changed = ensure_service_stack(
        is_up=lambda: False, start=lambda: started.append(1)
    )
    assert changed is True
    assert started == [1]


# --------------------------------------------------------------------------
# Fresh-user no-op
# --------------------------------------------------------------------------


def test_is_fresh_user_true_when_no_data() -> None:
    from nexus.migration.detection import DetectionReport

    empty = DetectionReport(classifications=())
    only_empty = DetectionReport(
        classifications=(_cls("code__x__minilm-l6-v2-384__v1", _ONNX, has_data=False),)
    )
    assert is_fresh_user(empty) is True
    assert is_fresh_user(only_empty) is True  # no data-bearing legs


def test_is_fresh_user_false_when_data_present() -> None:
    from nexus.migration.detection import DetectionReport

    report = DetectionReport(
        classifications=(_cls("code__x__minilm-l6-v2-384__v1", _ONNX),)
    )
    assert is_fresh_user(report) is False


def test_wired_model_source_is_a_protocol() -> None:
    # _FixedSource structurally satisfies the protocol without inheritance.
    src: WiredModelSource = _FixedSource(_WIRED_ONNX_ONLY)
    assert src.wired_models() == _WIRED_ONNX_ONLY
