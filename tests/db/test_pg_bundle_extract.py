# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3.4 local-distribution first-run bundle extraction (RDR-157, bead nexus-vwvv5.13).

The local distribution ships, ship-alongside (CA-2 verdict, bead nexus-vwvv5.11),
a ``nexus-pg-<platform>.txz`` next to the native binary. On first run nexus must:

  1. LOCATE that archive (explicit ``NEXUS_PG_BUNDLE`` override, else a
     conventional ``nexus-pg-<tag>.txz`` in known search dirs),
  2. EXTRACT it to a stable cache dir under the config dir (idempotently — a
     re-run must NOT re-extract), and
  3. SELECT it for provisioning by handing back the bundle's ``bin/`` dir, which
     the caller exports as ``NEXUS_PG_BIN`` so the existing
     :func:`nexus.db.pg_provision.discover_pg_binaries` /
     :func:`~nexus.db.pg_provision.provision` flow runs unchanged.

These are pure filesystem-orchestration tests over a SYNTHETIC bundle (four stub
binaries under ``bundle/bin`` + the ``.build_prefix`` marker, built by the
``make_pg_bundle_txz`` fixture in ``tests/conftest.py``); they do not run
PostgreSQL. The real extract→initdb→provision→``CREATE EXTENSION vector``
round-trip is proven against a genuine artifact by
``tests/db/test_pg_bundle_relocation.py``.
"""
from __future__ import annotations

import os
import platform
import tarfile
from pathlib import Path

import pytest

from nexus.db import pg_bundle
from nexus.db.pg_provision import PgBinaries


# ── platform tag ────────────────────────────────────────────────────────────────


def test_current_platform_tag_matches_ci_artifact_naming() -> None:
    """The tag must match the CI artifact names (nexus-pg-<tag>.txz)."""
    tag = pg_bundle.current_platform_tag()
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    if sys_name == "darwin":
        assert tag == ("mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64")
    elif sys_name == "linux":
        assert tag == ("linux-arm64" if machine in {"aarch64", "arm64"} else "linux-amd64")
    # Release-N shipped targets (the only ones with a real artifact).
    assert tag in {"mac-arm64", "mac-x64", "linux-amd64", "linux-arm64"}


# ── locate_bundle_archive ───────────────────────────────────────────────────────


def test_locate_via_explicit_env_override(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-mac-arm64.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    assert pg_bundle.locate_bundle_archive() == archive


def test_locate_env_override_missing_fails_loud(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(tmp_path / "nope.txz"))
    with pytest.raises(FileNotFoundError, match=pg_bundle.BUNDLE_ENV):
        pg_bundle.locate_bundle_archive()


def test_locate_via_conventional_name_in_search_dir(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    tag = pg_bundle.current_platform_tag()
    archive = make_pg_bundle_txz(tmp_path, f"nexus-pg-{tag}.txz")
    assert pg_bundle.locate_bundle_archive(search_dirs=[tmp_path]) == archive


def test_locate_returns_none_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    assert pg_bundle.locate_bundle_archive(search_dirs=[tmp_path]) is None


def test_locate_ignores_wrong_platform_archive(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    """A bundle for a different platform must not be picked up by convention."""
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    tag = pg_bundle.current_platform_tag()
    other = "linux-amd64" if tag != "linux-amd64" else "mac-arm64"
    make_pg_bundle_txz(tmp_path, f"nexus-pg-{other}.txz")
    assert pg_bundle.locate_bundle_archive(search_dirs=[tmp_path]) is None


# ── extract_bundle ──────────────────────────────────────────────────────────────


def test_extract_bundle_returns_bin_dir_with_all_binaries(tmp_path, make_pg_bundle_txz) -> None:
    archive = make_pg_bundle_txz(tmp_path)
    extract_root = tmp_path / "cache"
    bin_dir = pg_bundle.extract_bundle(archive, extract_root)
    assert bin_dir == pg_bundle.bundle_bin_dir(extract_root)
    assert PgBinaries.from_dir(bin_dir).all_present()
    assert (extract_root / "bundle" / ".build_prefix").is_file()


def test_extract_bundle_is_idempotent_no_reextract(tmp_path, make_pg_bundle_txz) -> None:
    archive = make_pg_bundle_txz(tmp_path)
    extract_root = tmp_path / "cache"
    bin_dir = pg_bundle.extract_bundle(archive, extract_root)
    # Stamp a sentinel inside the extracted tree; a re-extract would wipe it.
    sentinel = bin_dir / "initdb"
    sentinel.write_text("MUTATED-BY-TEST\n")
    again = pg_bundle.extract_bundle(archive, extract_root)
    assert again == bin_dir
    assert sentinel.read_text() == "MUTATED-BY-TEST\n", "re-extract clobbered an existing tree"


def test_extract_bundle_incomplete_tree_fails_loud(tmp_path) -> None:
    """A .txz missing required binaries must raise, not return a broken bin dir."""
    staging = tmp_path / "_bad"
    (staging / "bundle" / "bin").mkdir(parents=True)
    (staging / "bundle" / "bin" / "initdb").write_text("only one\n")  # missing 3
    (staging / "bundle" / ".build_prefix").write_text("/x\n")
    archive = tmp_path / "nexus-pg-bad.txz"
    with tarfile.open(archive, "w:xz") as tf:
        tf.add(staging / "bundle", arcname="bundle")
    extract_root = tmp_path / "cache"
    with pytest.raises(RuntimeError, match="incomplete|missing"):
        pg_bundle.extract_bundle(archive, extract_root)
    # Partial tree removed so a retry starts clean.
    assert not extract_root.exists()


def test_extract_bundle_missing_build_prefix_fails_loud(tmp_path, make_pg_bundle_txz) -> None:
    """A tarball without the .build_prefix relocation marker is rejected."""
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-noprefix.txz", with_build_prefix=False)
    with pytest.raises(RuntimeError, match="build_prefix|relocation"):
        pg_bundle.extract_bundle(archive, tmp_path / "cache")


def test_is_bundle_extracted_reflects_state(tmp_path, make_pg_bundle_txz) -> None:
    archive = make_pg_bundle_txz(tmp_path)
    extract_root = tmp_path / "cache"
    assert pg_bundle.is_bundle_extracted(extract_root) is False
    pg_bundle.extract_bundle(archive, extract_root)
    assert pg_bundle.is_bundle_extracted(extract_root) is True


# ── ensure_pg_bundle orchestration ──────────────────────────────────────────────


def test_ensure_returns_none_without_bundle(tmp_path, monkeypatch) -> None:
    """No bundle locatable → None (host-PG / dev mode, provision discovers normally)."""
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    assert pg_bundle.ensure_pg_bundle(config_dir, search_dirs=[tmp_path / "empty"]) is None


def test_ensure_extracts_under_config_dir_and_returns_bin(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-mac-arm64.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    bin_dir = pg_bundle.ensure_pg_bundle(config_dir)
    assert bin_dir is not None
    # Extracted under the config dir, not next to the archive.
    assert str(bin_dir).startswith(str(config_dir))
    assert PgBinaries.from_dir(bin_dir).all_present()


def test_ensure_is_idempotent(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-mac-arm64.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    first = pg_bundle.ensure_pg_bundle(config_dir)
    assert first is not None
    sentinel = first / "psql"
    sentinel.write_text("KEEP\n")
    second = pg_bundle.ensure_pg_bundle(config_dir)
    assert second == first
    assert sentinel.read_text() == "KEEP\n"


def test_extracted_bin_dir_finds_already_extracted(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    """The archive-free discovery helper used by discover_pg_binaries."""
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-mac-arm64.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    assert pg_bundle.extracted_bin_dir(config_dir) is None  # not yet extracted
    bin_dir = pg_bundle.ensure_pg_bundle(config_dir)
    assert pg_bundle.extracted_bin_dir(config_dir) == bin_dir  # found, no archive needed


def test_discover_pg_binaries_finds_bundle_without_env(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    """Critical (substantive-critic, P3.4): the daemon's PG-restart path calls
    discover_pg_binaries() in a SEPARATE process from `nx init`, so it cannot rely
    on the transient NEXUS_PG_BIN set during extraction. discover_pg_binaries must
    find the already-extracted bundle via the config dir with NO env set.
    """
    import nexus.db.pg_provision as pgp

    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-mac-arm64.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    bin_dir = pg_bundle.ensure_pg_bundle(config_dir)
    assert bin_dir is not None

    # Simulate the daemon process: no NEXUS_PG_BIN, config dir points at the
    # extracted bundle, and NO host PG on the candidate dirs/PATH would match
    # these stub binaries anyway.
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    # discover_pg_binaries does a local `from nexus.config import nexus_config_dir`,
    # so patch it at the source module.
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: config_dir)

    bins = pgp.discover_pg_binaries()
    assert bins.bin_dir == bin_dir


# ── Integration: real artifact (gated on NEXUS_PG_BUNDLE pointing at a .txz) ─────


@pytest.mark.integration
def test_ensure_real_bundle_discovers_and_has_pgvector(tmp_path, monkeypatch) -> None:
    """First-run orchestration end to end on a GENUINE artifact.

    Gated on ``NEXUS_PG_BUNDLE`` pointing at a real ``nexus-pg-<plat>.txz`` (the
    P3.1 CI artifact). Proves the NEW locate→extract→select path: the extracted
    bundle's binaries are discoverable via ``NEXUS_PG_BIN`` and pgvector's
    ``vector.control`` resolves (re-anchored on the relocated bin dir). The full
    extract→initdb→provision→``CREATE EXTENSION`` round-trip is covered by
    ``test_pg_bundle_relocation.py``.
    """
    from nexus.db.pg_provision import (
        check_pgvector_available,
        discover_pg_binaries,
    )

    archive = os.environ.get("NEXUS_PG_BUNDLE", "").strip()
    if not archive or not Path(archive).is_file():
        pytest.skip("no NEXUS_PG_BUNDLE .txz artifact (provided by the CA-3 bundle job)")

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)

    bin_dir = pg_bundle.ensure_pg_bundle(config_dir)
    assert bin_dir is not None
    assert PgBinaries.from_dir(bin_dir).all_present()
    assert (bin_dir.parent / ".build_prefix").is_file()

    monkeypatch.setenv("NEXUS_PG_BIN", str(bin_dir))
    bins = discover_pg_binaries()
    assert bins.bin_dir == bin_dir
    check_pgvector_available(bins)  # raises if pgvector is not loadable from the bundle
