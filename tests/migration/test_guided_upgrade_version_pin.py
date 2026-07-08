# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.4 — RDR-002 version-pin for ``nx guided-upgrade``.

Asserts the engine-service is at or above the required release (>= v0.1.8) by
pinning on the dedicated ``release_version`` field (RDR-002 contract; conexus
PR #78). FAIL-CLOSED on every uncertain outcome — transport error, non-200,
absent/null/dev/SNAPSHOT release_version (an engine predating the field is
older than the floor), unparseable, or below the floor.
"""

from __future__ import annotations

from nexus.migration.guided_upgrade import (
    REQUIRED_RELEASE_VERSION,
    VersionPinOutcome,
    _parse_semver,
    verify_service_version,
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


def _version_body(release_version, app_version="1.0-SNAPSHOT"):  # noqa: ANN001, ANN202
    return {
        "app_version": app_version,
        "release_version": release_version,
        "embedding_mode": "onnx-local",
        "schema_latest_id": "vectors-002",
        "schema_changeset_count": 64,
    }


class TestParseSemver:
    def test_parses_plain_and_v_prefixed(self) -> None:
        assert _parse_semver("0.1.5") == (0, 1, 5)
        assert _parse_semver("v1.2.3") == (1, 2, 3)

    def test_rejects_dev_snapshot_blank_and_malformed(self) -> None:
        for bad in (None, "", "  ", "1.0-SNAPSHOT", "0.1.6-dev", "0.1", "1.2.3.4", "x.y.z"):
            assert _parse_semver(bad) is None


class TestVerifyServiceVersion:
    def test_required_floor_is_0134(self) -> None:
        # (0,1,5)->(0,1,8) for nexus-x2g1z; ->(0,1,34) for 6.4.1: the client
        # hard-requires catalog-012 (graph-hop `where` — pre-012 engines
        # silently ignore the key, the H2 version-skew failure class) and
        # catalog-013-1b (pre-1b engines fail boot VALIDATE on tenants with
        # legacy 64-char chash rows — the nexus-1wjmq incident).
        assert REQUIRED_RELEASE_VERSION == (0, 1, 34)

    def test_at_floor_passes(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body("0.1.34")))
        )
        assert isinstance(out, VersionPinOutcome)
        assert out.ok is True
        assert out.reason is None

    def test_above_floor_passes(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body("0.2.0")))
        )
        assert out.ok is True

    def test_below_floor_fails_closed(self) -> None:
        out = verify_service_version(
            _URL, http_get=_get_returning(_Resp(200, _version_body("0.1.5")))
        )
        assert out.ok is False
        assert "0.1.5" in out.reason and "0.1.34" in out.reason

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
            return _Resp(200, _version_body("0.1.5"))

        verify_service_version(_URL, http_get=_get)
        assert seen == [f"{_URL}/version"]
