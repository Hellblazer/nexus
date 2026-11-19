# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade rehearsal's seed cache (release step 6e preparation), offline.

A fake run dir stands in for what the seeder leaves: ``config/`` with ``pg_credentials``, a PG data dir
with a big relation file, a bundle, ``prepare.json`` and a synthetic repo, plus the install-prev client
under ``home/``. Nothing here starts a real PG: the store's reads and path rewrites are injected (a JSON
file inside the fake relation dir stands for the catalog's path columns), the PG start/stop path is
exercised against fake ``pg_ctl`` and ``pg_controldata`` scripts, and the SQL helpers against a fake psql.

What is pinned: the key and each input that changes it; a save then a restore into a different run dir with
every path that named the old one rewritten, the PG port replaced and the catalog's path columns moved to
the new run dir; a corrupt (also same-size), partial, mismatched or unreadable entry, a row count under the
floor or different from the manifest, a failing PG read (the production ``ProbeError``) each fall back with
the restored paths scrubbed and the defective entry evicted, while a transient failure (a lock, the
ceiling, a dirty destination) never evicts; nothing is saved from an unclean or too-small store, and a save
never raises; one entry per start and template family is kept, and an unused one expires.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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
OLD_PORT = 54999
NEW_PORT = 55123


class ProbeError(Exception):
    """Stands for the driver's ``ProbeError``: a plain Exception no cache handler names."""


def make_run(
    base: Path, name: str, *, rows: int = ROWS, home_config: bool = True
) -> Path:
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
    if home_config:
        (run / "home" / ".config" / "tool").mkdir(parents=True)
        (run / "home" / ".config" / "tool" / "settings").write_text("x=1\n")
    (run / "logs" / "install-prev.log").write_text("log")
    (run / "synthetic-repo" / "src" / "module_0.py").write_text(
        "def handler_0(x):\n    return x\n"
    )
    (config / "pg_credentials").write_text(
        f"# nexus-managed\nPG_DATA={pgdata}\nPG_PORT={OLD_PORT}\n"
        f"NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:{OLD_PORT}/nexus\n"
        f"NX_DB_URL=jdbc:postgresql://127.0.0.1:{OLD_PORT}/nexus\n"
        "PG_SUPERUSER_PASS=hunter2\n"
    )
    (config / "config.yml").write_text(f"repo: {run}/synthetic-repo\n")
    (config / "logs" / "storage_service_native.log").write_text(
        f"data at {pgdata}, listening on 127.0.0.1:{OLD_PORT}\n"
    )
    (pgdata / "PG_VERSION").write_text("17\n")
    (pgdata / "postgresql.conf").write_text(
        f"# managed\nport = {OLD_PORT}\nlisten_addresses = '127.0.0.1'\n"
    )
    (pgdata / "postmaster.opts").write_text(f"/bundle/bin/postgres -D {pgdata}\n")
    (pgdata / "base" / "1" / "16384").write_bytes(
        os.urandom(3 << 20)
    )  # a relation file: not scanned, now hashed
    (pgdata / "pg_wal" / "000000010000000000000001").write_bytes(b"\0" * (2 << 20))
    # the catalog's path columns, as the fake store holds them (inside the relation dir: never rewritten
    # as a file, only by the injected store functions)
    (pgdata / "base" / "1" / "pathcols.json").write_text(
        json.dumps(
            {
                "catalog_owners.repo_root": [f"{run}/synthetic-repo", "/elsewhere"],
                "catalog_documents.source_uri": [
                    f"file://{run}/synthetic-repo/src/module_0.py",
                    f"file://{run}/synthetic-repo/docs/guide_0.md",
                    "file:///elsewhere/a.md",
                ],
            }
        )
    )
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


def _cols(config: Path) -> tuple[Path, dict]:
    pg = Path(sc.read_credentials(config)["PG_DATA"]) / "base" / "1" / "pathcols.json"
    return pg, json.loads(pg.read_text())


def read_fn(config: Path, olds) -> object:
    _, data = _cols(config)
    return sc.StoreRead(
        rows_of(config),
        {k: sum(any(o in v for o in olds) for v in vs) for k, vs in data.items()},
    )


def rewrite_fn(config: Path, pairs, shift_s: float = 0.0) -> object:
    path, data = _cols(config)
    olds = [p[0] for p in pairs]
    before = {k: sum(any(o in v for o in olds) for v in vs) for k, vs in data.items()}
    for k, vs in data.items():
        for i, v in enumerate(vs):
            for old, new in sorted(pairs, key=lambda p: -len(p[0])):
                v = v.replace(old, new)
            vs[i] = v
    path.write_text(json.dumps(data))
    after = {k: sum(any(o in v for o in olds) for v in vs) for k, vs in data.items()}
    return sc.StoreRewrite(rows_of(config), before, after)


def clean(_config: Path) -> str | None:
    return None


def key(**over) -> object:
    base = dict(
        from_version="8.0.0",
        kind="real",
        engine_pin="0.1.186",
        python="3.12",
        prepare_template="{uv} run python scripts/upgrade_boot_writes.py --prepare-only --work {work}",
        collections=5,
        seeder=(("upgrade_boot_seed.py", "aa"), ("upgrade_boot_writes.py", "bb")),
        platform="linux-x86_64",
    )
    base.update(over)
    return sc.KeyInputs(**base)


class Clock:
    def __init__(self, t: float = 1_794_840_000.0) -> None:  # 2026-11-16T14:40:00Z
        self.t = t

    def __call__(self) -> float:
        return self.t


def cache(tmp_path: Path, **kw) -> object:
    kw.setdefault("read_fn", read_fn)
    kw.setdefault("rewrite_fn", rewrite_fn)
    kw.setdefault("clean_check", clean)
    kw.setdefault("pick_port", lambda: NEW_PORT)
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


def _entry(tmp_path: Path, k) -> Path:
    return tmp_path / "cache" / k.name


def _live(tmp_path: Path) -> list[str]:
    root = tmp_path / "cache"
    return (
        sorted(
            p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
        if root.is_dir()
        else []
    )


# --- the key --------------------------------------------------------------------------------------


def test_the_key_is_stable_and_names_its_start_and_family() -> None:
    assert key().digest() == key().digest()
    assert key().name == f"8.0.0-real-{key().digest()[:16]}"
    assert key(kind="standin").name.startswith("8.0.0-standin-")


@pytest.mark.parametrize(
    "over",
    [
        {"from_version": "7.99.0"},
        {"kind": "standin"},
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
        "home/.config",
        "home/.local/share/other",
        "prepare.json",
        "synthetic-repo",
    ]


# --- save, then restore into another run dir ---------------------------------------------------------


def test_a_saved_state_restores_into_a_different_run_dir_with_every_path_rewritten(
    tmp_path: Path,
) -> None:
    clock = Clock()
    c, k, src = seeded_cache(tmp_path, clock=clock)
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    assert res.ok, res.reason
    assert res.rows == ROWS and res.prepare["verdict"] == "prepared"
    assert res.saved_at == "2026-11-16T14:40:00Z"
    config, pgdata = dst / "config", dst / "config" / "postgres"
    creds = sc.parse_credentials((config / "pg_credentials").read_text())
    assert creds["PG_DATA"] == str(pgdata)
    assert (config / "config.yml").read_text() == f"repo: {dst}/synthetic-repo\n"
    assert (
        pgdata / "postmaster.opts"
    ).read_text() == f"/bundle/bin/postgres -D {pgdata}\n"
    assert os.readlink(config / "repo-link") == str(dst / "synthetic-repo")
    # nothing small still names the source run dir
    for p in dst.rglob("*"):
        if (
            p.is_file()
            and not p.is_symlink()
            and p.stat().st_size <= sc.SMALL_FILE_BYTES
            and "pathcols" not in p.name
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
    assert (dst / "prepare.json").is_file()
    # the install-prev client the run made is untouched, and the seeder's other home state came back
    assert "fresh client" in (dst / "home" / ".local" / "bin" / "nx").read_text()
    assert (dst / "home" / ".local" / "share" / "other" / "state").read_text() == "kept"
    assert (dst / "home" / ".config" / "tool" / "settings").read_text() == "x=1\n"
    assert not (dst / "home" / ".cache").exists()


def test_a_restored_store_gets_a_fresh_pg_port_everywhere_the_state_records_it(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    assert res.ok, res.reason
    assert res.port == NEW_PORT
    config, pgdata = dst / "config", dst / "config" / "postgres"
    creds = (config / "pg_credentials").read_text()
    assert sc.parse_credentials(creds)["PG_PORT"] == str(NEW_PORT)
    assert (
        creds.count(f"127.0.0.1:{NEW_PORT}/nexus") == 2 and str(OLD_PORT) not in creds
    )
    assert f"port = {NEW_PORT}" in (pgdata / "postgresql.conf").read_text()
    assert str(OLD_PORT) not in (pgdata / "postgresql.conf").read_text()
    # a log keeps the port it was written under: it is history, not state
    assert (
        f"127.0.0.1:{OLD_PORT}"
        in (config / "logs" / "storage_service_native.log").read_text()
    )
    assert stat.S_IMODE((config / "pg_credentials").stat().st_mode) == stat.S_IMODE(
        (tmp_path / "seed-a" / "config" / "pg_credentials").stat().st_mode
    )


def test_a_restored_store_has_its_catalog_paths_on_the_new_run_dir(
    tmp_path: Path,
) -> None:
    c, k, src = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    assert res.ok, res.reason
    _, data = _cols(dst / "config")
    assert data["catalog_owners.repo_root"] == [f"{dst}/synthetic-repo", "/elsewhere"]
    assert data["catalog_documents.source_uri"][0] == (
        f"file://{dst}/synthetic-repo/src/module_0.py"
    )
    assert data["catalog_documents.source_uri"][2] == "file:///elsewhere/a.md"
    assert not any(str(src) in v for vs in data.values() for v in vs)
    assert res.path_rows == {
        "catalog_owners.repo_root": 1,
        "catalog_documents.source_uri": 2,
    }


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
    c, k, src = seeded_cache(tmp_path, clock=Clock())
    entry = _entry(tmp_path, k)
    man = json.loads((entry / "manifest.json").read_text())
    assert (
        man["key_inputs"] == k.as_dict()
        and man["rows"] == ROWS
        and man["pg_port"] == OLD_PORT
    )
    assert man["collections"] == {"bootw-a": ROWS} and man["source_run_dir"] == str(src)
    assert man["saved_at"] == man["last_used_at"] == "2026-11-16T14:40:00Z"
    assert man["path_rows"] == {
        "catalog_owners.repo_root": 1,
        "catalog_documents.source_uri": 2,
    }
    assert man["paths"] == [
        "config",
        "home/.config",
        "home/.local/share/other",
        "prepare.json",
        "synthetic-repo",
    ]
    assert _live(tmp_path) == [k.name]
    assert not [
        p for p in (tmp_path / "cache").iterdir() if p.name.startswith(".tmp")
    ], "no temp dir left"


def test_a_hit_records_its_use(tmp_path: Path) -> None:
    clock = Clock()
    c, k, _ = seeded_cache(tmp_path, clock=clock)
    clock.t += 3 * 86400
    assert c.restore(k, new_run(tmp_path), floor=FLOOR).ok
    man = json.loads((_entry(tmp_path, k) / "manifest.json").read_text())
    assert man["saved_at"] == "2026-11-16T14:40:00Z"
    assert man["last_used_at"] == "2026-11-19T14:40:00Z"


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


@pytest.mark.parametrize("new", ["/new/run", "/new/run/longer"])
def test_a_binary_file_naming_the_old_dir_is_refused_whatever_the_lengths(
    tmp_path: Path, new: str
) -> None:
    f = tmp_path / "blob"
    f.write_bytes(b"\xff\xfe/old/run\x00\x01")
    with pytest.raises(sc.RelocationError, match="binary"):
        sc.relocate([tmp_path], [("/old/run", new)])
    assert f.read_bytes() == b"\xff\xfe/old/run\x00\x01", "nothing was half rewritten"


# --- the PG port ---------------------------------------------------------------------------------------


def _port_tree(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "pt" / "config"
    pgdata = config / "postgres"
    (pgdata / "base").mkdir(parents=True)
    (config / "logs").mkdir()
    (config / "pg_credentials").write_text(
        f"PG_DATA={pgdata}\nPG_PORT=5000\nNX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:5000/nexus\n"
        "NX_DB_URL=jdbc:postgresql://127.0.0.1:5000/nexus\n"
    )
    (pgdata / "postgresql.conf").write_text("port = 5000\n")
    (pgdata / "postgresql.auto.conf").write_text("# nothing\n")
    (pgdata / "base" / "rel").write_text("PG_PORT=5000 in a relation file\n")
    (config / "logs" / "e.log").write_text("127.0.0.1:5000\n")
    return config, pgdata


def test_the_port_is_rewritten_in_the_credentials_and_the_conf_and_nowhere_else(
    tmp_path: Path,
) -> None:
    config, pgdata = _port_tree(tmp_path)
    sc.rewrite_port(config, pgdata, 5000, 6000)
    creds = (config / "pg_credentials").read_text()
    assert "PG_PORT=6000" in creds and creds.count("127.0.0.1:6000/nexus") == 2
    assert ":5000/" not in creds and "PG_PORT=5000" not in creds
    assert (pgdata / "postgresql.conf").read_text() == "port = 6000\n"
    assert (config / "logs" / "e.log").read_text() == "127.0.0.1:5000\n"
    assert "5000" in (pgdata / "base" / "rel").read_text()


def test_a_place_the_rewrite_missed_refuses_the_restore(tmp_path: Path) -> None:
    config, pgdata = _port_tree(tmp_path)
    (config / "other.yml").write_text("db: localhost:5000/nexus\n")
    with pytest.raises(
        sc.SeedCacheError, match="other.yml still names the old PG port"
    ):
        sc.rewrite_port(config, pgdata, 5000, 6000)


def test_the_same_port_is_left_alone(tmp_path: Path) -> None:
    config, pgdata = _port_tree(tmp_path)
    before = (config / "pg_credentials").read_text()
    sc.rewrite_port(config, pgdata, 5000, 5000)
    assert (config / "pg_credentials").read_text() == before


def test_free_port_is_one_nothing_holds() -> None:
    port = sc.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_an_unrewritable_port_falls_back_and_evicts(tmp_path: Path) -> None:
    c = cache(tmp_path)
    k = key()
    src = make_run(tmp_path, "seed-a")
    (src / "config" / "extra.yml").write_text(f"db: localhost:{OLD_PORT}/nexus\n")
    assert c.save(k, src, floor=FLOOR).saved
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    _assert_fell_back(res, dst, "still names the old PG port")
    assert _live(tmp_path) == []


# --- a corrupt, partial or mismatched entry falls back ----------------------------------------------------


def _assert_fell_back(res, dst: Path, reason_has: str) -> None:
    assert res.ok is False and reason_has in res.reason, res.reason
    assert not (dst / "prepare.json").exists() and not (dst / "synthetic-repo").exists()
    assert list((dst / "config").iterdir()) == [], (
        "the run's own config dir is left empty for the seeder"
    )
    assert not (dst / "home" / ".config").exists(), (
        "a parent the restore made is removed too"
    )
    assert "fresh client" in (dst / "home" / ".local" / "bin" / "nx").read_text()


def test_a_truncated_relation_file_is_a_corrupt_entry(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    rel = (
        _entry(tmp_path, k) / "payload" / "config" / "postgres" / "base" / "1" / "16384"
    )
    rel.write_bytes(rel.read_bytes()[: 1 << 20])
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "corrupt or partial")
    assert _live(tmp_path) == [], "the defective entry is evicted"


def test_a_same_size_corruption_of_a_file_over_1_mib_is_a_corrupt_entry(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    rel = (
        _entry(tmp_path, k) / "payload" / "config" / "postgres" / "base" / "1" / "16384"
    )
    data = bytearray(rel.read_bytes())
    assert len(data) > sc.SMALL_FILE_BYTES
    data[len(data) // 2] ^= 0xFF
    rel.write_bytes(bytes(data))  # one flipped bit-pattern, the same length
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "corrupt or partial")
    assert _live(tmp_path) == []


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
    creds.write_text(creds.read_text().replace(str(OLD_PORT), "54998"))
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
    assert _live(tmp_path) == []


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


def test_path_rows_that_differ_from_the_manifest_fall_back(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    man_path = _entry(tmp_path, k) / "manifest.json"
    man = json.loads(man_path.read_text())
    man["path_rows"]["catalog_owners.repo_root"] = 0
    man_path.write_text(json.dumps(man))
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "the manifest says")


def test_rows_that_still_name_the_saved_run_dir_after_the_rewrite_fall_back(
    tmp_path: Path,
) -> None:
    def half(config: Path, pairs, shift_s: float = 0.0) -> object:
        got = rewrite_fn(config, pairs)
        return sc.StoreRewrite(
            got.rows, got.before, {**got.after, "catalog_owners.repo_root": 1}
        )

    c, k, _ = seeded_cache(tmp_path, rewrite_fn=half)
    dst = new_run(tmp_path)
    _assert_fell_back(
        c.restore(k, dst, floor=FLOOR), dst, "still name the saved run dir"
    )


def test_a_restored_cluster_that_is_not_cleanly_shut_down_falls_back(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    c.clean_check = lambda _cfg: "cluster state is 'in production'"
    dst = new_run(tmp_path)
    _assert_fell_back(c.restore(k, dst, floor=FLOOR), dst, "not cleanly shut down")


def test_a_failing_pg_read_with_the_production_exception_type_falls_back_scrubs_and_evicts(
    tmp_path: Path,
) -> None:
    def boom(_config: Path, _pairs, _shift_s: float = 0.0) -> object:
        raise ProbeError(
            "psql failed: invalid page in block 7 of relation base/16384/2619"
        )

    c, k, _ = seeded_cache(tmp_path)
    c.rewrite_fn = boom
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    _assert_fell_back(res, dst, "ProbeError: psql failed: invalid page")
    assert _live(tmp_path) == [], (
        "the rejected entry is evicted, so a rerun seeds and saves"
    )
    assert not res.transient


def test_a_pg_read_that_raises_something_unforeseen_still_falls_back(
    tmp_path: Path,
) -> None:
    def boom(_config: Path, _pairs, _shift_s: float = 0.0) -> object:
        raise RuntimeError("anything at all")

    c, k, _ = seeded_cache(tmp_path)
    c.rewrite_fn = boom
    dst = new_run(tmp_path)
    _assert_fell_back(
        c.restore(k, dst, floor=FLOOR), dst, "RuntimeError: anything at all"
    )


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


# --- a restore never merges into, or removes, what the run had -------------------------------------------------


def test_a_destination_with_stale_pg_files_is_refused_and_left_alone(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    stale_wal = dst / "config" / "postgres" / "pg_wal" / "000000010000000000000009"
    stale_rel = dst / "config" / "postgres" / "base" / "1" / "99999"
    stale_wal.parent.mkdir(parents=True)
    stale_rel.parent.mkdir(parents=True)
    stale_wal.write_text("stale wal")
    stale_rel.write_text("stale relation")
    res = c.restore(k, dst, floor=FLOOR)
    assert not res.ok and "is not empty" in res.reason, res.reason
    assert (
        stale_wal.read_text() == "stale wal"
        and stale_rel.read_text() == "stale relation"
    )
    assert not (dst / "prepare.json").exists() and not (dst / "synthetic-repo").exists()
    assert _live(tmp_path) == [k.name], (
        "a dirty destination says nothing about the entry"
    )
    assert res.transient


def test_a_file_the_run_already_has_must_be_what_the_entry_holds(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    (dst / "prepare.json").write_text("{}")
    res = c.restore(k, dst, floor=FLOOR)
    assert not res.ok and "already exists and is not what the entry holds" in res.reason
    assert (dst / "prepare.json").read_text() == "{}" and _live(tmp_path) == [k.name]


def test_an_empty_dir_the_run_made_is_filled_and_emptied_again_by_a_failed_restore(
    tmp_path: Path,
) -> None:
    def boom(_config: Path, _pairs, _shift_s: float = 0.0) -> object:
        raise ProbeError("x")

    c, k, _ = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    existing = dst / "home" / ".local" / "share" / "other"
    existing.mkdir(parents=True)
    c.rewrite_fn = boom
    assert not c.restore(k, dst, floor=FLOOR).ok
    assert existing.is_dir() and list(existing.iterdir()) == []
    assert (dst / "home" / ".local" / "bin" / "nx").is_file()


def test_an_identical_file_the_run_already_has_is_kept_by_a_good_restore_and_a_failed_one(
    tmp_path: Path,
) -> None:
    c, k, src = seeded_cache(tmp_path)
    dst = new_run(tmp_path)
    (dst / "prepare.json").write_bytes((src / "prepare.json").read_bytes())
    mtime = (dst / "prepare.json").stat().st_mtime_ns
    assert c.restore(k, dst, floor=FLOOR).ok
    assert (dst / "prepare.json").stat().st_mtime_ns == mtime, "not copied over"
    bad = new_run(tmp_path.parent / (tmp_path.name + "-b"))
    (bad / "prepare.json").write_bytes((src / "prepare.json").read_bytes())
    c.rewrite_fn = lambda _c, _p, _s=0.0: sc.StoreRewrite(1, {}, {})
    assert not c.restore(k, bad, floor=FLOOR).ok
    assert (bad / "prepare.json").is_file(), "a file the run had is never scrubbed"


def test_a_restore_copies_nothing_when_the_ceiling_is_already_spent_and_keeps_the_entry(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    c.ceiling_s = 0.0
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    assert not res.ok and "ceiling" in res.reason and res.transient
    assert (
        list((dst / "config").iterdir()) == [] and not (dst / "prepare.json").exists()
    )
    assert _live(tmp_path) == [k.name], "the ceiling says nothing about the entry"


def test_a_restore_that_cannot_get_the_lock_falls_back_and_keeps_the_entry(
    tmp_path: Path,
) -> None:
    c, k, _ = seeded_cache(tmp_path, lock_timeout_s=0.3)
    dst = new_run(tmp_path)
    with sc._locked(tmp_path / "cache", exclusive=True, timeout_s=1) as got:
        assert got
        res = c.restore(k, dst, floor=FLOOR)
    assert not res.ok and "lock was not free" in res.reason and res.transient
    assert _live(tmp_path) == [k.name]
    assert not (dst / "prepare.json").exists()


def test_a_disk_error_while_copying_falls_back_and_keeps_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, k, _ = seeded_cache(tmp_path)
    real = sc._copy_tree
    calls = {"n": 0}

    def flaky(src: Path, dst: Path, deadline=None) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real(src, dst, deadline)

    monkeypatch.setattr(sc, "_copy_tree", flaky)
    dst = new_run(tmp_path)
    res = c.restore(k, dst, floor=FLOOR)
    assert not res.ok and "No space left" in res.reason and res.transient
    assert _live(tmp_path) == [k.name] and list((dst / "config").iterdir()) == []


# --- the clock shift on restore ---------------------------------------------------------------------


def test_a_restore_shifts_the_store_clock_by_the_time_since_the_save(tmp_path: Path) -> None:
    """The entry was saved at the clock's time; a restore 62 hours later passes a 62-hour shift to the store
    rewrite and reports it, so doctor's clock rows read the seed's ages, not the cache's."""
    clock = Clock()
    seen: list[float] = []

    def spy(config: Path, pairs, shift_s: float = 0.0) -> object:
        seen.append(shift_s)
        return rewrite_fn(config, pairs, shift_s)

    c, k, _ = seeded_cache(tmp_path, clock=clock, rewrite_fn=spy)
    clock.t += 62 * 3600
    res = c.restore(k, new_run(tmp_path), floor=FLOOR)
    assert res.ok, res.reason
    assert seen == [62 * 3600.0]
    assert res.shift_s == 62 * 3600.0


def test_a_restore_right_after_the_save_shifts_nothing(tmp_path: Path) -> None:
    seen: list[float] = []

    def spy(config: Path, pairs, shift_s: float = 0.0) -> object:
        seen.append(shift_s)
        return rewrite_fn(config, pairs, shift_s)

    c, k, _ = seeded_cache(tmp_path, clock=Clock(), rewrite_fn=spy)
    assert c.restore(k, new_run(tmp_path), floor=FLOOR).ok
    assert seen == [0.0]


def test_an_entry_whose_saved_at_cannot_be_read_is_not_restored(tmp_path: Path) -> None:
    """Without saved_at the shift is unknown, and a restore with the wrong clock fails doctor later, far from
    the cause: refuse it here, naming the field, and keep the run dir untouched."""
    c, k, _ = seeded_cache(tmp_path)
    entry = _entry(tmp_path, k)
    man_path = entry / sc.MANIFEST
    man = json.loads(man_path.read_text())
    man["saved_at"] = "not a time"
    man_path.write_text(json.dumps(man))
    res = c.restore(k, new_run(tmp_path), floor=FLOOR)
    assert not res.ok and "saved_at" in res.reason
