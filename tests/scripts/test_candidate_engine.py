# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-0kmat: the cut battery must run the pinned-engine gates against the
CANDIDATE engine, and a leg that ran against the pinned published engine must
be a failure in cut mode.

``tests/e2e/lib/candidate_engine.py`` carries the logic; the three gates
(fresh-install-mvv, data-token-cli-gate, release-sandbox smoke/shakedown) and
``release-battery.sh`` consume it. The shell face is covered by
``tests/e2e/lib/candidate_engine_test.sh`` (wired in
``test_shell_suite_wiring.py``); this file covers the Python module against a
stub engine and the battery's own cut-mode non-vacuity check, extracted from
the real script rather than retyped.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests" / "e2e" / "lib" / "candidate_engine.py"
BATTERY = REPO_ROOT / "tests" / "e2e" / "release-battery.sh"


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
    cfg.mkdir(parents=True, exist_ok=True)
    ep: dict = {"host": "127.0.0.1", "port": port}
    if artifact is not None:
        ep["artifact"] = str(artifact)
    (cfg / "storage_service_addr.501").write_text(json.dumps({"endpoint": ep}))


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
    line, failure = ce.identity(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert failure is None and "candidate=yes" in line and f"artifact={staged}" in line
    log = tmp_path / "leg.log"
    log.write_text(line + "\n")
    assert ce.cut_assert_log(str(log), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)}) is None


# ── identity ────────────────────────────────────────────────────────────────


def test_identity_names_the_candidate_jar(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    env = {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)}
    line, failure = ce.identity(str(cfg), "mvv", env)
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
    line, failure = ce.identity(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert "candidate=no" in line
    assert failure is not None and "pinned published engine" in failure


def test_cut_mode_fails_a_lease_that_names_no_artifact(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, None)
    _line, failure = ce.identity(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
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
    env = {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)}
    line, failure = ce.identity(str(cfg), "mvv", env)
    assert "ownerless_write_mode=none" in line
    assert failure is not None and "no ownerless-write check" in failure
    # The named escape drops that one assert and nothing else.
    _line, failure = ce.identity(str(cfg), "mvv", {**env, "NX_CANDIDATE_EXPECT_OWNERLESS_MODE": "none"})
    assert failure is None


def test_cut_mode_rejects_a_candidate_running_log_only(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine(status={"ownerless_write_mode": "log-only"})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _line, failure = ce.identity(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert failure is not None and "expected enforce" in failure


def test_no_lease_is_a_refusal_not_a_guess(tmp_path: Path) -> None:
    with pytest.raises(ce.CandidateError, match="no storage_service_addr"):
        ce.identity(str(tmp_path / "empty"), "mvv", {})


# ── refusals ────────────────────────────────────────────────────────────────


def test_a_clean_journey_reports_zero_refusals(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    line, failure = ce.refusals(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert failure is None
    assert "refused_total=0 would_refuse_total=0 log_lines=0" in line


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
    _line, failure = ce.refusals(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert failure is not None and "fix the writer before tagging" in failure


def test_cut_mode_fails_on_a_refusal_in_the_engine_log(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    logs = cfg / "logs"
    logs.mkdir()
    (logs / "storage_service_native.log").write_text(
        "INFO boot\nWARN event=ownerless_chunk_write_refused source_path=/x\n"
    )
    line, failure = ce.refusals(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert "log_lines=1" in line
    assert failure is not None


def test_a_refusal_outside_cut_mode_is_reported_not_failed(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine(status={"ownerless_write_mode": "enforce", "ownerless_writes_refused_total": 3,
                          "ownerless_writes_would_refuse_total": 0})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    line, failure = ce.refusals(str(cfg), "mvv", {})
    assert failure is None and "refused_total=3" in line


def test_cut_mode_with_no_counters_cannot_have_seen_a_refusal(tmp_path: Path, jar: Path, engine) -> None:
    stub = engine(status={"embedding_mode": "local"})
    cfg = tmp_path / "cfg"
    _lease(cfg, stub.port, jar)
    _line, failure = ce.refusals(str(cfg), "mvv", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert failure is not None and "no ownerless-write counters" in failure


# ── cut-assert-log ──────────────────────────────────────────────────────────

_ID = "ENGINE IDENTITY [smoke]: candidate={flag} kind=jar artifact={art} sha256={sha} release_version=0.1.142 build_ref=x ownerless_write_mode=enforce"


def _id(flag: str, art: Path) -> str:
    return _ID.format(flag=flag, art=art, sha=ce._sha256(str(art)))


def test_assert_log_passes_an_indented_identity_naming_the_candidate(tmp_path: Path, jar: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text("noise\n    " + _id("yes", jar) + "\nSMOKE PASSED\n")
    assert ce.cut_assert_log(str(log), "smoke", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)}) is None


def test_assert_log_fails_a_green_leg_with_no_identity_line(tmp_path: Path, jar: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text("SMOKE PASSED\n")
    reason = ce.cut_assert_log(str(log), "smoke", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert reason is not None and "no 'ENGINE IDENTITY' line" in reason


def test_assert_log_fails_a_leg_that_ran_the_pinned_engine(tmp_path: Path, jar: Path, pinned: Path) -> None:
    log = tmp_path / "leg.log"
    log.write_text(_id("no", pinned) + "\n")
    reason = ce.cut_assert_log(str(log), "smoke", {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)})
    assert reason is not None and "not the candidate" in reason


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


def test_cli_identity_exit_codes(tmp_path: Path, jar: Path, pinned: Path, engine) -> None:
    stub = engine()
    cfg = tmp_path / "cfg"
    env = {"NX_CUT_MODE": "1", "NX_CANDIDATE_ENGINE": str(jar)}
    _lease(cfg, stub.port, jar)
    good = _cli("identity", str(cfg), "--label", "x", env=env)
    assert good.returncode == 0 and "candidate=yes" in good.stdout
    _lease(cfg, stub.port, pinned)
    bad = _cli("identity", str(cfg), "--label", "x", env=env)
    assert bad.returncode == 1 and "CANDIDATE ENGINE CHECK FAILED" in bad.stderr


# ── the battery's own cut-mode non-vacuity check (extracted, not retyped) ───


def _extract(text: str, header: str) -> str:
    start = text.index(header)
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _run_finish_leg(tmp_path: Path, log_text: str, *, cut: bool, cand: Path | None, leg: str = "mvv") -> tuple[str, str]:
    text = BATTERY.read_text()
    funcs = _extract(text, "cut_mode_vacuity() {") + "\n" + _extract(text, "finish_leg() {")
    cut_engine_legs = text[text.index("CUT_ENGINE_LEGS="):].splitlines()[0]
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"{leg}.log").write_text(log_text)
    harness = f"""#!/bin/bash
set -euo pipefail
REPO_ROOT={REPO_ROOT!s}
LOGS={logs!s}
CUT_MODE={1 if cut else 0}
export NX_CUT_MODE={1 if cut else 0}
{cut_engine_legs}
declare -A LEG_END LEG_RC LEG_STATUS LEG_LINE LEG_START LEG_VERDICT
LEG_START[{leg}]=1000
LEG_VERDICT[{leg}]='(PASSED|FAILED)'
{funcs}
finish_leg {leg} 0 >/dev/null
printf '%s|%s' "${{LEG_STATUS[{leg}]}}" "${{LEG_LINE[{leg}]}}"
"""
    path = tmp_path / "h.sh"
    path.write_text(harness)
    env = {"PATH": os.environ["PATH"]}
    if cand is not None:
        env["NX_CANDIDATE_ENGINE"] = str(cand)
    r = subprocess.run(["bash", str(path)], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"harness failed (extraction drifted?): {r.stdout!r} {r.stderr!r}"
    status, _, line = r.stdout.partition("|")
    return status, line


def test_battery_downgrades_a_green_cut_leg_that_never_named_an_engine(tmp_path: Path, jar: Path) -> None:
    status, line = _run_finish_leg(tmp_path, "FRESH PASSED\n", cut=True, cand=jar)
    assert status == "FAILED" and "VACUOUS in cut mode" in line


def test_battery_downgrades_a_green_cut_leg_that_ran_the_pinned_engine(tmp_path: Path, jar: Path, pinned: Path) -> None:
    log = _id("no", pinned) + "\nFRESH PASSED\n"
    status, line = _run_finish_leg(tmp_path, log, cut=True, cand=jar)
    assert status == "FAILED" and "VACUOUS in cut mode" in line


def test_battery_keeps_a_green_cut_leg_that_ran_the_candidate(tmp_path: Path, jar: Path) -> None:
    log = _id("yes", jar) + "\nFRESH PASSED\n"
    status, _line = _run_finish_leg(tmp_path, log, cut=True, cand=jar)
    assert status == "PASSED"


def test_battery_does_not_police_a_leg_that_provisions_no_engine(tmp_path: Path, jar: Path) -> None:
    status, _line = _run_finish_leg(tmp_path, "HOOK SKEW PASSED\n", cut=True, cand=jar, leg="hookskew")
    assert status == "PASSED"


def test_battery_outside_cut_mode_is_unchanged(tmp_path: Path) -> None:
    status, _line = _run_finish_leg(tmp_path, "FRESH PASSED\n", cut=False, cand=None)
    assert status == "PASSED"


def test_battery_cut_mode_wiring_pins() -> None:
    text = BATTERY.read_text()
    assert "--cut)" in text and "--candidate-engine)" in text
    # dtok is a leg in cut mode only, and the engine-bearing legs are all policed
    assert 'define_leg dtok' in text
    assert '[ "$CUT_MODE" != 1 ] || \\\ndefine_leg dtok' in text
    legs = text[text.index("CUT_ENGINE_LEGS="):].splitlines()[0]
    for leg in ("mvv", "smoke", "shakedown", "dtok"):
        assert f" {leg} " in legs, leg
    # no candidate in cut mode aborts the battery rather than running the pinned engine
    assert "CUT MODE: no candidate engine" in text and "LEG0_ABORT=1" in text
    # ...and that abort must read RED: the artifacts leg PASSED, and "NOT RUN" is not red.
    assert "CUT_NO_CANDIDATE=1" in text and 'if [ "$CUT_NO_CANDIDATE" = 1 ]' in text
