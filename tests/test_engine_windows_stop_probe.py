# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure parts of the CTRL_BREAK stop probe (RDR-224 P1.1, nexus-f9bgu.8 / .30).

``scripts/engine_windows_stop_probe.py`` drives a native Windows engine (CTRL_BREAK_EVENT,
taskkill, a throwaway PG cluster) and so only RUNS on Windows; its decisions (the changeset
count, the log reading, the exit-code verdict, the engine environment) are plain functions
tested here on every OS. The real run is recorded on the bead and in T2
``nexus_rdr/224-p1.1-stop-probe``.
"""

from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path

import pytest

import engine_windows_smoke as es
import engine_windows_stop_probe as probe

REPO = Path(__file__).resolve().parent.parent
DB_DIR = REPO / "service" / "src" / "main" / "resources" / "db"
SCRIPT = REPO / "scripts" / "engine_windows_stop_probe.py"


def test_count_changesets_ignores_xml_comments_and_walks_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "changelog").mkdir()
    (tmp_path / "db.changelog-master.xml").write_text(
        '<databaseChangeLog><changeSet id="a" author="x"/><!-- <changeSet id="old" author="x"/> --></databaseChangeLog>'
    )
    (tmp_path / "changelog" / "more.xml").write_text('<databaseChangeLog><changeSet id="b"/><changeSet id="c"/></databaseChangeLog>')
    (tmp_path / "changelog" / "notes.txt").write_text("<changeSet")
    assert probe.count_changesets(tmp_path) == 3


def test_count_changesets_agrees_with_the_smokes_include_following_count_on_the_real_changelog() -> None:
    """Two counters over one changelog: the probe's directory walk and the smoke's include walk. They must agree,
    or one of them is counting something that is not applied."""
    assert probe.count_changesets(DB_DIR) == es.changeset_count(DB_DIR / "changelog")


def test_stop_verdict_names_the_three_outcomes() -> None:
    assert probe.CLEAN_STOP_EXIT_CODE == 149 == 128 + 21
    assert probe.stop_verdict(149) == "clean"
    assert probe.stop_verdict("no-exit-20s") == "no-exit"
    assert probe.stop_verdict(None) == "no-exit"
    assert probe.stop_verdict(0) == "unexpected:0"
    assert probe.stop_verdict(143) == "unexpected:143"
    assert probe.stop_verdict(143, expected=143) == "clean"


def test_shutdown_flags_read_the_engines_own_events() -> None:
    log = "\n".join([
        "12:00:00 INFO x - event=shutdown_signal signal=BREAK",
        "12:00:00 INFO x - event=own_backends_terminated application_name=nexus-service/dev/ab count=10 pool_active=0",
        "12:00:00 INFO x - event=service_stopped",
        "12:00:01 WARN x - ort_init_signal_gate_unavailable reason=none",
    ])
    flags = probe.shutdown_flags(log)
    assert flags["shutdown_signal_logged"] is True and flags["service_stopped_logged"] is True
    assert flags["own_backends_terminated"] == ["event=own_backends_terminated application_name=nexus-service/dev/ab count=10 pool_active=0"]
    assert len(flags["unavailable_warnings"]) == 1  # type: ignore[arg-type]
    empty = probe.shutdown_flags("event=service_ready\n")
    assert empty["shutdown_signal_logged"] is False and empty["service_stopped_logged"] is False
    # the bare words without the event= key are not the events
    assert probe.shutdown_flags("shutdown_signal service_stopped")["shutdown_signal_logged"] is False


def test_new_changesets_takes_the_last_completion_line() -> None:
    assert probe.new_changesets("event=schema_migration_complete new_changesets=508 reexecuted_changesets=12") == 508
    assert probe.new_changesets("new_changesets=508\nnew_changesets=0") == 0
    assert probe.new_changesets("event=service_ready") is None


def test_landed_in_init_needs_the_gates_wait_event() -> None:
    assert probe.landed_in_init("x - event=ort_init_shutdown_wait in_flight=1 bound_ms=3000\n")
    assert not probe.landed_in_init("event=ort_init_shutdown_wait_done waited_ms=444")
    assert not probe.landed_in_init("event=service_ready")


def test_event_lines_keep_only_the_interesting_ones_trimmed_and_capped() -> None:
    log = "\n".join(["noise", "a b - event=service_ready", "other", "c d - event=shutdown_signal"] + ["x - event=ort_init y"] * 30)
    lines = probe.event_lines(log, limit=5)
    assert lines[:2] == ["event=service_ready", "event=shutdown_signal"] and len(lines) == 5


def test_the_shortened_wait_env_name_is_the_one_the_engine_reads() -> None:
    java = (REPO / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "vectors" / "OrtInitGate.java").read_text(encoding="utf-8")
    assert 'WAIT_ENV = "NX_ORT_INIT_SHUTDOWN_WAIT_MS"' in java
    assert "NX_ORT_INIT_SHUTDOWN_WAIT_MS" in SCRIPT.read_text(encoding="utf-8")


def _cfg(tmp_path: Path, **kw) -> probe.Config:  # noqa: ANN003
    return probe.Config(tmp_path / "e.exe", tmp_path / "pg" / "bin", tmp_path / "models", tmp_path / "db", tmp_path / "run", **kw)


def test_the_engine_environment_is_a_throwaway_one(tmp_path: Path) -> None:
    class Cl:
        port = 5555

    env = probe.engine_env(_cfg(tmp_path), Cl(), 7777, {"NX_VOYAGE_API_KEY": "secret", "KEEP": "1"})  # type: ignore[arg-type]
    assert "NX_VOYAGE_API_KEY" not in env, "a probe engine must never see a real embedding key"
    assert env["NX_DB_URL"] == "jdbc:postgresql://127.0.0.1:5555/nexus" and env["NX_SERVICE_PORT"] == "7777"
    assert env["NX_ALLOW_PROD_WRITE"] and env["NX_ONNX_MODEL_DIR"] == str(tmp_path / "models") and env["KEEP"] == "1"
    assert "NX_ORT_INIT_SHUTDOWN_WAIT_MS" not in env
    short = probe.engine_env(_cfg(tmp_path, ort_wait_ms=50), Cl(), 1, {})  # type: ignore[arg-type]
    assert short["NX_ORT_INIT_SHUTDOWN_WAIT_MS"] == "50"


def test_config_resolves_pg_tools_beside_the_bundle_bin(tmp_path: Path) -> None:
    assert _cfg(tmp_path).pg("initdb") == str(tmp_path / "pg" / "bin" / "initdb.exe")


def test_free_port_returns_a_bindable_port() -> None:
    port = probe.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_the_parser_requires_the_paths_and_knows_the_four_phases() -> None:
    parser = probe.build_parser()
    assert sorted(probe.PHASES) == ["a", "b", "c", "d"]
    with pytest.raises(SystemExit):
        parser.parse_args(["d"])  # no --exe etc.
    base = ["--exe", "e", "--pg-bin", "p", "--models", "m", "--changelog-dir", "c", "--run-dir", "r"]
    ns = parser.parse_args([*base, "--ort-wait-ms", "50", "a", "d"])
    assert ns.phases == ["a", "d"] and ns.ort_wait_ms == 50
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "z"])


def test_main_refuses_off_windows_with_a_distinct_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(probe, "sys", types.SimpleNamespace(platform="linux", stderr=sys.stderr))
    rc = probe.main(["--exe", "e", "--pg-bin", "p", "--models", "m", "--changelog-dir", "c", "--run-dir", "r", "d"])
    assert rc == 2
    assert "Windows only" in capsys.readouterr().err


def test_the_script_is_stdlib_only_and_names_its_record() -> None:
    src = SCRIPT.read_text(encoding="utf-8")
    import re

    tops = {m.split(".")[0] for m in re.findall(r"^(?:from|import) ([a-zA-Z_][\w.]*)", src, re.M)}
    assert not (tops - set(sys.stdlib_module_names)), tops - set(sys.stdlib_module_names)
    assert "224-p1.1-stop-probe" in src


# --------------------------------------------------------------------------- #
# The verdict: the probe ends with PASS or FAIL, never just a recording (review finding 8)
# --------------------------------------------------------------------------- #

CLEAN = probe.CLEAN_STOP_EXIT_CODE


def _good(phase: str) -> dict[str, object]:
    """A canned result in the shape the phase functions produce, for a run that proved what the phase is for."""
    if phase == "a":
        return {"boot1_break_before_lock": {"seen": "migration_pending", "exit_code": CLEAN},
                "boot2_plain": {"seen": "ready", "stop_exit_code": CLEAN}}
    if phase == "b":
        return {"boot1_break_mid_changeset": {"seen": "changesets_running=120", "exit_code": CLEAN}}
    if phase == "c":
        rows = [{"seen": "onnx_model_root", "exit_code": CLEAN, "landed_in_init": i in (3, 4)} for i in range(8)]
        return {"warm_stop_exit": CLEAN, "sweep": rows, "landed_count": 2, "crash_artifacts": [],
                "next_boot": {"stop_exit_code": CLEAN}}
    return {"serving_stop": {"exit_code": CLEAN, "shutdown_signal_logged": True, "service_stopped_logged": True},
            "next_boot": {"stop_exit_code": CLEAN, "new_changesets": 0}, "crash_artifacts": []}


@pytest.mark.parametrize("phase", ["a", "b", "c", "d"])
def test_a_run_that_proved_its_phase_has_no_problems(phase: str) -> None:
    assert probe.phase_problems(phase, _good(phase)) == []


def _with(phase: str, path: tuple[str, ...], value: object) -> dict[str, object]:
    res = _good(phase)
    node = res
    for key in path[:-1]:
        node = node[key]  # type: ignore[assignment,index]
    node[path[-1]] = value  # type: ignore[index]
    return res


@pytest.mark.parametrize(
    "phase, result, expect",
    [
        ("a", {"error": "RuntimeError('cluster did not start')", "tb": "..."}, "raised"),
        ("a", _with("a", ("boot1_break_before_lock", "seen"), "missed"), "trigger saw 'missed'"),
        ("a", _with("a", ("boot1_break_before_lock", "seen"), None), "trigger saw None"),
        ("a", _with("a", ("boot1_break_before_lock", "exit_code"), 1), "a boot 1: stop was unexpected:1"),
        ("a", _with("a", ("boot1_break_before_lock", "exit_code"), "no-exit-20s"), "a boot 1: stop was no-exit"),
        ("a", _with("a", ("boot2_plain", "seen"), None), "did not reach ready"),
        ("a", _with("a", ("boot2_plain", "stop_exit_code"), 143), "a boot 2: stop was unexpected:143"),
        ("b", _with("b", ("boot1_break_mid_changeset", "seen"), "missed"), "mid-changeset"),
        ("b", _with("b", ("boot1_break_mid_changeset", "seen"), None), "mid-changeset"),
        ("b", _with("b", ("boot1_break_mid_changeset", "exit_code"), 0), "b boot 1: stop was unexpected:0"),
        ("c", _with("c", ("landed_count",), 0), "no stop landed inside ORT init"),
        ("c", {k: v for k, v in _good("c").items() if k != "landed_count"}, "no stop landed inside ORT init"),
        ("c", _with("c", ("sweep",), []), "no sweep rows"),
        ("c", _with("c", ("warm_stop_exit",), 1), "c warm boot: stop was unexpected:1"),
        ("c", _with("c", ("next_boot", "stop_exit_code"), None), "c next boot: stop was no-exit"),
        ("c", _with("c", ("crash_artifacts",), ["C:\\run\\hs_err_pid1.log"]), "crash artifacts found"),
        ("d", _with("d", ("serving_stop", "exit_code"), 143), "d serving stop: stop was unexpected:143"),
        ("d", _with("d", ("serving_stop", "shutdown_signal_logged"), False), "did not log shutdown_signal"),
        ("d", _with("d", ("serving_stop", "service_stopped_logged"), False), "did not log service_stopped"),
        ("d", _with("d", ("next_boot", "new_changesets"), 3), "second boot applied changesets"),
        ("d", _with("d", ("next_boot", "new_changesets"), None), "second boot applied changesets"),
        ("d", _with("d", ("crash_artifacts",), ["x.dmp"]), "crash artifacts found"),
    ],
)
def test_the_verdict_names_each_way_a_phase_can_fail(phase: str, result: dict[str, object], expect: str) -> None:
    problems = probe.phase_problems(phase, result)
    assert any(expect in p for p in problems), problems


def test_a_sweep_row_with_a_missed_trigger_or_a_bad_exit_is_named_by_its_index() -> None:
    res = _good("c")
    res["sweep"][2]["seen"] = "ready"  # type: ignore[index]
    res["sweep"][5]["exit_code"] = 134  # type: ignore[index]
    problems = probe.phase_problems("c", res)
    assert any("row 2" in p and "not onnx_model_root" in p for p in problems), problems
    assert any("row 5" in p and "unexpected:134" in p for p in problems), problems


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, results: dict[str, object]) -> int:
    """main() as on Windows, with the phase functions replaced by canned results."""
    monkeypatch.setattr(probe, "sys", types.SimpleNamespace(platform="win32", stderr=sys.stderr))
    for ph, value in results.items():
        if isinstance(value, BaseException):
            def boom(cfg, _v=value):  # noqa: ANN001
                raise _v
            monkeypatch.setitem(probe.PHASES, ph, boom)
        else:
            monkeypatch.setitem(probe.PHASES, ph, lambda cfg, _v=value: dict(_v))  # type: ignore[arg-type]
    return probe.main(["--exe", "e", "--pg-bin", "p", "--models", "m", "--changelog-dir", "c", "--run-dir", str(tmp_path / "run"),
                       *results])


def test_main_exits_zero_and_says_pass_when_every_phase_proved_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_main(monkeypatch, tmp_path, {"a": _good("a"), "d": _good("d")}) == 0
    out = capsys.readouterr().out
    assert "PROBE phase a: PASS" in out and "PROBE phase d: PASS" in out
    saved = json.loads((tmp_path / "run" / "results-d.json").read_text())
    assert saved["verdict"] == "PASS" and saved["problems"] == []


def test_main_exits_one_on_a_phase_error_a_missed_trigger_or_a_bad_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results: dict[str, object] = {
        "a": RuntimeError("cluster did not start"),
        "b": _with("b", ("boot1_break_mid_changeset", "seen"), "missed"),
        "c": _with("c", ("landed_count",), 0),
        "d": _with("d", ("serving_stop", "exit_code"), 143),
    }
    assert _run_main(monkeypatch, tmp_path, results) == 1
    out = capsys.readouterr().out
    for ph in "abcd":
        assert f"PROBE phase {ph}: FAIL" in out, out
        assert json.loads((tmp_path / "run" / f"results-{ph}.json").read_text())["verdict"] == "FAIL"


def test_one_failing_phase_fails_the_run_even_when_the_others_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run_main(monkeypatch, tmp_path, {"a": _good("a"), "d": _with("d", ("next_boot", "new_changesets"), 2)}) == 1


def test_the_probes_clean_stop_exit_code_is_the_smokes_and_is_128_plus_sigbreak() -> None:
    """Two scripts name the CTRL_BREAK exit code separately; a drift between them would make the probe
    call clean what the release smoke refuses (or the reverse)."""
    assert probe.CLEAN_STOP_EXIT_CODE == es.WINDOWS_STOP_EXIT_CODE == 128 + 21 == 149
