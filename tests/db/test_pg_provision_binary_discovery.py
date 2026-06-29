# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for pg_provision binary discovery (no real PG cluster needed).

Tests the NEXUS_PG_BIN env-override behaviour without spawning a real cluster,
so these run in the standard unit suite with no @pytest.mark.integration.
"""
from __future__ import annotations

import pytest

from nexus.db.pg_provision import PgBinaryNotFoundError, discover_pg_binaries


class TestNexusPgBinEnvOverride:
    """NEXUS_PG_BIN behaviour: valid → use it; set-but-missing → fail loud."""

    def test_nexus_pg_bin_set_to_nonexistent_dir_raises_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """NEXUS_PG_BIN pointing at a dir with no binaries must raise PgBinaryNotFoundError,
        NOT silently fall back to system-path discovery.
        """
        empty_dir = tmp_path / "pg_empty"
        empty_dir.mkdir()
        monkeypatch.setenv("NEXUS_PG_BIN", str(empty_dir))

        with pytest.raises(PgBinaryNotFoundError) as exc_info:
            discover_pg_binaries()

        err = str(exc_info.value)
        # Must mention the configured path, not just a generic "not found" message.
        assert str(empty_dir) in err, (
            f"Error must mention NEXUS_PG_BIN path '{empty_dir}'; got: {err!r}"
        )
        # Must indicate what's missing, not just that something is wrong.
        assert "missing" in err.lower() or "NEXUS_PG_BIN" in err, (
            f"Error must mention NEXUS_PG_BIN or 'missing'; got: {err!r}"
        )
        # Must include an install hint.
        assert "postgresql" in err.lower() or "brew" in err.lower() or "apt" in err.lower(), (
            f"Error must include an install hint; got: {err!r}"
        )

    def test_nexus_pg_bin_set_to_nonexistent_path_does_not_fall_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEXUS_PG_BIN set to a path that doesn't exist must also fail loudly."""
        monkeypatch.setenv("NEXUS_PG_BIN", "/definitely/does/not/exist/pg/bin")

        with pytest.raises(PgBinaryNotFoundError) as exc_info:
            discover_pg_binaries()

        err = str(exc_info.value)
        assert "/definitely/does/not/exist/pg/bin" in err, (
            "Error must name the misconfigured NEXUS_PG_BIN path"
        )


class TestConfigDirBundleBootSafeDiscovery:
    """RDR-174 P2.2 (nexus-exfns): the config-dir bundle is discovered with
    NEXUS_PG_BIN unset — the boot-safe path the storage-service daemon's
    _ensure_pg_running relies on at cold boot (no provisioning env). This is
    why the autostart unit needs no external postgresql.service ordering."""

    def test_bundle_under_config_dir_resolved_without_nexus_pg_bin(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nexus.db.pg_bundle as pg_bundle

        monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
        bundle_bin = tmp_path / "bundle" / "bin"
        bundle_bin.mkdir(parents=True)
        for name in ("initdb", "pg_ctl", "psql", "createdb"):
            (bundle_bin / name).write_text("#!/bin/sh\n")
            (bundle_bin / name).chmod(0o755)

        # Force the config-dir bundle seam to resolve to our fake bundle,
        # exactly as it would on a local-distribution machine at boot.
        monkeypatch.setattr(pg_bundle, "extracted_bin_dir", lambda _cfg: bundle_bin)

        bins = discover_pg_binaries()

        assert bins.all_present(), "config-dir bundle must satisfy discovery at boot"
        assert bins.initdb == bundle_bin / "initdb"
        # Belt-and-suspenders: every binary resolved from the bundle, not a
        # host PG that happens to be on PATH / in a candidate dir.
        assert bins.initdb.parent == bundle_bin
        assert bins.pg_ctl.parent == bundle_bin
