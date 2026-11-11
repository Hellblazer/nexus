# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline self-test for the upgrade rehearsal's write-rate probe, version selection and verdict (nexus-f3yqd.5).

No engine, no PG, no network: PG is a fake ``psql`` script or a scripted callable, time is a fake
clock. The PCI keep check's watcher, trigger, judge and log reader are in test_upgrade_rehearsal_pci.py.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_wp", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_wp"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

A = "pci_" + "a" * 24
B = "pci_" + "b" * 24
C = "pci_" + "c" * 24


# --- credentials and the bundled psql ---------------------------------------------------------


CREDS = (
    "# generated\n"
    "PG_DATA=/data/pg\nPG_PORT=54321\nNX_DB_URL=jdbc:postgresql://127.0.0.1:54321/nexusdb?x=1\n"
    "PG_SUPERUSER_PASS=s3cret\nNX_DB_ADMIN_USER=nexus_admin\nNX_DB_ADMIN_PASS=adm1n\n"
)


def test_parse_credentials_skips_comments_and_blank_lines() -> None:
    c = ur.parse_credentials(CREDS + "\n  \nbroken line\n")
    assert c["PG_PORT"] == "54321" and c["PG_SUPERUSER_PASS"] == "s3cret"
    assert "# generated" not in c and len(c) == 6


def _fake_install(tmp_path: Path, *, creds: str = CREDS, reply: str = "") -> Path:
    config = tmp_path / "config"
    bin_dir = config / "pg-bundle" / "bundle" / "bin"
    bin_dir.mkdir(parents=True)
    (config / "pg_credentials").write_text(creds)
    psql = bin_dir / "psql"
    psql.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {tmp_path}/argv.txt\n'
        f'printf "%s" "$PGPASSWORD" > {tmp_path}/pgpassword.txt\n'
        f'printf "%s" "$LD_LIBRARY_PATH" > {tmp_path}/ldpath.txt\n'
        f"cat {tmp_path}/reply.txt\n"
        f"exit $(cat {tmp_path}/rc.txt 2>/dev/null || echo 0)\n"
    )
    psql.chmod(psql.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "reply.txt").write_text(reply)
    return config


def test_probe_runs_the_installs_own_psql_with_the_password_in_the_environment(
    tmp_path: Path,
) -> None:
    config = _fake_install(tmp_path, reply="0/1A2B3C4\t77\n")
    rows = ur.PgProbe(config).sql("select 1")
    assert rows == [["0/1A2B3C4", "77"]]
    argv = (tmp_path / "argv.txt").read_text().split("\n")
    for want in ("-h", "127.0.0.1", "-p", "54321", "-d", "nexusdb", "-c", "select 1"):
        assert want in argv, argv
    assert "s3cret" not in " ".join(argv), (
        "the password must never be on a command line"
    )
    assert (tmp_path / "pgpassword.txt").read_text() == "s3cret"
    assert (
        str(config / "pg-bundle" / "bundle" / "lib")
        in (tmp_path / "ldpath.txt").read_text()
    )


def test_probe_uses_the_schema_owner_where_no_superuser_password_is_recorded(
    tmp_path: Path,
) -> None:
    old = "\n".join(
        ln for ln in CREDS.splitlines() if not ln.startswith("PG_SUPERUSER_PASS")
    )
    config = _fake_install(tmp_path, creds=old, reply="x\n")
    ur.PgProbe(config).sql("select 1")
    argv = (tmp_path / "argv.txt").read_text().split("\n")
    assert argv[argv.index("-U") + 1] == "nexus_admin"
    assert (tmp_path / "pgpassword.txt").read_text() == "adm1n"


def test_probe_returns_none_while_pg_is_not_accepting_connections(
    tmp_path: Path,
) -> None:
    config = _fake_install(tmp_path)
    psql = config / "pg-bundle" / "bundle" / "bin" / "psql"
    psql.write_text(
        '#!/bin/sh\necho \'psql: error: connection to server at "127.0.0.1" failed: '
        "Connection refused' >&2\nexit 2\n"
    )
    assert ur.PgProbe(config).sql("select 1") is None


def test_probe_raises_on_a_real_sql_error_and_on_missing_credentials(
    tmp_path: Path,
) -> None:
    config = _fake_install(tmp_path)
    psql = config / "pg-bundle" / "bundle" / "bin" / "psql"
    psql.write_text("#!/bin/sh\necho 'ERROR:  syntax error' >&2\nexit 1\n")
    with pytest.raises(ur.ProbeError, match="syntax error"):
        ur.PgProbe(config).sql("selec 1")
    with pytest.raises(ur.ProbeError, match="pg_credentials"):
        ur.PgProbe(tmp_path / "nowhere").sql("select 1")


@pytest.mark.parametrize("creds_has_superuser", [True, False])
def test_probe_error_text_never_carries_the_password(
    tmp_path: Path, creds_has_superuser: bool
) -> None:
    """A psql that echoes its connection string on failure (it can, on an authentication error) must
    not leak the password through the ProbeError the driver prints and writes to verdict.json."""
    creds = (
        CREDS
        if creds_has_superuser
        else "\n".join(
            ln for ln in CREDS.splitlines() if not ln.startswith("PG_SUPERUSER_PASS")
        )
    )
    password = "s3cret" if creds_has_superuser else "adm1n"
    config = _fake_install(tmp_path, creds=creds)
    psql = config / "pg-bundle" / "bundle" / "bin" / "psql"
    psql.write_text(
        "#!/bin/sh\n"
        f'echo "psql: error: FATAL: password authentication failed (password={password}) '
        f'conninfo postgresql://u:{password}@127.0.0.1/db" >&2\n'
        "exit 2\n"
    )
    with pytest.raises(ur.ProbeError) as err:
        ur.PgProbe(config).sql("select 1")
    text = str(err.value)
    assert password not in text
    assert "password authentication failed" in text and "***" in text


# --- WAL counters and the write rate ------------------------------------------------------------


def test_lsn_bytes_reads_pg_lsn_text() -> None:
    assert ur.lsn_bytes("0/0") == 0
    assert ur.lsn_bytes("0/1A2B3C4") == 0x1A2B3C4
    assert ur.lsn_bytes("2/10") == (2 << 32) + 0x10


def _sql_with(**answers):
    def sql(query: str):
        for key, val in answers.items():
            if key in query:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unscripted query: {query}")

    return sql


def test_read_counters_collects_lsn_wal_and_io() -> None:
    sql = _sql_with(
        pg_current_wal_lsn=[["1/0"]],
        pg_stat_wal=[["4096"]],
        write_bytes=[["1000", "50"]],
    )
    c = ur.read_counters(sql)
    assert c == ur.PgCounters(
        lsn=1 << 32, wal_bytes=4096, io_write_bytes=1000, io_extend_bytes=50
    )


def test_read_counters_falls_back_to_op_bytes_and_tolerates_missing_stats() -> None:
    sql = _sql_with(
        pg_current_wal_lsn=[["0/10"]],
        pg_stat_wal=ur.ProbeError("no such view"),
        write_bytes=ur.ProbeError("no column write_bytes"),
        op_bytes=[["7", "3"]],
    )
    c = ur.read_counters(sql)
    assert c.lsn == 16 and c.wal_bytes is None
    assert (c.io_write_bytes, c.io_extend_bytes) == (7, 3)
    none_sql = _sql_with(
        pg_current_wal_lsn=[["0/10"]],
        pg_stat_wal=ur.ProbeError("x"),
        write_bytes=ur.ProbeError("x"),
        op_bytes=ur.ProbeError("x"),
    )
    c2 = ur.read_counters(none_sql)
    assert c2.io_write_bytes is None and c2.io_extend_bytes is None


def test_read_counters_is_none_while_pg_is_down() -> None:
    assert ur.read_counters(lambda q: None) is None


def test_write_rate_is_wal_bytes_over_the_step_wall_in_mb() -> None:
    before = ur.PgCounters(
        lsn=100 * ur.MB, wal_bytes=10, io_write_bytes=0, io_extend_bytes=0
    )
    after = ur.PgCounters(
        lsn=300 * ur.MB,
        wal_bytes=10 + 190 * ur.MB,
        io_write_bytes=50 * ur.MB,
        io_extend_bytes=ur.MB,
    )
    wr = ur.write_rate(before, after, wall_s=10.0, store_bytes=2_000 * ur.MB)
    assert wr["wal_bytes"] == 200 * ur.MB
    assert wr["wal_mb"] == 200.0
    assert wr["seconds"] == 10.0
    assert wr["mb_per_s"] == 20.0
    assert wr["pg_stat_wal_bytes"] == 190 * ur.MB
    assert wr["io_write_bytes"] == 50 * ur.MB and wr["io_extend_bytes"] == ur.MB
    assert wr["wal_per_store_byte"] == pytest.approx(0.1)
    # S GB of store at this rate: S*1024*ratio/MBps; with ratio 1 it is S*1024/MBps
    assert wr["seconds_per_gb_of_wal"] == pytest.approx(1024 / 20.0)


def test_write_rate_handles_a_zero_second_step_and_a_missing_store_size() -> None:
    c = ur.PgCounters(lsn=5, wal_bytes=None, io_write_bytes=None, io_extend_bytes=None)
    wr = ur.write_rate(c, c, wall_s=0.0)
    assert wr["mb_per_s"] is None and wr["wal_bytes"] == 0
    assert wr["wal_per_store_byte"] is None and wr["pg_stat_wal_bytes"] is None


def test_write_rate_drops_a_stat_counter_that_went_backwards_but_keeps_the_lsn() -> (
    None
):
    before = ur.PgCounters(lsn=0, wal_bytes=900, io_write_bytes=900, io_extend_bytes=1)
    after = ur.PgCounters(lsn=ur.MB, wal_bytes=5, io_write_bytes=5, io_extend_bytes=1)
    wr = ur.write_rate(before, after, wall_s=1.0)
    assert wr["wal_bytes"] == ur.MB
    assert wr["pg_stat_wal_bytes"] is None and wr["io_write_bytes"] is None


def test_write_rate_refuses_a_lsn_that_went_backwards() -> None:
    before = ur.PgCounters(lsn=10, wal_bytes=0, io_write_bytes=0, io_extend_bytes=0)
    after = ur.PgCounters(lsn=5, wal_bytes=0, io_write_bytes=0, io_extend_bytes=0)
    with pytest.raises(ur.ProbeError, match="backwards"):
        ur.write_rate(before, after, wall_s=1.0)


def test_take_counters_retries_until_pg_answers_and_fails_loud_at_the_bound() -> None:
    now = [0.0]
    calls = {"n": 0}

    def sql(q: str):
        if "pg_current_wal_lsn" not in q:
            return []
        calls["n"] += 1
        return None if calls["n"] < 3 else [["0/20"]]

    got = ur.take_counters(
        sql,
        bound_s=30.0,
        clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert got.lsn == 32 and calls["n"] >= 3
    with pytest.raises(ur.ProbeError, match="did not accept"):
        ur.take_counters(
            lambda q: None,
            bound_s=5.0,
            clock=lambda: now[0],
            sleep=lambda s: now.__setitem__(0, now[0] + s),
        )


# --- the three starts and the prepare for the pre-PCI one ------------------------------------------

PUBLISHED = ["7.76.0", "7.77.0", "7.98.0", "7.99.0", "8.0.0", "8.1.0rc1", "8.0.0.post1"]


def test_both_is_three_starts_newest_below_last_7x_and_the_pre_pci_release() -> None:
    assert ur.resolve_from_versions("both", PUBLISHED, candidate="8.1.0") == [
        "8.0.0.post1",
        "7.99.0",
        "7.76.0",
    ]


def test_both_dedupes_the_pre_pci_start_when_it_is_already_selected() -> None:
    assert ur.resolve_from_versions("both", ["7.76.0"], candidate="8.0.0") == ["7.76.0"]
    assert ur.resolve_from_versions(
        "both", ["7.75.0", "7.76.0", "8.0.0"], candidate="8.1.0"
    ) == ["8.0.0", "7.76.0"]


def test_both_fails_loud_when_the_pre_pci_release_is_not_published() -> None:
    with pytest.raises(ur.RehearsalError, match="7.76.0"):
        ur.resolve_from_versions("both", ["7.98.0", "8.0.0"], candidate="8.1.0")


def test_the_pre_pci_prepare_asks_for_n_large_collections_and_no_pci_wait() -> None:
    cmd = ur.prepare_template_for("7.76.0", ur.REAL_PREPARE_CMD, standin=False)
    assert "--no-pci-wait" in cmd and "--collections {collections}" in cmd
    assert "--prepare-only" in cmd
    assert (
        ur.prepare_template_for("7.99.0", ur.REAL_PREPARE_CMD, standin=False)
        == ur.REAL_PREPARE_CMD
    )
    # an explicit --prepare-cmd and the stand-in are left alone
    custom = "mycmd {work}"
    assert ur.prepare_template_for("7.76.0", custom, standin=False) == custom
    assert (
        ur.prepare_template_for("7.76.0", ur.STANDIN_PREPARE_CMD, standin=True)
        == ur.STANDIN_PREPARE_CMD
    )


def test_expected_pci_indexes_counts_the_collections_above_the_build_threshold() -> (
    None
):
    prep = {"collections": {"a": 27_001, "b": 27_001, "c": 5_001}}
    assert ur.expected_pci_indexes(prep) == 2
    assert ur.expected_pci_indexes({}) == 0


def test_the_check_is_on_by_default_for_a_pre_pci_start_only() -> None:
    assert ur.pci_check_enabled(None, "7.76.0", standin=False) is True
    assert ur.pci_check_enabled(None, "7.99.0", standin=False) is False
    assert ur.pci_check_enabled(None, "7.76.0", standin=True) is False
    assert ur.pci_check_enabled(False, "7.76.0", standin=False) is False
    assert ur.pci_check_enabled(True, "8.0.0", standin=False) is True


# --- the engine supply override ------------------------------------------------------------------------


def test_the_engine_jar_reaches_only_the_post_start_old_environment(
    tmp_path: Path,
) -> None:
    jar = tmp_path / "nexus-service.jar"
    jar.write_bytes(b"x")
    base = {"HOME": "/h", "PATH": "/usr/bin"}
    assert ur.engine_env(base, None, java_dir="") == base
    env = ur.engine_env(base, str(jar), java_dir="/opt/jdk/bin")
    assert env["NEXUS_SERVICE_JAR"] == str(jar)
    assert env["PATH"].split(":")[0] == "/opt/jdk/bin"
    assert "NEXUS_SERVICE_JAR" not in base


def test_a_missing_engine_jar_is_a_harness_error(tmp_path: Path) -> None:
    with pytest.raises(ur.RehearsalError, match="engine jar"):
        ur.resolve_engine_jar(str(tmp_path / "nope.jar"))
    assert ur.resolve_engine_jar("") is None
    assert ur.resolve_engine_jar(None) is None


# --- the verdict and the table ---------------------------------------------------------------------------


def _ok_steps():
    return [
        ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0)
        for n in ur.JUDGED_STEPS
    ]


def test_the_verdict_carries_the_write_rate_and_the_pci_evidence_and_the_table_shows_them() -> (
    None
):
    wr = {
        "wal_bytes": 200 * ur.MB,
        "wal_mb": 200.0,
        "seconds": 10.0,
        "mb_per_s": 20.0,
        "pg_stat_wal_bytes": 190 * ur.MB,
        "io_write_bytes": 50 * ur.MB,
        "io_extend_bytes": ur.MB,
        "wal_per_store_byte": 0.1,
        "seconds_per_gb_of_wal": 51.2,
    }
    pci = {
        "in_flight_at_restart": [
            {
                "index": A,
                "pid": 11,
                "phase": "p",
                "blocks_done": 5,
                "blocks_total": 10,
                "tuples_done": 50,
                "tuples_total": 100,
                "t_s": 10.0,
            }
        ],
        "builders": {A: [11]},
        "valid_at_end": [A],
        "expected_pci_indexes": 1,
        "log": {"failure_events": [], "kept": ["k"], "adopted": ["a"]},
    }
    steps = _ok_steps() + [
        ur.StepResult(
            name="pci-restart-check", status="ok", exit_code=None, wall_s=61.0
        )
    ]
    v = ur.build_verdict(
        from_version="7.76.0",
        target="wheel",
        steps=steps,
        findings=[],
        prepare={"verdict": "prepared", "collections": {"a": 60_000}},
        floor=1,
        write_rate=wr,
        pci_check=pci,
        engine_jar="/x/y.jar",
    )
    assert v["passed"] and v["write_rate"]["mb_per_s"] == 20.0
    assert v["pci_check"]["builders"] == {A: [11]} and v["engine_jar"] == "/x/y.jar"
    json.dumps(v)
    table = ur.format_table(v)
    assert "20.0 MB/s" in table and "200.0 MB" in table
    assert "pci-restart-check" in table and A in table and "11" in table
    assert "/x/y.jar" in table


def test_a_failed_pci_check_fails_the_verdict_and_the_lines_name_it() -> None:
    steps = _ok_steps() + [
        ur.StepResult(name="pci-restart-check", status="failed", reason="rebuilt")
    ]
    v = ur.build_verdict(
        from_version="7.76.0",
        target="wheel",
        steps=steps,
        findings=["x"],
        prepare={"verdict": "prepared", "collections": {"a": 60_000}},
        floor=1,
    )
    assert not v["passed"]
    assert "pci-restart-check" in ur.verdict_line(v)
    assert "7.76.0 (at pci-restart-check)" in ur.combined_line([v])


def test_nonvacuity_requires_the_pci_step_when_the_check_is_on() -> None:
    prep = {"verdict": "prepared", "collections": {"a": 60_000}}
    req = ur.JUDGED_STEPS + (ur.PCI_STEP,)
    f = ur.nonvacuity_findings(
        prep,
        _ok_steps(),
        floor=1,
        doctor_text="✓",
        search_text="s",
        sentinel_expect="s",
        required_steps=req,
    )
    assert any(ur.PCI_STEP in x and "did not run" in x for x in f)
    assert (
        ur.nonvacuity_findings(
            prep,
            _ok_steps(),
            floor=1,
            doctor_text="✓",
            search_text="s",
            sentinel_expect="s",
        )
        == []
    )


def test_format_write_rate_says_what_the_mb_is() -> None:
    wr = {
        "wal_mb": 12.5,
        "seconds": 2.5,
        "mb_per_s": 5.0,
        "io_write_bytes": None,
        "io_extend_bytes": None,
        "wal_per_store_byte": None,
        "pg_stat_wal_bytes": None,
        "wal_bytes": 12 * ur.MB,
        "seconds_per_gb_of_wal": 204.8,
    }
    line = ur.format_write_rate(wr)
    assert "5.0 MB/s" in line and "12.5 MB" in line and "MB = 2**20" in line
    assert ur.format_write_rate(None) == ""


def test_a_check_that_did_not_run_still_shows_the_builds_the_watcher_saw() -> None:
    pci = {
        "not_run": True,
        "builders": {A: [11]},
        "timeline": [
            {
                "index": A,
                "pid": 11,
                "first_s": 1.0,
                "last_s": 9.0,
                "phase": "p",
                "blocks": "5/10",
                "tuples": "50/100",
            }
        ],
    }
    lines = ur.format_pci(pci)
    assert len(lines) == 1 and A in lines[0] and "check not run" in lines[0]
    assert ur.format_pci({"not_run": True, "timeline": []}) == [
        "pci watch (check not run): no pci_ build seen"
    ]
