# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-utbip: the connection-class retry in ``http_vector_client._request``
must not resend a non-idempotent sweep route after a reset or broken pipe (the
server may have run it); a refused connection or a 401 still re-resolves once.
"""
from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from nexus.db import http_vector_client as hvc
from nexus.db.gateway_backoff import _NON_IDEMPOTENT_SWEEP_PATH_SUFFIXES

READ = "/v1/vectors/search"


def _sweep_path() -> str:
    return "/v1/vectors" + sorted(_NON_IDEMPOTENT_SWEEP_PATH_SUFFIXES)[0]


def _run(path: str, exc: Exception) -> tuple[int, object]:
    calls = {"n": 0}

    def once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return {"ok": True}

    with (
        patch.object(hvc, "_request_once", once),
        patch.object(hvc, "_invalidate_endpoint"),
        patch.object(hvc, "_wait_for_lease_republication"),
        patch.object(hvc, "_resolve_endpoint", return_value=("http://x", "")),
    ):
        try:
            out = hvc._request("POST", path, tenant="t", timeout=1, body={})
        except Exception as e:  # noqa: BLE001
            out = e
    return calls["n"], out


@pytest.mark.parametrize(
    "exc",
    [ConnectionResetError(), BrokenPipeError(), ConnectionAbortedError(),
     urllib.error.URLError(ConnectionResetError())],
)
def test_sweep_route_not_resent_after_drop(exc: Exception) -> None:
    n, out = _run(_sweep_path(), exc)
    assert n == 1 and isinstance(out, Exception)


@pytest.mark.parametrize(
    "exc", [ConnectionRefusedError(), urllib.error.URLError(ConnectionRefusedError())]
)
def test_sweep_route_resent_after_refusal(exc: Exception) -> None:
    n, out = _run(_sweep_path(), exc)
    assert n == 2 and out == {"ok": True}


@pytest.mark.parametrize("exc", [ConnectionResetError(), BrokenPipeError()])
def test_ordinary_route_still_retries_a_drop(exc: Exception) -> None:
    n, out = _run(READ, exc)
    assert n == 2 and out == {"ok": True}
