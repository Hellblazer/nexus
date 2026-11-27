# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o residual 2: a repository's ``SSLKEYLOGFILE`` and ``OPENSSL_CONF`` reach no nexus process.

A repository's ``.claude/settings.json`` env reaches every plugin hook and every ``nx`` the model runs. Two variables
OpenSSL itself reads are not about proxies or CAs and so were not covered by the bearer transport (nexus-ec37o):

* ``SSLKEYLOGFILE``: ``ssl.create_default_context()`` opens the file it names. On Windows, in uv's CPython, a file BIO
  without an ``OPENSSL_Applink`` kills the process (``no OPENSSL_Applink``, exit 3): a repository could stop any ``nx``
  that makes a default TLS context. Elsewhere the file collects the session's TLS secrets at a path the repository chose.
* ``OPENSSL_CONF``: read when OpenSSL initialises (``import ssl``); a configuration can load an engine or provider, which
  is code.

So the ``nexus`` package drops both from ``os.environ`` before it imports ``ssl`` (every ``nx`` entry point imports it),
the plugin's stdlib ``_endpoint_resolve`` does the same, and the telemetry children (RDR-259 Decision 9) are started
without them. The survival test is the one that matters on Windows (``windows_hand_test.py``): it sets
``SSLKEYLOGFILE`` and makes a default TLS context in a process that imported ``nexus``.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "conexus" / "hooks" / "scripts"

#: what a repository's settings can put in the environment, spelled as OpenSSL reads them
NAMES = ("SSLKEYLOGFILE", "OPENSSL_CONF")


def _env(tmp_path: Path) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k.upper() not in NAMES}
    # an OPENSSL_CONF that names a file that is not there: honoured, OpenSSL would try to open it
    return {**base, "SSLKEYLOGFILE": str(tmp_path / "keys.log"), "OPENSSL_CONF": str(tmp_path / "evil.cnf")}


def _run(code: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args], capture_output=True, text=True, env=env, timeout=120, encoding="utf-8", errors="replace",
    )


def test_a_process_that_imports_nexus_survives_a_repo_set_ssl_key_log_file(tmp_path: Path) -> None:
    """The process makes a default TLS context (what every non-bearer ``httpx``/``urllib`` request does) with
    ``SSLKEYLOGFILE`` set. It must exit 0 and write no key log; on Windows it used to die with no OPENSSL_Applink."""
    code = (
        "import nexus, os, ssl\n"
        "ctx = ssl.create_default_context()\n"
        "print(ctx.verify_mode == ssl.CERT_REQUIRED, os.environ.get('SSLKEYLOGFILE'), os.environ.get('OPENSSL_CONF'))\n"
    )
    proc = _run(code, _env(tmp_path))
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    assert proc.stdout.strip() == "True None None", proc.stdout
    assert not (tmp_path / "keys.log").exists()


def test_the_nx_cli_survives_the_same_environment(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from nexus.cli import main; sys.argv = ['nx', '--version']; main()"],
        capture_output=True, text=True, env=_env(tmp_path), timeout=120, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    assert not (tmp_path / "keys.log").exists()


def test_a_non_bearer_https_request_survives_a_repo_set_ssl_key_log_file(tmp_path: Path) -> None:
    """The request path the residual named: httpx with its own default context (what a request that is not a nexus
    bearer or probe builds, a model download or a bibliographic lookup), made against a closed port so it fails to
    connect and never needs the network."""
    code = (
        "import nexus, httpx, ssl\n"
        "from nexus.db.t2.shared_ssl import get_shared_ssl_context\n"
        "get_shared_ssl_context()\n"
        "try:\n"
        "    httpx.get('https://127.0.0.1:9/', timeout=2)\n"
        "except httpx.HTTPError as exc:\n"
        "    print(type(exc).__name__)\n"
    )
    proc = _run(code, _env(tmp_path))
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    assert proc.stdout.strip() in ("ConnectError", "ConnectTimeout"), proc.stdout
    assert not (tmp_path / "keys.log").exists()


def test_the_plugins_endpoint_resolver_drops_both_on_import(tmp_path: Path) -> None:
    """The plugin runs as bare python with no ``nexus`` importable: its own TLS module scrubs the process, before it
    builds any context."""
    code = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import _endpoint_resolve\n"
        "import ssl\n"
        "ctx = ssl.create_default_context()\n"
        "print(os.environ.get('SSLKEYLOGFILE'), os.environ.get('OPENSSL_CONF'))\n"
    )
    proc = _run(code, _env(tmp_path))
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    assert proc.stdout.strip() == "None None", proc.stdout
    assert not (tmp_path / "keys.log").exists()


def test_the_harness_is_not_vacuous(tmp_path: Path) -> None:
    """A process that does not import nexus keeps what the environment names: the scrubs above are the package's doing."""
    proc = _run("import os; print(os.environ.get('SSLKEYLOGFILE') is not None, os.environ.get('OPENSSL_CONF') is not None)", _env(tmp_path))
    assert proc.returncode == 0 and proc.stdout.strip() == "True True", (proc.stdout, proc.stderr[-200:])


@pytest.fixture(params=["plugin", "src"])
def audience(request, tmp_path, monkeypatch):
    path = SCRIPTS / "_audience.py" if request.param == "plugin" else ROOT / "src" / "nexus" / "hooks" / "_audience.py"
    spec = importlib.util.spec_from_file_location(f"_audience_scrub_{request.param}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    home = tmp_path / "acct"
    home.mkdir()
    monkeypatch.setattr(module, "_ACCOUNT_HOME_OVERRIDE", str(home))
    return module


def test_the_telemetry_child_is_started_without_them_whatever_the_case(audience) -> None:
    env = audience.telemetry_child_env({
        "SSLKEYLOGFILE": "x", "sslkeylogfile": "x", "OPENSSL_CONF": "y", "openssl_conf": "y", "PATH": "/bin",
    })
    assert env is not None
    assert not {k.upper() for k in env} & set(NAMES), env
    assert env["PATH"] == "/bin"
