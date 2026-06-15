# SPDX-License-Identifier: AGPL-3.0-or-later
"""Relocation-awareness of check_pgvector_available (RDR-157, bead nexus-1e205).

`pg_config` reports build-time absolute paths. For a relocatable bundle (the
RDR-157 local distribution) extracted to a new prefix, `pg_config --sharedir`
returns the build prefix — which does not exist on the target. The preflight
must instead resolve the sharedir relative to the actual binary location.

These tests use a stub `pg_config` shell script (no mocking, no real PG) that
reports a nonexistent build prefix, and place `vector.control` only at the
re-anchored (binary-relative) location.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nexus.db.pg_provision import (
    PgBinaries,
    PgVectorNotInstalledError,
    check_pgvector_available,
)


def _write_pg_config(bin_dir: Path, reported_prefix: str) -> None:
    """Create an executable stub pg_config reporting a fixed build prefix.

    --bindir -> <prefix>/bin, --sharedir -> <prefix>/share (the from-source
    --prefix layout). The prefix is deliberately a path that does not exist on
    disk, simulating a bundle relocated away from its build location.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    pg_config = bin_dir / "pg_config"
    pg_config.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        f"  --sharedir) echo {reported_prefix}/share ;;\n"
        f"  --bindir)   echo {reported_prefix}/bin ;;\n"
        "  *) echo '' ;;\n"
        "esac\n"
    )
    pg_config.chmod(pg_config.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _bins(bin_dir: Path) -> PgBinaries:
    # check_pgvector_available only touches bins.bin_dir / "pg_config".
    return PgBinaries.from_dir(bin_dir)


class TestRelocationAware:
    def test_reanchored_control_passes_when_reported_prefix_is_gone(self, tmp_path):
        """The build prefix does not exist; vector.control lives only at the
        binary-relative sharedir. Preflight must find it via re-anchoring."""
        bundle = tmp_path / "relocated-bundle"
        bin_dir = bundle / "bin"
        _write_pg_config(bin_dir, reported_prefix="/nonexistent/build/pg")
        # Re-anchored sharedir = bin_dir/../share  ==  bundle/share
        control = bundle / "share" / "extension" / "vector.control"
        control.parent.mkdir(parents=True)
        control.write_text("comment = 'vector'\ndefault_version = '0.8.2'\n")

        check_pgvector_available(_bins(bin_dir))  # must NOT raise

    def test_reported_sharedir_still_works_when_not_relocated(self, tmp_path):
        """Non-relocated install: the reported sharedir exists and has the
        control file. (Here the reported prefix IS the real bundle.)"""
        bundle = tmp_path / "insitu"
        bin_dir = bundle / "bin"
        _write_pg_config(bin_dir, reported_prefix=str(bundle))
        control = bundle / "share" / "extension" / "vector.control"
        control.parent.mkdir(parents=True)
        control.write_text("comment = 'vector'\n")

        check_pgvector_available(_bins(bin_dir))  # must NOT raise

    def test_missing_everywhere_raises(self, tmp_path):
        """No vector.control at either the reported or re-anchored sharedir."""
        bundle = tmp_path / "no-pgvector"
        bin_dir = bundle / "bin"
        _write_pg_config(bin_dir, reported_prefix="/nonexistent/build/pg")
        # No control file created anywhere.
        with pytest.raises(PgVectorNotInstalledError):
            check_pgvector_available(_bins(bin_dir))

    def test_no_pg_config_does_not_block(self, tmp_path):
        """Indeterminate (pg_config absent) is a warning, not a failure."""
        bin_dir = tmp_path / "empty" / "bin"
        bin_dir.mkdir(parents=True)
        check_pgvector_available(_bins(bin_dir))  # must NOT raise
