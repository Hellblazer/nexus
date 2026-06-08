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
