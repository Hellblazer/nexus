# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seed cache as ``rehearse()`` and ``main()`` use it (release step 6e), offline, against the fake world.

The cache itself is pinned in ``tests/test_upgrade_rehearsal_seed_cache.py``. What is pinned here is that the
driver wires it so that it changes only the preparation:

* a start seeded once is restored the next time, the seeder is not run, and every judged step, finding and
  state is what a full seed gives, with the prepare row of the step table saying which it was;
* a corrupt entry, a row count that differs from the manifest, a store under the floor and a cache that is
  off each seed in full, say why, and a prepare that then fails is still a FAILED start;
* ``--no-seed-cache`` is a non-default setting (the run is not the release gate); the cache directory comes
  from the environment, and a custom ``--prepare-cmd`` turns the cache off.
"""

from __future__ import annotations

import dataclasses
import json
import re
import signal
from pathlib import Path

import pytest

from tests.test_upgrade_rehearsal_rehearse import (
    UV,
    World,
    _script,
    ur,
)

PIN_LINES = """
d="$HOME/.local/share/uv/tools/conexus/lib/python3.12/site-packages/nexus"
mkdir -p "$d"
echo "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 156)" > "$d/engine_version.py"
"""

FROM = "8.0.0"
NEW_PORT = 55123
SAVED = r" \(saved \d{4}-\d{2}-\d{2}\)"


@pytest.fixture
def cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The fake world with a client that pins an engine and a PG that is stopped and counts 60,000 rows."""
    w = World(tmp_path, monkeypatch)
    _script(w.fakebin / "uv", UV + PIN_LINES, w.state)
    monkeypatch.setattr(ur.seed_cache, "stopped_clean", lambda _config: None)
    monkeypatch.setattr(ur.seed_cache, "free_port", lambda: NEW_PORT)
    monkeypatch.setattr(ur.seed_cache, "rows_with_pg_up", lambda _config, work: work())
    (tmp_path / "seeder.py").write_text("# the seeder\n")
    return w


def cfg(w: World, tag: str = "work", **kw):
    kw.setdefault("seed_cache_dir", w.root / "seed-cache")
    kw.setdefault("seed_inputs", (w.root / "seeder.py",))
    kw.setdefault("work", w.root / tag)
    return w.cfg(**kw)


def seeded_runs(w: World) -> int:
    return sum(c.startswith("prepare ") for c in w.calls())


def label(v: dict) -> str:
    return v["setup"]["prepare"]["label"]


def entries(w: World) -> list[Path]:
    root = w.root / "seed-cache"
    return (
        sorted(p for p in root.iterdir() if not p.name.startswith("."))
        if root.is_dir()
        else []
    )


# --- a hit is indistinguishable to every judged step ------------------------------------------------------------


def test_a_start_seeded_once_is_restored_the_next_time_and_judged_the_same(
    cached: World,
) -> None:
    first = ur.rehearse(FROM, cfg(cached, "w1"))
    assert first["state"] == "PASSED" and seeded_runs(cached) == 1
    (entry,) = entries(cached)
    assert label(first) == f"seeded (cache miss, saved {entry.name})"
    assert first["setup"]["prepare"]["saved"] is True

    second = ur.rehearse(FROM, cfg(cached, "w2"))
    assert seeded_runs(cached) == 1, "the second start did not run the seeder"
    assert re.fullmatch(
        re.escape(f"store restored from cache {entry.name}") + SAVED, label(second)
    ), label(second)
    assert (
        second["setup"]["prepare"]["source"] == "cache"
        and second["setup"]["prepare"]["rows"] == 60000
    )

    # every judged step, finding and figure is the one a full seed gave
    assert (
        second["state"] == first["state"] == "PASSED"
        and second["findings"] == first["findings"] == []
    )
    assert [(s["name"], s["status"], s["ceiling_s"]) for s in second["steps"]] == [
        (s["name"], s["status"], s["ceiling_s"]) for s in first["steps"]
    ]
    assert [s["name"] for s in second["steps"]] == list(ur.JUDGED_STEPS), (
        "the prepare row is not a step"
    )
    assert (
        second["store_rows"] == first["store_rows"] == {"before": 60000, "after": 60000}
    )
    assert (
        second["prepare"] == first["prepare"]
        and second["prepared_rows"] == first["prepared_rows"]
    )
    assert second["upgrade_ceiling_s"] == first["upgrade_ceiling_s"]

    # the setup order is what it was: install, (restore instead of prepare), start-old, then the judged steps
    calls = cached.calls()
    last_start = [i for i, c in enumerate(calls) if c == "nx daemon service start"][-1]
    assert calls[last_start + 1].startswith("uv tool install --force"), calls[
        last_start:
    ]
    # the restored store's own paths name the second run's dir, not the first's
    run1 = (cached.root / "w1" / f"from-{FROM}").resolve()
    run2 = (cached.root / "w2" / f"from-{FROM}").resolve()
    creds = (run2 / "config" / "pg_credentials").read_text()
    # match the first run's whole dir, never a bare "w1": an xdist tmp path holds "popen-gw1"
    assert f"PG_DATA={run2}/pgdata" in creds and str(run1) not in creds
    # the restored store has a fresh port, as a seed would have: the credentials say so
    assert f"PG_PORT={NEW_PORT}" in creds and f"127.0.0.1:{NEW_PORT}/" in creds
    assert second["setup"]["prepare"]["pg_port"] == NEW_PORT


def test_the_step_table_and_verdict_file_say_how_the_store_was_prepared(
    cached: World,
) -> None:
    first = ur.rehearse(FROM, cfg(cached, "w1"))
    second = ur.rehearse(FROM, cfg(cached, "w2"))
    for v in (first, second):
        table = ur.format_table(v)
        row = [ln for ln in table.splitlines() if ln.startswith("prepare (setup)")]
        assert len(row) == 1 and row[0].rstrip().endswith(label(v)), table
        # the setup row sits above the judged ones and names no judged status
        assert table.index("prepare (setup)") < table.index("package-upgrade")
    on_disk = json.loads(
        (cached.root / "w2" / f"from-{FROM}" / "verdict.json").read_text()
    )
    assert on_disk["setup"]["prepare"]["label"].startswith("store restored from cache ")
    assert "prepare" not in [s["name"] for s in on_disk["steps"]]


def test_a_verdict_without_a_setup_entry_prints_no_prepare_row() -> None:
    v = ur.build_verdict(
        from_version=FROM, target="t", steps=[], findings=[], prepare=None, floor=1
    )
    assert "setup" not in v and "prepare (setup)" not in ur.format_table(v)


def test_a_restored_store_that_then_fails_a_judged_step_is_still_a_failed_start(
    cached: World,
) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    cached.upgrade_does('echo "upgrade broke"\nexit 1\n')
    v = ur.rehearse(FROM, cfg(cached, "w2"))
    assert label(v).startswith("store restored from cache ")
    assert (
        v["state"] == "FAILED"
        and v["steps"][1]["name"] == "upgrade"
        and v["steps"][1]["status"] == "failed"
    )


# --- anything doubtful seeds in full --------------------------------------------------------------------------------


def test_a_corrupt_entry_seeds_in_full_and_replaces_it(cached: World) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    (entry,) = entries(cached)
    big = entry / "payload" / "pgdata" / "base" / "file"
    big.write_text("truncated")  # the saved file said "seed"
    v = ur.rehearse(FROM, cfg(cached, "w2"))
    assert seeded_runs(cached) == 2, "the seeder ran again"
    assert label(v).startswith(
        "seeded (cache miss, rejected: "
    ) and "corrupt or partial" in label(v)
    assert label(v).endswith(f"saved {entry.name})")
    assert v["state"] == "PASSED" and v["findings"] == []
    # the next start restores the replaced entry
    again = ur.rehearse(FROM, cfg(cached, "w3"))
    assert (
        label(again).startswith("store restored from cache ")
        and seeded_runs(cached) == 2
    )


def test_a_row_count_that_differs_from_the_manifest_seeds_in_full(
    cached: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    monkeypatch.setattr(
        ur.seed_cache,
        "rows_with_pg_up",
        lambda _config, work: dataclasses.replace(work(), rows=59_999),
    )
    v = ur.rehearse(FROM, cfg(cached, "w2"))
    assert seeded_runs(cached) == 2
    assert "holds 59999 rows, the manifest says 60000" in label(v)
    assert v["state"] == "PASSED"  # judged on the seeded store alone
    assert v["store_rows"] == {"before": 60000, "after": 60000}, (
        "the judged reads are PG's, not the cache's"
    )


def test_a_failing_seeder_after_a_rejected_entry_is_a_failed_start_not_a_pass(
    cached: World,
) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    (entry,) = entries(cached)
    (entry / "manifest.json").write_text("{broken")
    cached.flag("prepare_fail", "1")
    v = ur.rehearse(FROM, cfg(cached, "w2"))
    assert v["state"] == "FAILED"
    assert any("setup step prepare failed" in f for f in v["findings"]), v["findings"]
    assert all(s["status"] == "not-run" for s in v["steps"])
    assert label(v) == "seeded (the prepare step failed)"


def test_a_store_under_the_floor_is_seeded_not_cached_and_still_fails_on_its_floor(
    cached: World,
) -> None:
    v = ur.rehearse(FROM, cfg(cached, "w1", rows_floor=100_000))
    assert "not saved" in label(v) and "below the floor of 100000" in label(v)
    assert entries(cached) == []
    assert v["state"] == "FAILED" and any("below the floor" in f for f in v["findings"])


def test_a_cache_that_is_off_seeds_in_full_and_says_so(cached: World) -> None:
    v = ur.rehearse(
        FROM, cfg(cached, "w1", seed_cache_dir=None, seed_cache_off="--no-seed-cache")
    )
    assert label(v) == "seeded (seed cache off: --no-seed-cache)"
    assert entries(cached) == [] and v["state"] == "PASSED"
    again = ur.rehearse(
        FROM, cfg(cached, "w2", seed_cache_dir=None, seed_cache_off="--no-seed-cache")
    )
    assert seeded_runs(cached) == 2 and label(again) == label(v)


def test_a_client_whose_engine_pin_cannot_be_read_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path, monkeypatch)  # its fake uv leaves no engine_version.py
    (tmp_path / "seeder.py").write_text("x")
    v = ur.rehearse(FROM, cfg(w, "w1"))
    assert "engine pin of conexus 8.0.0 cannot be read" in label(v)
    assert entries(w) == [] and v["state"] == "PASSED"


def test_the_key_follows_the_seeders_files(cached: World) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    (cached.root / "seeder.py").write_text("# the seeder, changed\n")
    v = ur.rehearse(FROM, cfg(cached, "w2"))
    assert seeded_runs(cached) == 2 and label(v).startswith(
        "seeded (cache miss, saved "
    )
    assert len(entries(cached)) == 1, "the entry for the old seeder was evicted"


# --- main -----------------------------------------------------------------------------------------------------------

PUBLISHED = "7.76.0,7.99.0,8.0.0"


@pytest.fixture
def main_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setenv(ur.seed_cache.CACHE_ENV, str(tmp_path / "env-cache"))
    monkeypatch.setattr(ur, "fetch_published_versions", lambda: PUBLISHED.split(","))
    seen: list = []

    def spy(version: str, config: object) -> dict:
        seen.append(config)
        steps = [
            ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0)
            for n in ur.JUDGED_STEPS
        ]
        return ur.build_verdict(
            from_version=version,
            target="conexus 9.9.9",
            steps=steps,
            findings=[],
            prepare=None,
            floor=1,
        )

    monkeypatch.setattr(ur, "rehearse", spy)
    yield seen
    for sig, handler in saved.items():
        signal.signal(sig, handler)
    ur._EXIT.__init__()


def _main(tmp_path: Path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / "v.json"
    rc = ur.main(
        [
            "--work",
            str(tmp_path / "w"),
            "--target-release",
            "9.9.9",
            "--json-out",
            str(out),
            *extra,
        ]
    )
    return rc, json.loads(out.read_text())


def test_the_cache_is_on_by_default_and_is_not_a_non_default_setting(
    tmp_path: Path, main_env: list
) -> None:
    rc, verdict = _main(tmp_path)
    assert (
        rc == 0 and verdict["non_default_flags"] == [] and verdict["result"] == "PASSED"
    )
    c = main_env[0]
    assert c.seed_cache_dir == tmp_path / "env-cache" and c.seed_cache_off is None
    assert [p.name for p in c.seed_inputs] == [
        "upgrade_boot_writes.py",
        "upgrade_boot_seed.py",
    ]
    assert all(p.is_file() for p in c.seed_inputs), "the key hashes files that exist"


def test_the_default_cache_root_is_under_the_hosts_lab_remote_cache(
    tmp_path: Path, main_env: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ur.seed_cache.CACHE_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _main(tmp_path)
    assert (
        main_env[0].seed_cache_dir
        == tmp_path / "xdg" / "lab-remote" / "upgrade-rehearsal-seeds"
    )


def test_no_seed_cache_turns_the_cache_off_and_the_pass_into_not_the_release_gate(
    tmp_path: Path, main_env: list, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, verdict = _main(tmp_path, "--no-seed-cache")
    assert rc == 2 and verdict["non_default_flags"] == ["--no-seed-cache"]
    assert verdict["result"] == "NOT THE RELEASE GATE"
    assert (
        capsys.readouterr()
        .out.strip()
        .splitlines()[-1]
        .startswith("UPGRADE REHEARSAL GATE: NOT THE RELEASE GATE (--no-seed-cache)")
    )
    c = main_env[0]
    assert c.seed_cache_dir is None and c.seed_cache_off == "--no-seed-cache"


def test_the_standin_keys_on_its_own_prepare_script(
    tmp_path: Path, main_env: list
) -> None:
    _main(tmp_path, "--standin")
    assert [p.name for p in main_env[0].seed_inputs] == [
        "upgrade_rehearsal_standin_prepare.py"
    ]


def test_a_custom_prepare_command_is_not_cached(tmp_path: Path, main_env: list) -> None:
    _main(tmp_path, "--prepare-cmd", "true {work} {json}")
    c = main_env[0]
    assert c.seed_cache_dir is None and "custom --prepare-cmd" in c.seed_cache_off


# --- the production failure types, the label's age, the families -----------------------------------------------------


def _probe_that_fails_for_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real PgProbe, except that the cache's own catalog query fails the way a bad page does."""
    real = ur.PgProbe.sql

    def sql(self, query: str):
        if "information_schema" in query:
            raise ur.ProbeError(
                "psql failed: invalid page in block 7 of relation base/16384/2619"
            )
        return real(self, query)

    monkeypatch.setattr(ur.PgProbe, "sql", sql)


def test_a_probe_error_while_reading_a_restored_store_seeds_in_full_and_evicts_the_entry(
    cached: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    assert len(entries(cached)) == 1
    _probe_that_fails_for_the_cache(monkeypatch)
    v = ur.rehearse(FROM, cfg(cached, "w2"))  # no exception reaches here
    assert seeded_runs(cached) == 2, "the seeder ran"
    assert label(v).startswith("seeded (cache miss, rejected: ")
    assert "ProbeError: psql failed: invalid page" in label(v)
    assert v["state"] == "PASSED" and v["findings"] == []
    assert "not saved" in label(v), (
        "the same failure at save time is reported, not raised"
    )
    assert entries(cached) == [], (
        "the rejected entry is gone, so a rerun seeds and saves"
    )
    monkeypatch.undo()


def test_a_probe_error_at_save_time_does_not_fail_a_seeded_start(
    cached: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _probe_that_fails_for_the_cache(monkeypatch)
    v = ur.rehearse(FROM, cfg(cached, "w1"))
    assert v["state"] == "PASSED" and v["findings"] == []
    assert "not saved: " in label(v) and "ProbeError: psql failed" in label(v)
    assert entries(cached) == []


def test_the_driver_turns_a_probe_error_into_a_cache_error(
    cached: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _probe_that_fails_for_the_cache(monkeypatch)
    config = cached.root / "cfgdir"
    config.mkdir()
    with pytest.raises(ur.seed_cache.SeedCacheError, match="ProbeError"):
        ur._offline_read(config, ["/x"])
    with pytest.raises(ur.seed_cache.SeedCacheError, match="ProbeError"):
        ur._offline_rewrite(config, [("/x", "/y")])


def test_a_pg_that_is_not_accepting_connections_is_a_cache_error(
    cached: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ur.PgProbe, "sql", lambda self, query: None)
    with pytest.raises(ur.seed_cache.SeedCacheError, match="not accepting"):
        ur._offline_read(cached.root, ["/x"])


def test_the_label_of_a_hit_says_when_the_store_was_saved(cached: World) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    (entry,) = entries(cached)
    man_path = entry / "manifest.json"
    man = json.loads(man_path.read_text())
    man["saved_at"] = "2026-11-02T08:00:00Z"
    man_path.write_text(json.dumps(man))
    v = ur.rehearse(FROM, cfg(cached, "w2"))
    assert label(v) == f"store restored from cache {entry.name} (saved 2026-11-02)"
    assert any(
        ln.startswith("prepare (setup)") and ln.endswith("(saved 2026-11-02)")
        for ln in ur.format_table(v).splitlines()
    )


def test_a_standin_run_does_not_evict_or_use_the_real_entry(cached: World) -> None:
    ur.rehearse(FROM, cfg(cached, "w1"))
    (real,) = entries(cached)
    v = ur.rehearse(FROM, cfg(cached, "w2", standin=True))
    assert label(v).startswith("seeded (cache miss, saved "), label(v)
    names = [e.name for e in entries(cached)]
    assert real.name in names and len(names) == 2
    assert any("-standin-" in n for n in names) and any("-real-" in n for n in names)


def test_the_driver_passes_the_shift_to_the_store_rewrite(
    cached: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_offline_rewrite is the production rewrite_fn: the shift the cache computes must reach rewrite_store."""
    seen: list[float] = []
    monkeypatch.setattr(ur.seed_cache, "rows_with_pg_up", lambda config, work: work())
    monkeypatch.setattr(ur, "_store_sql", lambda config: (lambda q: [["0"]]))
    monkeypatch.setattr(
        ur.seed_cache, "rewrite_store",
        lambda sql, pairs, shift_s=0.0: seen.append(shift_s) or ur.seed_cache.StoreRewrite(0, {}, {}),
    )
    ur._offline_rewrite(cached.root, [("/x", "/y")], 7200.0)
    assert seen == [7200.0]
