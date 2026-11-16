# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade rehearsal's seed cache (release step 6e preparation), offline.

A fake run dir stands in for what the seeder leaves: ``config/`` with ``pg_credentials``, a PG data dir
with a big relation file, a bundle, ``prepare.json`` and a synthetic repo, plus the install-prev client
under ``home/``. Nothing here starts a real PG: the row count is injected, and the PG start/stop path is
exercised against fake ``pg_ctl`` and ``pg_controldata`` scripts.

What is pinned: the key and each input that changes it; a save then a restore into a different run dir
with every path that named the old one rewritten; a corrupt, partial or mismatched entry, a row count
under the floor or different from the manifest, and a busy PG port each fall back (a reason, the restored
paths scrubbed); nothing is saved from an unclean or too-small store; one entry per start is kept;
and a save is atomic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "upgrade_rehearsal_seed_cache.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "_upgrade_rehearsal_seed_cache_t", MODULE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_seed_cache_t"] = module
    spec.loader.exec_module(module)
    return module


sc = _load()

ROWS = 86_000
FLOOR = 50_000


def make_run(base: Path, name: str, *, rows: int = ROWS) -> Path:
    """What the seeder leaves under ``<base>/<name>``, with every absolute path naming that dir."""
    run = base / name
    config = run / "config"
    pgdata = config / "postgres"
    (pgdata / "base" / "1").mkdir(parents=True)
    (pgdata / "pg_wal").mkdir()
    (config / "pg-bundle" / "bundle" / "bin").mkdir(parents=True)
    (config / "logs").mkdir()
    (run / "home" / ".local" / "bin").mkdir(parents=True)
    (run / "home" / ".local" / "share" / "uv" / "tools").mkdir(parents=True)
    (run / "home" / ".local" / "share" / "other").mkdir(parents=True)
    (run / "home" / ".cache" / "uv").mkdir(parents=True)
    (run / "logs").mkdir()
    (run / "cwd").mkdir()
    (run / "synthetic-repo" / "src").mkdir(parents=True)
    (run / "home" / ".local" / "bin" / "nx").write_text("#!/bin/sh\necho old client\n")
    (run / "home" / ".local" / "share" / "uv" / "tools" / "marker").write_text(
        "install-prev"
    )
    (run / "home" / ".local" / "share" / "other" / "state").write_text("kept")
    (run / "home" / ".cache" / "uv" / "wheel").write_text("uv cache")
    (run / "logs" / "install-prev.log").write_text("log")
    (run / "synthetic-repo" / "src" / "module_0.py").write_text(
        "def handler_0(x):\n    return x\n"
    )
    (config / "pg_credentials").write_text(
        f"# nexus-managed\nPG_DATA={pgdata}\nPG_PORT=54999\nNX_DB_URL=jdbc:postgresql://127.0.0.1:54999/nexus\n"
        "PG_SUPERUSER_PASS=hunter2\n"
    )
    (config / "config.yml").write_text(f"repo: {run}/synthetic-repo\n")
    (config / "logs" / "storage_service_native.log").write_text(f"data at {pgdata}\n")
    (pgdata / "PG_VERSION").write_text("17\n")
    (pgdata / "postgresql.conf").write_text(
        "port = 54999\nlisten_addresses = '127.0.0.1'\n"
    )
    (pgdata / "postmaster.opts").write_text(f"/bundle/bin/postgres -D {pgdata}\n")
    (pgdata / "base" / "1" / "16384").write_bytes(
        os.urandom(3 << 20)
    )  # a relation file: not scanned
    (pgdata / "pg_wal" / "000000010000000000000001").write_bytes(b"\0" * (2 << 20))
    os.symlink(str(run / "synthetic-repo"), config / "repo-link")
    (run / "prepare.json").write_text(
        json.dumps(
            {
                "verdict": "prepared",
                "collections": {"bootw-a": rows},
                "data_dir_bytes": 1234,
            }
        )
    )
    (config / "rows").write_text(str(rows))  # what the injected row counter reads
    return run


def rows_of(config: Path) -> int:
    return int((config / "rows").read_text())


def clean(_config: Path) -> str | None:
    return None


def key(**over) -> object:
    base = dict(
        from_version="8.0.0",
        engine_pin="0.1.186",
        python="3.12",
        prepare_template="{uv} run python scripts/upgrade_boot_writes.py --prepare-only --work {work}",
        collections=5,
        seeder=(("upgrade_boot_seed.py", "aa"), ("upgrade_boot_writes.py", "bb")),
        platform="linux-x86_64",
    )
    base.update(over)
    return sc.KeyInputs(**base)


def cache(tmp_path: Path, **kw) -> object:
    kw.setdefault("rows_fn", rows_of)
    kw.setdefault("clean_check", clean)
    kw.setdefault("is_port_free", lambda _p: True)
    return sc.SeedCache(tmp_path / "cache", **kw)


def seeded_cache(tmp_path: Path, **kw):
    """A cache holding a saved entry from ``tmp_path/seed-a``; returns (cache, key, source run dir)."""
    c = cache(tmp_path, **kw)
    k = key()
    src = make_run(tmp_path, "seed-a")
    saved = c.save(k, src, floor=FLOOR)
    assert saved.saved, saved.reason
    return c, k, src


def new_run(tmp_path: Path) -> Path:
    """The run dir ``rehearse`` makes: empty home/config/logs/cwd, install-prev already there."""
    run = tmp_path / "run-b" / "from-8.0.0"
    for d in ("home/.local/bin", "config", "logs", "cwd"):
        (run / d).mkdir(parents=True)
    (run / "home" / ".local" / "bin" / "nx").write_text(
        "#!/bin/sh\necho fresh client\n"
    )
    return run


# --- the key --------------------------------------------------------------------------------------


def test_the_key_is_stable_and_names_its_start() -> None:
    assert key().digest() == key().digest()
    assert (
        key().name == f"8.0.0-{key().digest()[:16]}"
        and len(key().name) == len("8.0.0-") + 16
    )


@pytest.mark.parametrize(
    "over",
    [
        {"from_version": "7.99.0"},
        {"engine_pin": "0.1.187"},
        {"python": "3.13"},
        {
            "prepare_template": "{uv} run python scripts/upgrade_boot_writes.py --prepare-only --no-pci-wait"
        },
        {"collections": 3},
        {"seeder": (("upgrade_boot_seed.py", "aa"), ("upgrade_boot_writes.py", "cc"))},
        {"seeder": (("upgrade_boot_seed.py", "aa"),)},
        {"platform": "darwin-arm64"},
    ],
)
def test_each_key_input_changes_the_key(over: dict) -> None:
    assert key(**over).name != key().name


def test_hashing_the_seeder_files_follows_their_content(tmp_path: Path) -> None:
    a, b = tmp_path / "upgrade_boot_writes.py", tmp_path / "upgrade_boot_seed.py"
    a.write_text("one")
    b.write_text("two")
    first = sc.hash_inputs([a, b])
    assert first == sc.hash_inputs([b, a]), (
        "the order of the list is not part of the key"
    )
    b.write_text("two!")
    assert sc.hash_inputs([a, b]) != first
    with pytest.raises(OSError):
        sc.hash_inputs([tmp_path / "missing.py"])


def test_the_cache_root_is_under_the_hosts_lab_remote_cache_unless_overridden(
    tmp_path: Path,
) -> None:
    home = tmp_path / "h"
    assert (
        sc.default_cache_root({}, home)
        == home / ".cache" / "lab-remote" / "upgrade-rehearsal-seeds"
    )
    assert sc.default_cache_root({"XDG_CACHE_HOME": "/x"}, home) == Path(
        "/x/lab-remote/upgrade-rehearsal-seeds"
    )
    assert sc.default_cache_root(
        {sc.CACHE_ENV: "/y", "XDG_CACHE_HOME": "/x"}, home
    ) == Path("/y")


# --- what the prepared state is ---------------------------------------------------------------------


def test_the_prepared_state_leaves_out_the_client_the_uv_cache_the_logs_and_the_cwd(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path, "r")
    assert sc.payload_relpaths(run) == [
        "config",
        "home/.local/share/other",
        "prepare.json",
        "synthetic-repo",
    ]


# --- save, then restore into another run dir ---------------------------------------------------------


def test_a_saved_state_restores_into_a_different_run_dir_with_every_path_rewritten(
    tmp_path: Path,
) -> None:
    c, k, src = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    assert res.ok, res.reason
    assert res.rows == ROWS and res.prepare["verdict"] == "prepared"
    config, pgdata = dst / "config", dst / "config" / "postgres"
    creds = sc.parse_credentials((config / "pg_credentials").read_text())
    assert creds["PG_DATA"] == str(pgdata) and creds["PG_PORT"] == "54999"
    assert (config / "config.yml").read_text() == f"repo: {dst}/synthetic-repo\n"
    assert (
        pgdata / "postmaster.opts"
    ).read_text() == f"/bundle/bin/postgres -D {pgdata}\n"
    assert (
        config / "logs" / "storage_service_native.log"
    ).read_text() == f"data at {pgdata}\n"
    assert os.readlink(config / "repo-link") == str(dst / "synthetic-repo")
    # nothing small still names the source run dir
    for p in dst.rglob("*"):
        if (
            p.is_file()
            and not p.is_symlink()
            and p.stat().st_size <= sc.SMALL_FILE_BYTES
        ):
            assert str(src).encode() not in p.read_bytes(), p
    assert res.relocated.files >= 4 and res.relocated.links == 1
    # the bulk is byte for byte what was saved, and the data dir keeps its mode
    assert (pgdata / "base" / "1" / "16384").read_bytes() == (
        src / "config" / "postgres" / "base" / "1" / "16384"
    ).read_bytes()
    assert (pgdata / "pg_wal" / "000000010000000000000001").stat().st_size == 2 << 20
    assert stat.S_IMODE((pgdata).stat().st_mode) == stat.S_IMODE(
        (src / "config" / "postgres").stat().st_mode
    )
    assert (
        (dst / "synthetic-repo" / "src" / "module_0.py")
        .read_text()
        .startswith("def handler_0")
    )
    assert (dst / "prepare.json").is_file()
    # the install-prev client the run made is untouched, and the seeder's other home state came back
    assert "fresh client" in (dst / "home" / ".local" / "bin" / "nx").read_text()
    assert (dst / "home" / ".local" / "share" / "other" / "state").read_text() == "kept"
    assert not (dst / "home" / ".cache").exists()


def test_the_source_run_dir_is_unchanged_by_a_save(tmp_path: Path) -> None:
    run = make_run(tmp_path, "seed-a")
    before = sc.inventory(run, ["config", "prepare.json", "synthetic-repo"])
    assert cache(tmp_path).save(key(), run, floor=FLOOR).saved
    assert sc.inventory(run, ["config", "prepare.json", "synthetic-repo"]) == before
    assert "hunter2" in (run / "config" / "pg_credentials").read_text()


def test_a_restore_with_no_entry_is_a_plain_miss_and_creates_nothing(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path)
    res = c.restore(key(), new_run(tmp_path), floor=FLOOR)
    assert (res.ok, res.reason) == (False, "no entry")
    assert not (tmp_path / "cache").exists()


def test_an_entry_is_one_dir_named_for_its_key_with_a_manifest(tmp_path: Path) -> None:
    c, k, src = seeded_cache(tmp_path)
    entry = tmp_path / "cache" / k.name
    man = json.loads((entry / "manifest.json").read_text())
    assert (
        man["key_inputs"] == k.as_dict()
        and man["rows"] == ROWS
        and man["pg_port"] == 54999
    )
    assert man["collections"] == {"bootw-a": ROWS} and man["source_run_dir"] == str(src)
    assert man["paths"] == [
        "config",
        "home/.local/share/other",
        "prepare.json",
        "synthetic-repo",
    ]
    assert sorted(
        p.name for p in (tmp_path / "cache").iterdir() if not p.name.startswith(".")
    ) == [k.name]
    assert (
        sorted(
            p.name for p in (tmp_path / "cache").iterdir() if p.name.startswith(".tmp")
        )
        == []
    ), "no temp dir left"


# --- relocation ---------------------------------------------------------------------------------------


def test_relocation_rewrites_small_text_and_symlinks_and_leaves_the_relation_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "t"
    (root / "pg" / "base").mkdir(parents=True)
    (root / "a.conf").write_text("dir=/old/run/x\n")
    (root / "pg" / "base" / "rel").write_text("/old/run/x inside a relation file\n")
    os.symlink("/old/run/x/data", root / "link")
    os.symlink("/elsewhere", root / "other-link")
    out = sc.relocate(
        [root], [("/old/run/x", "/new/run/yy")], skip_dirs=[root / "pg" / "base"]
    )
    assert (root / "a.conf").read_text() == "dir=/new/run/yy\n"
    assert (
        os.readlink(root / "link") == "/new/run/yy/data"
        and os.readlink(root / "other-link") == "/elsewhere"
    )
    assert (root / "pg" / "base" / "rel").read_text().startswith("/old/run/x"), (
        "relation pages are never rewritten"
    )
    assert (out.files, out.links) == (1, 1)


def test_relocation_keeps_file_modes(tmp_path: Path) -> None:
    f = tmp_path / "pg_credentials"
    f.write_text("PG_DATA=/old\n")
    f.chmod(0o600)
    sc.relocate([tmp_path], [("/old", "/newer")])
    assert (
        stat.S_IMODE(f.stat().st_mode) == 0o600 and f.read_text() == "PG_DATA=/newer\n"
    )


def test_a_binary_file_naming_the_old_dir_is_rewritten_only_at_the_same_length(
    tmp_path: Path,
) -> None:
    f = tmp_path / "blob"
    f.write_bytes(b"\xff\xfe/old/run\x00\x01")
    sc.relocate([tmp_path], [("/old/run", "/new/run")])
    assert f.read_bytes() == b"\xff\xfe/new/run\x00\x01"
    f.write_bytes(b"\xff\xfe/old/run\x00\x01")
    with pytest.raises(sc.RelocationError, match="binary"):
        sc.relocate([tmp_path], [("/old/run", "/new/run/longer")])


# --- a corrupt, partial or mismatched entry falls back ----------------------------------------------------


def _assert_fell_back(res, dst: Path, reason_has: str) -> None:
    assert res.ok is False and reason_has in res.reason, res.reason
    assert not (dst / "prepare.json").exists() and not (dst / "synthetic-repo").exists()
    assert list((dst / "config").iterdir()) == [], (
        "the run's own config dir is left empty for the seeder"
    )
    assert "fresh client" in (dst / "home" / ".local" / "bin" / "nx").read_text()


def _entry(tmp_path: Path, k) -> Path:
    return tmp_path / "cache" / k.name


def test_a_truncated_relation_file_is_a_corrupt_entry(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    rel = (
        _entry(tmp_path, k) / "payload" / "config" / "postgres" / "base" / "1" / "16384"
    )
    rel.write_bytes(rel.read_bytes()[: 1 << 20])
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "corrupt or partial")


def test_a_missing_file_is_a_partial_entry(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    (
        _entry(tmp_path, k)
        / "payload"
        / "config"
        / "postgres"
        / "pg_wal"
        / "000000010000000000000001"
    ).unlink()
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "corrupt or partial")


def test_a_changed_credentials_file_is_a_corrupt_entry(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    creds = _entry(tmp_path, k) / "payload" / "config" / "pg_credentials"
    creds.write_text(creds.read_text().replace("54999", "54998"))
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "corrupt or partial")


def test_an_entry_with_no_manifest_or_an_unreadable_one_is_not_used(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    man = _entry(tmp_path, k) / "manifest.json"
    man.write_text("{not json")
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "manifest unreadable")
    man.unlink()
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "manifest unreadable")


def test_a_payload_missing_its_prepare_json_is_not_used(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    (_entry(tmp_path, k) / "payload" / "prepare.json").unlink()
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "unreadable or incomplete")


def test_an_entry_whose_key_inputs_differ_from_this_runs_is_a_mismatch(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    other = key(engine_pin="0.1.187")
    # the same dir, asked for under another key: what a renamed or copied entry would look like
    os.rename(_entry(tmp_path, k), _entry(tmp_path, other))
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(other, dst, floor=FLOOR), dst, "key inputs differ")


def test_a_manifest_row_count_that_differs_from_the_stores_falls_back(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    man_path = _entry(tmp_path, k) / "manifest.json"
    man = json.loads(man_path.read_text())
    man["rows"] = ROWS + 1
    man_path.write_text(json.dumps(man))
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    _assert_fell_back(res, dst, f"holds {ROWS} rows, the manifest says {ROWS + 1}")


def test_a_restored_store_under_the_floor_falls_back_even_when_it_matches_the_manifest(
    tmp_path: Path,
) -> None:
    small = 1_000
    c = cache(tmp_path)
    k = key()
    assert c.save(k, make_run(tmp_path, "seed-a", rows=small), floor=500).saved
    dst = new_run(tmp_path)
    _assert_fell_back(
        c.restore(k, dst, floor=FLOOR), dst, f"{small} rows, below the floor of {FLOOR}"
    )


def test_a_busy_pg_port_falls_back(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path, is_port_free=lambda p: p != 54999)
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "port 54999 is in use")


def test_a_restored_cluster_that_is_not_cleanly_shut_down_falls_back(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    c.clean_check = lambda _cfg: "cluster state is 'in production'"
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "not cleanly shut down")


def test_a_row_counter_that_fails_falls_back_rather_than_raising(
    tmp_path: Path,
) -> None:
    def broken(_config: Path) -> int:
        raise sc.SeedCacheError("pg_ctl start failed (exit 1): bad")

    c, k, _ = seeded_cache(tmp_path)
    c.rows_fn = broken
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "pg_ctl start failed")


def test_a_relocation_that_cannot_be_made_falls_back(tmp_path: Path) -> None:
    c = cache(tmp_path)
    k = key()
    src = make_run(tmp_path, "seed-a")
    (src / "config" / "blob").write_bytes(b"\xff\xfe" + str(src).encode() + b"\x00")
    assert c.save(k, src, floor=FLOOR).saved
    dst = tmp_path / "run-b-with-a-much-longer-name" / "from-8.0.0"
    for d in ("home/.local/bin", "config", "logs", "cwd"):
        (dst / d).mkdir(parents=True)
    (dst / "home" / ".local" / "bin" / "nx").write_text("fresh client")
    res = c.restore(k, dst, floor=FLOOR)
    assert not res.ok and "binary" in res.reason
    assert (
        not (dst / "prepare.json").exists() and list((dst / "config").iterdir()) == []
    )


def test_a_failed_restore_leaves_files_the_run_already_had(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    c.rows_fn = lambda _c: 1
    dst = new_run(tmp_path)
    (dst / "home" / ".local" / "share" / "other").mkdir(parents=True)
    (dst / "home" / ".local" / "share" / "other" / "mine").write_text("keep me")
    assert not c.restore(k, dst, floor=FLOOR).ok
    assert (
        dst / "home" / ".local" / "share" / "other" / "mine"
    ).read_text() == "keep me"
    assert not (dst / "home" / ".local" / "share" / "other" / "state").exists()


# --- a save never keeps a state it should not ----------------------------------------------------------------


def test_a_store_that_was_not_cleanly_stopped_is_not_saved(tmp_path: Path) -> None:
    c = cache(tmp_path, clean_check=lambda _c: "postmaster.pid exists")
    res = c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "not cleanly shut down" in res.reason
    assert not (tmp_path / "cache").exists() or not any(
        (tmp_path / "cache").glob("8.0.0-*")
    )


def test_a_store_under_the_floor_is_not_saved(tmp_path: Path) -> None:
    res = cache(tmp_path).save(
        key(), make_run(tmp_path, "seed-a", rows=10), floor=FLOOR
    )
    assert not res.saved and f"10 rows, below the floor of {FLOOR}" in res.reason


def test_a_prepare_that_did_not_prepare_is_not_saved(tmp_path: Path) -> None:
    run = make_run(tmp_path, "seed-a")
    (run / "prepare.json").write_text(
        json.dumps({"verdict": "error", "collections": {}})
    )
    res = cache(tmp_path).save(key(), run, floor=FLOOR)
    assert not res.saved and "prepare verdict" in res.reason


def test_a_store_outside_the_run_dir_is_not_saved(tmp_path: Path) -> None:
    run = make_run(tmp_path, "seed-a")
    elsewhere = tmp_path / "elsewhere-pg"
    elsewhere.mkdir()
    creds = run / "config" / "pg_credentials"
    creds.write_text(
        creds.read_text().replace(str(run / "config" / "postgres"), str(elsewhere))
    )
    res = cache(tmp_path).save(key(), run, floor=FLOOR)
    assert not res.saved and "outside" in res.reason


def test_a_save_that_fails_midway_leaves_no_entry_and_no_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sc._copy_tree
    calls = {"n": 0}

    def flaky(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(sc, "_copy_tree", flaky)
    res = cache(tmp_path).save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "No space left" in res.reason
    assert [
        p.name for p in (tmp_path / "cache").iterdir() if p.name != sc.LOCK_NAME
    ] == []


def test_a_save_over_a_rejected_entry_replaces_it_and_otherwise_keeps_the_first(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    first = json.loads((_entry(tmp_path, k) / "manifest.json").read_text())["created"]
    again = make_run(tmp_path, "seed-b")
    kept = c.save(k, again, floor=FLOOR)
    assert not kept.saved and "kept its entry" in kept.reason
    (_entry(tmp_path, k) / "payload" / "prepare.json").unlink()
    assert c.save(k, again, floor=FLOOR, replace=True).saved
    dst = new_run(tmp_path)
    assert c.restore(k, dst, floor=FLOOR).ok
    assert json.loads((_entry(tmp_path, k) / "manifest.json").read_text())[
        "source_run_dir"
    ] == str(again), first


def test_a_save_waits_for_a_restore_in_progress_and_gives_up_when_it_does_not_end(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path, lock_timeout_s=0.5)
    (tmp_path / "cache").mkdir()
    with sc._locked(tmp_path / "cache", exclusive=False, timeout_s=1) as got:
        assert got
        res = c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "lock was not free" in res.reason
    assert [
        p.name for p in (tmp_path / "cache").iterdir() if p.name != sc.LOCK_NAME
    ] == [], "no entry, no temp dir"


# --- bounded size ----------------------------------------------------------------------------------------------


def test_saving_a_new_key_evicts_the_old_one_of_the_same_start_and_no_other(
    tmp_path: Path,
) -> None:
    logged: list[str] = []
    c = cache(tmp_path, log=logged.append)
    k_old, k_new = key(), key(seeder=(("upgrade_boot_writes.py", "new"),))
    k_other = key(from_version="7.99.0")
    for i, k in enumerate((k_old, k_other, k_new)):
        assert c.save(k, make_run(tmp_path, f"seed-{i}"), floor=FLOOR).saved
    names = sorted(
        p.name for p in (tmp_path / "cache").iterdir() if not p.name.startswith(".")
    )
    assert names == sorted([k_new.name, k_other.name]), names
    assert any(k_old.name in m and "evicted" in m for m in logged)
    assert not [
        p for p in (tmp_path / "cache").iterdir() if p.name.startswith(".evict")
    ]


def test_a_start_whose_version_prefixes_another_is_not_evicted_by_it(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path)
    assert c.save(
        key(from_version="8.0.0.post1"), make_run(tmp_path, "a"), floor=FLOOR
    ).saved
    assert c.save(key(from_version="8.0.0"), make_run(tmp_path, "b"), floor=FLOOR).saved
    assert (
        len([p for p in (tmp_path / "cache").iterdir() if not p.name.startswith(".")])
        == 2
    )


def test_a_stale_temp_dir_from_a_killed_save_is_removed_on_the_next_save(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path)
    stale, fresh = tmp_path / "cache" / ".tmp-old-x", tmp_path / "cache" / ".tmp-new-y"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = stale.stat().st_mtime - 2 * sc.STALE_TMP_S
    os.utime(stale, (old, old))
    assert c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR).saved
    assert not stale.exists() and fresh.exists(), (
        "another run's save in flight is left alone"
    )


# --- the PG cluster, stopped: against a fake bundle -----------------------------------------------------------------

PG_CTL = """#!/bin/sh
echo "pg_ctl $*" >> "@LOG@"
case "$*" in
  *" start") touch "@PGDATA@/postmaster.pid"; [ -e "@STATE@/start_fails" ] && { rm "@PGDATA@/postmaster.pid"; echo "no" >&2; exit 1; }; exit 0 ;;
  *"-m fast"*) [ -e "@STATE@/stop_fails" ] && exit 1; rm -f "@PGDATA@/postmaster.pid"; echo "shut down" > "@STATE@/cluster"; exit 0 ;;
  *"-m immediate"*) rm -f "@PGDATA@/postmaster.pid"; exit 0 ;;
esac
"""

PG_CONTROLDATA = """#!/bin/sh
echo "pg_controldata $*" >> "@LOG@"
echo "Database cluster state:               $(cat "@STATE@/cluster")"
"""


@pytest.fixture
def fake_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = make_run(tmp_path, "pgrun")
    config, state = run / "config", tmp_path / "state"
    state.mkdir()
    (state / "cluster").write_text("shut down")
    # the scripts run under the helper's own scrubbed environment, so the paths are written into them
    subs = {
        "@LOG@": str(state / "calls.log"),
        "@STATE@": str(state),
        "@PGDATA@": str(config / "postgres"),
    }
    for name, body in (("pg_ctl", PG_CTL), ("pg_controldata", PG_CONTROLDATA)):
        for k, v in subs.items():
            body = body.replace(k, v)
        f = config / "pg-bundle" / "bundle" / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (state / "calls.log").write_text("")
    return config, state


def _calls(state: Path) -> list[str]:
    return (state / "calls.log").read_text().splitlines()


def test_a_cleanly_stopped_cluster_reads_as_clean(fake_bundle) -> None:
    config, state = fake_bundle
    assert sc.stopped_clean(config) is None
    (state / "cluster").write_text("in production")
    assert "'in production'" in sc.stopped_clean(config)
    (state / "cluster").write_text("shut down")
    (config / "postgres" / "postmaster.pid").write_text("1")
    assert "postmaster.pid" in sc.stopped_clean(config)


def test_counting_rows_starts_pg_alone_counts_and_stops_it_cleanly(fake_bundle) -> None:
    config, state = fake_bundle
    seen: list[bool] = []
    rows = sc.rows_with_pg_up(
        config,
        lambda: (
            seen.append((config / "postgres" / "postmaster.pid").exists()) or 86_000
        ),
    )
    assert rows == 86_000 and seen == [True], "the count ran while PG was up"
    calls = _calls(state)
    assert [c.split()[0] for c in calls] == ["pg_ctl", "pg_ctl", "pg_controldata"]
    assert calls[0].endswith(" start") and "-m fast" in calls[1]
    assert not (config / "postgres" / "postmaster.pid").exists()


def test_a_count_that_fails_still_stops_pg(fake_bundle) -> None:
    config, _ = fake_bundle

    def boom() -> int:
        raise RuntimeError("psql failed")

    with pytest.raises(RuntimeError):
        sc.rows_with_pg_up(config, boom)
    assert not (config / "postgres" / "postmaster.pid").exists()


def test_a_pg_that_does_not_start_or_does_not_stop_is_an_error(fake_bundle) -> None:
    config, state = fake_bundle
    (state / "start_fails").write_text("1")
    with pytest.raises(sc.SeedCacheError, match="pg_ctl start failed"):
        sc.rows_with_pg_up(config, lambda: 1)
    (state / "start_fails").unlink()
    (state / "stop_fails").write_text("1")
    with pytest.raises(sc.SeedCacheError, match="pg_ctl stop failed"):
        sc.rows_with_pg_up(config, lambda: 1)
    assert any("-m immediate" in c for c in _calls(state)), (
        "a stop that failed is forced"
    )


def test_a_cluster_that_reads_unclean_after_the_count_is_an_error(fake_bundle) -> None:
    config, state = fake_bundle
    stop = config / "pg-bundle" / "bundle" / "bin" / "pg_ctl"
    stop.write_text(
        stop.read_text().replace('echo "shut down" >', 'echo "in crash recovery" >')
    )
    with pytest.raises(sc.SeedCacheError, match="after the row count"):
        sc.rows_with_pg_up(config, lambda: 1)


def test_port_free_sees_a_bound_port() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert sc.port_free(port) is False
    assert sc.port_free(port) is True
