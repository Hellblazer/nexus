# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.4-adjacent (nexus-8o9pm) — voyage-capability pre-flight for guided-upgrade.

If the footprint has voyage-model collections, the migration target MUST be a
voyage-capable service (its /version embedding_models lists a voyage-* model) —
voyage collections are NOT cross-model-remapped to bge (re-embedding voyage text
into bge silently changes recall), so a bge-only target cannot serve them. The
existing migration pre-gate blocks them, but LATE and via the client voyage
probe; this gate catches it EARLY with a precise message, keyed on the target
service's ACTUAL capability (server-side, the authoritative signal).
"""

from __future__ import annotations

from nexus.migration.detection import (
    CollectionClassification,
    DetectionReport,
)
from nexus.migration.guided_upgrade import (
    VoyageCapabilityOutcome,
    footprint_has_voyage_collections,
    verify_voyage_capability,
)

_URL = "http://127.0.0.1:8099"


def _cls(collection: str, model: str | None, *, has_data: bool = True) -> CollectionClassification:
    return CollectionClassification(
        collection=collection, leg="local", model=model, dim=None,
        support="unsupported", source_count=12 if has_data else 0, has_data=has_data,
    )


def _report(*classifications: CollectionClassification) -> DetectionReport:
    return DetectionReport(classifications=tuple(classifications))


class _Resp:
    def __init__(self, status_code: int, body) -> None:  # noqa: ANN001
        self.status_code = status_code
        self._body = body

    def json(self):  # noqa: ANN202
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _get_returning(resp_or_exc):  # noqa: ANN001, ANN202
    def _get(url: str, timeout: float):  # noqa: ANN202
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc
    return _get


def _vbody(models: list[str]) -> dict:
    return {"app_version": "1.0-SNAPSHOT", "release_version": "0.1.6",
            "embedding_mode": "voyage", "embedding_models": models}


class TestFootprintHasVoyage:
    def test_voyage_data_bearing_is_true(self) -> None:
        r = _report(_cls("knowledge__o__voyage-context-3__v1", "voyage-context-3"))
        assert footprint_has_voyage_collections(r) is True

    def test_voyage_empty_is_false(self) -> None:
        r = _report(_cls("knowledge__o__voyage-context-3__v1", "voyage-context-3", has_data=False))
        assert footprint_has_voyage_collections(r) is False

    def test_bge_and_minilm_only_is_false(self) -> None:
        r = _report(
            _cls("code__o__bge-base-en-v15-768__v1", "bge-base-en-v15-768"),
            _cls("knowledge__o__minilm-l6-v2-384__v1", "minilm-l6-v2-384"),
        )
        assert footprint_has_voyage_collections(r) is False

    def test_mixed_with_one_voyage_is_true(self) -> None:
        r = _report(
            _cls("knowledge__o__minilm-l6-v2-384__v1", "minilm-l6-v2-384"),
            _cls("knowledge__o__voyage-code-3__v1", "voyage-code-3"),
        )
        assert footprint_has_voyage_collections(r) is True

    def test_nonconformant_none_model_is_false(self) -> None:
        assert footprint_has_voyage_collections(_report(_cls("legacy", None))) is False

    def test_non_canonical_voyage_name_does_not_misfire(self) -> None:
        # A voyage-*-PREFIXED but non-canonical model (not in _VOYAGE_MODELS) is
        # an unrecognized model, not a voyage-capability problem — the classifier
        # gives it the re-index diagnostic; this gate must NOT fire on it.
        r = _report(_cls("knowledge__o__voyage-future-9__v1", "voyage-future-9"))
        assert footprint_has_voyage_collections(r) is False


class TestVerifyVoyageCapability:
    def test_service_with_voyage_is_capable(self) -> None:
        out = verify_voyage_capability(
            _URL, http_get=_get_returning(_Resp(200, _vbody(["voyage-context-3", "voyage-3"]))))
        assert isinstance(out, VoyageCapabilityOutcome)
        assert out.ok is True
        assert out.reason is None

    def test_bge_only_service_is_not_capable(self) -> None:
        out = verify_voyage_capability(
            _URL, http_get=_get_returning(_Resp(200, _vbody(["bge-base-en-v15-768"]))))
        assert out.ok is False
        assert "voyage" in out.reason and "bge-base-en-v15-768" in out.reason

    def test_empty_models_is_not_capable(self) -> None:
        out = verify_voyage_capability(
            _URL, http_get=_get_returning(_Resp(200, _vbody([]))))
        assert out.ok is False

    def test_missing_models_field_is_not_capable(self) -> None:
        out = verify_voyage_capability(
            _URL, http_get=_get_returning(_Resp(200, {"app_version": "x"})))
        assert out.ok is False

    def test_non_200_fails_closed(self) -> None:
        out = verify_voyage_capability(
            _URL, http_get=_get_returning(_Resp(503, {})))
        assert out.ok is False

    def test_transport_error_fails_closed(self) -> None:
        out = verify_voyage_capability(
            _URL, http_get=_get_returning(ConnectionError("refused")))
        assert out.ok is False
        assert "refused" in out.reason

    def test_probes_version_endpoint(self) -> None:
        seen: list[str] = []

        def _get(url: str, timeout: float):  # noqa: ANN202
            seen.append(url)
            return _Resp(200, _vbody(["voyage-3"]))

        verify_voyage_capability(_URL, http_get=_get)
        assert seen == [f"{_URL}/version"]
