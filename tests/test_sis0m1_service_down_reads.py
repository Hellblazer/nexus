# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.1: read verbs say the service is down; they never trace back
or report data loss.

Shakeout 7.64.1 Surface F F6 (T2 nexus/shakeout-7.64.1-local-driver-2026-09-28):
on a local install with the service stopped, ``nx search``, ``nx store get``
and ``nx collection list`` printed a 46-line traceback, and ``nx store list
--docs`` printed "Collection not found" and exited 0, which reads as data
loss. Reproduced here the same way: no endpoint in the environment, no lease
to discover.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.db.http_vector_client as hvc
from nexus.cli import main

_VERBS = [
    ["search", "foo"],
    ["store", "get", "a" * 64, "-c", "sis0m-down"],
    ["collection", "list"],
    ["store", "list", "-c", "sis0m-down", "--docs"],
]


@pytest.fixture
def service_down(monkeypatch):
    for name in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "")
    monkeypatch.setattr(hvc, "_discover_lease", lambda: (None, None))
    monkeypatch.setattr(hvc, "_lease_cache", None)
    monkeypatch.setattr("nexus.db.service_endpoint.mint_armed", lambda: False)


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: " ".join(a[:2]))
def test_a_read_verb_names_the_stopped_service(service_down, argv):
    result = CliRunner().invoke(main, argv)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), (
        f"{argv}: {type(result.exception).__name__} escaped as a traceback"
    )
    assert "nx daemon service start" in result.output, result.output
    assert "Collection not found" not in result.output, result.output
