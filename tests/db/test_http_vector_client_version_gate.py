# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Cloud-mode fail-loud version-probe gate for get_http_vector_client (nexus-jn0nm).

nexus-b6qlf: cloud-mode clients previously got ZERO version-compatibility
check on the actual connection path -- probe_managed_service() was only ever
invoked from nx init/nx doctor/nx service probe, never from
get_http_vector_client() construction. A too-old cloud engine degraded
silently. This suite pins the fix: the probe runs once per process in cloud
mode, a pass is cached forever, an INCOMPATIBLE failure is cached and
re-raised verbatim on every subsequent call (never re-probed), an
UNREACHABLE failure is authoritative only within the nexus-5t1jp retry
window (then re-probed, so the session heals), and local mode is provably
untouched.
"""
from __future__ import annotations

import traceback
from unittest.mock import MagicMock

import pytest

import nexus.db.http_vector_client as hvc
from nexus.db.http_vector_client import (
    _PROBE_UNREACHABLE_RETRY_S,
    HttpVectorClient,
    _cloud_probe_failure_message,
    get_http_vector_client,
    reset_http_vector_client_for_tests,
)
from nexus.db.managed_endpoint import (
    ManagedCapabilities,
    ManagedServiceIncompatible,
    ManagedServiceUnreachable,
    probe_managed_service,
)


from nexus.engine_version import REQUIRED_ENGINE_VERSION as _FLOOR_TUPLE

#: The current floor as a string — these tests assert the DIAGNOSTIC text
#: carries the real floor, whatever it is; hard-coding the literal made every
#: floor bump fail CI here (v6.9.0 release, 2026-07-14).
_FLOOR_STR = ".".join(str(p) for p in _FLOOR_TUPLE)

def _caps() -> ManagedCapabilities:
    return ManagedCapabilities(
        base_url="https://api.conexus-nexus.com",
        app_version="1.0-SNAPSHOT",
        release_version="0.1.99",
        embedding_mode="voyage",
        embedding_models=["voyage-context-3"],
        schema_latest_id="latest",
        schema_changeset_count=42,
    )


@pytest.fixture(autouse=True)
def _reset_singleton_and_probe_cache():
    reset_http_vector_client_for_tests()
    yield
    reset_http_vector_client_for_tests()


class TestCloudModeCompatible:
    def test_probe_called_exactly_once_across_multiple_calls(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(return_value=_caps())
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        first = get_http_vector_client()
        second = get_http_vector_client()
        third = get_http_vector_client()

        assert isinstance(first, HttpVectorClient)
        assert first is second is third
        assert probe.call_count == 1


class TestCloudModeIncompatible:
    def test_first_call_raises_with_cloud_specific_no_local_remedy_message(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(
            side_effect=ManagedServiceIncompatible(
                "managed nexus service at https://api.conexus-nexus.com is "
                "release_version '0.1.8', below the minimum this client "
                f"supports (v{_FLOOR_STR})."
            )
        )
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        with pytest.raises(ManagedServiceIncompatible) as exc_info:
            get_http_vector_client()

        message = str(exc_info.value).lower()
        assert "cannot be fixed locally" in message
        assert "service operator" in message
        assert "upgrade the engine" not in message
        assert probe.call_count == 1

    def test_subsequent_calls_reraise_cached_error_without_reprobing(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceIncompatible("stale engine"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        with pytest.raises(ManagedServiceIncompatible) as first_exc:
            get_http_vector_client()
        with pytest.raises(ManagedServiceIncompatible) as second_exc:
            get_http_vector_client()
        with pytest.raises(ManagedServiceIncompatible) as third_exc:
            get_http_vector_client()

        assert str(first_exc.value) == str(second_exc.value) == str(third_exc.value)
        assert probe.call_count == 1  # never re-probed once the outcome is cached

    def test_unreachable_error_cached_WITHIN_the_retry_window_only(self, monkeypatch):
        """nexus-5t1jp revises the original pin here: unreachable is cached,
        but only inside _PROBE_UNREACHABLE_RETRY_S — not for the process
        lifetime. This test holds the clock still and asserts the cheap
        cached re-raise (probe called once); the healing behavior past the
        window is TestUnreachableRetryWindow below."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceUnreachable("connection refused"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)
        monkeypatch.setattr("nexus.db.http_vector_client._monotonic", lambda: 1000.0)

        with pytest.raises(ManagedServiceUnreachable) as first_exc:
            get_http_vector_client()
        with pytest.raises(ManagedServiceUnreachable) as second_exc:
            get_http_vector_client()

        assert str(first_exc.value) == str(second_exc.value)
        assert probe.call_count == 1

    def test_below_floor_stays_cached_even_after_the_retry_window(self, monkeypatch):
        """The class split's other half: INCOMPATIBLE never re-probes, no
        matter how much time passes — the deployed engine does not change
        under a running client (nexus-b6qlf). Mutation check: making the
        incompatible class retryable turns this red."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceIncompatible("stale engine"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)
        clock = {"now": 1000.0}
        monkeypatch.setattr("nexus.db.http_vector_client._monotonic", lambda: clock["now"])

        with pytest.raises(ManagedServiceIncompatible):
            get_http_vector_client()
        clock["now"] += 3600.0  # far beyond any retry window
        with pytest.raises(ManagedServiceIncompatible):
            get_http_vector_client()

        assert probe.call_count == 1


class TestUnreachableRetryWindow:
    """nexus-5t1jp: the session HEALS when the managed service comes back."""

    def test_reprobe_after_window_heals_and_stops_probing(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        outcomes = [ManagedServiceUnreachable("connection refused"), _caps(), _caps()]
        probe = MagicMock(side_effect=outcomes)
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)
        clock = {"now": 1000.0}
        monkeypatch.setattr("nexus.db.http_vector_client._monotonic", lambda: clock["now"])

        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        # still inside the window: cached, no second probe
        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        assert probe.call_count == 1

        clock["now"] += _PROBE_UNREACHABLE_RETRY_S + 0.1
        healed = get_http_vector_client()
        assert isinstance(healed, HttpVectorClient)
        assert probe.call_count == 2

        # once healed, the pass is cached forever — no per-call probing
        assert get_http_vector_client() is healed
        assert probe.call_count == 2

    def test_reprobe_failure_rearms_the_window(self, monkeypatch):
        """Still down after the window: exactly one fresh probe, then the
        cache is authoritative again — never a probe per call."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceUnreachable("connection refused"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)
        clock = {"now": 1000.0}
        monkeypatch.setattr("nexus.db.http_vector_client._monotonic", lambda: clock["now"])

        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        clock["now"] += _PROBE_UNREACHABLE_RETRY_S + 0.1
        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        assert probe.call_count == 2
        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        assert probe.call_count == 2  # re-armed: cached again inside the new window


class TestLocalModeUntouched:
    def test_probe_never_called_in_local_mode_regardless_of_call_count(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        probe = MagicMock(
            side_effect=AssertionError("probe_managed_service must never be called in local mode")
        )
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        first = get_http_vector_client()
        second = get_http_vector_client()
        third = get_http_vector_client()

        assert isinstance(first, HttpVectorClient)
        assert first is second is third
        probe.assert_not_called()


class TestCloudProbeMessageDoesNotSelfContradict:
    """nexus-b6qlf remediation Fix 2 (CRITICAL): the cloud-mode wrapper
    asserted "This cannot be fixed locally... not by any local action you
    can take" and then appended the raw ManagedServiceIncompatible message
    verbatim, which itself ends "...Upgrade the managed service, or
    upgrade/downgrade the nx client to match." -- directly contradicting
    the no-local-remedy claim in the same string. The final message must
    state both the deployed and required versions (diagnostic value)
    without ever telling the client to upgrade/downgrade itself."""

    def test_below_floor_message_omits_client_remedy_but_keeps_both_versions(
        self, monkeypatch
    ):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

        def _fake_probe():
            probe_managed_service(
                base_url="https://api.conexus-nexus.com",
                http_get=lambda url, timeout: _Resp200(
                    {"app_version": "1.0-SNAPSHOT", "release_version": "0.1.8"}
                ),
            )

        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service", _fake_probe
        )

        with pytest.raises(ManagedServiceIncompatible) as exc_info:
            get_http_vector_client()

        message = str(exc_info.value).lower()
        assert "cannot be fixed locally" in message
        assert "0.1.8" in message  # deployed version, for diagnostic value
        assert _FLOOR_STR in message  # required floor, for diagnostic value
        assert "downgrade" not in message
        assert "upgrade the nx client" not in message
        assert "or upgrade/downgrade" not in message

    def test_message_function_falls_back_cleanly_when_fields_absent(self):
        """Non-below-floor ManagedServiceIncompatible shapes (no token,
        non-200, non-JSON, no usable release_version) carry no structured
        deployed_version/required_version -- the wrapper must still produce
        a sensible message rather than crashing on missing attributes."""
        exc = ManagedServiceIncompatible("managed service returned HTTP 503")
        message = _cloud_probe_failure_message(exc)
        assert "cannot be fixed locally" in message.lower()
        assert "HTTP 503" in message


class _Resp200:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


class TestRepeatedReraiseFreshInstance:
    """nexus-b6qlf remediation Fix 3 (IMPORTANT): the cached-failure fast
    path re-raised the SAME exception instance on every call. CPython
    prepends a frame to ``__traceback__`` on every re-raise of the same
    instance across call frames -- in a long-running process (the MCP
    server) this grows unboundedly. Each re-raise must construct a FRESH
    instance (preserving type + message, chained via __cause__) so no
    single object's traceback accumulates across calls."""

    def test_repeated_calls_raise_distinct_instances_with_stable_traceback_depth(
        self, monkeypatch
    ):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceIncompatible("stale engine"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        # Warm the cache with one call (this first failure takes a DIFFERENT
        # code path -- the original probe-failure raise -- so its traceback
        # depth is not comparable to the cached-reraise path below; the bug
        # this test pins is specifically about REPEATED reraises of an
        # already-cached error, not the initial probe failure).
        with pytest.raises(ManagedServiceIncompatible):
            get_http_vector_client()
        assert probe.call_count == 1

        excs = []
        tb_lengths = []
        for _ in range(5):
            try:
                get_http_vector_client()
            except ManagedServiceIncompatible as exc:
                excs.append(exc)
                tb_lengths.append(len(traceback.extract_tb(exc.__traceback__)))

        assert len(excs) == 5
        assert probe.call_count == 1  # never re-probed -- purely cached reraises
        # Every raised object must be a DISTINCT instance -- reusing the
        # same instance is exactly what accumulates traceback frames.
        assert len({id(e) for e in excs}) == 5
        # Traceback depth stays constant across repeated cached reraises (does
        # not grow monotonically the way it would if the same instance were
        # re-raised across frames).
        assert len(set(tb_lengths)) == 1
        # __cause__ preserves the original underlying failure for diagnostics.
        assert all(e.__cause__ is not None for e in excs)


class TestResetClearsProbeCache:
    def test_reset_allows_reprobe_after_a_prior_failure(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceIncompatible("stale engine"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        with pytest.raises(ManagedServiceIncompatible):
            get_http_vector_client()
        assert probe.call_count == 1

        reset_http_vector_client_for_tests()

        # A fixed/compatible probe post-reset must be re-attempted -- the
        # pre-reset failure must not permanently poison this process.
        probe.side_effect = None
        probe.return_value = _caps()

        client = get_http_vector_client()
        assert isinstance(client, HttpVectorClient)
        assert probe.call_count == 2


class TestProbeCacheStateHygiene:
    """Review findings H1/M1 (nexus-5t1jp): the cache is the PAIR
    (_version_probe_error, _version_probe_failed_at); nothing may clear one
    without the other. The original H1 defect (reset dropped the stamp) was
    invisible to every behavioral test because the None check on the error
    short-circuits first — so these pin the globals directly."""

    def test_reset_clears_the_failure_stamp_too(self, monkeypatch):
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceUnreachable("connection refused"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)

        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        assert hvc._version_probe_error is not None
        assert hvc._version_probe_failed_at is not None

        reset_http_vector_client_for_tests()
        assert hvc._version_probe_error is None
        assert hvc._version_probe_failed_at is None

    def test_window_boundary_is_strict_less_than(self, monkeypatch):
        """L3: at EXACTLY _PROBE_UNREACHABLE_RETRY_S elapsed the cache is no
        longer binding — a < → <= flip turns this red."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        probe = MagicMock(side_effect=ManagedServiceUnreachable("connection refused"))
        monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", probe)
        clock = {"now": 1000.0}
        monkeypatch.setattr("nexus.db.http_vector_client._monotonic", lambda: clock["now"])

        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        clock["now"] += _PROBE_UNREACHABLE_RETRY_S
        with pytest.raises(ManagedServiceUnreachable):
            get_http_vector_client()
        assert probe.call_count == 2  # exactly-at-boundary => expired => re-probed

    def test_fast_path_reraises_the_validated_snapshot_not_a_reread(self, monkeypatch):
        """nexus-aedaw TOCTOU contract: get_http_vector_client must re-raise
        exactly the object _authoritative_cached_probe_error returned, never
        re-read the global — a concurrent healing clear between validate and
        re-raise would turn the re-read into TypeError(None). Simulated
        deterministically: helper returns a sentinel while the global is None;
        a re-read implementation raises TypeError here instead."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        sentinel = ManagedServiceUnreachable("validated snapshot")
        monkeypatch.setattr(
            "nexus.db.http_vector_client._authoritative_cached_probe_error",
            lambda: sentinel,
        )
        assert hvc._version_probe_error is None  # the torn-state half

        with pytest.raises(ManagedServiceUnreachable, match="validated snapshot"):
            get_http_vector_client()
