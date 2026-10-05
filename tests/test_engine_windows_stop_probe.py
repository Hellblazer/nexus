# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure parts of the CTRL_BREAK stop probe (RDR-224 P1.1, nexus-f9bgu.8 / .30).

``scripts/engine_windows_stop_probe.py`` drives a native Windows engine (CTRL_BREAK_EVENT,
taskkill, a throwaway PG cluster) and so only RUNS on Windows; its decisions (the changeset
count, the log reading, the exit-code verdict, the engine environment) are plain functions
tested here on every OS. The real run is recorded on the bead and in T2
``nexus_rdr/224-p1.1-stop-probe``.
"""

from __future__ import annotations

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
