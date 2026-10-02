# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-0kmat: the cut battery must run the engine gates against the CANDIDATE
engine, a leg that ran against the pinned published engine must be a failure in
cut mode, and so must a leg that never read the engine's ownerless-write counters
and log.

``tests/e2e/lib/candidate_engine.py`` carries the logic; the gates
(fresh-install-mvv, data-token-cli-gate, release-sandbox smoke/shakedown,
local-service-gate, the shakeout and candidate-migration journeys) and
``release-battery.sh`` consume it. The shell face is covered by
``tests/e2e/lib/candidate_engine_test.sh`` (wired in
``test_shell_suite_wiring.py``); this file covers the Python module against a
stub engine, and RUNS the real shell blocks the gates and the battery carry,
extracted from the scripts rather than retyped. A string pin of a block's text
cannot fail when the block is deleted or neutered; running the block can, and
each such test below was shown red under the mutation named in its docstring.
"""
from __future__ import annotations

import ast
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"
LIB = E2E / "lib" / "candidate_engine.py"
BATTERY = E2E / "release-battery.sh"
DAEMON = REPO_ROOT / "src" / "nexus" / "daemon" / "storage_service_daemon.py"


def _load():
    spec = importlib.util.spec_from_file_location("candidate_engine", LIB)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ce = _load()


@pytest.fixture()
def jar(tmp_path: Path) -> Path:
    p = tmp_path / "cand" / "nexus-service.jar"
    p.parent.mkdir()
    p.write_bytes(b"jar-bytes")
    return p


@pytest.fixture()
def pinned(tmp_path: Path) -> Path:
    p = tmp_path / "pinned" / "nexus-service"
    p.parent.mkdir()
    p.write_bytes(b"native-bytes")
    p.chmod(0o755)
    return p


class _Stub:
    """A loopback engine answering /version and /v1/status with what it is told."""

    def __init__(self, version: dict, status: dict) -> None:
        bodies = {"/version": version, "/v1/status": status}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def do_GET(self):  # noqa: N802
                body = bodies.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _lease(cfg: Path, port: int, artifact: Path | None) -> None:
    """A lease the way storage_service_daemon.py writes it: ``launch_kind`` names the
    log (``jar`` -> storage_service_jar.log, ``native`` -> storage_service_native.log)."""
    cfg.mkdir(parents=True, exist_ok=True)
    ep: dict = {"host": "127.0.0.1", "port": port}
    if artifact is not None:
        ep["artifact"] = str(artifact)
        ep["launch_kind"] = "jar" if str(artifact).endswith(".jar") else "native"
    (cfg / "storage_service_addr.501").write_text(json.dumps({"endpoint": ep}))


#: The engine log name per launch kind. test_log_names_match_the_daemon pins these to the
#: daemon source, so a fixture that writes the wrong name for a kind cannot hide.
_LOG_NAME = {"jar": "storage_service_jar.log", "native": "storage_service_native.log"}


def _engine_log(cfg: Path, artifact: Path, text: str = "INFO boot\n") -> Path:
    kind = "jar" if str(artifact).endswith(".jar") else "native"
    logs = cfg / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / _LOG_NAME[kind]
    path.write_text(text)
    return path


@pytest.fixture()
def engine():
    stubs: list[_Stub] = []

    def make(version=None, status=None) -> _Stub:
        s = _Stub(
            version if version is not None else {"release_version": "0.1.142", "build_ref": "abc+1"},
            status if status is not None else {
                "ownerless_write_mode": "enforce",
                "ownerless_writes_refused_total": 0,
                "ownerless_writes_would_refuse_total": 0,
            },
        )
        stubs.append(s)
        return s

    yield make
    for s in stubs:
        s.close()


def _cut(jar: Path, **extra: str) -> dict[str, str]:
    return {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar), **extra}


def test_log_names_match_the_daemon() -> None:
    """The fixture's log names are the daemon's. A jar launch writes ``storage_service_jar``,
    a native launch ``storage_service_native`` (``_svc_log_name``): the bug this bead's review
    found was a reader that looked for the native name beside a jar lease."""
    text = DAEMON.read_text()
    assert '"storage_service_jar" if launch_kind == "jar" else "storage_service_native"' in text
    assert _LOG_NAME == {"jar": "storage_service_jar.log", "native": "storage_service_native.log"}


def test_the_module_parses_as_python_3_9() -> None:
    """The non-cut gates call this with ``/usr/bin/python3`` before any uv environment
    exists, and that is 3.9 on a stock Mac. A ``match`` statement is a SyntaxError there."""
    ast.parse(LIB.read_text(), feature_version=(3, 9))


# ── resolve_env ─────────────────────────────────────────────────────────────


def test_no_candidate_and_no_cut_mode_is_the_unchanged_default() -> None:
    assert ce.resolve_env({}) == []


def test_cut_mode_without_a_candidate_is_refused() -> None:
    with pytest.raises(ce.CandidateError, match="PINNED PUBLISHED"):
        ce.resolve_env({"NX_CUT_MODE": "1"})


def test_a_candidate_that_cannot_be_found_is_refused_even_outside_cut_mode(tmp_path: Path) -> None:
    with pytest.raises(ce.CandidateError, match="not a file"):
        ce.resolve_env({"NX_CANDIDATE_ENGINE": str(tmp_path / "absent.jar")})


def test_jar_goes_to_nexus_service_jar_with_java_home(jar: Path) -> None:
    lines = ce.resolve_env({"NX_CANDIDATE_ENGINE": str(jar), "JAVA_HOME": "/some/jdk"})
    assert lines == [f"NEXUS_SERVICE_JAR={jar.resolve()}", "JAVA_HOME=/some/jdk"]


def test_native_goes_to_nexus_service_bin(pinned: Path) -> None:
    assert ce.resolve_env({"NX_CANDIDATE_ENGINE": str(pinned)}) == [
        f"NEXUS_SERVICE_BIN={pinned.resolve()}"
    ]


def test_non_executable_native_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "svc"
    p.write_bytes(b"x")
    with pytest.raises(ce.CandidateError, match="not executable"):
        ce.resolve_env({"NX_CANDIDATE_ENGINE": str(p)})


@pytest.mark.parametrize("var", ["NEXUS_SERVICE_JAR", "NEXUS_SERVICE_BIN"])
def test_a_different_ambient_launch_artifact_is_a_contradiction(jar: Path, tmp_path: Path, var: str) -> None:
    other = tmp_path / "other"
    other.write_bytes(b"x")
    other.chmod(0o755)
    with pytest.raises(ce.CandidateError, match="different launch artifacts"):
        ce.resolve_env({"NX_CANDIDATE_ENGINE": str(jar), "JAVA_HOME": "/j", var: str(other)})


def test_stage_gives_each_gate_a_private_copy_of_the_candidate(jar: Path, tmp_path: Path) -> None:
    """The supervisor finds its engine by argv, so two gates on one jar path stop each
    other's engines (2026-10-01: exit 143 on a freshly spawned engine). Staged copies
    differ in path and are the same bytes."""
    env = {"NX_CANDIDATE_ENGINE": str(jar), "JAVA_HOME": "/j"}
    a = ce.resolve_env(env, stage_dir=str(tmp_path / "gate-a"))
    b = ce.resolve_env(env, stage_dir=str(tmp_path / "gate-b"))
    assert a[0] != b[0] and a[0] != f"NEXUS_SERVICE_JAR={jar.resolve()}"
    copy_a = Path(a[0].split("=", 1)[1])
    assert copy_a.read_bytes() == jar.read_bytes()
    assert copy_a.parent.resolve() == (tmp_path / "gate-a").resolve()


def test_a_staged_copy_is_still_the_candidate_by_content(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    staged = Path(ce.resolve_env({"NX_CANDIDATE_ENGINE": str(jar), "JAVA_HOME": "/j"},
                                 stage_dir=str(tmp_path / "s"))[0].split("=", 1)[1])
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, staged)
    _engine_log(cfg, staged)
    env = _cut(jar)
    line, failure = ce.identity(str(cfg), "mvv", env)
    assert failure is None and "candidate=yes" in line and f"artifact={staged}" in line
    ref_line, ref_failure = ce.refusals(str(cfg), "mvv", env)
    assert ref_failure is None
    log = tmp_path / "leg.log"
    log.write_text(line + "\n" + ref_line + "\n")
    assert ce.cut_assert_log(str(log), "mvv", env) is None


# ── identity ────────────────────────────────────────────────────────────────


def test_identity_names_the_candidate_jar(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    line, failure = ce.identity(str(cfg), "mvv", _cut(jar))
    assert failure is None
    assert line.startswith("ENGINE IDENTITY [mvv]: candidate=yes kind=jar")
    assert f"artifact={jar}" in line
    assert "release_version=0.1.142" in line and "build_ref=abc+1" in line
    assert "ownerless_write_mode=enforce" in line
    assert "sha256=" in line and "sha256=none" not in line


def test_cut_mode_fails_a_leg_that_ran_against_the_pinned_engine(
    tmp_path: Path, jar: Path, pinned: Path, engine,
) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, pinned)
    line, failure = ce.identity(str(cfg), "mvv", _cut(jar))
    assert "candidate=no" in line
    assert failure is not None and "pinned published engine" in failure


def test_cut_mode_fails_a_lease_that_names_no_artifact(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, None)
    _line, failure = ce.identity(str(cfg), "mvv", _cut(jar))
    assert failure is not None


def test_outside_cut_mode_a_pinned_engine_is_reported_not_failed(tmp_path: Path, pinned: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, pinned)
    line, failure = ce.identity(str(cfg), "mvv", {})
    assert failure is None
    assert "candidate=no" in line and f"artifact={pinned}" in line


def test_cut_mode_requires_the_ownerless_write_mode_the_candidate_must_carry(
    tmp_path: Path, jar: Path, engine,
) -> None:
    # An engine that predates the check reports no ownerless_write_mode at all.
    stub = engine(status={"embedding_mode": "local"})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    line, failure = ce.identity(str(cfg), "mvv", _cut(jar))
    assert "ownerless_write_mode=none" in line
    assert failure is not None and "no ownerless-write check" in failure
    # The named escape drops that one assert and nothing else.
    _line, failure = ce.identity(str(cfg), "mvv", _cut(jar, NX_CANDIDATE_EXPECT_OWNERLESS_MODE="none"))
    assert failure is None


def test_cut_mode_rejects_a_candidate_running_log_only(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine(status={"ownerless_write_mode": "log-only"})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _line, failure = ce.identity(str(cfg), "mvv", _cut(jar))
    assert failure is not None and "expected enforce" in failure


def test_the_escape_does_not_excuse_an_unreachable_engine(tmp_path: Path, jar: Path) -> None:
    """NX_CANDIDATE_EXPECT_OWNERLESS_MODE=none drops the mode assert only. A lease whose engine
    answers nothing used to pass identity under it (the probe swallowed the error to {})."""
    cfg = tmp_path / "cfg"
    _lease(cfg, 1, jar)  # nothing listens on port 1
    env = _cut(jar, NX_CANDIDATE_EXPECT_OWNERLESS_MODE="none")
    _line, failure = ce.identity(str(cfg), "mvv", env)
    assert failure is not None and "unreachable" in failure
    _engine_log(cfg, jar)
    _line, failure = ce.refusals(str(cfg), "mvv", env)
    assert failure is not None and "unreachable" in failure


def test_the_probe_ignores_an_ambient_proxy(tmp_path: Path, jar: Path, engine, monkeypatch) -> None:
    """urllib honours HTTP(S)_PROXY / ALL_PROXY even for 127.0.0.1 unless told not to, and a
    proxy that cannot answer made the loopback probe read 'no counters'."""
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar)
    line, failure = ce.identity(str(cfg), "mvv", _cut(jar))
    assert failure is None and "ownerless_write_mode=enforce" in line
    line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert failure is None and "refused_total=0" in line


def test_no_lease_is_a_refusal_not_a_guess(tmp_path: Path) -> None:
    with pytest.raises(ce.CandidateError, match="no storage_service_addr"):
        ce.identity(str(tmp_path / "empty"), "mvv", {})


# ── refusals ────────────────────────────────────────────────────────────────


def test_a_clean_journey_reports_zero_refusals(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar)
    line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert failure is None
    assert "candidate=yes" in line
    assert "refused_total=0 would_refuse_total=0 log_lines=0 log=storage_service_jar.log" in line


@pytest.mark.parametrize(
    "status",
    [
        {"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": 2,
         "ownerless_writes_would_refuse_total": 0},
        {"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": 0,
         "ownerless_writes_would_refuse_total": 1},
    ],
)
def test_cut_mode_fails_on_a_counted_refusal(tmp_path: Path, jar: Path, engine, status: dict) -> None:
    stub = engine(status=status)
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar)
    _line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert failure is not None and "fix the writer before tagging" in failure


@pytest.mark.parametrize("kind", ["jar", "native"])
@pytest.mark.parametrize(
    "event", ["ownerless_chunk_write_refused", "ownerless_chunk_write_would_refuse"],
)
def test_cut_mode_fails_on_a_refusal_in_the_engine_log_of_the_launch_kind(
    tmp_path: Path, jar: Path, pinned: Path, engine, kind: str, event: str,
) -> None:
    """The log is the half that survives an engine restart mid-journey. Its name follows the
    lease's launch kind: a jar candidate (the default) writes storage_service_jar.log, and a
    reader of storage_service_native.log alone saw log_lines=0 for every jar run. Mutation
    (read only the native name): the jar cases go red. Both events count: enforce logs
    ``..._refused``, log-only logs ``..._would_refuse``."""
    artifact = jar if kind == "jar" else pinned
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, artifact)
    _engine_log(cfg, artifact, f"INFO boot\nWARN event={event} source_path=/x\n")
    env = {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(artifact)}
    line, failure = ce.refusals(str(cfg), "mvv", env)
    assert f"log_lines=1 log={_LOG_NAME[kind]}" in line
    assert failure is not None and "fix the writer before tagging" in failure


def test_a_refusal_logged_before_a_rotation_still_counts(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    log = _engine_log(cfg, jar)
    log.with_name(log.name + ".1").write_text("WARN event=ownerless_chunk_write_refused\n")
    line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert "log_lines=1" in line and failure is not None


def test_cut_mode_fails_when_the_engine_log_is_missing(tmp_path: Path, jar: Path, engine) -> None:
    """The engine ran, so its log exists. A log nobody could read means the half of the oracle
    that survives a restart read nothing, which is not a pass."""
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    # A log of the OTHER launch kind does not stand in for the one the lease names.
    _engine_log(cfg, Path("x-native"))
    line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert "log=none" in line and "log_lines=none" in line
    assert failure is not None and "no engine log" in failure


def test_cut_mode_fails_when_the_engine_at_the_end_is_not_the_candidate(
    tmp_path: Path, jar: Path, pinned: Path, engine,
) -> None:
    """Identity is re-checked at the END of the journey: an engine swapped mid-journey (the
    mvv generation-install leg restarts it) must not let the start-of-journey identity stand."""
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, pinned)
    _engine_log(cfg, pinned)
    line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert "candidate=no" in line
    assert failure is not None and "ended its journey" in failure


def test_a_refusal_outside_cut_mode_is_reported_not_failed(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine(status={"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": 3,
                          "ownerless_writes_would_refuse_total": 0})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    line, failure = ce.refusals(str(cfg), "mvv", {})
    assert failure is None and "refused_total=3" in line


@pytest.mark.parametrize("escape", [None, "none"])
def test_cut_mode_with_no_counters_cannot_have_seen_a_refusal(
    tmp_path: Path, jar: Path, engine, escape: str | None,
) -> None:
    """Both counters absent fails, escape or not: the escape relaxes the mode assert only."""
    stub = engine(status={"embedding_mode": "local"})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar)
    extra = {"NX_CANDIDATE_EXPECT_OWNERLESS_MODE": escape} if escape else {}
    _line, failure = ce.refusals(str(cfg), "mvv", _cut(jar, **extra))
    assert failure is not None and "no ownerless-write counters" in failure


@pytest.mark.parametrize(
    "mode, refused, would, log_event, controls, ok",
    [
        ("enforce", 1, 0, "ownerless_chunk_write_refused", 1, True),
        ("enforce", 0, 0, "ownerless_chunk_write_refused", 1, False),   # dead counter
        ("enforce", 1, 0, None, 1, False),                              # counter, no log line
        ("enforce", 2, 0, "ownerless_chunk_write_refused", 1, False),
        ("enforce", 1, 1, "ownerless_chunk_write_refused", 1, False),
        ("enforce", 1, 0, "ownerless_chunk_write_refused", 0, False),   # no control declared
        ("enforce", 0, 0, None, 0, True),
        ("log-only", 0, 1, "ownerless_chunk_write_would_refuse", 1, True),
        ("log-only", 1, 0, "ownerless_chunk_write_would_refuse", 1, False),
        ("log-only", 0, 1, "ownerless_chunk_write_refused", 1, True),   # the log event name is not the mode
    ],
)
def test_cut_mode_holds_the_end_reading_to_the_gates_own_control(
    tmp_path: Path, jar: Path, engine, mode: str, refused: int, would: int, log_event: str | None,
    controls: int, ok: bool,
) -> None:
    """lsg sends one deliberate ownerless write to the engine it then reads (nexus-z0o2p.24's
    negative leg). The read must find exactly that one, in the counter the engine's mode moves and
    in the log, so a zero from a dead counter or a missing log cannot pass. Mutation (compare to
    zero, as round 2 did): the lsg rows go red against a real refusing engine."""
    stub = engine(status={"ownerless_write_mode": mode, "ownerless_writes_refused_total": refused,
                          "ownerless_writes_would_refuse_total": would})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar, "INFO boot\n" + (f"WARN event={log_event} source_path=/x\n" if log_event else ""))
    line, failure = ce.refusals(str(cfg), "lsg", _cut(jar, NX_CANDIDATE_EXPECT_OWNERLESS_MODE=mode), controls=controls)
    assert (failure is None) is ok, (line, failure)
    assert f"controls={controls} mode={mode}" in line


@pytest.mark.parametrize(
    "refused, would, log_lines, phrase",
    [
        # counter below the log: the engine restarted after the write (counters are in memory, the log is a file)
        (0, 0, 1, "restarted"),
        # counter saw the control, the log did not: the log half of the oracle reads the wrong thing
        (1, 0, 0, "the counter saw the write and the log did not"),
        # neither saw it: a dead oracle
        (0, 0, 0, "neither saw the gate's own control"),
        # more than the control: a stranger wrote
        (2, 0, 2, "More than the control"),
        # right counts, wrong counter for the mode
        (0, 1, 1, "wrong counter"),
    ],
)
def test_the_red_message_says_which_half_of_the_reading_is_missing(
    tmp_path: Path, jar: Path, engine, refused: int, would: int, log_lines: int, phrase: str,
) -> None:
    """nexus-0kmat critique S4: an engine restart inside lsg's pytest selection resets the in-memory
    counter while the log persists. The exact-equality read stays red (a restarted engine cannot
    be trusted to have counted), but it must say RESTART, not 'dead counter', or the operator
    chases a writer that is not there. Each reading gets its own cause. Mutation (collapse the
    branches back into the single 'fewer' message): the restart and counter-without-log rows lose
    their phrases."""
    stub = engine(status={"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": refused,
                          "ownerless_writes_would_refuse_total": would})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar, "INFO boot\n" + "WARN event=ownerless_chunk_write_refused source_path=/x\n" * log_lines)
    _line, failure = ce.refusals(str(cfg), "lsg", _cut(jar), controls=1)
    assert failure is not None and phrase in failure, failure


def test_cut_mode_fails_a_mode_that_flipped_by_the_end_of_the_journey(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine(status={"ownerless_write_mode": "log-only", "ownerless_writes_refused_total": 0,
                          "ownerless_writes_would_refuse_total": 0})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar)
    _line, failure = ce.refusals(str(cfg), "mvv", _cut(jar))
    assert failure is not None and "expected enforce" in failure


# ── cut-assert-log ──────────────────────────────────────────────────────────

_ID = "ENGINE IDENTITY [smoke]: candidate={flag} kind=jar artifact={art} sha256={sha} release_version=0.1.142 build_ref=x ownerless_write_mode=enforce"
_REF = ("ENGINE OWNERLESS REFUSALS [smoke]: candidate={flag} sha256={sha} refused_total={refused} "
        "would_refuse_total={would} log_lines={lines} log=storage_service_jar.log controls={controls} mode={mode}")


def _id(flag: str, art: Path, sha: str | None = None) -> str:
    return _ID.format(flag=flag, art=art, sha=sha or ce._sha256(str(art)))


def _ref(art: Path, *, flag: str = "yes", sha: str | None = None, refused: str = "0", would: str = "0",
         lines: str = "0", controls: str = "0", mode: str = "enforce") -> str:
    return _REF.format(flag=flag, sha=sha or ce._sha256(str(art)), refused=refused, would=would,
                       lines=lines, controls=controls, mode=mode)


def _assert_log(tmp_path: Path, jar: Path, *lines: str, candidate: str = "", **extra: str) -> str | None:
    log = tmp_path / "leg.log"
    log.write_text("noise\n" + "\n".join(f"    {ln}" for ln in lines) + "\nSMOKE PASSED\n")
    return ce.cut_assert_log(str(log), "smoke", _cut(jar, **extra), candidate=candidate)


def test_assert_log_passes_both_lines_naming_the_candidate(tmp_path: Path, jar: Path) -> None:
    assert _assert_log(tmp_path, jar, _id("yes", jar), _ref(jar)) is None


def test_assert_log_fails_a_green_leg_with_no_identity_line(tmp_path: Path, jar: Path) -> None:
    reason = _assert_log(tmp_path, jar, _ref(jar))
    assert reason is not None and "no 'ENGINE IDENTITY' line" in reason


def test_assert_log_fails_a_leg_that_never_read_the_refusal_counters(tmp_path: Path, jar: Path) -> None:
    """The refusals line is the bead's actual oracle. Deleting the end-of-journey read from a
    gate used to leave the battery green because only the identity line was required."""
    reason = _assert_log(tmp_path, jar, _id("yes", jar))
    assert reason is not None and "no 'ENGINE OWNERLESS REFUSALS' line" in reason


def test_assert_log_fails_a_leg_that_ran_the_pinned_engine(tmp_path: Path, jar: Path, pinned: Path) -> None:
    reason = _assert_log(tmp_path, jar, _id("no", pinned), _ref(pinned, flag="no"))
    assert reason is not None and "not the candidate" in reason


def test_assert_log_checks_the_sha_not_the_flag_on_the_identity_line(tmp_path: Path, jar: Path, pinned: Path) -> None:
    """A line that SAYS candidate=yes but carries another engine's bytes is not the candidate.
    Mutation (drop the sha half of the comparison): this passes."""
    reason = _assert_log(tmp_path, jar, _id("yes", jar, sha=ce._sha256(str(pinned))), _ref(jar))
    assert reason is not None and "not the candidate" in reason


def test_assert_log_checks_the_sha_on_the_refusals_line_too(tmp_path: Path, jar: Path, pinned: Path) -> None:
    reason = _assert_log(tmp_path, jar, _id("yes", jar), _ref(jar, sha=ce._sha256(str(pinned))))
    assert reason is not None and "END of the journey" in reason
    reason = _assert_log(tmp_path, jar, _id("yes", jar), _ref(jar, flag="no"))
    assert reason is not None and "END of the journey" in reason


@pytest.mark.parametrize(
    "kwargs",
    [{"refused": "1"}, {"would": "2"}, {"lines": "3"}, {"refused": "none"}, {"would": "none"}, {"lines": "none"},
     {"controls": "none"}, {"controls": "x"}],
)
def test_assert_log_fails_a_refusal_or_an_unreadable_counter(tmp_path: Path, jar: Path, kwargs: dict) -> None:
    reason = _assert_log(tmp_path, jar, _id("yes", jar), _ref(jar, **kwargs))
    assert reason is not None


def test_assert_log_fails_the_wrong_mode_unless_the_escape_names_it(tmp_path: Path, jar: Path) -> None:
    lines = (_id("yes", jar), _ref(jar, mode="log-only"))
    assert _assert_log(tmp_path, jar, *lines) is not None
    assert _assert_log(tmp_path, jar, *lines, NX_CANDIDATE_EXPECT_OWNERLESS_MODE="none") is None
    assert _assert_log(tmp_path, jar, *lines, NX_CANDIDATE_EXPECT_OWNERLESS_MODE="log-only") is None


@pytest.mark.parametrize(
    "kwargs, ok",
    [
        # lsg's own control, enforce: exactly one refused, no would-refuse, one log line
        ({"controls": "1", "refused": "1", "lines": "1"}, True),
        ({"controls": "1", "refused": "0", "lines": "0"}, False),  # a dead counter and log: zero proves nothing
        ({"controls": "1", "refused": "1", "lines": "0"}, False),  # counter without its log line
        ({"controls": "1", "refused": "0", "lines": "1"}, False),  # log line without its counter
        ({"controls": "1", "refused": "2", "lines": "2"}, False),  # a writer beyond the control
        ({"controls": "1", "refused": "1", "would": "1", "lines": "1"}, False),
        ({"controls": "0", "refused": "1", "lines": "1"}, False),  # no control declared: one is a stranger
        ({"controls": "0"}, True),
        # log-only: the control is counted as would-refuse
        ({"controls": "1", "would": "1", "lines": "1", "mode": "log-only"}, True),
        ({"controls": "1", "refused": "1", "lines": "1", "mode": "log-only"}, False),
    ],
)
def test_assert_log_holds_the_reading_to_the_control_the_line_declares(
    tmp_path: Path, jar: Path, kwargs: dict, ok: bool,
) -> None:
    """The battery re-checks every refusals line against the control count that line declares, in
    the mode the line reports. Mutation (compare to zero again): the lsg control reads as a refusal."""
    mode = kwargs.get("mode", "enforce")
    extra = {"NX_CANDIDATE_EXPECT_OWNERLESS_MODE": mode}
    reason = _assert_log(tmp_path, jar, _id("yes", jar), _ref(jar, **kwargs), **extra)
    assert (reason is None) is ok, reason


def test_assert_log_with_min_controls_fails_a_leg_that_declared_none(tmp_path: Path, jar: Path) -> None:
    """nexus-0kmat critique S1: a leg that carries the battery's positive control must DECLARE one. A
    gate that dropped its control (NEXUS_GATE_NO_VECTOR_SMOKE=1) prints controls=0 and a clean 0/0/0
    reading, which the per-line check accepts. Mutation (ignore min_controls): the controls=0 line passes."""
    log = tmp_path / "leg.log"
    log.write_text(_id("yes", jar) + "\n" + _ref(jar) + "\nLOCAL-SERVICE GATE PASSED\n")
    reason = ce.cut_assert_log(str(log), "lsg", _cut(jar), min_controls=1)
    assert reason is not None and "controls>=1" in reason, reason
    assert ce.cut_assert_log(str(log), "lsg", _cut(jar), min_controls=0) is None
    log.write_text(_id("yes", jar) + "\n" + _ref(jar, controls="1", refused="1", lines="1") + "\nLOCAL-SERVICE GATE PASSED\n")
    assert ce.cut_assert_log(str(log), "lsg", _cut(jar), min_controls=1) is None


def test_cli_cut_assert_log_takes_min_controls(tmp_path: Path, jar: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text(_id("yes", jar) + "\n" + _ref(jar) + "\nPASSED\n")
    ok = _cli("cut-assert-log", str(log), "lsg", "--candidate", str(jar), env=_cut(jar))
    assert ok.returncode == 0, ok.stderr
    red = _cli("cut-assert-log", str(log), "lsg", "--candidate", str(jar), "--min-controls", "1", env=_cut(jar))
    assert red.returncode == 1 and "controls>=1" in red.stderr
    bad = _cli("cut-assert-log", str(log), "lsg", "--min-controls", "x", env=_cut(jar))
    assert bad.returncode == 2


def test_assert_log_takes_the_candidate_a_container_leg_ran(tmp_path: Path, jar: Path, pinned: Path) -> None:
    """shakeout and candmig run the artifacts' NATIVE binary, not NX_CANDIDATE_ENGINE's jar."""
    lines = (_id("yes", pinned), _ref(pinned))
    assert _assert_log(tmp_path, jar, *lines) is not None
    assert _assert_log(tmp_path, jar, *lines, candidate=str(pinned)) is None


def test_assert_log_is_a_noop_outside_cut_mode(tmp_path: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text("SMOKE PASSED\n")
    assert ce.cut_assert_log(str(log), "smoke", {}) is None


# ── the CLI the gates and the battery actually call ─────────────────────────


def _cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(LIB), *args],
        capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"], **env},
    )


def test_cli_env_exit_codes(jar: Path) -> None:
    assert _cli("env", env={}).returncode == 0
    refused = _cli("env", env={"NX_CUT_MODE": "1"})
    assert refused.returncode == 2 and "CANDIDATE ENGINE REFUSED" in refused.stderr
    ok = _cli("env", env={"NX_CANDIDATE_ENGINE": str(jar), "JAVA_HOME": "/j"})
    assert ok.returncode == 0 and ok.stdout.startswith("NEXUS_SERVICE_JAR=")


def test_cli_identity_and_refusals_exit_codes(tmp_path: Path, jar: Path, pinned: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    env = _cut(jar)
    _lease(cfg, stub.port, jar)
    _engine_log(cfg, jar)
    good = _cli("identity", str(cfg), "--label", "x", env=env)
    assert good.returncode == 0 and "candidate=yes" in good.stdout
    end = _cli("refusals", str(cfg), "--label", "x", "--controls", "0", env=env)
    assert end.returncode == 0 and "ENGINE OWNERLESS REFUSALS [x]: candidate=yes" in end.stdout
    assert "controls=0 mode=enforce" in end.stdout
    unsaid = _cli("refusals", str(cfg), "--label", "x", env=env)
    assert unsaid.returncode == 2 and "--controls N" in unsaid.stderr, "a gate must declare its controls"
    assert _cli("refusals", str(cfg), "--controls", "one", env=env).returncode == 2
    # identity accepts the flag (the container journeys pass one argument list to both reads)
    assert _cli("identity", str(cfg), "--label", "x", "--controls", "0", env=env).returncode == 0
    _lease(cfg, stub.port, pinned)
    bad = _cli("identity", str(cfg), "--label", "x", env=env)
    assert bad.returncode == 1 and "CANDIDATE ENGINE CHECK FAILED" in bad.stderr


def test_cli_cut_assert_log_takes_a_candidate_override(tmp_path: Path, jar: Path, pinned: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text(_id("yes", pinned) + "\n" + _ref(pinned) + "\n")
    assert _cli("cut-assert-log", str(log), "x", env=_cut(jar)).returncode == 1
    assert _cli("cut-assert-log", str(log), "x", "--candidate", str(pinned), env=_cut(jar)).returncode == 0


def test_cli_manifest_helpers(tmp_path: Path, jar: Path, pinned: Path) -> None:
    arts = tmp_path / "arts"
    (arts / "jar").mkdir(parents=True)
    (arts / "native").mkdir()
    shutil.copy(jar, arts / "jar" / "svc.jar")
    shutil.copy(pinned, arts / "native" / "nexus-service")
    (arts / "manifest.json").write_text(json.dumps({"artifacts": {
        "jar": {"path": "jar/svc.jar", "sha256": ce._sha256(str(jar))},
        "native": {"path": "native/nexus-service", "sha256": ce._sha256(str(pinned))},
    }}))
    assert _cli("manifest-artifact", str(arts), "jar", env={}).stdout.strip() == str(arts / "jar" / "svc.jar")
    assert _cli("candidate-in-manifest", str(arts), str(jar), env={}).stdout.strip() == "jar"
    assert _cli("candidate-in-manifest", str(arts), str(pinned), env={}).stdout.strip() == "native"
    other = tmp_path / "other.jar"
    other.write_bytes(b"different")
    res = _cli("candidate-in-manifest", str(arts), str(other), env={})
    assert res.returncode == 1 and res.stdout.strip() == "none"


# ── the battery: real code blocks, extracted from the real script ────────────


def _extract_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _extract_statement(text: str, first_line_prefix: str, nth: int = 0) -> str:
    """The shell statement whose first line starts (after indentation) with *first_line_prefix*,
    through its backslash continuations."""
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if ln.strip().startswith(first_line_prefix)]
    assert len(hits) > nth, f"no statement starting with {first_line_prefix!r} (deleted or renamed?)"
    i = hits[nth]
    out = [lines[i]]
    while out[-1].rstrip().endswith("\\"):
        i += 1
        out.append(lines[i])
    return "\n".join(out)


def _bash(script: str, tmp_path: Path, env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "h.sh"
    path.write_text(script)
    return subprocess.run(
        ["bash", str(path)], capture_output=True, text=True, timeout=timeout,
        env={"PATH": os.environ["PATH"], **(env or {})},
    )


def _artifacts_dir(tmp_path: Path, jar: Path, native: Path) -> Path:
    arts = tmp_path / "arts"
    (arts / "jar").mkdir(parents=True)
    (arts / "native").mkdir()
    shutil.copy(jar, arts / "jar" / "svc.jar")
    shutil.copy(native, arts / "native" / "nexus-service")
    (arts / "manifest.json").write_text(json.dumps({"artifacts": {
        "jar": {"path": "jar/svc.jar", "sha256": ce._sha256(str(jar))},
        "native": {"path": "native/nexus-service", "sha256": ce._sha256(str(native))},
    }}))
    return arts


def _battery_root(tmp_path: Path, manifest_rc: int = 0) -> Path:
    """A REPO_ROOT stand-in: the real candidate_engine.py, and a stub artifact_manifest.py
    (the real one recomputes this checkout's tree identity) that verifies or refuses."""
    root = tmp_path / "root"
    lib = root / "tests" / "e2e" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    shutil.copy(LIB, lib / "candidate_engine.py")
    (lib / "artifact_manifest.py").write_text(
        "import sys\n"
        + ("print('{}')\n" if manifest_rc == 0 else
           f"sys.stderr.write('tree identity mismatch (stub)\\n'); sys.exit({manifest_rc})\n")
    )
    return root


_RESOLVE_HARNESS = """#!/bin/bash
set -uo pipefail
REPO_ROOT={root}
ARTIFACTS={arts}
LOGS={logs}
ACCEPT_CANDIDATE_MISMATCH={accept}
CUT_ABORT_REASON=""; CUT_MISMATCH_ACCEPTED=0; CUT_JAR=""; CUT_NATIVE=""
{cand_export}
{func}
cut_resolve_candidate; rc=$?
printf 'rc=%s\\nabort=%s\\nmismatch=%s\\ncand=%s\\njar=%s\\nnative=%s\\n' "$rc" "$CUT_ABORT_REASON" "$CUT_MISMATCH_ACCEPTED" "${{NX_CANDIDATE_ENGINE:-}}" "$CUT_JAR" "$CUT_NATIVE"
"""


def _resolve(tmp_path: Path, jar: Path, native: Path, *, candidate: Path | None, accept: int = 0,
             manifest_rc: int = 0) -> dict[str, str]:
    root = _battery_root(tmp_path, manifest_rc)
    arts = _artifacts_dir(tmp_path, jar, native)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    func = _extract_function(BATTERY.read_text(), "cut_resolve_candidate")
    script = _RESOLVE_HARNESS.format(
        root=root, arts=arts, logs=logs, accept=accept, func=func,
        cand_export=f"export NX_CANDIDATE_ENGINE={candidate}" if candidate else "unset NX_CANDIDATE_ENGINE",
    )
    r = _bash(script, tmp_path)
    assert r.returncode == 0, (r.stdout, r.stderr)
    out = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
    out["_arts"] = str(arts)
    return out


def test_cut_resolve_defaults_the_candidate_to_the_manifest_jar(tmp_path: Path, jar: Path, pinned: Path) -> None:
    out = _resolve(tmp_path, jar, pinned, candidate=None)
    assert out["rc"] == "0" and out["cand"] == f"{out['_arts']}/jar/svc.jar" == out["jar"]
    assert out["native"] == f"{out['_arts']}/native/nexus-service"


def test_cut_resolve_aborts_when_the_manifest_does_not_verify(tmp_path: Path, jar: Path, pinned: Path) -> None:
    """The no-candidate abort. Mutation (neuter the verify branch): rc stays 0 and the battery
    would run every leg against nothing it had checked."""
    out = _resolve(tmp_path, jar, pinned, candidate=None, manifest_rc=3)
    assert out["rc"] == "1" and "no candidate engine" in out["abort"] and out["cand"] == ""


def test_cut_resolve_refuses_a_candidate_that_is_not_the_manifest_engine(tmp_path: Path, jar: Path, pinned: Path) -> None:
    """lsg/shakeout/candmig run the manifest's engine, mvv/smoke/shakedown/dtok the candidate:
    two engines in one green battery unless the operator says so."""
    other = tmp_path / "other.jar"
    other.write_bytes(b"a different engine")
    out = _resolve(tmp_path, jar, pinned, candidate=other)
    assert out["rc"] == "1" and "not the engine in the artifacts manifest" in out["abort"]
    assert out["mismatch"] == "0"


def test_cut_resolve_accepts_a_mismatch_only_when_told_and_says_so(tmp_path: Path, jar: Path, pinned: Path) -> None:
    other = tmp_path / "other.jar"
    other.write_bytes(b"a different engine")
    out = _resolve(tmp_path, jar, pinned, candidate=other, accept=1)
    assert out["rc"] == "0" and out["mismatch"] == "1"


@pytest.mark.parametrize("which", ["jar", "native"])
def test_cut_resolve_takes_either_manifest_artifact_as_the_candidate(
    tmp_path: Path, jar: Path, pinned: Path, which: str,
) -> None:
    out = _resolve(tmp_path, jar, pinned, candidate=jar if which == "jar" else pinned)
    assert out["rc"] == "0" and out["mismatch"] == "0"


_VERDICT_HARNESS = """#!/bin/bash
set -uo pipefail
RED={red}; ONLY_SKIPPED={only}; CUT_ABORT_REASON={abort!r}; CUT_MISMATCH_ACCEPTED={mismatch}
CUT_NON_ENFORCE={nonenforce!r}; LAG_COUNT={lag}; LAG_BEAD=nexus-x
{func}
battery_verdict "$RED"; echo "rc=$?"
"""


def _verdict(tmp_path: Path, *, red: int = 0, only: int = 0, abort: str = "", mismatch: int = 0,
             nonenforce: str = "", lag: int = 0) -> tuple[str, str]:
    func = _extract_function(BATTERY.read_text(), "battery_verdict")
    r = _bash(_VERDICT_HARNESS.format(red=red, only=only, abort=abort, mismatch=mismatch,
                                      nonenforce=nonenforce, lag=lag, func=func), tmp_path)
    assert r.returncode == 0, (r.stdout, r.stderr)
    body, _, rc = r.stdout.rpartition("rc=")
    return body, rc.strip()


def test_verdict_clean_run_passes(tmp_path: Path) -> None:
    body, rc = _verdict(tmp_path)
    assert rc == "0" and body.strip() == "RELEASE BATTERY PASSED"


def test_verdict_a_cut_abort_is_red_even_with_no_red_leg(tmp_path: Path) -> None:
    """The artifacts leg PASSED when the candidate could not be resolved, every other leg reads
    'NOT RUN', and 'NOT RUN' is not red. Mutation (drop the increment): this reads PASSED."""
    body, rc = _verdict(tmp_path, abort="no candidate engine: x")
    assert rc == "1" and "RELEASE BATTERY FAILED: 1 red leg(s)" in body and "RED" in body


@pytest.mark.parametrize(
    "kwargs, phrase",
    [
        ({"only": 2}, "2 leg(s) skipped by --only"),
        ({"mismatch": 1}, "--accept-candidate-mismatch"),
        ({"nonenforce": "none"}, "NX_CANDIDATE_EXPECT_OWNERLESS_MODE=none"),
        ({"lag": 3}, "3 leg(s) EXPECTED-LAG(nexus-x)"),
    ],
)
def test_verdict_partial_reasons_are_named_and_never_a_release_verdict(tmp_path: Path, kwargs: dict, phrase: str) -> None:
    body, rc = _verdict(tmp_path, **kwargs)
    assert rc == "0" and "PARTIAL" in body and phrase in body and "not a release verdict" in body


def test_verdict_red_legs_fail(tmp_path: Path) -> None:
    body, rc = _verdict(tmp_path, red=2, only=1)
    assert rc == "1" and "FAILED: 2 red leg(s)" in body


_FINISH_HARNESS = """#!/bin/bash
set -uo pipefail
REPO_ROOT={root}
LOGS={logs}
CUT_MODE={cut}
export NX_CUT_MODE={cut}
CUT_JAR={cut_jar}; CUT_NATIVE={cut_native}; LAG_BEAD={lag_bead}; LAG_ENGINE=0.1.142; LAG_COUNT=0
{cand_export}
{engine_legs}
{lag_legs}
{lag_sig}
declare -A LEG_END LEG_RC LEG_STATUS LEG_LINE LEG_START LEG_VERDICT
LEG_START[{leg}]=1000
LEG_VERDICT[{leg}]='(PASSED|FAILED)'
{funcs}
finish_leg {leg} {rc} >/dev/null
printf '%s|%s|%s' "${{LEG_STATUS[{leg}]}}" "$LAG_COUNT" "${{LEG_LINE[{leg}]}}"
"""


def _finish(tmp_path: Path, log_text: str, *, cut: bool, cand: Path | None, leg: str = "mvv", rc: int = 0,
            cut_jar: Path | None = None, cut_native: Path | None = None, lag: bool = False,
            root: Path | None = None) -> tuple[str, str, str]:
    text = BATTERY.read_text()
    funcs = "\n".join(_extract_function(text, n) for n in (
        "cut_leg_candidate", "cut_leg_controls_args", "cut_mode_vacuity", "engine_lag_verdict", "finish_leg"))
    grab = lambda prefix: text[text.index(prefix):].splitlines()[0]  # noqa: E731
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"{leg}.log").write_text(log_text)
    harness = _FINISH_HARNESS.format(
        root=root or _battery_root(tmp_path), logs=logs, cut=1 if cut else 0, leg=leg, rc=rc,
        cut_jar=cut_jar or "''", cut_native=cut_native or "''", lag_bead="nexus-x" if lag else "''",
        cand_export=f"export NX_CANDIDATE_ENGINE={cand}" if cand else "unset NX_CANDIDATE_ENGINE",
        engine_legs=grab("CUT_ENGINE_LEGS="), lag_legs=grab("ENGINE_LAG_LEGS="), lag_sig=grab("ENGINE_LAG_SIGNATURE="),
        funcs=funcs,
    )
    r = _bash(harness, tmp_path)
    assert r.returncode == 0, f"harness failed (extraction drifted?): {r.stdout!r} {r.stderr!r}"
    status, count, line = r.stdout.split("|", 2)
    return status, count, line


def test_battery_downgrades_a_green_cut_leg_that_never_named_an_engine(tmp_path: Path, jar: Path) -> None:
    status, _n, line = _finish(tmp_path, "FRESH PASSED\n", cut=True, cand=jar)
    assert status == "FAILED" and "VACUOUS in cut mode" in line


def test_battery_downgrades_a_green_cut_leg_that_ran_the_pinned_engine(tmp_path: Path, jar: Path, pinned: Path) -> None:
    log = _id("no", pinned) + "\n" + _ref(pinned, flag="no") + "\nFRESH PASSED\n"
    status, _n, line = _finish(tmp_path, log, cut=True, cand=jar)
    assert status == "FAILED" and "VACUOUS in cut mode" in line


def test_battery_downgrades_a_green_cut_leg_that_never_read_the_refusals(tmp_path: Path, jar: Path) -> None:
    status, _n, line = _finish(tmp_path, _id("yes", jar) + "\nFRESH PASSED\n", cut=True, cand=jar)
    assert status == "FAILED" and "ENGINE OWNERLESS REFUSALS" in line


def test_battery_downgrades_a_green_cut_leg_that_saw_a_refusal(tmp_path: Path, jar: Path) -> None:
    log = _id("yes", jar) + "\n" + _ref(jar, refused="1") + "\nFRESH PASSED\n"
    status, _n, line = _finish(tmp_path, log, cut=True, cand=jar)
    assert status == "FAILED" and "refused" in line


def test_battery_keeps_a_green_cut_leg_that_ran_the_candidate(tmp_path: Path, jar: Path) -> None:
    log = _id("yes", jar) + "\n" + _ref(jar) + "\nFRESH PASSED\n"
    status, _n, _line = _finish(tmp_path, log, cut=True, cand=jar)
    assert status == "PASSED"


def test_battery_fails_a_leg_when_the_log_reader_crashes_with_no_output(tmp_path: Path, jar: Path) -> None:
    """The reader's exit status decides. ``... || true`` over an empty capture read a crashed
    reader as 'no complaint'. Mutation (restore ``|| true`` and the empty-means-ok read): PASSED."""
    root = _battery_root(tmp_path)
    (root / "tests" / "e2e" / "lib" / "candidate_engine.py").write_text("import sys\nsys.exit(7)\n")
    log = _id("yes", jar) + "\n" + _ref(jar) + "\nFRESH PASSED\n"
    status, _n, line = _finish(tmp_path, log, cut=True, cand=jar, root=root)
    assert status == "FAILED" and "exited 7 with no output" in line


@pytest.mark.parametrize(
    "leg, which",
    [("lsg", "jar"), ("shakeout", "native"), ("candmig", "native")],
)
def test_battery_judges_each_engine_leg_against_its_own_candidate(
    tmp_path: Path, jar: Path, pinned: Path, leg: str, which: str,
) -> None:
    """lsg runs the artifacts jar, shakeout and candmig the artifacts native binary; a candidate
    named with --candidate-engine (here: some other file) is not what they served."""
    served = jar if which == "jar" else pinned
    elsewhere = tmp_path / "elsewhere.jar"
    elsewhere.write_bytes(b"not either")
    own = {"controls": "1", "refused": "1", "lines": "1"} if leg == "lsg" else {}  # lsg declares its control
    log = _id("yes", served) + "\n" + _ref(served, **own) + "\nPASSED\n"
    status, _n, line = _finish(tmp_path, log, cut=True, cand=elsewhere, leg=leg, cut_jar=jar, cut_native=pinned)
    assert status == "PASSED", line
    other = pinned if which == "jar" else jar
    bad = _id("yes", other) + "\n" + _ref(other, **own) + "\nPASSED\n"
    status, _n, _line = _finish(tmp_path, bad, cut=True, cand=elsewhere, leg=leg, cut_jar=jar, cut_native=pinned)
    assert status == "FAILED"


@pytest.mark.parametrize("leg", ["mvv", "smoke", "shakedown", "dtok"])
def test_battery_holds_each_engine_leg_to_its_own_control_count(tmp_path: Path, jar: Path, leg: str) -> None:
    """nexus-0kmat critique S1 and round 3 review M1. lsg is the one leg that sends its engine a deliberate
    ownerless write: a green lsg that declares controls=0 has no positive control in the whole battery, and
    every other leg's 0/0/0 rests on nothing. And every OTHER engine leg declares exactly 0: a leg whose
    literal were bumped to 1 would excuse the one refusal it reads, which is how a stray writer turns green
    (mutation N1l, round 3: dtok declaring 1 survived 85 tests). The battery holds the table, not the count
    a leg prints about itself. Mutations: drop ``$cargs`` from cut_mode_vacuity, or make the table's default
    arm empty, and the bumped leg reads PASSED; drop the lsg arm and the control-less lsg reads PASSED."""
    none = _id("yes", jar) + "\n" + _ref(jar) + "\nPASSED\n"
    one = _id("yes", jar) + "\n" + _ref(jar, controls="1", refused="1", lines="1") + "\nPASSED\n"
    status, _n, line = _finish(tmp_path, none, cut=True, cand=jar, leg="lsg", cut_jar=jar)
    assert status == "FAILED" and "VACUOUS in cut mode" in line and "controls>=1" in line, (status, line)
    status, _n, line = _finish(tmp_path, one, cut=True, cand=jar, leg="lsg", cut_jar=jar)
    assert status == "PASSED", line
    status, _n, line = _finish(tmp_path, none, cut=True, cand=jar, leg=leg)
    assert status == "PASSED", line
    status, _n, line = _finish(tmp_path, one, cut=True, cand=jar, leg=leg)
    assert status == "FAILED" and "controls<=0" in line, (status, line)


def test_assert_log_with_max_controls_fails_a_leg_that_declared_one(tmp_path: Path, jar: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text(_id("yes", jar) + "\n" + _ref(jar, controls="1", refused="1", lines="1") + "\nPASSED\n")
    reason = ce.cut_assert_log(str(log), "dtok", _cut(jar), max_controls=0)
    assert reason is not None and "controls<=0" in reason, reason
    assert ce.cut_assert_log(str(log), "dtok", _cut(jar), max_controls=1) is None
    assert ce.cut_assert_log(str(log), "dtok", _cut(jar)) is None
    red = _cli("cut-assert-log", str(log), "dtok", "--candidate", str(jar), "--max-controls", "0", env=_cut(jar))
    assert red.returncode == 1 and "controls<=0" in red.stderr


def test_battery_does_not_police_a_leg_that_provisions_no_engine(tmp_path: Path, jar: Path) -> None:
    for leg in ("hookskew", "pkgup"):
        status, _n, _line = _finish(tmp_path, "HOOK SKEW PASSED\n", cut=True, cand=jar, leg=leg)
        assert status == "PASSED", leg


def test_battery_outside_cut_mode_is_unchanged(tmp_path: Path) -> None:
    status, _n, _line = _finish(tmp_path, "FRESH PASSED\n", cut=False, cand=None)
    assert status == "PASSED"


_LAG_TAIL = "...\nEngineOlderThanClientError: asked for metadata_merge but the response did not echo it. The engine is older than this client.\nFRESH-INSTALL MVV FAILED: nx index\n"


def test_expected_lag_names_a_red_against_the_pinned_engine(tmp_path: Path) -> None:
    status, count, line = _finish(tmp_path, _LAG_TAIL, cut=False, cand=None, rc=1, lag=True)
    assert status == "EXPECTED-LAG" and count == "1"
    assert "EXPECTED-LAG(nexus-x)" in line and "pinned engine 0.1.142" in line and "FRESH-INSTALL MVV FAILED" in line


def test_expected_lag_reads_the_log_the_failed_line_names(tmp_path: Path) -> None:
    """data-token-cli-gate fails at ``store put ... failed (see <dir>/store-put.log / .stderr.log)``:
    the error text is in the stderr file, not the leg log (measured 2026-10-01). The evidence is the
    log the FAILED line names and its ``.stderr.log`` sibling. Another, newer log in the same
    evidence directory that names the signature is not the failing step's, and decides nothing."""
    ev = tmp_path / "evidence" / "logs"
    ev.mkdir(parents=True)
    stderr, other = ev / "store-put.stderr.log", ev / "init.log"
    stderr.write_text("Error: asked for metadata_merge but the response did not echo it. The engine is older than this client.\n")
    other.write_text("EngineOlderThanClientError (an earlier, tolerated mention)\n")
    leg_log = (f"DATA-TOKEN CLI GATE FAILED: store put via self-minted data token failed (see {ev}/store-put.log / .stderr.log)\n"
               f"FAILURE EVIDENCE PRESERVED: {ev} (home: {tmp_path}/home)\n")
    status, count, line = _finish(tmp_path, leg_log, cut=False, cand=None, rc=1, lag=True, leg="dtok")
    assert status == "EXPECTED-LAG" and count == "1" and "EXPECTED-LAG(nexus-x)" in line
    # The named log decides: an unrelated failing output there keeps the leg red even though the
    # newest log in the same directory names the signature (the old rule read the newest log).
    stderr.write_text("Error: doctor warned about something else\n")
    os.utime(other, None)
    status, _n, _line = _finish(tmp_path, leg_log, cut=False, cand=None, rc=1, lag=True, leg="dtok")
    assert status == "FAILED"


def test_expected_lag_needs_the_ack(tmp_path: Path) -> None:
    status, count, _line = _finish(tmp_path, _LAG_TAIL, cut=False, cand=None, rc=1, lag=False)
    assert status == "FAILED" and count == "0"


def test_expected_lag_does_not_mask_a_different_red(tmp_path: Path) -> None:
    status, _n, _line = _finish(tmp_path, "FRESH-INSTALL MVV FAILED: doctor warned\n", cut=False, cand=None, rc=1, lag=True)
    assert status == "FAILED"


def test_expected_lag_looks_at_the_tail_only(tmp_path: Path) -> None:
    """A tolerated early mention of the error followed by an unrelated red stays red."""
    log = "EngineOlderThanClientError (tolerated)\n" + "noise\n" * 200 + "FRESH-INSTALL MVV FAILED: doctor warned\n"
    status, _n, _line = _finish(tmp_path, log, cut=False, cand=None, rc=1, lag=True)
    assert status == "FAILED"


def test_expected_lag_is_tied_to_the_failing_step_not_to_the_last_80_lines(tmp_path: Path) -> None:
    """The reviewer's case (nexus/review-0kmat-round2-verify N3): a step that PASSED tolerated the
    error early, and a DIFFERENT step failed a few lines later, well inside the old 80-line tail.
    Mutation (read the last 80 lines of the log again): this is acked as lag."""
    log = (
        "── 3/10 nx init ──\n"
        "EngineOlderThanClientError: tolerated here, the step retried and passed\n"
        "init ok\n"
        + "filler\n" * 20
        + "── 4/10 doctor ──\n"
        "doctor warned about the taxonomy\n"
        "FRESH-INSTALL MVV FAILED: doctor warned\n"
    )
    status, count, _line = _finish(tmp_path, log, cut=False, cand=None, rc=1, lag=True)
    assert status == "FAILED" and count == "0"
    # The same mention inside the failing step is the lag: acked.
    own = log.replace("doctor warned about the taxonomy", "EngineOlderThanClientError: the engine is older than this client")
    status, count, _line = _finish(tmp_path, own, cut=False, cand=None, rc=1, lag=True)
    assert status == "EXPECTED-LAG" and count == "1"


def test_expected_lag_reads_the_step_block_before_a_fail_marker(tmp_path: Path) -> None:
    """release-sandbox.sh smoke/shakedown print the failing step's output under a ``  nx ...:``
    header, then ``[FAIL]``, and only at the very end the summary verdict line."""
    sandbox = (
        "  nx doctor --check-schema:\n    [pass]\n"
        "  nx plan reseed (seeds plan library):\n"
        "    EngineOlderThanClientError: the engine is older than this client\n"
        "    [FAIL] -- exit non-zero\n"
        "  nx doctor --check-taxonomy:\n    [pass]\n"
        "[done] Sandbox state at /x. Run 'reset' to tear down.\n"
        "SMOKE FAILED: 1 step(s) exited non-zero:\n"
    )
    status, count, _line = _finish(tmp_path, sandbox, cut=False, cand=None, rc=1, lag=True, leg="smoke")
    assert status == "EXPECTED-LAG" and count == "1"
    other = sandbox.replace("EngineOlderThanClientError: the engine is older than this client", "boom: unrelated")
    other = "EngineOlderThanClientError (tolerated, an earlier step)\n  nx index:\n    [pass]\n" + other
    status, _n, _line = _finish(tmp_path, other, cut=False, cand=None, rc=1, lag=True, leg="smoke")
    assert status == "FAILED"


_LAG = "EngineOlderThanClientError: the engine is older than this client"


def test_expected_lag_needs_every_failing_step_to_be_the_lag(tmp_path: Path) -> None:
    """Round 3 review M2 (probe n3probe.py: ACKED). The pinned-engine lag makes MANY sandbox steps fail, so
    a leg with one step on the lag and another on an unrelated KeyError must stay red: the ack covers the
    lag, never a different red beside it. Mutation (evidence = union of all [FAIL] blocks, the round 3 shape,
    or ``any`` for ``all`` in failed_step_lag): the mixed leg reads EXPECTED-LAG."""
    def sandbox(second: str) -> str:
        return (
            "  nx plan reseed (seeds plan library):\n"
            f"    {_LAG}\n    [FAIL] -- exit non-zero\n"
            "  nx index repo:\n"
            f"    {second}\n    [FAIL] -- exit non-zero\n"
            "SMOKE FAILED: 2 step(s) exited non-zero:\n"
        )

    both = _finish(tmp_path, sandbox(_LAG), cut=False, cand=None, rc=1, lag=True, leg="smoke")
    assert both[0] == "EXPECTED-LAG" and both[1] == "1", both
    mixed = _finish(tmp_path, sandbox("KeyError: 'collection'"), cut=False, cand=None, rc=1, lag=True, leg="smoke")
    assert mixed[0] == "FAILED" and mixed[1] == "0", mixed


def test_expected_lag_does_not_ack_a_failure_that_prints_no_fail_marker(tmp_path: Path) -> None:
    """Round 4 review I1 (probe: one [FAIL] lag block + the throughput line + a 2-step verdict, ACKED).
    release-sandbox.sh shakedown adds throughput failures to SHAKEDOWN_FAILED with a line that carries
    no ``[FAIL]`` token (migration-rehearsal/lib/index_throughput.sh: ``-- FAIL: above Nx baseline``),
    and the end-of-journey engine read appends one with no marker either. The verdict line's count is
    the only place those steps show. Mutation (drop the count comparison from failed_step_lag): the
    mixed leg reads EXPECTED-LAG."""
    throughput = "  throughput[fresh]: 900 chunks in 40s = 0.044 s/chunk \u2014 FAIL: above 3x baseline 0.01 (ceiling 0.03)\n"
    def sandbox(verdict: str, extra: str = "") -> str:
        return (
            "  nx plan reseed (seeds plan library):\n"
            f"    {_LAG}\n    [FAIL] -- exit non-zero\n"
            f"{extra}"
            f"{verdict}\n"
        )

    # The probe: two failing steps, one of them unmarked and not the lag.
    probe = _finish(
        tmp_path, sandbox("SHAKEDOWN FAILED: 2 release-gate step(s) exited non-zero:", throughput),
        cut=False, cand=None, rc=1, lag=True, leg="shakedown",
    )
    assert probe[0] == "FAILED" and probe[1] == "0", probe
    # The same leg with only the lag step, and a verdict line that counts one, is the ack.
    only = _finish(
        tmp_path, sandbox("SHAKEDOWN FAILED: 1 release-gate step(s) exited non-zero:"),
        cut=False, cand=None, rc=1, lag=True, leg="shakedown",
    )
    assert only[0] == "EXPECTED-LAG" and only[1] == "1", only
    # A [FAIL] block with no countable verdict line cannot be reconciled with anything: red.
    nocount = _finish(
        tmp_path, sandbox("SHAKEDOWN FAILED"), cut=False, cand=None, rc=1, lag=True, leg="shakedown",
    )
    assert nocount[0] == "FAILED", nocount
    # More markers than the verdict counts is a mismatch in the other direction: red.
    over = _finish(
        tmp_path,
        sandbox("SMOKE FAILED: 1 step(s) exited non-zero:", f"  nx index repo:\n    {_LAG}\n    [FAIL] -- exit non-zero\n"),
        cut=False, cand=None, rc=1, lag=True, leg="smoke",
    )
    assert over[0] == "FAILED", over


def test_expected_lag_stops_at_a_passed_marker_and_at_a_step_header(tmp_path: Path) -> None:
    """Round 3 review L2: the ``[pass]`` boundary and the ``  nx ...:`` header each survived deletion because
    the other covered the one fixture. Here each is the ONLY boundary between a tolerated mention and the
    failing step, and the verdict line sits where the ``FAILED`` line of dtok sits (not at the log's end).
    Mutation (drop either alternative from _STEP_BOUNDARY_RE): the earlier mention leaks into the failing
    step's block and the leg is acked."""
    for boundary in ("    [pass]", "  nx doctor --check-schema:", "== next step", "── next step"):
        log = f"{_LAG} (tolerated, an earlier step)\n{boundary}\nunrelated output\nDATA-TOKEN CLI GATE FAILED: boom\n"
        status, _n, line = _finish(tmp_path, log, cut=False, cand=None, rc=1, lag=True, leg="dtok")
        assert status == "FAILED", (boundary, line)
        own = f"{boundary}\n{_LAG}\nDATA-TOKEN CLI GATE FAILED: boom\n"
        status, _n, line = _finish(tmp_path, own, cut=False, cand=None, rc=1, lag=True, leg="dtok")
        assert status == "EXPECTED-LAG", (boundary, line)


def test_expected_lag_finds_the_verdict_line_when_it_is_not_the_last_line(tmp_path: Path) -> None:
    """Round 3 review L2 (N3e: failed-line locator replaced by the last line). dtok prints FAILURE EVIDENCE
    PRESERVED, and more, after its verdict line. The failing step is the stretch that ENDS at the verdict
    line; with a long trailer, a locator that took the log's last line would read the trailer instead.
    Mutation (point = the last line): the lag mention is out of reach and the leg reads FAILED."""
    trailer = "".join(f"evidence line {i}\n" for i in range(40))
    log = f"  nx store put:\n    {_LAG}\nDATA-TOKEN CLI GATE FAILED: store put failed\n{trailer}"
    status, _n, line = _finish(tmp_path, log, cut=False, cand=None, rc=1, lag=True, leg="dtok")
    assert status == "EXPECTED-LAG", line


def test_expected_lag_never_applies_in_cut_mode_or_to_other_legs(tmp_path: Path, jar: Path) -> None:
    status, _n, _line = _finish(tmp_path, _LAG_TAIL, cut=True, cand=jar, rc=1, lag=True)
    assert status == "FAILED"
    status, _n, _line = _finish(tmp_path, _LAG_TAIL, cut=False, cand=None, rc=1, lag=True, leg="lsg")
    assert status == "FAILED"


# ── the battery, end to end through its argument handling (it stops before any leg) ──


def _battery(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(BATTERY), *args], capture_output=True, text=True, timeout=120,
        cwd=str(cwd or REPO_ROOT), env={**os.environ, "NX_BATTERY_ALLOW_DEVELOP": "1", **(env or {})},
    )


def test_battery_refuses_a_candidate_without_cut_mode() -> None:
    for args in (["--candidate-engine", "x.jar"], ["--accept-candidate-mismatch"]):
        r = _battery("--plan", *args)
        assert r.returncode == 2 and "need --cut" in r.stderr, args
    r = _battery("--plan", env={"NX_CANDIDATE_ENGINE": "/x/y.jar"})
    assert r.returncode == 2 and "need --cut" in r.stderr


def test_battery_resolves_a_relative_candidate_against_the_callers_directory(tmp_path: Path) -> None:
    r = _battery("--plan", "--cut", "--candidate-engine", "rel.jar", cwd=tmp_path)
    assert r.returncode == 2
    printed = re.search(r"--candidate-engine: (\S+) is not a file", r.stderr)
    assert printed, r.stderr
    resolved = printed.group(1)
    assert resolved.endswith("/rel.jar") and resolved.startswith("/")
    assert str(REPO_ROOT) not in resolved


def test_battery_refuses_a_lag_ack_in_cut_mode_for_the_wrong_engine_or_a_bad_shape() -> None:
    r = _battery("--plan", "--cut", "--expected-engine-lag", "nexus-z0o2p.9@0.1.142")
    assert r.returncode == 2 and "PINNED engine" in r.stderr
    r = _battery("--plan", "--expected-engine-lag", "nexus-z0o2p.9@0.0.1")
    assert r.returncode == 2 and "the pin moved" in r.stderr
    r = _battery("--plan", "--expected-engine-lag", "nonsense")
    assert r.returncode == 2 and "<bead>@<engine-version>" in r.stderr


def test_cut_mode_refuses_the_knob_that_drops_the_positive_control() -> None:
    """nexus-0kmat critique S1, the env path: NEXUS_GATE_NO_VECTOR_SMOKE=1 drops lsg's vector leg and
    with it the only deliberate ownerless write. The static check that lsg declares its control where
    it sends it does not see this knob; the battery must refuse it in cut mode (any non-empty value, as
    the gate reads it) and leave a non-cut run alone. Mutation (delete the refusal): rc 0."""
    for val in ("1", "yes"):
        r = _battery("--plan", "--cut", env={"NEXUS_GATE_NO_VECTOR_SMOKE": val})
        assert r.returncode == 2 and "NEXUS_GATE_NO_VECTOR_SMOKE" in r.stderr and "positive control" in r.stderr, (val, r.stderr)
    assert _battery("--plan", "--cut", env={"NEXUS_GATE_NO_VECTOR_SMOKE": ""}).returncode == 0
    assert _battery("--plan", env={"NEXUS_GATE_NO_VECTOR_SMOKE": "1"}).returncode == 0


def test_lsg_itself_refuses_that_knob_in_cut_mode_and_only_there(tmp_path: Path) -> None:
    """Run through the battery the knob is refused before lsg starts; run by hand (`NX_CUT_MODE=1
    tests/e2e/local-service-gate.sh`, as Step 3 does) lsg must refuse it itself. The real block is
    extracted and run: starting the real gate here would provision an engine."""
    text = (REPO_ROOT / "tests" / "e2e" / "local-service-gate.sh").read_text()
    m = re.search(r'^if \[ "\$\{NX_CUT_MODE:-0\}" = 1 \] && \[ -n "\$\{NEXUS_GATE_NO_VECTOR_SMOKE:-\}" \]; then\n.*?^fi\n', text, re.M | re.S)
    assert m, "the cut-mode refusal block is gone from local-service-gate.sh"
    script = "set -euo pipefail\n" + m.group(0) + "echo PROCEEDED\n"

    def run(**env: str) -> subprocess.CompletedProcess[str]:
        return _bash(script, tmp_path, env=env)

    r = run(NX_CUT_MODE="1", NEXUS_GATE_NO_VECTOR_SMOKE="1")
    assert r.returncode == 2 and "PROCEEDED" not in r.stdout and "positive control" in r.stderr, (r.stdout, r.stderr)
    assert "PROCEEDED" in run(NX_CUT_MODE="1").stdout
    assert "PROCEEDED" in run(NX_CUT_MODE="0", NEXUS_GATE_NO_VECTOR_SMOKE="1").stdout


def test_battery_plan_runs_dtok_in_cut_mode_or_when_named_only() -> None:
    def plan(*args: str) -> dict[str, str]:
        r = _battery("--plan", *args)
        assert r.returncode == 0, r.stderr
        return {ln.split()[1]: ln.split(None, 3)[3] for ln in r.stdout.splitlines() if ln.startswith("PLAN ")}

    assert plan()["dtok"] == "SKIPPED(cut mode only)"
    assert plan("--cut")["dtok"] == "PENDING"
    only = plan("--only", "dtok")
    assert only["dtok"] == "PENDING" and only["mvv"] == "SKIPPED(--only)"


def test_battery_marks_the_engine_legs_it_polices() -> None:
    legs = BATTERY.read_text()
    legs = legs[legs.index("CUT_ENGINE_LEGS="):].splitlines()[0]
    for leg in ("mvv", "smoke", "shakedown", "dtok", "lsg", "shakeout", "candmig"):
        assert f" {leg} " in legs, leg
    assert " pkgup " not in legs  # converges to the PUBLISHED engine: it never runs the candidate


# ── the gates: the real blocks, run ──────────────────────────────────────────

_NX_STUB = "#!/bin/sh\nenv\n"


def _run_nx_function(tmp_path: Path, gate: str, fn: str) -> str:
    text = (E2E / gate).read_text()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "nx").write_text(_NX_STUB)
    (bindir / "nx").chmod(0o755)
    script = f"""#!/bin/bash
set -uo pipefail
HOME_DIR={tmp_path}/home; BIN_DIR={bindir}; _POISON_SENTINEL=x
CAND_ENV_ARGS=(NEXUS_SERVICE_JAR=/staged/engine.jar JAVA_HOME=/jdk)
{_extract_function(text, fn)}
{fn} --version
"""
    r = _bash(script, tmp_path)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return r.stdout


@pytest.mark.parametrize(
    "gate, fn",
    [
        ("fresh-install-mvv.sh", "_nx"),
        ("data-token-cli-gate.sh", "_nx"),
        ("data-token-cli-gate.sh", "_nx_poisoned"),
    ],
)
def test_the_candidate_survives_the_gates_env_scrub(tmp_path: Path, gate: str, fn: str) -> None:
    """The point of the whole bead: ``nx`` runs under ``env -i``, and the candidate rides inside
    the allowlist. Mutation (delete the CAND_ENV_ARGS line from that function): red."""
    out = _run_nx_function(tmp_path, gate, fn)
    assert "NEXUS_SERVICE_JAR=/staged/engine.jar" in out and "JAVA_HOME=/jdk" in out


_FAIL_HARNESS = """#!/bin/bash
set -uo pipefail
LOGS={tmp}; HOME_DIR={tmp}/home; HOME={tmp}/home; MODE=smoke
SMOKE_FAILED=(); SHAKEDOWN_FAILED=()
{stubs}
_fail() {{ echo "GATE-FAILED: $*"; exit 7; }}
_die() {{ echo "GATE-DIED: $*"; exit 8; }}
{stmt}
echo "REACHED failed=${{#SMOKE_FAILED[@]}}/${{#SHAKEDOWN_FAILED[@]}}"
"""


def _run_statement(tmp_path: Path, stmt: str, *, result: int) -> subprocess.CompletedProcess[str]:
    stubs = (
        f'candidate_engine_refusals() {{ echo "ENGINE OWNERLESS REFUSALS [$2]: stub"; return {result}; }}\n'
        f'candidate_engine_identity() {{ echo "ENGINE IDENTITY [$2]: stub"; return {result}; }}'
    )
    return _bash(_FAIL_HARNESS.format(tmp=tmp_path, stubs=stubs, stmt=stmt), tmp_path)


@pytest.mark.parametrize(
    "gate, prefix, nth, verdict",
    [
        ("fresh-install-mvv.sh", "candidate_engine_refusals", 0, "GATE-FAILED"),
        ("fresh-install-mvv.sh", "candidate_engine_identity", 0, "GATE-FAILED"),
        ("data-token-cli-gate.sh", "candidate_engine_refusals", 0, "GATE-FAILED"),
        ("data-token-cli-gate.sh", "candidate_engine_identity", 0, "GATE-FAILED"),
        ("release-sandbox.sh", "candidate_engine_identity", 0, "GATE-DIED"),
    ],
)
def test_a_failed_engine_read_fails_the_gate(tmp_path: Path, gate: str, prefix: str, nth: int, verdict: str) -> None:
    """The call exists and its failure propagates. Mutation (delete the call, or its ``|| _fail``):
    ``_extract_statement`` finds nothing, or the failing stub lets the journey reach its end."""
    stmt = _extract_statement((E2E / gate).read_text(), prefix, nth)
    r = _run_statement(tmp_path, stmt, result=1)
    assert r.returncode in (7, 8) and verdict in r.stdout and "REACHED" not in r.stdout, (r.stdout, r.stderr)
    ok = _run_statement(tmp_path, stmt, result=0)
    assert ok.returncode == 0 and "REACHED" in ok.stdout, (ok.stdout, ok.stderr)


@pytest.mark.parametrize("nth, array", [(0, "SMOKE_FAILED"), (1, "SHAKEDOWN_FAILED")])
def test_the_sandbox_modes_record_a_refusal_as_a_failed_step(tmp_path: Path, nth: int, array: str) -> None:
    stmt = _extract_statement((E2E / "release-sandbox.sh").read_text(), "candidate_engine_refusals", nth)
    assert array in stmt
    bad = _run_statement(tmp_path, stmt, result=1)
    assert "REACHED" in bad.stdout and ("failed=1/0" if array == "SMOKE_FAILED" else "failed=0/1") in bad.stdout
    good = _run_statement(tmp_path, stmt, result=0)
    assert "failed=0/0" in good.stdout


def test_the_sandbox_exports_the_candidate_into_nx_init(tmp_path: Path) -> None:
    text = (E2E / "release-sandbox.sh").read_text()
    start = text.index("    local _cand_kv\n")
    snippet = text[start:text.index("    done\n", start) + len("    done\n")]
    script = f"""#!/bin/bash
set -uo pipefail
CAND_ENV_ARGS=(NEXUS_SERVICE_JAR=/staged/engine.jar JAVA_HOME=/jdk)
f() {{
{snippet}
    env
}}
f
"""
    r = _bash(script, tmp_path)
    assert "NEXUS_SERVICE_JAR=/staged/engine.jar" in r.stdout and "JAVA_HOME=/jdk" in r.stdout


@pytest.mark.parametrize("gate", ["fresh-install-mvv.sh", "data-token-cli-gate.sh"])
def test_a_cut_mode_gate_with_no_candidate_refuses_before_provisioning(tmp_path: Path, gate: str) -> None:
    """The gate itself, run: cut mode with no NX_CANDIDATE_ENGINE stops at the candidate load and
    never builds a wheel or reaches ``nx init``. Mutation (delete the load): the gate proceeds to its
    wheel build and this times out or lacks the refusal."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NX_CUT_MODE": "1"}
    try:
        r = subprocess.run(["bash", str(E2E / gate)], capture_output=True, text=True, timeout=45, env=env)
    except subprocess.TimeoutExpired as exc:  # the load was bypassed and the journey started
        pytest.fail(f"{gate} did not stop at the candidate load: {exc.stdout!r}")
    # The gate preserves its work dir on failure; remove exactly the one it names, never a glob.
    kept = re.search(r"FAILURE EVIDENCE PRESERVED: (/tmp/[A-Za-z0-9_.-]+)/logs", r.stderr)
    if kept:
        shutil.rmtree(kept.group(1), ignore_errors=True)
    assert r.returncode != 0
    assert "CANDIDATE ENGINE REFUSED" in r.stderr, (r.stdout[-400:], r.stderr[-400:])
    assert "1/10" not in r.stdout


# ── lsg, shakeout, candmig: the end-of-journey read, run against a stub engine ──


def _read_block(path: Path, start_marker: str, end_marker: str) -> str:
    text = path.read_text()
    a = text.index(start_marker)
    return text[a:text.index(end_marker, a)]


def _serving(tmp_path: Path, artifact: Path, engine, *, status: dict | None = None, log: str = "INFO boot\n") -> Path:
    stub = engine(status=status)
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, artifact)
    _engine_log(cfg, artifact, log)
    return cfg


def test_lsg_reads_the_engine_and_fails_a_cut_run_on_a_refusal(tmp_path: Path, jar: Path, engine) -> None:
    """local-service-gate.sh, its block run. The candidate it judges is the artifacts jar it copied
    in, so a battery that names another --candidate-engine cannot make lsg pass against the wrong
    bytes. Mutation (delete the block): STATUS never moves."""
    block = _read_block(E2E / "local-service-gate.sh", "GATE_CAND_ENV=()", "\nSUMMARY_LINE=")
    arts = tmp_path / "arts"
    (arts / "jar").mkdir(parents=True)
    shutil.copy(jar, arts / "jar" / "svc.jar")

    def run(cfg: Path, cut: str, controls: int = 0) -> subprocess.CompletedProcess[str]:
        script = f"""#!/bin/bash
set -uo pipefail
REPO_ROOT={REPO_ROOT}; SCRATCH={cfg}; STATUS=0; GATE_OWNERLESS_CONTROLS={controls}
NX_GATE_ARTIFACTS={arts}; GATE_JAR_REL=jar/svc.jar
export NX_CUT_MODE={cut}
unset NX_CANDIDATE_ENGINE
{block}
echo "STATUS=$STATUS"
"""
        return _bash(script, tmp_path)

    clean = _serving(tmp_path / "clean", jar, engine)
    r = run(clean, "1")
    assert "STATUS=0" in r.stdout and "ENGINE OWNERLESS REFUSALS [local-service-gate]: candidate=yes" in r.stdout, (r.stdout, r.stderr)
    refused = _serving(tmp_path / "refused", jar, engine, log="WARN event=ownerless_chunk_write_refused\n")
    r = run(refused, "1")
    assert "STATUS=1" in r.stdout and "CANDIDATE ENGINE CHECK FAILED" in r.stderr, (r.stdout, r.stderr)
    r = run(refused, "0")
    assert "STATUS=0" in r.stdout and "log_lines=1" in r.stdout  # reported, not failed, outside cut mode


_REFUSING = {"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": 1,
             "ownerless_writes_would_refuse_total": 0}


def test_lsg_expects_exactly_its_own_deliberate_ownerless_write(tmp_path: Path, jar: Path, engine) -> None:
    """nexus-z0o2p.24 added a negative control to the smoke leg: one ownerless upsert-chunks sent to
    the same engine whose counters this block reads. Round 2 failed any hit, so a cut run on a tree
    with both was red by construction. The block now takes GATE_OWNERLESS_CONTROLS and requires the
    engine to have counted exactly that. Mutations: compare to zero (the first case goes red);
    ignore the count (the dead-counter case passes)."""
    block = _read_block(E2E / "local-service-gate.sh", "GATE_CAND_ENV=()", "\nSUMMARY_LINE=")
    arts = tmp_path / "arts"
    (arts / "jar").mkdir(parents=True)
    shutil.copy(jar, arts / "jar" / "svc.jar")

    def run(name: str, controls: int, status: dict, log: str) -> str:
        cfg = _serving(tmp_path / name, jar, engine, status=status, log=log)
        script = f"""#!/bin/bash
set -uo pipefail
REPO_ROOT={REPO_ROOT}; SCRATCH={cfg}; STATUS=0; GATE_OWNERLESS_CONTROLS={controls}
NX_GATE_ARTIFACTS={arts}; GATE_JAR_REL=jar/svc.jar
export NX_CUT_MODE=1
unset NX_CANDIDATE_ENGINE
{block}
echo "STATUS=$STATUS"
"""
        return _bash(script, tmp_path).stdout

    line = "INFO boot\nWARN event=ownerless_chunk_write_refused route=upsert-chunks\n"
    assert "STATUS=0" in run("own-control", 1, _REFUSING, line)
    assert "STATUS=1" in run("dead-counter", 1, {**_REFUSING, "ownerless_writes_refused_total": 0}, line)
    assert "STATUS=1" in run("no-log-line", 1, _REFUSING, "INFO boot\n")
    assert "STATUS=1" in run("a-stranger", 1, {**_REFUSING, "ownerless_writes_refused_total": 2}, line)
    assert "STATUS=1" in run("undeclared", 0, _REFUSING, line)


def test_lsg_declares_its_control_where_it_sends_it(tmp_path: Path) -> None:
    """GATE_OWNERLESS_CONTROLS is bumped on the line after the one deliberate ownerless
    upsert-chunks the smoke leg sends, inside the vector leg NEXUS_GATE_NO_VECTOR_SMOKE drops, and
    the end-of-journey read passes it. Static, honestly: the leg needs a live engine to run. The
    declaration and the bump are run for real below to read the value they produce."""
    text = (E2E / "local-service-gate.sh").read_text()
    lines = text.splitlines()
    idx = [i for i, ln in enumerate(lines) if "GATE_OWNERLESS_CONTROLS=$((GATE_OWNERLESS_CONTROLS + 1))" in ln]
    assert len(idx) == 1, "exactly one control is declared"
    send = max(i for i in range(idx[0]) if "smoke_request POST /v1/vectors/upsert-chunks" in lines[i])
    assert "SMOKE_ORPHAN" in " ".join(lines[send:idx[0]]), "the bump follows the ownerless request"
    opened = max(i for i in range(idx[0]) if lines[i].startswith('if [ -z "${NEXUS_GATE_NO_VECTOR_SMOKE:-}" ]'))
    assert lines[opened - 1] == "GATE_OWNERLESS_CONTROLS=0", "declared at zero right before the vector leg"
    assert '--controls "$GATE_OWNERLESS_CONTROLS"' in text, "the end-of-journey read passes the declared count"
    value = _bash(f"{lines[opened - 1]}\n{lines[idx[0]].strip().split('#')[0]}\necho $GATE_OWNERLESS_CONTROLS", tmp_path)
    assert value.stdout.strip() == "1"


_CONTAINER_START = "# ── Engine identity + ownerless-write refusals (nexus-0kmat)"


@pytest.mark.parametrize(
    "script, label, end",
    [
        ("rehearse_shakeout.sh", "shakeout", "# ── Verdict"),
        ("rehearse_candidate_migration.sh", "candidate-migration", 'say "RESULT"'),
    ],
)
def test_the_container_journeys_read_the_engine_at_the_end(
    tmp_path: Path, jar: Path, pinned: Path, engine, script: str, label: str, end: str,
) -> None:
    """The native-candidate journeys run inside a container; run.sh stages candidate_engine.py into
    their lib/. Their block, run against a stub engine: clean passes, a refusal is a FAIL in cut
    mode, a missing module is a FAIL in cut mode and only a note outside it."""
    block = _read_block(E2E / "migration-rehearsal" / script, _CONTAINER_START, end)
    native_dir = tmp_path / "opt-native"
    native_dir.mkdir()
    shutil.copy(pinned, native_dir / "nexus-service")

    def run(refused: bool, cut: str, module: bool = True) -> subprocess.CompletedProcess[str]:
        cfg_home = tmp_path / ("h-ref" if refused else "h-ok")
        if cfg_home.exists():
            shutil.rmtree(cfg_home)
        (cfg_home / "lib").mkdir(parents=True)
        if module:
            shutil.copy(LIB, cfg_home / "lib" / "candidate_engine.py")
        stub = engine()
        cfg = cfg_home / ".config" / "nexus"
        served = cfg_home / "served-nexus-service"
        shutil.copy(pinned, served)
        served.chmod(0o755)
        _lease(cfg, stub.port, served)
        _engine_log(cfg, served, "WARN event=ownerless_chunk_write_refused\n" if refused else "INFO boot\n")
        script_text = f"""#!/bin/bash
set -uo pipefail
HOME={cfg_home}; SVC_NATIVE_DIR={native_dir}; FAILS=0
say() {{ echo "== $* =="; }}; ok() {{ echo "PASS $*"; }}; bad() {{ echo "FAIL $*"; FAILS=$((FAILS+1)); }}; note() {{ echo "note $*"; }}
export NX_CUT_MODE={cut}
{block}
echo "FAILS=$FAILS"
"""
        return _bash(script_text, tmp_path)

    r = run(False, "1")
    assert "FAILS=0" in r.stdout and f"candidate=yes" in r.stdout and f"[{label}]" in r.stdout, (r.stdout, r.stderr)
    r = run(True, "1")
    assert "FAILS=0" not in r.stdout and "CANDIDATE ENGINE CHECK FAILED" in r.stderr
    r = run(True, "0")
    assert "FAILS=0" in r.stdout  # outside cut mode a refusal is reported, never a failure
    r = run(False, "1", module=False)
    assert "FAILS=0" not in r.stdout and "not in the image" in r.stdout
    r = run(False, "0", module=False)
    assert "FAILS=0" in r.stdout and "skipped" in r.stdout


def test_run_sh_forwards_cut_mode_only_to_the_journeys_that_run_the_candidate(tmp_path: Path) -> None:
    text = (E2E / "migration-rehearsal" / "run.sh").read_text()
    start = text.index('if [ "$SHAKEOUT" = 1 ] || [ "$CANDIDATE_MIGRATION" = 1 ]; then')
    block = text[start:text.index("\nfi\n", start) + 4]

    def forwarded(shakeout: int, candmig: int, env: dict[str, str]) -> str:
        script = f"""#!/bin/bash
set -uo pipefail
SHAKEOUT={shakeout}; CANDIDATE_MIGRATION={candmig}; run_env=()
{block}
printf '%s\\n' ${{run_env[@]+"${{run_env[@]}}"}}
"""
        return _bash(script, tmp_path, env).stdout

    cut = {"NX_CUT_MODE": "1", "NX_CANDIDATE_EXPECT_OWNERLESS_MODE": "log-only"}
    assert "NX_CUT_MODE=1" in forwarded(1, 0, cut) and "NX_CANDIDATE_EXPECT_OWNERLESS_MODE=log-only" in forwarded(0, 1, cut)
    assert forwarded(0, 0, cut).strip() == ""  # pkgup and the rest stage no candidate engine
    assert forwarded(1, 0, {}).strip() == ""


def test_run_sh_stages_the_engine_reader_into_both_candidate_journeys() -> None:
    """A static pin, honestly: the staging needs Docker to run. The protection that cannot rot is
    the in-container block above, which FAILS in cut mode when the module is not there."""
    text = (E2E / "migration-rehearsal" / "run.sh").read_text()
    assert text.count('cp "$HERE/../lib/candidate_engine.py" "$STAGE/lib/"') == 2
