# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.6 — provision-and-serve sequence for ``nx guided-upgrade``.

Stage 2 of the guided upgrade: stand up the bge-768 engine-service backend so
the readiness gate (ez5.5) and version-pin (ez5.4) have something to probe, and
ez5.7 can emit a VERIFIED service_url. To avoid divergence from
``nx init --service``, this reuses the SAME two init steps (provision Postgres,
then the single persistent-supervisor start path) — it does not reimplement
provisioning. The serve step returns the live lease; the sequence derives the
service_url from the lease endpoint.
"""

from __future__ import annotations

import pytest

from nexus.migration.guided_upgrade import ProvisionResult, provision_and_serve


class _FakeLease:
    def __init__(self, endpoint: dict, generation: int) -> None:
        self.endpoint = endpoint
        self.generation = generation


class TestProvisionAndServe:
    def test_provision_runs_before_serve_and_returns_url(self) -> None:
        order: list[str] = []

        def provision_step() -> None:
            order.append("provision")

        def serve_step() -> _FakeLease:
            order.append("serve")
            return _FakeLease(
                {"host": "127.0.0.1", "port": 8099, "pid": 4242}, generation=3
            )

        result = provision_and_serve(
            provision_step=provision_step, serve_step=serve_step
        )

        assert isinstance(result, ProvisionResult)
        # Postgres MUST be provisioned before the service supervisor starts.
        assert order == ["provision", "serve"]
        assert result.service_url == "http://127.0.0.1:8099"
        assert result.host == "127.0.0.1"
        assert result.port == 8099
        assert result.pid == 4242
        assert result.generation == 3

    def test_provision_failure_aborts_before_serve(self) -> None:
        served = []

        def provision_step() -> None:
            raise SystemExit(1)

        def serve_step():  # noqa: ANN202
            served.append(True)
            return _FakeLease({"host": "h", "port": 1}, generation=0)

        with pytest.raises(SystemExit):
            provision_and_serve(
                provision_step=provision_step, serve_step=serve_step
            )
        # Never start a service on a failed provision.
        assert served == []

    def test_malformed_lease_endpoint_is_loud(self) -> None:
        # A lease with no host/port must NOT yield a bogus service_url.
        def provision_step() -> None:
            pass

        def serve_step() -> _FakeLease:
            return _FakeLease({"pid": 1}, generation=0)  # missing host/port

        with pytest.raises(RuntimeError, match="host.*port|endpoint"):
            provision_and_serve(
                provision_step=provision_step, serve_step=serve_step
            )
