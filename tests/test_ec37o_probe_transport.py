# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o residual 1: an unauthenticated probe of the service takes its proxy and CA from ``config.yml`` too.

``tests/test_ec37o_netcfg_suite.py`` holds the requests that carry a bearer to this rule. The client also reads
``GET /version`` and ``GET /health`` without one and TRUSTS the answer: the cloud compatibility probe takes
``embedding_mode`` and the engine floor from it, the note-write floor and the upgrade ladder take the serving version,
the supervisor takes "alive" from ``/health``. A repository's ``.claude/settings.json`` env reaches every ``nx`` the
model runs, so ``HTTPS_PROXY``, ``HTTP_PROXY`` and ``SSL_CERT_FILE`` can be set by a repository: a proxy it names, trusted
through a CA it names, could answer those probes with whatever it likes. Each probe here runs as a real process against
a real origin and a real proxy that terminates TLS (https) or records the request (plain http), in these legs:

* ``env_attack``: the environment names the attacker's proxy and its CA; ``config.yml`` names only the origin's CA. The
  proxy is never touched and the origin answers.
* ``env_trust_only``: the environment names the origin's own CA and ``config.yml`` none. No root is added by the
  environment, so the origin is not trusted and its answer is never read.
* ``config_tunnel``: ``config.yml`` names a tunnelling proxy and the origin's CA, and the environment still names the
  attacker's proxy. The probe goes through the configured proxy and the attacker's is never touched.
* ``plain_http``: the environment names a recording proxy for plain http; the loopback probe goes direct.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests._tls_pki import ENGINE_VERSION, TerminatingProxy, TlsOrigin, TunnelProxy, make_ca

#: every variable that names a proxy or a CA to a client that reads its environment, and the two OpenSSL reads
ENV_NAMES = frozenset({
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "SSLKEYLOGFILE", "OPENSSL_CONF", "NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT",
})


@dataclass(frozen=True)
class Probe:
    """One unauthenticated request the client trusts. ``code`` runs in the child with ``URL``, ``HOST`` and ``PORT`` bound to the
    origin and prints one JSON value that is truthy exactly when the probe read the origin's answer."""

    name: str
    code: str
    https: bool = True
    #: False when the child's printed value cannot say whether the answer was read (``check_pair`` also sends token
    #: requests that fail by design here): the assertion is then on what the origin and the proxies saw
    prints_read: bool = True


_PRELUDE = "import json, sys\nURL, HOST, PORT = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"

PROBES = (
    Probe("managed_service_probe", "from nexus.db.managed_endpoint import probe_managed_service\n"
          "print(json.dumps(probe_managed_service(base_url=URL).release_version))\n"),
    Probe("serving_engine_version",
          "import nexus.db.service_endpoint as se\n"
          "se.resolve_service_endpoint_with_evidence_gate = lambda *a, **k: (URL, 'tok')\n"
          "from nexus.db.serving_engine_version import probe_serving_engine_version\n"
          "print(json.dumps(probe_serving_engine_version()))\n"),
    Probe("t2_schema_fingerprint", "from nexus.health import probe_t2_schema_fingerprint\n"
          "print(json.dumps(probe_t2_schema_fingerprint(base_url=URL).reachable))\n"),
    Probe("fetch_service_version", "from nexus.daemon.binary_lifecycle import fetch_service_version\n"
          "print(json.dumps(fetch_service_version(HOST, PORT, scheme='https')))\n"),
    Probe("fetch_service_version_http", "from nexus.daemon.binary_lifecycle import fetch_service_version\n"
          "print(json.dumps(fetch_service_version(HOST, PORT)))\n", https=False),
    Probe("doctor_trim_version",
          "import contextlib, io\n"
          "import nexus.db.service_endpoint as se\n"
          "import nexus.engine_version as ev\n"
          "se.resolve_service_endpoint_with_evidence_gate = lambda *a, **k: (URL, 'tok')\n"
          "ev.REQUIRED_ENGINE_VERSION = (0, 0, 1)  # the dry-run guard probes /version only on an engine floor below the trim floor\n"
          "from nexus.commands.doctor import _run_trim_telemetry\n"
          "err = io.StringIO()\n"
          "with contextlib.redirect_stderr(err):\n"
          "    try:\n"
          "        _run_trim_telemetry(30, dry_run=True)\n"
          "    except BaseException:\n"
          "        pass\n"
          "print(json.dumps('unprobeable' not in err.getvalue()))\n"),
    Probe("verify_service_version", "from nexus.upgrade_ladder.provisioning import verify_service_version\n"
          "print(json.dumps(verify_service_version(URL).ok))\n"),
    Probe("verify_voyage_capability", "from nexus.upgrade_ladder.provisioning import verify_voyage_capability\n"
          "print(json.dumps('could not reach' not in (verify_voyage_capability(URL).reason or '')))\n"),
    Probe("nx_setup_check",
          "import sys\n"
          "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1] / "conexus" / "launch")) + ")\n"
          "import nx_setup\n"
          "print(json.dumps(nx_setup.check_pair(URL, 'setup-token').ok))\n", prints_read=False),
    Probe("storage_service_health",
          "import pathlib, tempfile\n"
          "from nexus.health import _check_storage_service_health\n"
          "creds = pathlib.Path(tempfile.mkdtemp()) / 'pg_credentials'\n"
          "creds.write_text('x')\n"
          "print(json.dumps(_check_storage_service_health(creds_path=creds, endpoint=(HOST, PORT))[0].ok))\n", https=False),
    Probe("registry_health_identity", "from nexus.daemon.service_registry import _probe_health_identity\n"
          "print(json.dumps(_probe_health_identity(HOST, PORT, timeout=5.0)))\n", https=False),
    Probe("daemon_probe_health", "from nexus.commands.daemon import _probe_health\n"
          "print(json.dumps(_probe_health(HOST, PORT, 5.0) == 'ok'))\n", https=False),
    Probe("supervisor_probe_health",
          "from nexus.daemon import storage_service_daemon as d\n"
          "s = d.StorageServiceSupervisor.__new__(d.StorageServiceSupervisor)\n"
          "s._service_port = PORT\n"
          "d._SERVICE_HOST = HOST\n"
          "print(json.dumps(s._probe_service_health() == d.HealthProbe.OK and s._probe_service_liveness()))\n", https=False),
)


class _Plain:
    """A port-0 plain-http server recording every request line, answering ``/health`` and ``/version`` like the engine."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- stdlib callback name
                outer.lines.append(self.requestline)
                body = json.dumps({"status": "ok", "db": "up", "release_version": ENGINE_VERSION}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("probe-pki")
    real, attacker = make_ca(d, "real-ca"), make_ca(d, "attacker-ca")
    return real, attacker, real.issue_leaf(d, "origin"), attacker.issue_leaf(d, "proxy")


@pytest.fixture
def origin(pki) -> Iterator[TlsOrigin]:
    server = TlsOrigin(pki[2])
    yield server
    server.close()


@pytest.fixture
def mitm(pki) -> Iterator[TerminatingProxy]:
    proxy = TerminatingProxy(pki[3])
    yield proxy
    proxy.close()


@pytest.fixture
def tunnel() -> Iterator[TunnelProxy]:
    proxy = TunnelProxy()
    yield proxy
    proxy.close()


@pytest.fixture
def plain() -> Iterator[_Plain]:
    server = _Plain()
    yield server
    server.close()


@pytest.fixture
def decoy() -> Iterator[_Plain]:
    server = _Plain()
    yield server
    server.close()


def _proxy_env(url: str) -> dict[str, str]:
    return {name: url for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")}


def _ca_env(pem: Path, directory: Path) -> dict[str, str]:
    directory.mkdir(exist_ok=True)
    (directory / pem.name).write_bytes(pem.read_bytes())
    return {"SSL_CERT_FILE": str(pem), "SSL_CERT_DIR": str(directory), "REQUESTS_CA_BUNDLE": str(pem), "CURL_CA_BUNDLE": str(pem)}


def _run(probe: Probe, tmp_path: Path, url: str, extra_env: dict[str, str], **network: str) -> tuple[subprocess.CompletedProcess[str], object]:
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "config.yml").write_text(yaml.safe_dump({"network": network} if network else {}), encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if k.upper() not in ENV_NAMES}
    env = {**base, "NEXUS_CONFIG_DIR": str(cfg), **extra_env}
    host, _, port = url.removeprefix("https://").removeprefix("http://").rpartition(":")
    proc = subprocess.run(
        [sys.executable, "-c", _PRELUDE + probe.code, url, host, port],
        capture_output=True, text=True, env=env, timeout=180, encoding="utf-8", errors="replace",
    )
    lines = proc.stdout.strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        value = None
    return proc, value


HTTPS_PROBES = [p for p in PROBES if p.https]
PLAIN_PROBES = [p for p in PROBES if not p.https]
LEGS = ("env_attack", "env_trust_only", "config_tunnel")


def _https_matrix() -> list[pytest.param]:
    # the compatibility probe raises ManagedServiceUnreachable (a guarded error, charged to the test by the
    # storage-touch guard unless marked) when the origin's CA is not trusted: that refusal is the point of env_trust_only
    return [
        pytest.param(
            probe, leg, id=f"{probe.name}-{leg}",
            marks=(pytest.mark.expects_unresolvable_endpoint,) if probe.name == "managed_service_probe" and leg == "env_trust_only" else (),
        )
        for probe in HTTPS_PROBES for leg in LEGS
    ]


@pytest.mark.parametrize(("probe", "leg"), _https_matrix())
def test_an_https_probe_takes_its_proxy_and_ca_from_config_only(tmp_path, pki, origin, mitm, tunnel, probe, leg):
    real_ca, attacker_ca = pki[0], pki[1]
    attack = {**_proxy_env(mitm.url), **_ca_env(attacker_ca.pem, tmp_path / "attacker-certs")}
    if leg == "env_attack":
        proc, value = _run(probe, tmp_path, origin.url, attack, ca_bundle=str(real_ca.pem))
    elif leg == "env_trust_only":
        proc, value = _run(probe, tmp_path, origin.url, _ca_env(real_ca.pem, tmp_path / "real-certs"))
    else:
        proc, value = _run(probe, tmp_path, origin.url, attack, https_proxy=tunnel.url, ca_bundle=str(real_ca.pem))

    context = (probe.name, leg, proc.returncode, proc.stdout[-400:], proc.stderr[-400:], origin.requests, mitm.connects, tunnel.connects)
    assert not mitm.touched, context  # the repository's proxy never saw the probe
    if leg == "env_attack":
        assert any(path == "/version" for path, _auth in origin.requests), context
        assert value or not probe.prints_read, context  # and its answer came from the origin, over the CA of config.yml
    elif leg == "env_trust_only":
        assert origin.requests == [] and not value, context  # an environment CA adds no root: the answer was never read
    else:
        assert any(c.startswith(f"CONNECT 127.0.0.1:{origin.port}") for c in tunnel.connects), context
        assert any(path == "/version" for path, _auth in origin.requests), context
        assert value or not probe.prints_read, context


@pytest.mark.parametrize("probe", PLAIN_PROBES, ids=lambda p: p.name)
def test_a_plain_http_probe_never_uses_a_proxy(tmp_path, plain, decoy, probe):
    proc, value = _run(probe, tmp_path, plain.url, _proxy_env(decoy.url))
    context = (probe.name, proc.returncode, proc.stdout[-400:], proc.stderr[-400:], plain.lines, decoy.lines)
    assert decoy.lines == [], context  # the repository's proxy answered nothing
    assert plain.lines, context
    assert value, context  # and the answer came from the loopback service itself


def test_the_harness_is_not_vacuous(tmp_path, pki, origin):
    """The absences above are measured against this presence: with the origin's CA in ``config.yml`` and no environment at all,
    every https probe reaches the origin and reads its answer."""
    for probe in HTTPS_PROBES:
        proc, value = _run(probe, tmp_path, origin.url, {}, ca_bundle=str(pki[0].pem))
        assert value or not probe.prints_read, (probe.name, proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
    assert {path for path, _auth in origin.requests} == {"/version"}


#: the production functions each probe above exercises: ``path::qualified name``, as the lint's census names them
COVERS: dict[str, tuple[str, ...]] = {
    "managed_service_probe": ("src/nexus/db/managed_endpoint.py::probe_managed_service.http_get",),
    "serving_engine_version": ("src/nexus/db/serving_engine_version.py::probe_serving_engine_version",),
    "t2_schema_fingerprint": ("src/nexus/health.py::probe_t2_schema_fingerprint",),
    "fetch_service_version": ("src/nexus/daemon/binary_lifecycle.py::fetch_service_version",),
    "fetch_service_version_http": ("src/nexus/daemon/binary_lifecycle.py::fetch_service_version",),
    "doctor_trim_version": ("src/nexus/commands/doctor.py::_run_trim_telemetry",),
    "verify_service_version": ("src/nexus/upgrade_ladder/provisioning.py::verify_service_version._get",),
    "verify_voyage_capability": ("src/nexus/upgrade_ladder/provisioning.py::verify_voyage_capability._get",),
    "nx_setup_check": ("conexus/launch/nx_setup.py::_probe",),
    "storage_service_health": ("src/nexus/health.py::_check_storage_service_health",),
    "registry_health_identity": ("src/nexus/daemon/service_registry.py::_probe_health_identity",),
    "daemon_probe_health": ("src/nexus/commands/daemon.py::_probe_health",),
    "supervisor_probe_health": (
        "src/nexus/daemon/storage_service_daemon.py::StorageServiceSupervisor._probe_service_health",
        "src/nexus/daemon/storage_service_daemon.py::StorageServiceSupervisor._probe_service_liveness",
    ),
}

#: decided probes the table does not run, with the reason: these send a bearer, so the bearer matrix
#: (``tests/test_ec37o_netcfg_suite.py``) is where they are held to the transport
NOT_IN_TABLE: dict[str, str] = {
    "src/nexus/upgrade_ladder/provisioning.py::wait_for_service_health._get": "its /health request may carry the bearer (bearer_httpx_kwargs)",
    "src/nexus/db/http_engine_status.py::fetch_engine_status": "its /v1/status request carries the bearer (bearer_httpx_kwargs)",
}


def test_the_probe_table_names_every_probe_the_tree_decides_and_nothing_stale() -> None:
    """Derived from the source, not a hand count: the lint's census (``tests/test_ec37o_bearer_transport_lint.py``) finds every
    function that names ``/version``, ``/health`` or ``/livez`` and carries the probe transport. Each is either run by a probe
    above or listed in ``NOT_IN_TABLE`` with its reason, so a new probe is a red here until it has a behaviour case, and an
    entry for a function that no longer exists is a red too."""
    from tests.test_ec37o_bearer_transport_lint import _decided_probes

    in_tree = {f"{path}::{qual}" for path, qual, _route in _decided_probes()}
    covered = {c for p in PROBES for c in COVERS.get(p.name, ())}
    assert {p.name for p in PROBES} == set(COVERS), sorted({p.name for p in PROBES} ^ set(COVERS))
    assert not in_tree - covered - set(NOT_IN_TABLE), f"decided probes with no behaviour case: {sorted(in_tree - covered - set(NOT_IN_TABLE))}"
    assert not (covered | set(NOT_IN_TABLE)) - in_tree, f"table entries naming a function that is not a decided probe: {sorted((covered | set(NOT_IN_TABLE)) - in_tree)}"
    assert len(HTTPS_PROBES) >= 8 and len(PLAIN_PROBES) >= 5
