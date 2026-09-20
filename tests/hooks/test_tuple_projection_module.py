# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported tuple projections (RDR-215 bead nexus-q02nx.20).

Bead nexus-q02nx.21 deleted the two bash wrappers
(``subagent-start-tuple-async.sh``, ``subagent-stop-tuple-async.sh``) and
``tests/hooks/test_subagent_tuple_async_wrappers.py`` that drove them; this
file holds the port to the same properties those wrappers were written to
guarantee — and names the one they guarantee BETTER than a daemon thread
can. ``TestTheBackgroundedWriteActuallyLands`` is the one scenario from
that file with no equivalent above: an end-to-end proof, against a real
HTTP server, that the detached subprocess still does the work after the
hook itself has already returned.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from nexus.hooks import tuple_projection as proj

#: The real resolver, captured before any fixture can replace it. The
#: seal below points ``_projector`` at nothing, and the one test that
#: must exercise real resolution needs a way back to the original.
_REAL_PROJECTOR = proj._projector


@pytest.fixture(autouse=True)
def _sealed(monkeypatch):
    """No test in this file may reach the real projector.

    Pointing ``CLAUDE_PLUGIN_ROOT`` at an empty directory does NOT do
    that: ``_projector`` falls back to this checkout, finds the real
    script, and a test that merely forgot to stub ``_project`` would
    subprocess it against a live tuple space. Scrubbing the env var is
    not the same as having no projector, so the resolver itself is
    sealed and each test opens exactly the hole it needs.
    """
    monkeypatch.setattr(proj, "_projector", lambda: None)


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
        """``{"junk": object()}`` is the one that matters: the body is
        JSON-encoded on the hook's own thread, before the spawn, so an
        unserialisable value would raise INTO the hook rather than into
        the thread that is allowed to fail."""
        assert proj.run_start(payload).exit_code == 0


class TestTheThread:
    def test_it_is_a_daemon(self, monkeypatch):
        """Deliberate, and strictly WEAKER than the bash: a disowned
        process outlives the hook, a daemon thread dies at interpreter
        exit. Recorded in the module docstring rather than hidden,
        because a lost projection is simply lost — the write is
        idempotent so a retry would be safe, but nothing retries and a
        stopped agent has no later firing."""
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

    def test_the_payload_reaches_the_projector_as_json(self, monkeypatch):
        bodies: list[str] = []
        monkeypatch.setattr(proj, "_project", lambda _v, b: bodies.append(b))
        proj.run_stop({"agent_id": "a1", "agent_type": "conexus:developer"})
        assert _settle(lambda: len(bodies) == 1), bodies
        assert json.loads(bodies[0])["agent_id"] == "a1"


class TestAMissingProjectorIsANoOp:
    def test_it_warns_rather_than_failing_silently(self, monkeypatch):
        """The bash's failure mode here was an empty background subshell
        — silent. This warns, because a projection that never ran is
        otherwise indistinguishable from one that ran and found nothing
        to do."""
        emitted: list[tuple] = []
        monkeypatch.setattr(
            proj, "_emit", lambda lvl, ev, **kw: emitted.append((lvl, ev, kw))
        )
        proj._project("start", "{}")
        assert emitted == [("warning", "tuple_projection_no_projector",
                            {"verb": "start"})]

    def test_resolution_prefers_the_plugin_root(self, monkeypatch, tmp_path):
        root = tmp_path / "plug"
        (root / "hooks" / "scripts").mkdir(parents=True)
        script = root / "hooks" / "scripts" / "tuple_ledger_project.py"
        script.write_text("import sys; sys.exit(0)\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
        assert _REAL_PROJECTOR() == script

    def test_it_falls_back_to_the_checkout(self, monkeypatch, tmp_path):
        """Two candidates, env var then checkout — the same shape the
        close gate uses. Asserted against the real resolver, since the
        seal above replaced it."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "absent"))
        found = _REAL_PROJECTOR()
        assert found is not None, "the checkout's own projector went missing"
        assert found.name == "tuple_ledger_project.py"
        assert found.is_file()


class TestWhatItSubprocesses:
    def test_the_argv_is_the_projector_and_nothing_else(self, monkeypatch, tmp_path):
        """Pins the argv. This hook's whole job is to run one script
        with one verb; anything else in that position is a finding."""
        calls: list[list[str]] = []
        script = tmp_path / "tuple_ledger_project.py"
        script.write_text("import sys; sys.exit(0)\n")
        monkeypatch.setattr(proj, "_projector", lambda: script)

        def _record(argv, **_kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(proj.subprocess, "run", _record)
        proj._project("report", '{"agent_id": "a1"}')
        assert calls == [["python3", str(script), "report"]]

    def test_the_payload_goes_in_on_stdin(self, monkeypatch, tmp_path):
        """The projector reads its payload from stdin, as the wrapper
        piped it. An argv-borne payload would silently project nothing."""
        seen: dict = {}
        script = tmp_path / "tuple_ledger_project.py"
        script.write_text("import sys; sys.exit(0)\n")
        monkeypatch.setattr(proj, "_projector", lambda: script)

        def _record(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(proj.subprocess, "run", _record)
        proj._project("start", '{"agent_id": "a1"}')
        assert seen["input"] == '{"agent_id": "a1"}'
        assert seen["timeout"] == proj._TIMEOUT_S

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param("nonzero", id="a failing projector"),
            pytest.param("raises", id="a projector that cannot be run"),
        ],
    )
    def test_neither_failure_escapes_the_thread(self, monkeypatch, tmp_path, outcome):
        script = tmp_path / "tuple_ledger_project.py"
        script.write_text("import sys; sys.exit(1)\n")
        monkeypatch.setattr(proj, "_projector", lambda: script)
        emitted: list[str] = []
        monkeypatch.setattr(
            proj, "_emit", lambda _lvl, ev, **_kw: emitted.append(ev)
        )

        if outcome == "raises":
            def _boom(*_a, **_kw):
                raise OSError("no python3")
            monkeypatch.setattr(proj.subprocess, "run", _boom)
            expected = "tuple_projection_failed"
        else:
            expected = "tuple_projection_nonzero"

        proj._project("start", "{}")  # must not raise
        assert emitted == [expected]

    def test_a_SUCCESSFUL_projection_says_so(self, monkeypatch, tmp_path):
        """Both outcomes are logged, not just the bad one (nexus-q02nx.24).

        The module's own docstring says this "adds only a line saying the
        attempt happened at all, because a thread that dies quietly is
        harder to notice than a process that was never spawned" -- and
        the success path emitted nothing, so from the hook log a clean
        projection and a thread that never started read identically.
        That is precisely the distinction the line exists to draw, and
        the failure-only parametrization above could not notice its
        absence.
        """
        script = tmp_path / "tuple_ledger_project.py"
        script.write_text("import sys; sys.exit(0)\n")
        monkeypatch.setattr(proj, "_projector", lambda: script)
        emitted: list[str] = []
        monkeypatch.setattr(proj, "_emit", lambda _lvl, ev, **_kw: emitted.append(ev))

        proj._project("start", "{}")
        assert emitted == ["tuple_projection_ok"], (
            f"a clean projection emitted {emitted}; with nothing on the success "
            f"path the log cannot distinguish it from a thread that never ran"
        )


class TestTheBackgroundedWriteActuallyLands:
    """The end-to-end proof, ported from
    ``tests/hooks/test_subagent_tuple_async_wrappers.py`` (RDR-215 bead
    nexus-q02nx.21, which deleted the bash wrappers this file's other
    classes already replace): the thread returns fast, but the real
    ``tuple_ledger_project.py`` subprocess it spawns must still do the
    work, confirmed by polling a REAL HTTP server after ``run_start``
    has already returned. Every other class in this file mocks
    ``_project``/``_projector``; this one is the one test that must
    reach the genuine subprocess, so it restores the un-sealed resolver.
    """

    def test_eventually_the_backgrounded_write_actually_lands(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(proj, "_projector", _REAL_PROJECTOR)

        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                try:
                    received.append(json.loads(body.decode("utf-8")))
                except json.JSONDecodeError:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            host, port = server.server_address[:2]
            base_url = f"http://{host}:{port}"

            config_dir = tmp_path / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(
                f"{urlsplit(base_url).netloc}\x00default".encode("utf-8")
            ).hexdigest()
            record = {
                "format_version": 1,
                "token": "async-e2e-token",
                "tenant": "default",
                "base_url_digest": digest,
                "expires_at": time.time() + 3600.0,
                "ttl_seconds": 3600.0,
                "minted_by_pid": 0,
            }
            (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))

            monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
            monkeypatch.setenv("NX_SERVICE_URL", base_url)
            monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
            monkeypatch.delenv("NX_SERVICE_PORT", raising=False)

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

            assert _settle(lambda: bool(received), timeout=5.0), (
                "the backgrounded write never reached the engine within 5s"
            )
            assert received[0]["subspace"] == "ledger/sess-async-wrap"
            assert received[0]["keys"] == {
                "agent_id": "aworkerasyncwrap", "kind": "start",
            }
        finally:
            server.shutdown()
            server.server_close()
