"""scripts/check_native_data_move_boot.py: each assertion passes on a log with its line and FAILS on a log without it.

The checker runs on the native engine's boot log in the release workflow's smoke-native-data-move job, a
runner nobody has locally, so its own proof is here: pure functions on log text in the engine's real line
format (copied from engine boot logs of 2026-11-17), one test per assertion, each against a log that is
good except for that one line.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_native_data_move_boot.py"

_spec = importlib.util.spec_from_file_location("check_native_data_move_boot", SCRIPT)
assert _spec is not None and _spec.loader is not None
chk = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = chk
_spec.loader.exec_module(chk)

SVC = "Svc-secret-0123456789abcdef"
ADMIN = "Adm-secret-fedcba9876543210"
SECRETS = {"svc": SVC, "admin": ADMIN}

_P = "2026-11-17 10:02:13,684 {lvl:<5} [main] {cls} [cid=- tenant=- session=-] - "
_INSTALLER = "d.nexus.service.db.DmlUtilsInstaller"
_MIGRATOR = "dev.nexus.service.db.SchemaMigrator"

L_MASKING = _P.format(lvl="INFO", cls="d.nexus.service.log.LogMasking") + "event=log_masking_installed masking=1 unmasked=0"
L_DDL = _P.format(lvl="INFO", cls=_INSTALLER) + (
    "event=data_move_library_applied library=ddl_utils changesets=77 master=db/vendor/ddl_utils/db.changelog-master.xml")
L_DML = _P.format(lvl="INFO", cls=_INSTALLER) + (
    "event=data_move_library_applied library=dml_utils changesets=132 master=db/vendor/dml_utils/db.changelog-master.xml")
L_SKIP = _P.format(lvl="WARN", cls=_INSTALLER) + 'event=data_move_libraries_skipped reason="pg_background is not installed"'
L_WALK = _P.format(lvl="INFO", cls=_MIGRATOR) + (
    "event=schema_migration_complete new_changesets=580 reexecuted_changesets=0 pending_at_start=580 mark_ran_changesets=0")
L_READY = _P.format(lvl="INFO", cls="dev.nexus.service.Main") + "event=service_ready port=45123"
L_NOISE = _P.format(lvl="INFO", cls=_MIGRATOR) + "event=schema_migration_start changelog=db/changelog/db.changelog-master.xml"


def _probe(label: str, value: str) -> str:
    return _P.format(lvl="WARN", cls="dev.nexus.service.http.AuthFilter") + (
        f"event=request_failed method=GET path={chk.PROBE_PATH_PREFIX}{label}-{value} status=401 exception=none duration_ms=2")


L_PROBE_SVC = _probe("svc", chk.REDACTED)
L_PROBE_ADMIN = _probe("admin", chk.REDACTED)


def log(*lines: str) -> str:
    return "\n".join(lines) + "\n"


GOOD_WITH = log(L_NOISE, L_MASKING, L_DDL, L_DML, L_WALK, L_READY, L_PROBE_SVC, L_PROBE_ADMIN)
GOOD_WITHOUT = log(L_NOISE, L_MASKING, L_SKIP, L_WALK, L_READY, L_PROBE_SVC, L_PROBE_ADMIN)


def failed(text: str, mode: str, secrets=SECRETS) -> list[str]:
    report = chk.run_checks(text, mode, secrets)
    return [r.name for r in report.results if not r.ok]


def passes(text: str, mode: str, secrets=SECRETS) -> bool:
    report = chk.run_checks(text, mode, secrets)
    return report.ok


def without(text: str, line: str) -> str:
    assert line in text, "the test's log must hold the line it removes"
    return text.replace(line + "\n", "")


# ── the good logs pass, and examine something ──────────────────────────────────


def test_a_good_log_with_pg_background_passes() -> None:
    report = chk.run_checks(GOOD_WITH, "with-pg-background", SECRETS)
    assert report.ok, report.render()
    assert report.lines == 8
    assert all(r.examined == 8 for r in report.results)
    assert len(report.results) >= 8


def test_a_good_log_without_pg_background_passes() -> None:
    report = chk.run_checks(GOOD_WITHOUT, "without-pg-background", SECRETS)
    assert report.ok, report.render()


# ── group 1: both library-applied lines (a bundle with pg_background) ──────────


def test_ddl_utils_line_missing_fails() -> None:
    text = without(GOOD_WITH, L_DDL)
    assert failed(text, "with-pg-background") == ["event=data_move_library_applied library=ddl_utils"]


def test_dml_utils_line_missing_fails_when_ddl_utils_is_present() -> None:
    text = without(GOOD_WITH, L_DML)
    assert failed(text, "with-pg-background") == ["event=data_move_library_applied library=dml_utils"]


def test_both_library_lines_missing_fails_both() -> None:
    text = without(without(GOOD_WITH, L_DDL), L_DML)
    assert len(failed(text, "with-pg-background")) == 2


def test_a_skip_instead_of_the_applied_lines_fails() -> None:
    text = log(L_MASKING, L_SKIP, L_WALK, L_READY, L_PROBE_SVC, L_PROBE_ADMIN)
    names = failed(text, "with-pg-background")
    assert "event=data_move_library_applied library=ddl_utils" in names
    assert "event=data_move_library_applied library=dml_utils" in names
    assert "no event=data_move_libraries_skipped on a bundle with pg_background" in names


def test_a_failed_library_changelog_fails_even_with_both_applied_lines() -> None:
    bad = _P.format(lvl="ERROR", cls=_INSTALLER) + 'event=data_move_libraries_failed reason="MissingReflectionRegistrationError"'
    assert failed(GOOD_WITH + bad + "\n", "with-pg-background") == ["no event=data_move_libraries_failed"]


# ── group 3: no pg_background (Sam's 8.4 ruling) ───────────────────────────────


def test_the_skip_line_missing_fails() -> None:
    text = without(GOOD_WITHOUT, L_SKIP)
    names = failed(text, "without-pg-background")
    assert 'event=data_move_libraries_skipped reason="pg_background is not installed"' in names


def test_a_skip_with_another_reason_fails() -> None:
    other = L_SKIP.replace("pg_background is not installed", "library role dml_utils_owner is missing")
    text = GOOD_WITHOUT.replace(L_SKIP, other)
    assert 'event=data_move_libraries_skipped reason="pg_background is not installed"' in failed(text, "without-pg-background")


def test_the_skip_at_info_instead_of_warn_fails() -> None:
    text = GOOD_WITHOUT.replace(L_SKIP, L_SKIP.replace("WARN ", "INFO "))
    assert failed(text, "without-pg-background") == [
        "the skip is a WARN (a separate admin role, so not the expected single-role skip)"]


def test_the_schema_walk_not_completing_fails() -> None:
    text = without(GOOD_WITHOUT, L_WALK)
    assert failed(text, "without-pg-background") == ["event=schema_migration_complete (the nexus schema walk finished)"]


def test_the_engine_not_serving_fails() -> None:
    text = without(GOOD_WITHOUT, L_READY)
    assert failed(text, "without-pg-background") == ["event=service_ready (the engine serves)"]


def test_a_library_applied_without_pg_background_fails() -> None:
    assert failed(GOOD_WITHOUT + L_DDL + "\n", "without-pg-background") == [
        "no event=data_move_library_applied without pg_background"]


# ── group 2a: the masking install line ─────────────────────────────────────────


@pytest.mark.parametrize("mode,good", [("with-pg-background", GOOD_WITH), ("without-pg-background", GOOD_WITHOUT)])
def test_the_masking_install_line_missing_fails(mode: str, good: str) -> None:
    assert failed(without(good, L_MASKING), mode) == ["event=log_masking_installed masking>=1"]


@pytest.mark.parametrize("mode,good", [("with-pg-background", GOOD_WITH), ("without-pg-background", GOOD_WITHOUT)])
def test_the_masking_install_line_with_masking_zero_fails(mode: str, good: str) -> None:
    text = good.replace("masking=1 unmasked=0", "masking=0 unmasked=1")
    assert failed(text, mode) == ["event=log_masking_installed masking>=1"]


def test_masking_two_appenders_passes() -> None:
    assert passes(GOOD_WITH.replace("masking=1 unmasked=0", "masking=2 unmasked=0"), "with-pg-background")


def test_log_masking_inactive_fails_even_with_an_install_line() -> None:
    inactive = _P.format(lvl="WARN", cls="dev.nexus.service.Main") + 'event=log_masking_inactive reason="no appender masks"'
    assert failed(GOOD_WITH + inactive + "\n", "with-pg-background") == ["event=log_masking_installed masking>=1"]


# ── group 2b: a masked line, and no raw secret ─────────────────────────────────


@pytest.mark.parametrize("mode,good", [("with-pg-background", GOOD_WITH), ("without-pg-background", GOOD_WITHOUT)])
def test_the_masked_probe_line_missing_fails(mode: str, good: str) -> None:
    names = failed(without(good, L_PROBE_SVC), mode)
    assert names == [f"masked line: path={chk.PROBE_PATH_PREFIX}svc-<redacted>"]


@pytest.mark.parametrize("mode,good", [("with-pg-background", GOOD_WITH), ("without-pg-background", GOOD_WITHOUT)])
def test_the_raw_secret_in_the_probe_line_fails(mode: str, good: str) -> None:
    text = good.replace(L_PROBE_SVC, _probe("svc", SVC))
    names = failed(text, mode)
    assert f"masked line: path={chk.PROBE_PATH_PREFIX}svc-<redacted>" in names
    assert "the svc secret appears in no line" in names


def test_the_raw_secret_on_an_unrelated_line_fails() -> None:
    leak = _P.format(lvl="ERROR", cls="com.zaxxer.hikari.pool.PoolBase") + f"Failed to validate connection: password={ADMIN}"
    assert failed(GOOD_WITH + leak + "\n", "with-pg-background") == ["the admin secret appears in no line"]


def test_the_raw_secret_in_a_stack_trace_line_fails() -> None:
    trace = f"Caused by: org.postgresql.util.PSQLException: bad value {SVC}"
    assert failed(GOOD_WITH + trace + "\n", "with-pg-background") == ["the svc secret appears in no line"]


def test_a_second_secret_is_checked_on_its_own() -> None:
    assert failed(without(GOOD_WITH, L_PROBE_ADMIN), "with-pg-background") == [
        f"masked line: path={chk.PROBE_PATH_PREFIX}admin-<redacted>"]


@pytest.mark.parametrize("secrets", [{}, {"svc": ""}, {"svc": "   "}])
def test_no_secret_to_look_for_fails(secrets: dict[str, str]) -> None:
    assert failed(GOOD_WITH, "with-pg-background", secrets) == ["masked line"]


# ── never vacuous ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "\n", "   \n\n"])
def test_an_empty_log_fails(text: str) -> None:
    report = chk.run_checks(text, "with-pg-background", SECRETS)
    assert not report.ok
    assert [r.name for r in report.results] == ["the log has lines"]


def test_an_unknown_mode_fails() -> None:
    assert not chk.run_checks(GOOD_WITH, "with-pgbackground", SECRETS).ok


def test_a_log_of_unrelated_lines_fails_every_mode() -> None:
    noise = log(L_NOISE, L_NOISE)
    for mode in chk.MODES:
        assert not chk.run_checks(noise, mode, SECRETS).ok


def test_the_report_names_the_failure_and_never_prints_a_secret() -> None:
    text = GOOD_WITH.replace(L_PROBE_SVC, _probe("svc", SVC))
    rendered = chk.run_checks(text, "with-pg-background", SECRETS).render()
    assert rendered.endswith("FAIL")
    assert "svc" in rendered
    assert SVC not in rendered and ADMIN not in rendered


# ── the command line ───────────────────────────────────────────────────────────


def _run(tmp_path: Path, text: str, mode: str, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "svc.log"
    f.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--mode", mode, "--log", str(f), *extra],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env}, check=False)


ENV = {"T_SVC": SVC, "T_ADMIN": ADMIN}
ARGS = ("--secret", "svc=T_SVC", "--secret", "admin=T_ADMIN")


def test_cli_exit_0_on_a_good_log(tmp_path: Path) -> None:
    r = _run(tmp_path, GOOD_WITH, "with-pg-background", ENV, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().endswith("PASS")
    assert re.search(r"examined 8 lines", r.stdout)


def test_cli_exit_1_on_a_log_missing_a_line(tmp_path: Path) -> None:
    r = _run(tmp_path, without(GOOD_WITH, L_DML), "with-pg-background", ENV, *ARGS)
    assert r.returncode == 1
    assert "FAIL event=data_move_library_applied library=dml_utils" in r.stdout


def test_cli_exit_2_without_a_secret_option(tmp_path: Path) -> None:
    assert _run(tmp_path, GOOD_WITH, "with-pg-background", ENV).returncode == 2


def test_cli_exit_2_on_an_unset_secret_variable(tmp_path: Path) -> None:
    r = _run(tmp_path, GOOD_WITH, "with-pg-background", {"T_SVC": SVC}, *ARGS)
    assert r.returncode == 2
    assert "T_ADMIN" in r.stderr and ADMIN not in r.stderr


def test_cli_exit_2_on_an_empty_log(tmp_path: Path) -> None:
    assert _run(tmp_path, "", "with-pg-background", ENV, *ARGS).returncode == 2


def test_cli_exit_2_on_a_missing_log(tmp_path: Path) -> None:
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "--mode", "with-pg-background", "--log",
                        str(tmp_path / "absent.log"), *ARGS], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", **ENV}, check=False)
    assert r.returncode == 2


def test_cli_never_prints_a_secret_a_quoted_log_line_carries(tmp_path: Path) -> None:
    """A failure's detail quotes engine lines; when masking is broken a quoted line can hold a secret, and the
    checker's output (a public workflow log) must not (review of 9a334f58c)."""
    leaked = _P.format(lvl="ERROR", cls=_INSTALLER) + (
        f"event=data_move_libraries_failed library=ddl_utils error=password authentication failed for {ADMIN}")
    text = log(L_NOISE, L_MASKING, leaked, L_DML, L_WALK, L_READY, L_PROBE_SVC, L_PROBE_ADMIN)
    r = _run(tmp_path, text, "with-pg-background", ENV, *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert ADMIN not in r.stdout and ADMIN not in r.stderr
    assert SVC not in r.stdout


@pytest.mark.parametrize("line", ["L_DDL", "L_DML"])
def test_an_applied_line_with_zero_changesets_fails(line: str) -> None:
    """The installer logs the line after the walk even when nothing was pending; on the smoke's fresh cluster a
    count of 0 means the library was not applied (critique of 9a334f58c, S4)."""
    original = globals()[line]
    zero = re.sub(r"changesets=\d+", "changesets=0", original)
    text = GOOD_WITH.replace(original, zero)
    lib = "ddl_utils" if line == "L_DDL" else "dml_utils"
    assert failed(text, "with-pg-background") == [f"event=data_move_library_applied library={lib}"]


_UNIQUE = "zq-not-in-any-fixture-7d1"


@pytest.mark.parametrize(
    ("mode", "line"),
    [
        ("with-pg-background", L_SKIP),
        ("with-pg-background", _P.format(lvl="ERROR", cls=_INSTALLER) + "event=data_move_libraries_failed library=ddl_utils"),
        ("without-pg-background", L_DDL),
        ("without-pg-background", _P.format(lvl="ERROR", cls=_INSTALLER) + "event=data_move_libraries_failed library=ddl_utils"),
        ("with-pg-background", _P.format(lvl="WARN", cls="d.nexus.service.log.LogMasking") + "event=log_masking_inactive appender=FILE"),
    ],
)
def test_a_failure_cites_the_offending_line_by_number_and_never_quotes_it(mode: str, line: str) -> None:
    """Nothing the checker prints is taken from a log line's text, so nothing it prints can carry a secret a
    broken mask left in one (CodeQL alert 62: printed text must not flow from the secret values; the old
    redact() pass is gone). The line number points into the smoke's redacted log tail."""
    base = GOOD_WITH if mode == "with-pg-background" else GOOD_WITHOUT
    marked = line + f" note={_UNIQUE}"
    text = base.replace(L_NOISE + "\n", L_NOISE + "\n" + marked + "\n")
    report = chk.run_checks(text, mode, SECRETS)
    bad = [r for r in report.results if not r.ok]
    assert bad, "fixture precondition: the inserted line must fail a check"
    rendered = report.render()
    assert _UNIQUE not in rendered, rendered
    assert any(re.search(r"\bline 2\b", r.detail) for r in bad), [r.detail for r in bad]


def test_the_checker_prints_its_report_without_a_secret_dependent_pass() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "def redact(" not in source and "redact(" not in source.split("def main(", 1)[1], (
        "the printed report must not be computed from the secrets (CodeQL alert 62)")
