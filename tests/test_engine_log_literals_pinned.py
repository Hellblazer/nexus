# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine log lines the Windows smoke and the stop probe read are pinned to the Java that writes them.

``scripts/engine_windows_smoke.py`` asserts the serving stop by finding ``event=shutdown_signal`` and
``event=service_stopped`` in the engine's log, and the second boot by ``new_changesets=<n>``;
``scripts/engine_windows_stop_probe.py`` also keys on ``event=service_ready``,
``event=schema_migration_pending``, ``event=onnx_model_root`` and the OrtInitGate wait events. They are
free text in a log line. A rename in the Java leaves every Python test green (the tests feed those
scripts hand-written logs) and turns the Windows release leg red at tag time. These scans fail at the
rename, on any OS (RDR-224 review finding 9, nexus-f9bgu.29/.32).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import engine_windows_smoke as es
import engine_windows_stop_probe as probe

REPO = Path(__file__).resolve().parent.parent
JAVA = REPO / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"
MAIN = JAVA / "Main.java"
NEXUS_SERVICE = JAVA / "NexusService.java"
SCHEMA_MIGRATOR = JAVA / "db" / "SchemaMigrator.java"
ORT_INIT_GATE = JAVA / "vectors" / "OrtInitGate.java"


def _code(path: Path) -> str:
    """The Java source with block and line comments removed: a literal a comment can satisfy is not a pin."""
    src = path.read_text(encoding="utf-8")
    return re.sub(r"//[^\n]*", "", re.sub(r"(?s)/\*.*?\*/", "", src))


def _log_literals(path: Path) -> list[str]:
    """The first string literal of every ``log.<level>("...")`` call, concatenated literals joined."""
    out: list[str] = []
    for m in re.finditer(r'log\.(?:info|warn|error|debug)\(\s*((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+)', _code(path)):
        out.append("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))))
    return out


@pytest.mark.parametrize(
    "source, event",
    [
        (MAIN, "shutdown_signal"),
        (NEXUS_SERVICE, "service_stopped"),
        (MAIN, "service_ready"),
        (MAIN, "onnx_model_root"),
        (SCHEMA_MIGRATOR, "schema_migration_pending"),
        (SCHEMA_MIGRATOR, "schema_migration_complete"),
    ],
)
def test_each_event_the_windows_scripts_wait_for_is_logged_by_the_engine(source: Path, event: str) -> None:
    lits = _log_literals(source)
    assert any(re.match(rf"event={event}\b", lit) for lit in lits), (
        f"{source.name} no longer logs event={event}; the Windows smoke/probe read it. Literals: {lits}"
    )


def test_the_smokes_shutdown_events_are_exactly_ones_the_engine_logs() -> None:
    """Derived from the smoke's own constant, so adding an event there without a producer fails here."""
    produced = [lit for p in (MAIN, NEXUS_SERVICE) for lit in _log_literals(p)]
    for ev in es._SHUTDOWN_EVENTS:
        assert any(re.search(rf"\bevent={ev}\b", lit) for lit in produced), f"nothing logs event={ev}"


def test_every_schema_migration_complete_line_carries_new_changesets_in_the_form_both_scripts_parse() -> None:
    """Both completion sites (a boot with changesets pending and a boot with none) render the field the
    smoke and the probe read; the second boot, the one that matters, takes the no-pending site."""
    lits = [lit for lit in _log_literals(SCHEMA_MIGRATOR) if lit.startswith("event=schema_migration_complete")]
    assert len(lits) >= 2, f"expected both completion sites, found {lits}"
    for lit in lits:
        rendered = lit.replace("{}", "0")
        assert es._NEW_CHANGESETS_RE.search(rendered), f"the smoke cannot read new_changesets from {lit!r}"
        assert probe.new_changesets(f"... - {rendered}") == 0, f"the probe cannot read new_changesets from {lit!r}"


def test_the_ort_init_wait_events_are_the_ones_the_probe_and_the_signal_tests_read() -> None:
    lits = _log_literals(ORT_INIT_GATE)
    waits = [lit for lit in lits if lit.startswith("event=ort_init_shutdown_wait ")]
    assert waits, f"OrtInitGate no longer logs 'event=ort_init_shutdown_wait ': {lits}"
    assert probe.landed_in_init(waits[0].replace("{}", "1")), "the probe cannot recognise the wait event it is meant to find"
    assert "in_flight={}" in waits[0], "OrtInitGateSignalTest asserts 'ort_init_shutdown_wait in_flight=1'"
    assert any(lit.startswith("event=ort_init_shutdown_wait_done") for lit in lits)
    assert any(lit.startswith("event=ort_init_shutdown_wait_timeout") for lit in lits)
    assert any(lit.startswith("event=ort_init_signal_gate_unavailable") for lit in lits)


def test_the_literal_scan_is_not_satisfied_by_a_comment(tmp_path: Path) -> None:
    f = tmp_path / "X.java"
    f.write_text('class X { // log.info("event=shutdown_signal");\n /* log.info("event=service_stopped"); */\n void m() { log.info("event=other"); } }')
    assert _log_literals(f) == ["event=other"]


# --------------------------------------------------------------------------- #
# A REAL engine log (tests/fixtures/windows_engine/PROVENANCE.md) through both consumers
# --------------------------------------------------------------------------- #

FIXTURES = REPO / "tests" / "fixtures" / "windows_engine"
FIRST_BOOT = (FIXTURES / "engine-first-boot.log").read_text(encoding="utf-8")
SECOND_BOOT = (FIXTURES / "engine-second-boot.log").read_text(encoding="utf-8")


def test_the_smokes_log_checks_accept_the_real_logs_and_refuse_the_wrong_boot() -> None:
    es.check_shutdown_log(FIRST_BOOT)
    es.check_shutdown_log(SECOND_BOOT)
    es.check_reboot_log(SECOND_BOOT)
    with pytest.raises(es.SmokeError, match="applied 508 new changesets"):
        es.check_reboot_log(FIRST_BOOT)
    with pytest.raises(es.SmokeError, match="lacks event=shutdown_signal, event=service_stopped"):
        es.check_shutdown_log("\n".join(ln for ln in FIRST_BOOT.splitlines() if "shutdown_signal" not in ln and "service_stopped" not in ln))


def test_the_probes_readers_agree_with_the_real_logs() -> None:
    flags = probe.shutdown_flags(FIRST_BOOT)
    assert flags["shutdown_signal_logged"] is True and flags["service_stopped_logged"] is True
    assert len(flags["own_backends_terminated"]) == 1  # type: ignore[arg-type]
    assert flags["unavailable_warnings"] == []
    assert probe.new_changesets(FIRST_BOOT) == 508
    assert probe.new_changesets(SECOND_BOOT) == 0
    assert probe.landed_in_init(FIRST_BOOT) is False, "a serving stop never deferred inside ORT init"
    lines = probe.event_lines(FIRST_BOOT)
    assert any("event=schema_migration_pending changesets=508" in ln for ln in lines)
    assert any("event=service_ready" in ln for ln in lines) and any("event=service_stopped" in ln for ln in lines)


def _java_literal_regex(lit: str) -> re.Pattern[str]:
    return re.compile(r"\b" + r"\S+".join(re.escape(part) for part in lit.rstrip().split("{}")))


@pytest.mark.parametrize(
    "source, event",
    [
        (MAIN, "shutdown_signal"),
        (NEXUS_SERVICE, "service_stopped"),
        (MAIN, "service_ready"),
        (MAIN, "onnx_model_root"),
        (SCHEMA_MIGRATOR, "schema_migration_pending"),
        (SCHEMA_MIGRATOR, "schema_migration_complete"),
        (JAVA / "vectors" / "OrtTempSweep.java", "ort_temp_sweep"),
    ],
)
def test_the_current_java_log_literals_still_render_the_lines_the_real_windows_run_produced(source: Path, event: str) -> None:
    """The fixture is a real run; the Java literal is today's. If a field is added, renamed or reordered in
    the literal, the real line stops matching and this fails before the Windows release leg does."""
    real = [ln for ln in (FIRST_BOOT + SECOND_BOOT).splitlines() if f"event={event}" in ln]
    assert real, f"the fixtures hold no event={event} line"
    literals = [lit for lit in _log_literals(source) if lit.startswith(f"event={event}")]
    assert literals, f"{source.name} no longer logs event={event}"
    assert all(any(_java_literal_regex(lit).search(ln) for lit in literals) for ln in real), (
        f"no {source.name} literal for event={event} matches the real line {real[0]!r}; literals: {literals}"
    )
