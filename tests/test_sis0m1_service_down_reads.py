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


@pytest.mark.t2_tenant
@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: " ".join(a[:2]))
def test_a_read_verb_names_the_stopped_service(service_down, argv):
    result = CliRunner().invoke(main, argv)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), (
        f"{argv}: {type(result.exception).__name__} escaped as a traceback"
    )
    assert "nx daemon service start" in result.output, result.output
    assert "Collection not found" not in result.output, result.output


@pytest.mark.expects_unresolvable_endpoint
@pytest.mark.parametrize("argv", [["store", "get", "a" * 64], ["store", "list"]], ids=["get", "list"])
def test_the_bare_default_store_reads_name_the_stopped_service(service_down, argv):
    """nexus-2y8yw: with no --collection the verbs list every knowledge
    collection first; that listing names the stopped service the same way. No
    tenant: the endpoint is unresolvable on purpose, nothing reaches storage."""
    result = CliRunner().invoke(main, argv)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), f"{argv}: {type(result.exception).__name__} escaped"
    assert "nx daemon service start" in result.output, result.output


@pytest.fixture
def lease_points_at_a_stopped_service(monkeypatch):
    """The other shape of "stopped": a lease that still names a port nothing
    listens on (review of dd203cf35)."""
    for name in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "")
    monkeypatch.setattr(hvc, "_discover_lease", lambda: ("http://127.0.0.1:9", "sis0m-token"))
    monkeypatch.setattr(hvc, "_lease_cache", None)
    monkeypatch.setattr("nexus.db.service_endpoint.mint_armed", lambda: False)


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: " ".join(a[:2]))
def test_a_read_verb_names_a_service_that_does_not_answer(lease_points_at_a_stopped_service, argv):
    result = CliRunner().invoke(main, argv)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), (
        f"{argv}: {type(result.exception).__name__} escaped as a traceback"
    )
    assert "did not answer" in result.output, result.output
    # The request that failed is named (host, port and route), not only
    # "[Errno 61] Connection refused" (critique of 22edf2fb5).
    assert "request: " in result.output and "127.0.0.1:9/v1/" in result.output, result.output
    assert "Collection not found" not in result.output, result.output


def test_a_service_error_mid_run_is_one_line(monkeypatch):
    """A service that answers with an error (a 5xx) is rendered once, with
    its message, never as a traceback (critique of dd203cf35)."""
    def _boom(*a, **k):
        raise hvc.VectorServiceError("POST /v1/vectors/search failed: HTTP 500", code=500)

    monkeypatch.setattr(hvc, "_post", _boom)
    monkeypatch.setattr(hvc, "_get", _boom)
    result = CliRunner().invoke(main, ["collection", "list"])

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), type(result.exception).__name__
    assert "HTTP 500" in result.output, result.output


def test_the_note_names_an_env_configured_endpoint(monkeypatch):
    """The request note used only the lease cache, so an endpoint set by
    NX_SERVICE_URL printed no host (review of 3857e7cd6)."""
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "sis0m-token")
    monkeypatch.setattr(hvc, "_lease_cache", None)
    monkeypatch.setattr("nexus.db.service_endpoint.mint_armed", lambda: False)
    monkeypatch.setattr(hvc, "_managed_remedy", lambda: None)

    result = CliRunner().invoke(main, ["collection", "list"])

    assert isinstance(result.exception, SystemExit), type(result.exception).__name__
    assert "127.0.0.1:9/v1/" in result.output, result.output


@pytest.fixture
def pdf_against_a_stopped_local_engine(monkeypatch, tmp_path):
    """A one-page PDF and a self-contained local mode whose engine is not running: its own HOME and
    config dir (a box whose real config is managed would otherwise answer through the managed-service
    path), NX_LOCAL=1, and an endpoint on a port nothing listens on."""
    import pymupdf

    pdf = tmp_path / "down.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "engine down " * 20)
    doc.save(str(pdf))
    home = tmp_path / "home"
    (home / "cfg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(home / "cfg"))
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "sis0m-token")
    monkeypatch.setattr(hvc, "_lease_cache", None)
    monkeypatch.setattr("nexus.db.service_endpoint.mint_armed", lambda: False)
    return pdf


def test_index_pdf_against_a_stopped_engine_is_one_message_not_a_traceback(pdf_against_a_stopped_local_engine):
    """7.94.0 shakedown (class J): `nx index pdf` with the engine stopped ended in a raw
    ``httpx.ConnectError`` traceback (two chained ``Traceback`` blocks per failure). The catalog client is
    httpx; the process boundary caught ``ConnectionError`` and ``URLError``, neither of which an httpx
    connect error is."""
    result = CliRunner().invoke(main, ["index", "pdf", str(pdf_against_a_stopped_local_engine), "--extractor", "pymupdf"])

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), (
        f"{type(result.exception).__name__} escaped as a traceback"
    )
    assert "did not answer" in result.output and "nx daemon service start" in result.output, result.output
    assert "Traceback" not in result.output, result.output


def test_the_one_message_keeps_the_cause_in_a_debug_log(pdf_against_a_stopped_local_engine):
    """Review of the 7.94.0 fix: the traceback is gone from the screen, so the exception itself is logged at
    debug with its traceback, and `nx -v` recovers the cause (which request, which layer) that one line drops."""
    result = CliRunner().invoke(main, ["-v", "index", "pdf", str(pdf_against_a_stopped_local_engine), "--extractor", "pymupdf"])

    assert isinstance(result.exception, SystemExit), type(result.exception).__name__
    seen = result.output + (result.stderr if result.stderr_bytes is not None else "")
    assert "service_did_not_answer" in seen, seen
    assert "httpx.ConnectError" in seen, "the debug event carries the raised exception's traceback: " + seen
