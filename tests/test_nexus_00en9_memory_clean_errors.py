# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-00en9: `nx memory` prints a clean one-liner (no raw traceback) when the
T2 backend errors mid-RPC — for BOTH backends.

GH-1061 E3 already covers daemon *discovery* failures
(``T2DaemonNotReachableError`` / ``T2SchemaVersionMismatchError``). This pins the
two gaps it left open, both of which surface as raw tracebacks today:

1. **Daemon/SQLite path** — a reachable-but-contended daemon returns a
   ``T2ClientError`` (e.g. ``database is locked`` under write contention). The
   original 00en9 symptom on 5.10.2.
2. **Service path (go-live)** — in SERVICE mode ``t2_handle`` routes to an
   ``HttpMemoryStore``; a down/unreachable service raises ``httpx.HTTPError``.
   The service branch had no error catch at all.

Both are caught in the single choke point ``t2_handle`` so every ``nx memory``
subcommand benefits.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_client import T2ClientError


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestDaemonContendedLockError:
    """SQLite-mode: a reachable daemon returning a locked-DB error must be clean."""

    def _locked_error(self) -> T2ClientError:
        return T2ClientError(
            error_type="OperationalError",
            message="database is locked",
            op="memory.put",
        )

    def test_put_locked_db_clean_one_liner(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")  # exercise the daemon branch
        with patch(
            "nexus.daemon.t2_client.T2Client.call", side_effect=self._locked_error()
        ):
            result = runner.invoke(
                main, ["memory", "put", "hello", "--project", "p", "--title", "t.md"]
            )
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output, result.output
        assert "T2ClientError" not in result.output, result.output
        # The daemon-side message must survive into the clean one-liner.
        assert "database is locked" in result.output, result.output
        # Actionable hint: the daemon is contended/degraded.
        assert "nx daemon t2 status" in result.output, result.output

    def test_list_locked_db_clean(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        with patch(
            "nexus.daemon.t2_client.T2Client.call", side_effect=self._locked_error()
        ):
            result = runner.invoke(main, ["memory", "list"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output, result.output


class TestServiceUnreachable:
    """SERVICE-mode (go-live): a down storage service must read as a clean error,
    not a raw traceback. Two distinct, both-real failure points:

    (a) PRE-YIELD construction — endpoint not resolvable: HttpMemoryStore's
        resolve_service_config() raises RuntimeError during T2Database.__init__,
        BEFORE t2_handle yields. This is the common "service never started" case
        and the one a post-yield-only catch would miss (the structural gap the
        substantive-critic flagged).
    (b) POST-YIELD RPC — endpoint resolved (lease existed) but the service is
        unreachable/erroring when the RPC fires: httpx transport/status error.
    """

    def _fake_t2db_construct_raises(self, exc: Exception):
        """A T2Database factory that raises *exc* AT CONSTRUCTION (pre-yield),
        faithfully reproducing resolve_service_config's fail-loud path."""

        def _factory(*a, **k):
            raise exc

        return _factory

    def _fake_t2db_rpc_raises(self, exc: Exception):
        """A stand-in T2Database that constructs fine but whose .memory ops raise
        *exc* (post-yield RPC failure); .close() is a noop."""

        class _Memory:
            def list_entries(self, *a, **k):
                raise exc

            def put(self, *a, **k):
                raise exc

        class _FakeDB:
            memory = _Memory()

            def close(self) -> None:
                pass

        return lambda *a, **k: _FakeDB()

    def test_list_service_endpoint_unresolvable_clean_PRE_YIELD(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The REAL service-never-started path: construction raises RuntimeError.
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        exc = RuntimeError(
            "nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND=service): "
            "start the supervisor with 'nx daemon service start'"
        )
        with patch("nexus.db.t2.T2Database", self._fake_t2db_construct_raises(exc)):
            result = runner.invoke(main, ["memory", "list"])
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output, result.output
        # The fail-loud RuntimeError message must survive into the clean error.
        assert "not resolvable" in result.output, result.output
        assert "service" in result.output, result.output

    def test_list_service_down_clean_POST_YIELD(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        exc = httpx.ConnectError("All connection attempts failed")  # TransportError
        with patch("nexus.db.t2.T2Database", self._fake_t2db_rpc_raises(exc)):
            result = runner.invoke(main, ["memory", "list"])
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output, result.output
        assert "ConnectError" not in result.output, result.output
        assert "nx doctor" in result.output, result.output

    def test_put_service_http_status_error_clean_POST_YIELD(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        req = httpx.Request("POST", "http://127.0.0.1:9/memory")
        resp = httpx.Response(503, request=req, text="service unavailable")
        exc = httpx.HTTPStatusError("503", request=req, response=resp)
        with patch("nexus.db.t2.T2Database", self._fake_t2db_rpc_raises(exc)):
            result = runner.invoke(
                main, ["memory", "put", "hi", "--project", "p", "--title", "t.md"]
            )
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output, result.output
        assert "nx doctor" in result.output, result.output

    def test_decode_error_is_NOT_swallowed_as_reachability(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # S2: a service-side bug (malformed JSON -> DecodingError) must NOT be
        # aliased to a "check the service" reachability hint; it is not a
        # TransportError/HTTPStatusError so it propagates (non-zero, no doctor hint).
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        exc = httpx.DecodingError("malformed body")
        with patch("nexus.db.t2.T2Database", self._fake_t2db_rpc_raises(exc)):
            result = runner.invoke(main, ["memory", "list"])
        assert result.exit_code != 0, result.output
        # Not converted to the reachability ClickException.
        assert "Check the storage service: nx doctor" not in result.output, result.output


class TestProtocolErrorVersionSkew:
    """S1: a frame-level ProtocolError (version skew) gets the version-skew remedy,
    not the transient-contention 'retry' hint."""

    def test_protocol_error_points_at_version_skew(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        exc = T2ClientError(
            error_type="ProtocolError",
            message="unknown op id 7",
            op="memory.list_entries",
        )
        with patch("nexus.daemon.t2_client.T2Client.call", side_effect=exc):
            result = runner.invoke(main, ["memory", "list"])
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output, result.output
        assert "version skew" in result.output, result.output
        # Must NOT give the plain contention retry hint.
        assert "nx daemon t2 status" not in result.output, result.output
