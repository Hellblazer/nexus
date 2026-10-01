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


# ── cut-assert-log ──────────────────────────────────────────────────────────

_ID = "ENGINE IDENTITY [smoke]: candidate={flag} kind=jar artifact={art} sha256={sha} release_version=0.1.142 build_ref=x ownerless_write_mode=enforce"
_REF = ("ENGINE OWNERLESS REFUSALS [smoke]: candidate={flag} sha256={sha} refused_total={refused} "
        "would_refuse_total={would} log_lines={lines} log=storage_service_jar.log mode={mode}")


def _id(flag: str, art: Path, sha: str | None = None) -> str:
    return _ID.format(flag=flag, art=art, sha=sha or ce._sha256(str(art)))


def _ref(art: Path, *, flag: str = "yes", sha: str | None = None, refused: str = "0", would: str = "0",
         lines: str = "0", mode: str = "enforce") -> str:
    return _REF.format(flag=flag, sha=sha or ce._sha256(str(art)), refused=refused, would=would,
                       lines=lines, mode=mode)


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
    [{"refused": "1"}, {"would": "2"}, {"lines": "3"}, {"refused": "none"}, {"would": "none"}, {"lines": "none"}],
)
def test_assert_log_fails_a_refusal_or_an_unreadable_counter(tmp_path: Path, jar: Path, kwargs: dict) -> None:
    reason = _assert_log(tmp_path, jar, _id("yes", jar), _ref(jar, **kwargs))
    assert reason is not None


def test_assert_log_fails_the_wrong_mode_unless_the_escape_names_it(tmp_path: Path, jar: Path) -> None:
    lines = (_id("yes", jar), _ref(jar, mode="log-only"))
    assert _assert_log(tmp_path, jar, *lines) is not None
    assert _assert_log(tmp_path, jar, *lines, NX_CANDIDATE_EXPECT_OWNERLESS_MODE="none") is None
    assert _assert_log(tmp_path, jar, *lines, NX_CANDIDATE_EXPECT_OWNERLESS_MODE="log-only") is None


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
    end = _cli("refusals", str(cfg), "--label", "x", env=env)
    assert end.returncode == 0 and "ENGINE OWNERLESS REFUSALS [x]: candidate=yes" in end.stdout
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
        "cut_leg_candidate", "cut_mode_vacuity", "engine_lag_verdict", "finish_leg"))
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
    log = _id("yes", served) + "\n" + _ref(served) + "\nPASSED\n"
    status, _n, line = _finish(tmp_path, log, cut=True, cand=elsewhere, leg=leg, cut_jar=jar, cut_native=pinned)
    assert status == "PASSED", line
    other = pinned if which == "jar" else jar
    bad = _id("yes", other) + "\n" + _ref(other) + "\nPASSED\n"
    status, _n, _line = _finish(tmp_path, bad, cut=True, cand=elsewhere, leg=leg, cut_jar=jar, cut_native=pinned)
    assert status == "FAILED"


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


def test_expected_lag_reads_the_evidence_dir_a_gate_preserves(tmp_path: Path) -> None:
    """data-token-cli-gate fails at ``store put ... failed (see <dir>/store-put.stderr.log)``: the
    error text is in that file and not in the leg log, which is what a first measurement of the
    real pinned-engine red showed (2026-10-01). The newest log in the preserved evidence dir is read."""
    ev = tmp_path / "evidence" / "logs"
    ev.mkdir(parents=True)
    older, newest = ev / "init.log", ev / "store-put.stderr.log"
    older.write_text("EngineOlderThanClientError (an earlier, tolerated mention)\n")
    newest.write_text("Error: asked for metadata_merge but the response did not echo it. The engine is older than this client.\n")
    os.utime(older, (1_000_000, 1_000_000))
    leg_log = (f"DATA-TOKEN CLI GATE FAILED: store put via self-minted data token failed (see {ev}/store-put.log)\n"
               f"FAILURE EVIDENCE PRESERVED: {ev} (home: {tmp_path}/home)\n")
    status, count, line = _finish(tmp_path, leg_log, cut=False, cand=None, rc=1, lag=True, leg="dtok")
    assert status == "EXPECTED-LAG" and count == "1" and "EXPECTED-LAG(nexus-x)" in line
    # The newest log decides: an unrelated failing output there keeps the leg red even though an
    # older log in the same directory names the signature.
    newest.write_text("Error: doctor warned about something else\n")
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

    def run(cfg: Path, cut: str) -> subprocess.CompletedProcess[str]:
        script = f"""#!/bin/bash
set -uo pipefail
REPO_ROOT={REPO_ROOT}; SCRATCH={cfg}; STATUS=0
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
