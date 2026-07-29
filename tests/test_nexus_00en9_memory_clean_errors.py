# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-00en9: `nx memory` prints a clean one-liner (no raw traceback) when the
T2 backend errors mid-RPC — for BOTH backends.

Originally pinned two gaps, one per backend. The daemon/SQLite half (a
reachable-but-contended daemon returning ``T2ClientError``, the original 00en9
symptom on 5.10.2) retired with the daemon in nexus-i711w Stage 2 sub-stage B —
see the tombstones below.

What remains is the **service path**: in SERVICE mode ``t2_handle`` routes to an
``HttpMemoryStore``, and a down/unreachable service raises ``httpx.HTTPError``.
That branch originally had no error catch at all.

Caught in the single choke point ``t2_handle`` so every ``nx memory``
subcommand benefits.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# NO TestDaemonContendedLockError: it drove the SQLite/daemon branch with
# NX_STORAGE_BACKEND=sqlite and injected a T2ClientError ("database is locked")
# at ``T2Client.call``. Both the client and the daemon retired in nexus-i711w
# Stage 2 sub-stage B, and `t2_handle`'s SQLite branch now raises before
# constructing any client, so that patch target is unreachable. Its sibling
# ``test_list_locked_db_clean`` still PASSED afterwards (it asserts only
# non-zero exit + no traceback, which the new fail-loud branch satisfies) — a
# vacuous green, removed with the rest rather than left as false coverage.
#
# ``TestServiceUnreachable`` below is the surviving half and always was the
# go-live-relevant one: the choke point it exercises (`t2_handle`) is the same,
# only the reachable backend has changed.


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


# NO TestProtocolErrorVersionSkew: a frame-level ProtocolError was a DAEMON
# wire-protocol condition, signalling client/daemon version skew. With no daemon
# there is no frame and no skew of that kind; the engine's version relationship
# is governed by REQUIRED_ENGINE_VERSION, not a per-RPC frame check.
