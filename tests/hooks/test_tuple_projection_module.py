# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported tuple projections (RDR-215 bead nexus-q02nx.20, rewired
in-process at bead nexus-b5ugt).

Bead nexus-q02nx.21 deleted the two bash wrappers
(``subagent-start-tuple-async.sh``, ``subagent-stop-tuple-async.sh``) and
``tests/hooks/test_subagent_tuple_async_wrappers.py`` that drove them; this
file holds the port to the same properties those wrappers were written to
guarantee.

Bead nexus-b5ugt then removed the plugin-script SUBPROCESS entirely:
``_project`` used to resolve ``conexus/hooks/scripts/tuple_ledger_project.py``
via ``CLAUDE_PLUGIN_ROOT`` (or a checkout-relative fallback) and spawn it as
a ``python3`` subprocess. Neither candidate resolves under an installed
wheel with ``CLAUDE_PLUGIN_ROOT`` set to the LITERAL, unexpanded string
``${CLAUDE_PLUGIN_ROOT}`` (Claude Code does not expand ``${...}`` in an MCP
``env`` block) -- which is exactly ``conexus/.mcp.json``'s real, shipped
value, and is why every RDR-205 ledger projection on a live installed box
had been silently writing nothing (``tuple_projection_no_projector`` on
every SubagentStart/SubagentStop). ``_project`` now calls
:func:`nexus.hooks.tuple_ledger_project.project` directly, in-process --
there is no plugin script to resolve any more, and every test in this file
that used to seal or exercise that resolution (``_sealed``,
``TestAMissingProjectorIsANoOp``, ``TestWhatItSubprocesses``) has gone with
it. ``TestTheHistoricalDefectIsFixed`` below is the direct regression proof:
it reproduces the exact env shape that broke on the live box and asserts
the tuple still lands.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from nexus.hooks import tuple_ledger_project
from nexus.hooks import tuple_projection as proj


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    """The projector's own per-session log file lives under
    ``XDG_STATE_HOME`` (see ``tuple_ledger_project._default_state_dir``).
    Every test in this file that reaches ``_Skip`` -- which, thanks to the
    repo-wide ``_isolate_config_dir``/``_isolate_service_endpoint_env``
    autouse fixtures, is the default outcome for any test that does not
    explicitly wire up an engine + lease -- writes a diagnostic line there.
    Without this, an unattended suite run would accumulate real files
    under the developer's own ``~/.local/state/nexus/orchestration/``."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _settle(predicate, timeout: float = 5.0) -> bool:
    """Wait on a daemon thread's side effect. Nothing joins these
    threads by design, so a test that reads their effect must poll."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestItReturnsImmediately:
    """The property both wrappers exist for, and the one their own
    header calls out as CA 4: the hook returns even when the projection
    would block."""

    def test_a_hanging_projector_does_not_delay_the_hook(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def _blocking(_verb, _body):
            started.set()
            release.wait(timeout=30)

        monkeypatch.setattr(proj, "_project", _blocking)

        began = time.monotonic()
        result = proj.run_stop({"agent_id": "a1"})
        elapsed = time.monotonic() - began

        assert started.wait(timeout=5), "the projection thread never started"
        assert elapsed < 1.0, (
            f"the hook waited {elapsed:.2f}s on a blocking projection; not "
            "waiting is the whole reason the wrapper it replaces exists"
        )
        assert result.stdout is None
        release.set()

    @pytest.mark.parametrize("entry", ["run_start", "run_stop"])
    def test_both_entries_return_nothing_on_stdout(self, entry):
        """These are SIBLINGS of the real hooks, not decision-makers. A
        byte on stdout here would join a decision they take no part in."""
        result = getattr(proj, entry)({"agent_id": "a1"})
        assert result.stdout is None
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "payload", [None, {}, {"agent_id": ""}, {"junk": object()}]
    )
    def test_no_payload_shape_can_raise(self, payload):
        """``{"junk": object()}`` is the one that matters: with no
        subprocess boundary any more, the payload dict is passed straight
        through to the in-process projector -- this proves an odd value in
        an ignored key still cannot raise into the hook."""
        assert proj.run_start(payload).exit_code == 0


class TestTheThread:
    def test_it_is_a_daemon(self, monkeypatch):
        """Deliberate, and strictly WEAKER than the bash: a disowned
        process outlives the hook, a daemon thread dies at interpreter
        exit. Recorded in the module docstring rather than hidden."""
        seen: dict = {}
        real = threading.Thread

        def _capture(*a, **k):
            seen.update(daemon=k.get("daemon"), name=k.get("name"))
            return real(*a, **k)

        monkeypatch.setattr(proj.threading, "Thread", _capture)
        monkeypatch.setattr(proj, "_project", lambda *_a: None)
        proj.run_start({"agent_id": "a1"})
        assert seen["daemon"] is True
        assert seen["name"] == "tuple-projection-start"

    def test_the_two_entries_use_different_verbs(self, monkeypatch):
        """start vs report. Swapping them would write the wrong tuple
        kind and, because the id derives from (agent_id, kind), would
        land on a DIFFERENT tuple rather than colliding visibly."""
        verbs: list[str] = []
        monkeypatch.setattr(proj, "_project", lambda v, _b: verbs.append(v))
        proj.run_start({"agent_id": "a1"})
        assert _settle(lambda: verbs == ["start"]), verbs
        proj.run_stop({"agent_id": "a1"})
        assert _settle(lambda: verbs == ["start", "report"]), verbs

    def test_the_payload_reaches_the_projector_as_a_dict(self, monkeypatch):
        """No serialization boundary any more (bead nexus-b5ugt) -- the
        payload dict is passed straight through to ``_project``, exactly
        as the hook itself received it, rather than JSON-encoded for a
        subprocess's stdin."""
        bodies: list[dict] = []
        monkeypatch.setattr(proj, "_project", lambda _v, b: bodies.append(b))
        proj.run_stop({"agent_id": "a1", "agent_type": "conexus:developer"})
        assert _settle(lambda: len(bodies) == 1), bodies
        assert bodies[0] == {"agent_id": "a1", "agent_type": "conexus:developer"}


class TestTheProjectionCall:
    """``_project`` no longer spawns a subprocess (bead nexus-b5ugt) -- it
    calls :func:`nexus.hooks.tuple_ledger_project.project` directly, on its
    own bounded inner thread (see the module docstring's "ONE PROPERTY
    PRESERVED" section for why the bound moved here)."""

    def test_project_is_called_with_the_verb_and_the_raw_payload(self, monkeypatch):
        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            tuple_ledger_project, "project", lambda v, p: calls.append((v, p))
        )
        proj._project("report", {"agent_id": "a1"})
        assert calls == [("report", {"agent_id": "a1"})]

    def test_a_raising_projection_is_logged_and_never_escapes(self, monkeypatch):
        def _boom(*_a):
            raise RuntimeError("boom")

        monkeypatch.setattr(tuple_ledger_project, "project", _boom)
        emitted: list[tuple] = []
        monkeypatch.setattr(
            proj, "_emit", lambda lvl, ev, **kw: emitted.append((lvl, ev, kw))
        )
        proj._project("start", {})  # must not raise
        assert emitted == [("warning", "tuple_projection_failed", {"verb": "start", "error": "boom"})]

    def test_a_wedged_projection_times_out_without_hanging_the_caller(self, monkeypatch):
        monkeypatch.setattr(proj, "_TIMEOUT_S", 0.05)
        release = threading.Event()
        monkeypatch.setattr(
            tuple_ledger_project, "project", lambda *_a: release.wait(timeout=10)
        )
        emitted: list[tuple] = []
        monkeypatch.setattr(
            proj, "_emit", lambda lvl, ev, **kw: emitted.append((lvl, ev, kw))
        )

        began = time.monotonic()
        proj._project("start", {})
        elapsed = time.monotonic() - began

        assert elapsed < 2.0, (
            f"_project blocked {elapsed:.2f}s past its own 0.05s timeout -- "
            "a wedged projection must not hold this thread open indefinitely"
        )
        assert emitted == [("warning", "tuple_projection_timeout", {"verb": "start", "timeout_s": 0.05})]
        release.set()

    def test_a_successful_projection_says_so(self, monkeypatch):
        """Both outcomes are logged, not just the bad one (nexus-q02nx.24):
        with nothing on the success path the log cannot distinguish a
        clean projection from a thread that never ran."""
        monkeypatch.setattr(tuple_ledger_project, "project", lambda *_a: None)
        emitted: list[str] = []
        monkeypatch.setattr(proj, "_emit", lambda _lvl, ev, **_kw: emitted.append(ev))
        proj._project("start", {})
        assert emitted == ["tuple_projection_ok"]


# ── Mock /v1/tuples/out engine (mirrors tests/hooks/test_tuple_ledger_project.py) ──


class _MockTupleEngine:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict] = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                try:
                    engine.requests.append(json.loads(body.decode("utf-8")))
                except json.JSONDecodeError:
                    engine.requests.append({})
                self.send_response(engine.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def mock_engine():
    engines: list[_MockTupleEngine] = []

    def make(status: int = 200) -> _MockTupleEngine:
        e = _MockTupleEngine(status=status)
        engines.append(e)
        return e

    yield make
    for e in engines:
        e.close()


def _write_fresh_data_token_lease(config_dir, *, base_url: str, token: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        f"{urlsplit(base_url).netloc}\x00default".encode("utf-8")
    ).hexdigest()
    record = {
        "format_version": 1,
        "token": token,
        "tenant": "default",
        "base_url_digest": digest,
        "expires_at": time.time() + 3600.0,
        "ttl_seconds": 3600.0,
        "minted_by_pid": 0,
    }
    (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))


class TestTheBackgroundedWriteActuallyLands:
    """The end-to-end proof, ported from
    ``tests/hooks/test_subagent_tuple_async_wrappers.py`` (RDR-215 bead
    nexus-q02nx.21, which deleted the bash wrappers this file's other
    classes already replace): the thread returns fast, but the projection
    it starts must still do the work, confirmed by polling a REAL HTTP
    server after ``run_start`` has already returned.

    Bead nexus-b5ugt removed the subprocess entirely, so there is no
    resolver left to un-seal here -- this now drives the real, in-process
    :func:`nexus.hooks.tuple_ledger_project.project` with nothing mocked
    but the far side of the wire.
    """

    def test_eventually_the_backgrounded_write_actually_lands(
        self, monkeypatch, tmp_path, mock_engine
    ):
        engine = mock_engine(status=200)
        config_dir = tmp_path / "config"
        _write_fresh_data_token_lease(config_dir, base_url=engine.base_url, token="async-e2e-token")

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)

        began = time.monotonic()
        result = proj.run_start({
            "session_id": "sess-async-wrap",
            "agent_id": "aworkerasyncwrap",
            "agent_type": "developer",
        })
        elapsed = time.monotonic() - began
        assert result.stdout is None
        assert elapsed < 1.0, (
            f"run_start took {elapsed:.2f}s -- must return before the "
            "background thread's own POST completes"
        )

        assert _settle(lambda: bool(engine.requests), timeout=5.0), (
            "the backgrounded write never reached the engine within 5s"
        )
        assert engine.requests[0]["subspace"] == "ledger/sess-async-wrap"
        assert engine.requests[0]["keys"] == {
            "agent_id": "aworkerasyncwrap", "kind": "start",
        }


class TestTheHistoricalDefectIsFixed:
    """RDR-215 bead nexus-b5ugt: the live defect this bead fixes,
    reproduced directly.

    ``conexus/.mcp.json`` sets the MCP server's env to
    ``{"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}`` and Claude Code
    does not expand ``${...}`` inside an MCP ``env`` block -- every real
    ``nx-mcp`` process therefore carries that LITERAL string. The OLD
    ``_projector()`` tried that literal as a path (never real), then a
    checkout-relative fallback (absent under any installed wheel); both
    missed, and every SubagentStart/SubagentStop projection on this box
    had been silently writing nothing.

    ``monkeypatch.setattr(proj, "checkout_plugin_root", ..., raising=False)``
    is deliberate: the FIXED module carries no such attribute at all (there
    is no plugin script left to resolve), so ``raising=False`` lets this
    same test run unchanged against the fixed code -- where the patch is
    simply inert -- and against a reverted ``tuple_projection.py``, where
    it defeats the checkout fallback exactly as the installed-wheel case
    does, reproducing the historical failure honestly rather than testing
    that a function was merely called.
    """

    def test_the_tuple_lands_even_with_the_literal_unexpanded_env_var(
        self, monkeypatch, tmp_path, mock_engine
    ):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "${CLAUDE_PLUGIN_ROOT}")
        monkeypatch.setattr(
            proj, "checkout_plugin_root", lambda: tmp_path / "no-such-checkout",
            raising=False,
        )

        engine = mock_engine(status=200)
        config_dir = tmp_path / "config"
        _write_fresh_data_token_lease(config_dir, base_url=engine.base_url, token="wheel-only-token")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)

        result = proj.run_start({
            "session_id": "sess-no-plugin-root",
            "agent_id": "aworkerwheelonly",
            "agent_type": "developer",
        })
        assert result.stdout is None

        assert _settle(lambda: bool(engine.requests), timeout=5.0), (
            "the projection never reached the engine even though this "
            "process carries the ported module in-process -- this is the "
            "live nexus-b5ugt regression: CLAUDE_PLUGIN_ROOT is the "
            "literal, unexpanded string every real nx-mcp process carries, "
            "and no checkout fallback is available either"
        )
        assert engine.requests[0]["subspace"] == "ledger/sess-no-plugin-root"
        assert engine.requests[0]["keys"] == {
            "agent_id": "aworkerwheelonly", "kind": "start",
        }
