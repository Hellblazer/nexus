# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-a2qhz acceptance repro — real subprocesses, the exact incident shapes.

Three real incidents (2026-08-19, 2026-08-21 x2, see the bead) reached Sam's
PRODUCTION T2/catalog substrate from a `uv run python -c` one-liner and a
scratchpad script, both running OUTSIDE pytest's `_pin_t2_substrate` autouse
fixture. These tests reproduce that exact shape as real subprocesses and
assert the write is refused.

SAFETY (mandatory for every test here): no test may reach a real network
endpoint. Every subprocess resolves its endpoint from an ISOLATED
NEXUS_CONFIG_DIR carrying a config.yml with a FAKE `service_url` — either a
guaranteed-unreachable localhost port (refusal-path tests) or a local stub
HTTPServer this test process itself starts and controls (the opt-in
proceeds-to-network test). No NX_SERVICE_* env override is set for the
refusal-path tests, so resolution takes the "ambient default" leg
(config.yml) the guard is designed to catch — never NX_MINT_TOKEN, so
construction never attempts a real data-token mint call either.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_fake_prod_config(config_dir: Path, *, service_url: str, service_token: str = "fake-token") -> None:
    """A config.yml carrying ONLY service_url/service_token — deliberately
    no mint_token, so construction never attempts a real mint call."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yml").write_text(
        yaml.dump({"credentials": {"service_url": service_url, "service_token": service_token}})
    )


def _scrubbed_env(config_dir: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A subprocess env with the ambient endpoint halves cleared and
    NEXUS_CONFIG_DIR pointed at *config_dir* — the "default config
    resolution" shape the acceptance criteria call for."""
    env = dict(os.environ)
    for var in (
        "NX_SERVICE_URL",
        "NX_SERVICE_HOST",
        "NX_SERVICE_PORT",
        "NX_SERVICE_TOKEN",
        "NX_MINT_TOKEN",
        "NX_ALLOW_PROD_WRITE",
    ):
        env.pop(var, None)
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    if extra:
        env.update(extra)
    return env


_T2_WRITE_SCRIPT = """
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

class _EchoStore(RefreshableHttpStoreMixin):
    def echo_post(self):
        return self._post("/v1/echo", {"value": "x"})

_EchoStore().echo_post()
print("WRITE-SUCCEEDED")
"""


class TestDefaultConfigResolutionWriteRefused:
    """Acceptance repro (1): a subprocess running `python -c` from the
    checkout with default config resolution attempting a T2 write is
    refused with the loud message."""

    def test_t2_write_from_default_config_resolution_is_refused(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "nexus-config"
        _write_fake_prod_config(config_dir, service_url="http://127.0.0.1:1")
        env = _scrubbed_env(config_dir)

        proc = subprocess.run(
            [sys.executable, "-c", _T2_WRITE_SCRIPT],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert proc.returncode != 0, (
            f"expected the write to be refused; stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "WRITE-SUCCEEDED" not in proc.stdout
        assert "ProductionWriteGuardError" in proc.stderr
        assert "NX_ALLOW_PROD_WRITE" in proc.stderr
        assert "http://127.0.0.1:1" in proc.stderr


class TestScriptOutsideTestsImportingFixtureOpsRefused:
    """Acceptance repro (2): a script OUTSIDE tests/ importing
    tests._catalog_fixture_ops and attempting a register is refused — the
    exact 2026-08-21 scratchpad-script incident shape. The script's own
    cwd is a scratch directory entirely outside the checkout; only
    PYTHONPATH resolves the `tests` package, proving the guard's dev-
    checkout detection is import-based, never cwd-based."""

    def test_register_from_outside_tests_is_refused(self, tmp_path: Path) -> None:
        scratch_dir = tmp_path / "scratchpad"
        scratch_dir.mkdir()
        script_path = scratch_dir / "verify_fix.py"
        script_path.write_text(
            "from tests._catalog_fixture_ops import register_real_doc_id\n"
            "register_real_doc_id()\n"
            'print("WRITE-SUCCEEDED")\n'
        )

        config_dir = tmp_path / "nexus-config"
        _write_fake_prod_config(config_dir, service_url="http://127.0.0.1:1")
        env = _scrubbed_env(config_dir, extra={"PYTHONPATH": str(_REPO_ROOT)})

        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=scratch_dir,  # deliberately NOT the checkout root, NOT tests/
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert proc.returncode != 0, (
            f"expected the register to be refused; stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "WRITE-SUCCEEDED" not in proc.stdout
        assert "ProductionWriteGuardError" in proc.stderr
        assert "NX_ALLOW_PROD_WRITE" in proc.stderr


class TestExportedProductionServiceUrlStillRefused:
    """Ship-blocker fix (second review round): conexus's own documented
    cloud onboarding (docs/getting-started.md, docs/managed-onboarding.md)
    instructs `export NX_SERVICE_URL=https://api.conexus-nexus.com` --
    exactly the permanently-exported shell shape every recorded incident
    ran under. The guard must refuse this write anyway; only the
    reason-bearing opt-in exempts it. SAFE: the guard raises inside
    _send(), BEFORE _request_once ever dials the URL -- construction
    itself resolves the (env-supplied) host/token from plain env reads,
    no network -- so this subprocess never attempts a real connection to
    the managed host."""

    def test_write_with_production_host_exported_is_refused(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "nexus-config"
        config_dir.mkdir(parents=True)
        env = dict(os.environ)
        for var in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_MINT_TOKEN", "NX_ALLOW_PROD_WRITE"):
            env.pop(var, None)
        env["NEXUS_CONFIG_DIR"] = str(config_dir)
        env["NX_SERVICE_URL"] = "https://api.conexus-nexus.com"
        env["NX_SERVICE_TOKEN"] = "a-real-looking-bearer-token"

        proc = subprocess.run(
            [sys.executable, "-c", _T2_WRITE_SCRIPT],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert proc.returncode != 0, (
            f"expected the write to be refused despite the exported production "
            f"NX_SERVICE_URL; stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "WRITE-SUCCEEDED" not in proc.stdout
        assert "ProductionWriteGuardError" in proc.stderr
        assert "NX_ALLOW_PROD_WRITE" in proc.stderr
        assert "api.conexus-nexus.com" in proc.stderr


class _CountingEchoHandler(BaseHTTPRequestHandler):
    """Minimal local stub: 200s every POST, echoing the body. Used only to
    prove the write PATH proceeds past the guard when opted in — never a
    real substrate."""

    hits: list[str] = []

    def log_message(self, *_a: object) -> None:  # noqa: D102 — suppress test noise
        pass

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).hits.append(self.path)
        body = b'{"echo": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestOptInProceedsToStubSubstrate:
    """Acceptance repro (3): with the opt-in set, the write path proceeds
    (against a local stub substrate this test starts and controls — never
    production)."""

    def test_opt_in_write_reaches_the_stub_server(self, tmp_path: Path) -> None:
        _CountingEchoHandler.hits = []
        server = HTTPServer(("127.0.0.1", 0), _CountingEchoHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config_dir = tmp_path / "nexus-config"
            _write_fake_prod_config(config_dir, service_url=f"http://127.0.0.1:{port}")
            env = _scrubbed_env(
                config_dir,
                extra={
                    "NX_ALLOW_PROD_WRITE": (
                        "acceptance-test proof that the opt-in lets a write "
                        "proceed -- targets a local stub, never production"
                    )
                },
            )

            proc = subprocess.run(
                [sys.executable, "-c", _T2_WRITE_SCRIPT],
                cwd=_REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            server.shutdown()
            server.server_close()

        assert proc.returncode == 0, (
            f"expected the opted-in write to succeed against the stub; "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "WRITE-SUCCEEDED" in proc.stdout
        assert "ProductionWriteGuardError" not in proc.stderr
        assert _CountingEchoHandler.hits == ["/v1/echo"], (
            "the guard must not have blocked the write -- the stub should "
            f"have seen exactly one POST /v1/echo, saw {_CountingEchoHandler.hits!r}"
        )


@pytest.mark.parametrize(
    "opt_in_value",
    ["0", "true", "yes", ""],
)
class TestOptInWrongValueStillRefuses:
    """Only the literal "1" opts in (unit-level coverage already lives in
    test_production_write_guard.py; this is the subprocess-level
    reconfirmation for the exact acceptance-repro shape)."""

    def test_non_one_opt_in_value_is_still_refused(
        self, opt_in_value: str, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / "nexus-config"
        _write_fake_prod_config(config_dir, service_url="http://127.0.0.1:1")
        env = _scrubbed_env(config_dir, extra={"NX_ALLOW_PROD_WRITE": opt_in_value})

        proc = subprocess.run(
            [sys.executable, "-c", _T2_WRITE_SCRIPT],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert proc.returncode != 0
        assert "ProductionWriteGuardError" in proc.stderr
