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
REASON = "unit test of the external-engine opt-in on throwaway tenant acme; nothing is written"


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
        return ExternalProbe(tenant="acme", embedding_mode="voyage", embedding_profile="voyage")

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
    assert _external_engine.resolve(request) is None
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


#: (embedding_mode, embedding_profile, NX_LOCAL). The profile decides when the engine reports one: it is what the
#: register_collection 422 is checked against, and ``dual`` is a runtime family that profiles either way.
_POSTURES = [
    ("onnx-local", "bge", "1"), ("onnx-local", "voyage", "0"),
    ("voyage", "voyage", "0"), ("voyage", "bge", "1"),
    ("dual", "voyage", "0"), ("dual", "bge", "1"),
    # an engine that reports no profile: embedding_mode decides
    ("onnx-local", None, "1"), ("voyage", None, "0"), ("dual", None, "0"), ("unknown", None, "0"),
]


@pytest.mark.external_engine_ok
@pytest.mark.parametrize(("mode", "profile", "nx_local"), _POSTURES)
def test_the_client_posture_follows_the_engines_embedding_profile_then_its_mode(
    mode: str, profile: str | None, nx_local: str,
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
    monkeypatch.setattr(
        _external_engine, "probe",
        lambda target: ExternalProbe(tenant="acme", embedding_mode=mode, embedding_profile=profile),
    )
    with _engine_tenant_env(monkeypatch, pin_local=False, request=request):
        assert os.environ["NX_LOCAL"] == nx_local


@pytest.mark.external_engine_ok
def test_a_degraded_engine_fails_at_setup_before_any_env_is_set(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
    degraded = {"chosen": "bge", "serving": "voyage", "missing": "the bge-768 model and tokenizer files are missing"}
    monkeypatch.setattr(
        _external_engine, "probe",
        lambda target: ExternalProbe(tenant="acme", embedding_mode="voyage", embedding_profile="bge", embedding_degraded=degraded),
    )
    before = os.environ.get("NX_SERVICE_URL")
    with pytest.raises(pytest.fail.Exception) as excinfo:
        with _engine_tenant_env(monkeypatch, pin_local=True, request=request):
            pytest.fail("the body must not run")  # pragma: no cover
    assert "degraded" in str(excinfo.value) and "chosen='bge'" in str(excinfo.value) and TOKEN not in str(excinfo.value)
    assert seams.window == [] and os.environ.get("NX_SERVICE_URL") == before


@pytest.mark.external_engine_ok
def test_the_setup_line_names_the_run_and_never_the_token(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
    monkeypatch.setattr(
        _external_engine, "probe",
        lambda target: ExternalProbe(
            tenant="acme", embedding_mode="dual", embedding_profile="voyage", release_version="9.9.9",
        ),
    )
    lines: list[str] = []
    with capture_logs() as logs, _engine_tenant_env(monkeypatch, pin_local=True, request=request):
        pass
    # the fixture's own writer is the terminal reporter; drive the same function with a recorder
    with pytest.MonkeyPatch.context() as inner:
        inner.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
        with _external_engine.external_tenant_env(inner, ExternalTarget(base_url=URL, token=TOKEN), report=lines.append):
            pass
    (line,) = lines
    for want in (
        "host=engine.example.test", "tenant=acme", "release_version=9.9.9", "embedding_mode=dual",
        "embedding_profile=voyage", "NX_LOCAL=0", "validated by GET /v1/_whoami",
    ):
        assert want in line, (want, line)
    assert TOKEN not in line and "https://" not in line
    assert [e["line"] for e in logs if e["event"] == "external_engine.setup"] == [line]


@pytest.mark.external_engine_ok
def test_the_terminal_writer_is_the_pytest_reporter(request: pytest.FixtureRequest) -> None:
    writer = _external_engine.terminal_writer(request)
    assert writer is not None, "the setup line would reach nobody under the operator's command"
    assert _external_engine.terminal_writer(None) is None


# -- the reason must name the tenant the engine says the bearer is bound to ------------------------------------


@pytest.mark.external_engine_ok
def test_a_reason_that_does_not_name_the_tenant_fails_at_setup(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, "RDR-235 Activation Step 1: MVV in throwaway tenant globex")
    with pytest.raises(pytest.fail.Exception) as excinfo:
        with _engine_tenant_env(monkeypatch, pin_local=True, request=request):
            pytest.fail("the body must not run")  # pragma: no cover
    msg = str(excinfo.value)
    assert "'acme'" in msg and service_endpoint.PROD_WRITE_OPT_IN_ENV in msg and TOKEN not in msg
    assert seams.window == [] and (seams.ensure_engine, seams.tenant_lease) == (0, 0)


@pytest.mark.external_engine_ok
def test_when_the_engine_names_no_tenant_the_reason_is_not_asked_to(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    """A 404 on /v1/_whoami leaves the literal ``external``; there is no name to require."""
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, "a reason that names no tenant at all")
    monkeypatch.setattr(
        _external_engine, "probe",
        lambda target: ExternalProbe(tenant="external", embedding_mode="voyage", tenant_named=False, whoami_status=404),
    )
    with _engine_tenant_env(monkeypatch, pin_local=True, request=request) as tenant:
        assert tenant == "external"


# -- the session exemption comes back (nexus-ckejb review B1) ----------------------------------------------------


@pytest.mark.external_engine_ok
def test_the_session_exemption_and_the_env_are_restored_after_the_external_span(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, seams: _Calls,
) -> None:
    """The span withdraws the suite-wide production-write exemption for its own duration. A direct assignment
    instead of ``monkeypatch.setattr`` would leave it withdrawn for every later test in the session."""
    _set_pair(monkeypatch)
    monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, REASON)
    monkeypatch.setattr(service_endpoint, "_test_only_opt_in_reason", "SESSION-STRING-SENTINEL")
    names = ("NX_STORAGE_BACKEND", "NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_LOCAL")
    before = {n: os.environ.get(n) for n in names}
    with pytest.MonkeyPatch.context() as span:
        with _engine_tenant_env(span, pin_local=True, request=request):
            assert service_endpoint._test_only_opt_in_reason is None, "withdrawn inside the span"
            assert os.environ["NX_SERVICE_URL"] == URL
    assert service_endpoint._test_only_opt_in_reason == "SESSION-STRING-SENTINEL", "not restored after the span"
    assert {n: os.environ.get(n) for n in names} == before


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
    assert result == ExternalProbe(tenant="acme", embedding_mode="voyage", release_version="x")
    assert not result.local_posture
    by_url = {s["url"]: s for s in seen}
    assert "headers" not in by_url[URL + "/version"], "the unauthenticated route gets no credential"
    assert by_url[URL + "/v1/_whoami"]["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert all(s["follow_redirects"] is False for s in seen), "a redirect must not carry the bearer elsewhere"


@pytest.mark.parametrize("whoami", [_Reply(404), _Reply(200, {"tenant": ""}), _Reply(200, ["x"]), _Reply(200, bad_json=True)])
def test_a_whoami_that_names_no_tenant_leaves_the_literal_external_and_says_the_name_is_unconfirmed(whoami: _Reply) -> None:
    result = _external_engine.probe(_TARGET, get=_getter(_OK_VERSION, whoami, []))
    assert result.tenant == _external_engine.FALLBACK_TENANT and not result.tenant_named


def test_a_404_whoami_is_reported_as_an_unvalidated_bearer_in_the_setup_line() -> None:
    result = _external_engine.probe(_TARGET, get=_getter(_OK_VERSION, _Reply(404), []))
    line = _external_engine.setup_line(_TARGET, result)
    assert "UNVALIDATED" in line and "404" in line and "tenant=external" in line and TOKEN not in line


@pytest.mark.parametrize("status", [500, 502, 429, 301])
def test_a_whoami_that_answers_anything_but_200_404_401_403_fails_naming_the_status(status: int) -> None:
    with pytest.raises(ExternalEngineError, match=f"HTTP {status}"):
        _external_engine.probe(_TARGET, get=_getter(_OK_VERSION, _Reply(status), []))


def test_the_probe_reads_the_profile_the_degradation_and_the_release() -> None:
    degraded = {"chosen": "bge", "serving": "voyage", "missing": "m", "remedy": "r"}
    body = {"embedding_mode": "dual", "embedding_profile": "bge", "embedding_degraded": degraded, "release_version": "1.2.3"}
    result = _external_engine.probe(_TARGET, get=_getter(_Reply(200, body), _Reply(200, {"tenant": "t"}), []))
    assert (result.embedding_profile, result.release_version, result.embedding_degraded) == ("bge", "1.2.3", degraded)
    assert result.local_posture, "bge profile is local posture whatever the mode says"
    plain = _external_engine.probe(_TARGET, get=_getter(_Reply(200, {"embedding_mode": "voyage", "embedding_degraded": None}), _Reply(200, {"tenant": "t"}), []))
    assert plain.embedding_profile is None and plain.embedding_degraded is None and plain.release_version == "unknown"


def test_a_profile_the_harness_does_not_know_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ExternalEngineError, match="embedding_profile 'mystery'"):
        _external_engine.probe(_TARGET, get=_getter(_Reply(200, {"embedding_mode": "voyage", "embedding_profile": "mystery"}), _Reply(200, {"tenant": "t"}), []))


def test_the_local_onnx_engine_without_a_profile_is_local_posture() -> None:
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


@pytest.mark.parametrize(
    "raw", ["engine.example.test", "tok:s3cretvalue@engine.example.test/x", "ftp://engine.example.test", "https://"],
)
def test_a_url_that_is_not_an_http_url_with_a_host_is_refused_without_echoing_it(raw: str) -> None:
    with pytest.raises(ExternalEngineError, match="http") as excinfo:
        _external_engine.probe(ExternalTarget(base_url=raw, token=TOKEN), get=_getter(_OK_VERSION, _Reply(200), []))
    assert "s3cretvalue" not in str(excinfo.value) and raw not in str(excinfo.value)


def test_a_url_with_credentials_is_refused_and_the_password_is_not_echoed() -> None:
    with pytest.raises(ExternalEngineError, match="credentials") as excinfo:
        _external_engine.probe(
            ExternalTarget(base_url="https://operator:s3cretvalue@engine.example.test", token=TOKEN),
            get=_getter(_OK_VERSION, _Reply(200), []),
        )
    assert "s3cretvalue" not in str(excinfo.value) and "operator" not in str(excinfo.value)


@pytest.mark.parametrize("host", ["engine.example.test", "10.0.0.5", "192.168.1.9:8080", "localhost.example.test"])
def test_plain_http_to_a_remote_host_is_refused_because_the_bearer_would_cross_in_clear(host: str) -> None:
    seen: list[dict] = []
    with pytest.raises(ExternalEngineError, match="in clear"):
        _external_engine.probe(ExternalTarget(base_url=f"http://{host}", token=TOKEN), get=_getter(_OK_VERSION, _Reply(200), seen))
    assert seen == [], "nothing was sent"


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:9000", "[::1]:8080", "127.0.0.9"])
def test_plain_http_to_a_loopback_host_is_accepted(host: str) -> None:
    result = _external_engine.probe(
        ExternalTarget(base_url=f"http://{host}", token=TOKEN), get=_getter(_OK_VERSION, _Reply(200, {"tenant": "t"}), []),
    )
    assert result.tenant == "t"


def test_an_invalid_url_error_from_httpx_becomes_the_named_error_without_the_url() -> None:
    boom = httpx.InvalidURL("Invalid non-printable ASCII character in URL: s3cretvalue")
    with pytest.raises(ExternalEngineError, match="not a usable URL") as excinfo:
        _external_engine.probe(_TARGET, get=_getter(boom, _Reply(200), []))
    assert "s3cretvalue" not in str(excinfo.value)
    with pytest.raises(ExternalEngineError, match="not a usable URL"):
        _external_engine.probe(_TARGET, get=_getter(_OK_VERSION, boom, []))


# -- the journey file's local-only scenarios -------------------------------------------------------------------


def test_the_journey_alone_in_its_file_carries_the_marker_and_no_scenario_is_skipped_by_the_pair() -> None:
    """Scenarios 24 and 25 run on the local engine whatever the pair says (S1, nexus-ckejb review round 1).

    A ``skipif`` keyed on the pair alone, with no look at the marker, skipped them whenever the variables were
    exported, and the scenario non-vacuity guard (budget 0) then failed the whole session. They carry neither
    the marker nor any skip: an unmarked test with the pair set takes the local tenant (the first test above).
    """
    import tests.test_rdr235_mvv_aspect_queue_tuples as mvv

    def marked(fn: Any) -> bool:
        return any(m.name == _external_engine.MARKER for m in getattr(fn, "pytestmark", []))

    def skips(fn: Any) -> list[Any]:
        return [m for m in getattr(fn, "pytestmark", []) if m.name in ("skip", "skipif")]

    journey = mvv.test_mvv_journey_on_the_tuple_queue
    s24 = mvv.test_scenario_24_an_out_from_a_second_process_wakes_a_parked_worker_within_two_seconds
    s25 = mvv.test_scenario_25_a_bulk_index_at_the_measured_backlog_is_not_refused_and_does_not_expire
    assert marked(journey) and not marked(s24) and not marked(s25)
    assert skips(s24) == [] and skips(s25) == [], "a scenario of this file must not skip on the pair"
    assert not hasattr(_external_engine, "requested"), "a pair-only predicate invites that skipif back"
