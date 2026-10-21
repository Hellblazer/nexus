# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j23ni phase 3: the storage-touch guard against REAL code paths.

``tests/test_storage_touch_guard.py`` proves the guard with errors a test
constructs by hand. That would stay green if a real client stopped raising the
guarded type (a rename, a refactor to a bare ``RuntimeError``), because then
the census would count the touch as clean. So each case here runs the real
client or resolver, unprovisioned, inside a nested guard session and checks the
guard names it, error type included. A client that stops constructing a guarded
error fails here, not silently in the census.
"""
from __future__ import annotations

import pytest

from tests.test_storage_touch_guard import _report, _run

_CLEAN_ENV = (
    "NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT",
    "NX_STORAGE_BACKEND", "NX_T1_SESSION", "NX_T1_SESSION_ID", "NX_SESSION_ID",
)


def _unprovisioned_env(monkeypatch: pytest.MonkeyPatch, *, posture_local: bool = True) -> None:
    """What an opt-in test with no tenant sees: no endpoint halves, no storage
    backend, no T1 session. The outer test's own tenant (default mode) is hidden."""
    for name in _CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NX_LOCAL", "1" if posture_local else "0")


#: (id, expected guarded type, the swallowed call). Every call is wrapped in
#: ``except RuntimeError`` below, like the production code that carries on.
_REAL_CLIENTS = [
    ("t1", "T1ServerNotFoundError", "from nexus.db.t1 import get_t1_database; get_t1_database()"),
    ("t2_database", "ServiceEndpointUnresolvableError",
     "from nexus.db.t2 import T2Database; T2Database(tmp_path / 't2.db')"),
    ("catalog_client", "ServiceEndpointUnresolvableError",
     "from nexus.catalog.factory import make_catalog_reader; make_catalog_reader().stats()"),
    ("t3_http_vector_client", "ServiceEndpointUnresolvableError",
     "from nexus.db.http_vector_client import HttpVectorClient; HttpVectorClient(tenant='x').list_collections()"),
    ("mcp_infra_t2_ctx", "ServiceEndpointUnresolvableError",
     "from nexus.mcp_infra import t2_ctx\n        with t2_ctx():\n            pass"),
]

_PROBE_BODY = '''
def test_the_real_thing(tmp_path):
    try:
        {call}
    except RuntimeError:
        pass            # swallowed, like the code under test that carries on
'''


@pytest.mark.parametrize(("expected", "call"), [c[1:] for c in _REAL_CLIENTS], ids=[c[0] for c in _REAL_CLIENTS])
def test_a_real_client_constructs_a_guarded_error_when_unprovisioned(
    pytester, monkeypatch, expected: str, call: str,
) -> None:
    import uuid

    _unprovisioned_env(monkeypatch)
    monkeypatch.setenv("NX_SESSION_ID", str(uuid.uuid4()))  # an explicit id makes T1 raise rather than mint
    result = _run(pytester, monkeypatch, _PROBE_BODY.format(call=call), mode="fail")
    result.assert_outcomes(passed=1, errors=1)  # the swallowed raise is named at teardown
    (finding,) = _report(result)["findings"]
    assert finding["kind"] == "unprovisioned_touch"
    assert expected in finding["error_types"], finding["error_types"]


#: The managed (cloud) posture and the bare RuntimeError the first phases left
#: unguarded. Each is constructed on its real path, not by hand.
_MANAGED_AND_TOKEN = [
    ("unreachable", "ManagedServiceUnreachable", '''
import httpx
from nexus.db.managed_endpoint import probe_managed_service

def refuse(url, timeout):
    raise httpx.ConnectError("refused")

probe_managed_service(base_url="http://127.0.0.1:9", http_get=refuse)
'''),
    ("incompatible_no_release_version", "ManagedServiceIncompatible", '''
import httpx
from nexus.db.managed_endpoint import probe_managed_service

probe_managed_service(
    base_url="http://127.0.0.1:9",
    http_get=lambda url, timeout: httpx.Response(200, json={"release_version": None}),
)
'''),
    ("incompatible_no_token", "ManagedServiceIncompatible", '''
from nexus.db.managed_endpoint import resolve_managed_endpoint

resolve_managed_endpoint(require_token=True)
'''),
    ("service_url_without_token", "ServiceTokenUnresolvableError", '''
import os
from nexus.db.service_endpoint import resolve_service_endpoint

os.environ["NX_SERVICE_URL"] = "https://managed.invalid"
resolve_service_endpoint()
'''),
]


@pytest.mark.parametrize(("expected", "code"), [c[1:] for c in _MANAGED_AND_TOKEN],
                         ids=[c[0] for c in _MANAGED_AND_TOKEN])
def test_a_managed_or_token_failure_is_constructed_on_its_real_path_and_named(
    pytester, monkeypatch, expected: str, code: str,
) -> None:
    _unprovisioned_env(monkeypatch, posture_local=False)
    indented = "\n".join("        " + line if line else line for line in code.strip().splitlines())
    body = f'''
import os

def test_the_real_path(monkeypatch):
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    try:
{indented}
    except RuntimeError:
        pass            # swallowed
    finally:
        os.environ.pop("NX_SERVICE_URL", None)
'''
    result = _run(pytester, monkeypatch, body, mode="fail")
    result.assert_outcomes(passed=1, errors=1)
    (finding,) = _report(result)["findings"]
    assert finding["error_types"] == [expected], finding["error_types"]


@pytest.mark.expects_unresolvable_endpoint  # it raises the type on purpose
def test_the_unresolvable_token_error_is_a_runtime_error_but_not_a_lease_wait_error(monkeypatch) -> None:
    """Keeps every ``except RuntimeError`` working, and stays out of the "worth a
    bounded lease wait" class (nexus-7dsgp: never wait on the managed-cloud path)."""
    from nexus.db.service_endpoint import (
        ServiceEndpointUnresolvableError,
        ServiceTokenUnresolvableError,
        resolve_service_endpoint,
    )

    assert issubclass(ServiceTokenUnresolvableError, RuntimeError)
    assert not issubclass(ServiceTokenUnresolvableError, ServiceEndpointUnresolvableError)
    monkeypatch.setenv("NX_SERVICE_URL", "https://managed.invalid")
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    # the outer test may carry a tenant; hide any lease that could fill the token
    monkeypatch.setattr("nexus.db.service_endpoint.discover_lease", lambda: (None, None))
    with pytest.raises(ServiceTokenUnresolvableError, match="service_url is set but no service_token"):
        resolve_service_endpoint()
