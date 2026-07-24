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

# RDR-155 P4b: footprint_has_voyage_collections (and the detection
# footprint it consumed) died with the migration machinery; only the
# capability probe — rehomed to the surviving provisioning module
# (P0e, D-C) — keeps its tests.
from nexus.upgrade_ladder.provisioning import (
    VoyageCapabilityOutcome,
    verify_voyage_capability,
)

_URL = "http://127.0.0.1:8099"


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
