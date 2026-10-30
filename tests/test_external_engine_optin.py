# SPDX-License-Identifier: AGPL-3.0-or-later
"""The external-engine opt-in of the engine-substrate fixture (nexus-ckejb, RDR-235 Activation Step 1).

``_engine_tenant_env`` (the body of ``t2_service_env`` / ``t2_tenant``) takes an operator's engine and tenant
instead of the session engine's minted one, but only for a test carrying ``external_engine_ok`` with BOTH
``NX_TEST_EXTERNAL_*`` variables set. These tests drive that body directly with the engine boot and the tenant
lease replaced by recorders that fail the test if called on the external branch, and the probe replaced by a
stub, so they need no engine, no network and no tenant (they run under ``NX_TEST_T2_SUBSTRATE=none`` as well).
Each claim has a control where it could hold by accident: the local-branch tests would pass vacuously if the
stubs were never reached, so they assert the stubs were.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from nexus.db import service_endpoint
from tests import _external_engine, _storage_touch_guard
from tests._external_engine import ExternalEngineError, ExternalProbe, ExternalTarget
from tests.conftest import _engine_tenant_env

URL = "https://engine.example.test"
TOKEN = "tok-not-a-real-secret-0123456789"
REASON = "unit test of the external-engine opt-in; nothing is written"


class _Calls:
    """What the replaced seams saw."""

    def __init__(self) -> None:
        self.ensure_engine = 0
        self.tenant_lease = 0
        self.probe: list[ExternalTarget] = []
        self.window: list[str] = []


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Replace the local branch's engine boot and tenant lease, the probe, and the guard window with recorders."""
    import tests._engine_substrate as engine_substrate
    import tests._tenant_pool as tenant_pool

    calls = _Calls()

    def ensure_engine() -> dict:
        calls.ensure_engine += 1
        return {"base_url": "http://127.0.0.1:1"}

    @contextlib.contextmanager
    def tenant_lease(state: Any, request: Any) -> Iterator[tuple[str, str]]:
        calls.tenant_lease += 1
        yield "local-tenant", "local-token"

    @contextlib.contextmanager
    def tenant_active() -> Iterator[None]:
        calls.window.append("open")
        try:
            yield
        finally:
            calls.window.append("closed")

    def probe(target: ExternalTarget) -> ExternalProbe:
        calls.probe.append(target)
        return ExternalProbe(tenant="acme", embedding_mode="voyage")

    monkeypatch.setattr(engine_substrate, "ensure_engine", ensure_engine)
    monkeypatch.setattr(tenant_pool, "tenant_lease", tenant_lease)
    monkeypatch.setattr(_storage_touch_guard, "tenant_active", tenant_active)
    monkeypatch.setattr(_external_engine, "probe", probe)
    monkeypatch.setattr(_external_engine, "_noted", set())
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # the CI jar skip is not under test
    monkeypatch.delenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, raising=False)
    monkeypatch.delenv(_external_engine.URL_ENV, raising=False)
    monkeypatch.delenv(_external_engine.TOKEN_ENV, raising=False)
    return calls


def _set_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_external_engine.URL_ENV, URL + "/")
    monkeypatch.setenv(_external_engine.TOKEN_ENV, TOKEN)


def _assert_took_local_branch(calls: _Calls, tenant: str) -> None:
    assert tenant == "local-tenant"
    assert (calls.ensure_engine, calls.tenant_lease) == (1, 1), "the local substrate was not reached"
    assert calls.probe == [], "the external engine was probed"
    assert os.environ["NX_SERVICE_URL"] == "http://127.0.0.1:1"
    assert os.environ["NX_SERVICE_TOKEN"] == "local-token"
    assert calls.window == ["open"], "the guard window is the local branch's own"


# -- (1) an unmarked test keeps the local minted tenant ------------------------------------------------------


def test_an_unmarked_test_with_both_variables_set_still_gets_the_local_tenant(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    assert request.node.get_closest_marker(_external_engine.MARKER) is None
    with capture_logs() as logs, _engine_tenant_env(monkeypatch, pin_local=True, request=request) as tenant:
        _assert_took_local_branch(seams, tenant)
        assert os.environ["NX_LOCAL"] == "1"
    notes = [e for e in logs if e["event"] == "external_engine.variables_ignored"]
    assert len(notes) == 1 and "not marked" in notes[0]["reason"], notes
    assert TOKEN not in repr(notes)


# -- (3) the marker with only one variable keeps it too -------------------------------------------------------


@pytest.mark.external_engine_ok
@pytest.mark.parametrize("name", [_external_engine.URL_ENV, _external_engine.TOKEN_ENV])
def test_a_marked_test_with_only_one_variable_set_falls_back_to_the_local_tenant(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    monkeypatch.setenv(name, URL if name == _external_engine.URL_ENV else TOKEN)
    with capture_logs() as logs, _engine_tenant_env(monkeypatch, pin_local=True, request=request) as tenant:
        _assert_took_local_branch(seams, tenant)
    notes = [e for e in logs if e["event"] == "external_engine.variables_ignored"]
    assert len(notes) == 1 and "only one" in notes[0]["reason"] and notes[0]["set"] == name, notes


@pytest.mark.external_engine_ok
def test_a_marked_test_with_neither_variable_takes_the_local_tenant_without_a_note(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    with capture_logs() as logs, _engine_tenant_env(monkeypatch, pin_local=True, request=request) as tenant:
        _assert_took_local_branch(seams, tenant)
    assert [e for e in logs if e["event"] == "external_engine.variables_ignored"] == []


def test_empty_variables_count_as_unset(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    monkeypatch.setenv(_external_engine.URL_ENV, "  ")
    monkeypatch.setenv(_external_engine.TOKEN_ENV, "")
    assert not _external_engine.requested()
    with _engine_tenant_env(monkeypatch, pin_local=True, request=request) as tenant:
        _assert_took_local_branch(seams, tenant)


# -- (2) a marked test with both variables gets exactly those values -----------------------------------------


@pytest.mark.external_engine_ok
def test_a_marked_test_with_both_variables_gets_exactly_those_values_and_the_guard_window(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
    monkeypatch.setenv("NX_LOCAL", "1")  # the suite's default pin; the engine's posture must win
    with _engine_tenant_env(monkeypatch, pin_local=True, request=request) as tenant:
        assert tenant == "acme", "the tenant name is the engine's"
        assert os.environ["NX_STORAGE_BACKEND"] == "service"
        assert os.environ["NX_SERVICE_URL"] == URL, "trailing slash stripped, otherwise exactly the variable"
        assert os.environ["NX_SERVICE_TOKEN"] == TOKEN
        assert os.environ["NX_LOCAL"] == "0", "a voyage engine is cloud posture, whatever the pin says"
        assert seams.window == ["open"], "the storage-touch guard window is open for the span"
        assert service_endpoint._test_only_opt_in_reason is None, "the blanket suite exemption is withdrawn"
        assert service_endpoint._opt_in_reason() == REASON, "the real guard now sees the operator's reason"
    assert seams.window == ["open", "closed"]
    assert [t.base_url for t in seams.probe] == [URL]
    assert (seams.ensure_engine, seams.tenant_lease) == (0, 0), "no session engine, no minted tenant"
    assert os.environ[service_endpoint.PROD_WRITE_OPT_IN_ENV] == REASON, "the operator's own value, untouched"


@pytest.mark.external_engine_ok
@pytest.mark.parametrize(("mode", "nx_local"), [("onnx-local", "1"), ("voyage", "0"), ("unknown", "0")])
def test_the_client_posture_follows_the_engines_embedding_mode(
    mode: str, nx_local: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
    monkeypatch.setattr(_external_engine, "probe", lambda target: ExternalProbe(tenant="t", embedding_mode=mode))
    with _engine_tenant_env(monkeypatch, pin_local=False, request=request):
        assert os.environ["NX_LOCAL"] == nx_local


@pytest.mark.external_engine_ok
def test_without_the_operators_reason_the_test_fails_at_setup_before_the_engine_is_touched(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        with _engine_tenant_env(monkeypatch, pin_local=True, request=request):
            pytest.fail("the body must not run")  # pragma: no cover
    assert service_endpoint.PROD_WRITE_OPT_IN_ENV in str(excinfo.value)
    assert "the harness never sets it" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)
    assert seams.probe == [], "no request left the process"
    assert seams.window == [], "no tenant window opened"
    assert service_endpoint.PROD_WRITE_OPT_IN_ENV not in os.environ, "the harness never sets the opt-in itself"


@pytest.mark.external_engine_ok
@pytest.mark.parametrize("lookalike", ["1", "true", "yes"])
def test_a_boolean_lookalike_is_not_a_reason(
    lookalike: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, lookalike)
    with pytest.raises(pytest.fail.Exception):
        with _engine_tenant_env(monkeypatch, pin_local=True, request=request):
            pytest.fail("the body must not run")  # pragma: no cover
    assert seams.probe == []


@pytest.mark.external_engine_ok
def test_an_engine_that_refuses_the_bearer_fails_at_setup_without_the_token_in_the_message(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)

    def refuse(target: ExternalTarget) -> ExternalProbe:
        raise ExternalEngineError("engine refused the bearer (HTTP 401)")

    monkeypatch.setattr(_external_engine, "probe", refuse)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        with _engine_tenant_env(monkeypatch, pin_local=True, request=request):
            pytest.fail("the body must not run")  # pragma: no cover
    assert "refused the bearer" in str(excinfo.value) and TOKEN not in str(excinfo.value)
    assert seams.window == []


# -- the probe -----------------------------------------------------------------------------------------------


class _Reply:
    def __init__(self, status: int, body: Any = None, *, bad_json: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._bad = bad_json

    def json(self) -> Any:
        if self._bad:
            raise ValueError("not json")
        return self._body


def _getter(version: _Reply | Exception, whoami: _Reply | Exception, seen: list[dict]) -> Any:
    def get(url: str, **kwargs: Any) -> _Reply:
        seen.append({"url": url, **kwargs})
        out = version if url.endswith("/version") else whoami
        if isinstance(out, Exception):
            raise out
        return out

    return get


_TARGET = ExternalTarget(base_url=URL, token=TOKEN)
_OK_VERSION = _Reply(200, {"embedding_mode": "voyage", "release_version": "x"})


def test_the_probe_reads_the_tenant_and_the_mode_and_sends_the_bearer_only_to_whoami() -> None:
    seen: list[dict] = []
    result = _external_engine.probe(
        _TARGET, get=_getter(_OK_VERSION, _Reply(200, {"tenant": "acme", "guc_tenant": "acme"}), seen),
    )
    assert result == ExternalProbe(tenant="acme", embedding_mode="voyage")
    assert not result.local_posture
    by_url = {s["url"]: s for s in seen}
    assert "headers" not in by_url[URL + "/version"], "the unauthenticated route gets no credential"
    assert by_url[URL + "/v1/_whoami"]["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert all(s["follow_redirects"] is False for s in seen), "a redirect must not carry the bearer elsewhere"


@pytest.mark.parametrize("whoami", [_Reply(404), _Reply(500), _Reply(200, {"tenant": ""}), _Reply(200, ["x"]), _Reply(200, bad_json=True)])
def test_a_whoami_that_names_no_tenant_leaves_the_literal_external(whoami: _Reply) -> None:
    result = _external_engine.probe(_TARGET, get=_getter(_OK_VERSION, whoami, []))
    assert result.tenant == _external_engine.FALLBACK_TENANT


def test_the_local_onnx_engine_is_local_posture() -> None:
    result = _external_engine.probe(
        _TARGET, get=_getter(_Reply(200, {"embedding_mode": "onnx-local"}), _Reply(200, {"tenant": "t"}), []),
    )
    assert result.local_posture


@pytest.mark.parametrize(
    ("version", "whoami", "needle"),
    [
        (httpx.ConnectError("boom"), _Reply(200), "does not answer GET /version"),
        (_Reply(502), _Reply(200), "HTTP 502"),
        (_Reply(200, bad_json=True), _Reply(200), "not a JSON object"),
        (_Reply(200, ["x"]), _Reply(200), "not a JSON object"),
        (_OK_VERSION, httpx.ReadTimeout("slow"), "does not answer GET /v1/_whoami"),
        (_OK_VERSION, _Reply(401), "refused the bearer"),
        (_OK_VERSION, _Reply(403), "refused the bearer"),
    ],
)
def test_the_probe_names_the_cause_and_never_the_token(version: Any, whoami: Any, needle: str) -> None:
    with pytest.raises(ExternalEngineError) as excinfo:
        _external_engine.probe(_TARGET, get=_getter(version, whoami, []))
    assert needle in str(excinfo.value)
    assert TOKEN not in str(excinfo.value) and TOKEN not in repr(_TARGET)


def test_a_url_without_a_scheme_is_refused() -> None:
    with pytest.raises(ExternalEngineError, match="http"):
        _external_engine.probe(ExternalTarget(base_url="engine.example.test", token=TOKEN), get=_getter(_OK_VERSION, _Reply(200), []))


# -- the journey file's local-only scenarios -------------------------------------------------------------------


def test_the_journey_is_the_only_test_in_its_file_that_opts_in() -> None:
    """Scenarios 24 and 25 stay local: they carry the skip, not the marker."""
    import tests.test_rdr235_mvv_aspect_queue_tuples as mvv

    def marked(fn: Any) -> bool:
        return any(m.name == _external_engine.MARKER for m in getattr(fn, "pytestmark", []))

    def skip_marks(fn: Any) -> list[Any]:
        return [m for m in getattr(fn, "pytestmark", []) if m.name == "skipif"]

    journey = mvv.test_mvv_journey_on_the_tuple_queue
    s24 = mvv.test_scenario_24_an_out_from_a_second_process_wakes_a_parked_worker_within_two_seconds
    s25 = mvv.test_scenario_25_a_bulk_index_at_the_measured_backlog_is_not_refused_and_does_not_expire
    assert marked(journey) and not marked(s24) and not marked(s25)
    for fn in (s24, s25):
        (skip,) = skip_marks(fn)
        assert "local-only" in skip.kwargs["reason"] and _external_engine.URL_ENV in skip.kwargs["reason"]
