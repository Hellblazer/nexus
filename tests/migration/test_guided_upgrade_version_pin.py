# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.4 — RDR-002 version-pin for ``nx guided-upgrade``.

Asserts the engine-service is at or above the required release by pinning on
the dedicated ``release_version`` field (RDR-002 contract; conexus PR #78).
FAIL-CLOSED on every uncertain outcome — transport error, non-200,
absent/null/dev/SNAPSHOT release_version (an engine predating the field is
older than the floor), unparseable, or below the floor.

The floor itself (:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION`) and
its parser (:func:`nexus.engine_version.parse_engine_version`) are unified
single-source-of-truth (nexus-b6qlf) — the pinned-value assertion and parser
unit tests live in ``tests/test_engine_version.py`` ONLY. This file exercises
``verify_service_version``'s own behavior (HTTP/probe wiring, fail-closed
branches), not the pin.
"""

from __future__ import annotations

from nexus.engine_version import REQUIRED_ENGINE_VERSION
from nexus.migration.guided_upgrade import VersionPinOutcome, verify_service_version

_URL = "http://127.0.0.1:8099"
_FLOOR = ".".join(str(n) for n in REQUIRED_ENGINE_VERSION)
_BELOW_FLOOR = "0.1.5"


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


def _version_body(release_version, app_version="1.0-SNAPSHOT"):  # noqa: ANN001, ANN202
    return {
        "app_version": app_version,
        "release_version": release_version,
        "embedding_mode": "onnx-local",
        "schema_latest_id": "vectors-002",
        "schema_changeset_count": 64,
    }


class TestVerifyServiceVersion:
    def test_uses_required_engine_version_as_default_floor(self) -> None:
        # verify_service_version's default `required` param is wired to the
        # canonical nexus.engine_version floor — not a local duplicate.
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body(_FLOOR)))
        )
        assert isinstance(out, VersionPinOutcome)
        assert out.ok is True
        assert out.reason is None

    def test_above_floor_passes(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body("99.0.0")))
        )
        assert out.ok is True

    def test_below_floor_fails_closed(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body(_BELOW_FLOOR)))
        )
        assert out.ok is False
        assert _BELOW_FLOOR in out.reason and _FLOOR in out.reason

    def test_null_release_version_fails_closed(self) -> None:
        # An engine predating the release_version field reports null → older
        # than the floor by definition.
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body(None)))
        )
        assert out.ok is False
        assert "release_version" in out.reason

    def test_snapshot_release_version_fails_closed(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body("1.0-SNAPSHOT")))
        )
        assert out.ok is False

    def test_non_200_fails_closed(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(503, {"status": "error"}))
        )
        assert out.ok is False
        assert "503" in out.reason

    def test_transport_error_fails_closed(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(ConnectionError("connection refused"))
        )
        assert out.ok is False
        assert "refused" in out.reason

    def test_non_json_body_fails_closed(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, ValueError("not json")))
        )
        assert out.ok is False

    def test_probes_the_version_endpoint(self) -> None:
        seen: list[str] = []

        def _get(url: str, timeout: float):  # noqa: ANN202
            seen.append(url)
            return _Resp(200, _version_body(_BELOW_FLOOR))

        verify_service_version(_URL, http_get=_get)
        assert seen == [f"{_URL}/version"]
