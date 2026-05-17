# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second 360° remediation Bundle A (nexus-bkvg).

FS-1: t2_daemon._unlink_discovery shutdown-marker stamp must be
atomic. The prior `path.write_text(...)` left a window for partial
JSON on a crash mid-write; ``_write_discovery`` already used the
``tmp + os.replace`` pattern, ``_unlink_discovery`` did not.

FS-2: bindings_crud._write_profile_dict must be atomic. A naked
``path.write_text(yaml.safe_dump(...))`` truncates the file in place,
so an ENOSPC mid-dump leaves an invalid YAML that load_profile
rejects.

FS-3: cockpit.bindings.user_profiles_dir must honour
``NEXUS_CONFIG_DIR`` like the rest of the path computations (TR-5).

FS-4: doctor.py tuples.db + daemon.log probes must use
``nexus_config_dir()`` rather than hardcoding ``~/.config/nexus/``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# FS-1: discovery-file shutdown marker must be atomic (tmp + os.replace)
# ---------------------------------------------------------------------------


class TestFS1DiscoveryShutdownMarkerAtomic:
    def test_marker_write_failure_preserves_prior_discovery(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A simulated failure in the marker write must NOT corrupt the
        on-disk discovery file. Validates the tmp + os.replace pattern:
        if the implementation uses in-place ``write_text``, the prior
        JSON is destroyed; with tmp + os.replace, the prior file
        survives intact when ``os.replace`` is the failing operation.
        """
        import asyncio

        from nexus.daemon.t2_daemon import T2Daemon

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        async def _run() -> None:
            daemon = T2Daemon(config_dir=tmp_path)
            await daemon.start()
            disco = daemon._discovery_path
            pre_text = disco.read_text()
            assert pre_text and json.loads(pre_text)

            real_replace = os.replace
            calls: list[tuple[str, str]] = []

            def _record_then_fail(src, dst, **kwargs):
                calls.append((str(src), str(dst)))
                raise OSError("simulated rename failure")

            # Suppress the unlink step too so we can observe the
            # discovery file's state after the marker write attempt;
            # otherwise unlink masks the bug by removing the file.
            monkeypatch.setattr(
                "pathlib.Path.unlink",
                lambda self, missing_ok=False: None,
            )
            monkeypatch.setattr(os, "replace", _record_then_fail)
            # _unlink_discovery is contract-bound to never raise.
            daemon._unlink_discovery()
            monkeypatch.setattr(os, "replace", real_replace)

            # The atomic pattern routes the write through a .tmp file
            # and os.replace; the failure leaves the prior file intact.
            assert disco.read_text() == pre_text, (
                "FS-1: marker write was not atomic — prior discovery "
                "file was overwritten before os.replace was even "
                "called."
            )
            assert calls, (
                "FS-1: expected the marker write to invoke os.replace "
                "(tmp + replace pattern). No os.replace observed."
            )
            await daemon.stop()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# FS-2: profile-dict YAML writes must be atomic
# ---------------------------------------------------------------------------


class TestFS2YAMLWriteAtomic:
    def test_ensp_mid_write_preserves_prior_yaml(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An ENOSPC during the YAML dump must leave the original file
        intact: load_profile must still parse it.
        """
        import errno
        from nexus.cockpit import bindings_crud

        path = tmp_path / "profile.yml"
        # Seed a valid prior YAML.
        prior_payload = {
            "version": 1,
            "profile": "claude",
            "bindings": [],
        }
        bindings_crud._write_profile_dict(path, prior_payload)
        original_bytes = path.read_bytes()
        assert original_bytes, "seed write produced empty file"

        # Now monkeypatch os.replace to raise ENOSPC AFTER the tmp
        # file has been written. The atomic-write pattern must roll
        # back to the prior state, leaving the original YAML intact.
        new_payload = {
            "version": 1,
            "profile": "claude",
            "bindings": [{"name": "b1", "match": {}, "action": {}}],
        }
        real_replace = os.replace

        def _raise_enospc(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "replace", _raise_enospc)
        with pytest.raises(OSError):
            bindings_crud._write_profile_dict(path, new_payload)
        monkeypatch.setattr(os, "replace", real_replace)

        # The original file must still parse to the prior payload.
        assert path.read_bytes() == original_bytes, (
            "FS-2: failed write corrupted the prior file in place"
        )

    def test_successful_write_leaves_no_tmp_residue(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit import bindings_crud

        path = tmp_path / "profile.yml"
        bindings_crud._write_profile_dict(path, {"version": 1, "profile": "x"})
        residues = [p for p in path.parent.iterdir() if p.name != path.name]
        assert not residues, f"FS-2: residual files {residues}"


# ---------------------------------------------------------------------------
# FS-3: user_profiles_dir must honour NEXUS_CONFIG_DIR
# ---------------------------------------------------------------------------


class TestFS3UserProfilesDirHonoursOverride:
    def test_override_redirects_user_profiles_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.cockpit.bindings import user_profiles_dir

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        out = user_profiles_dir()
        assert out == tmp_path / "bindings" / "profiles", (
            f"FS-3: user_profiles_dir() returned {out}; expected "
            f"{tmp_path / 'bindings' / 'profiles'}"
        )

    def test_no_override_uses_default(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.cockpit.bindings import user_profiles_dir

        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        out = user_profiles_dir()
        # Default falls through to ~/.config/nexus/bindings/profiles.
        # We can't assert the exact path without HOME, but we can
        # verify the suffix.
        assert out.parts[-3:] == ("nexus", "bindings", "profiles"), (
            f"FS-3: default user_profiles_dir() shape unexpected: {out}"
        )


# ---------------------------------------------------------------------------
# FS-4: doctor tuples.db + daemon.log paths must honour NEXUS_CONFIG_DIR
# ---------------------------------------------------------------------------


class TestFS4DoctorPathsHonourOverride:
    def test_tuples_db_path_redirects_under_override(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Smoke-check: doctor's tuples.db computation should use
        nexus_config_dir().  We verify by reading the relevant source
        line — the function-internal probe is intertwined with click
        I/O so this guards against the hardcoded path regressing.
        """
        from pathlib import Path as _Path

        source = (
            _Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "commands" / "doctor.py"
        ).read_text()
        assert (
            "os.path.expanduser(\"~/.config/nexus/tuples.db\")"
            not in source
        ), (
            "FS-4: doctor.py still hardcodes ~/.config/nexus/tuples.db; "
            "should use nexus_config_dir() / 'tuples.db'."
        )
        assert (
            "os.path.expanduser(\"~/.config/nexus/logs/daemon.log\")"
            not in source
        ), (
            "FS-4: doctor.py still hardcodes ~/.config/nexus/logs/"
            "daemon.log; should use nexus_config_dir() / 'logs' / "
            "'daemon.log'."
        )
