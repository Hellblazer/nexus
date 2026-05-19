# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second 360° remediation Bundle D (correctness: err + time + uni).

S360-err (nexus-p0sk):
- health.py four sites must surface DaemonModeDiagnosticError with a
  fix-suggestion hint instead of swallowing it into a terse generic.
- bindings_crud.create_binding must guard its rollback path so a
  failing rollback cannot mask the original BindingProfileError.

S360-time (nexus-9pkn):
- _idempotency_now() clamps wall-clock against backward NTP / sleep
  jumps so the action_idempotency dedup window cannot shorten.
- _fingerprint_profiles_dirs uses (mtime, size) so two writes within
  the same coarse-mtime second still produce distinct fingerprints.

S360-uni (nexus-uf3w):
- load_profile enforces the SR-1 allowlist on the 'profile:' YAML
  field so direct-write paths can't smuggle arbitrary subspace
  prefixes through derived/<profile>.
- Binding names are NFC-normalised on both the load and CRUD-create
  paths so NFD vs NFC collisions surface as duplicates.
"""
from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# nexus-p0sk S1: DaemonModeDiagnosticError surfaces fix suggestions
# ---------------------------------------------------------------------------


class TestP0skHealthSurfacesDaemonModeHint:
    """Health-probe sites must emit fix_suggestions when the daemon-mode
    rejection fires, rather than collapsing to 'could not query'."""

    def test_t3_local_probe_surfaces_daemon_mode_hint(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Daemon mode + local credentials present so the
        # reject_under_daemon_mode call fires inside the probe.
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        local_dir = tmp_path / "chroma"
        local_dir.mkdir()

        from nexus import health as h

        # Re-route the local path probe. ``_check_t3_local`` imports
        # ``_default_local_path`` from ``nexus.config`` on every call
        # (the import lives inside the function body) so patching the
        # source module is what actually steers the lookup; an
        # ``h._local_chroma_path`` patch would be a no-op against the
        # real call site. Without this redirection the probe falls
        # through to the developer's real local chroma path, which on
        # a fresh CI runner does not exist; ``path_exists`` is False;
        # the ``Local collections`` branch is never reached; the
        # assertion below fails with only the three pre-branch
        # results (T3 mode / Local ChromaDB path / Embedding model).
        monkeypatch.setattr(
            "nexus.config._default_local_path", lambda: local_dir, raising=True
        )
        # Re-route credential lookup so the local branch is taken.
        monkeypatch.setattr(
            h, "get_credential",
            lambda *_a, **_k: "" if True else None,
            raising=False,
        )

        results = h._check_t3_local()
        # The branch we care about is "Local collections" — assert the
        # diagnostic-aware result is present with daemon-mode hint.
        loc = [r for r in results if r.label == "Local collections"]
        assert loc, f"no Local collections result; got: {[r.label for r in results]}"
        assert "daemon" in loc[0].detail.lower(), loc[0].detail
        assert loc[0].fix_suggestions, "expected fix_suggestions for daemon-mode skip"


# ---------------------------------------------------------------------------
# nexus-p0sk S2: bindings_crud rollback failures don't mask originals
# ---------------------------------------------------------------------------


class TestP0skBindingsCrudRollbackGuarded:
    """A failing rollback inside create_binding must not eclipse the
    original BindingProfileError. The caller should still see the
    validation error, and the rollback failure should land in the log
    rather than propagating."""

    def test_rollback_failure_logged_original_error_propagates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.cockpit import bindings_crud

        target_dir = tmp_path / "profiles"
        target_dir.mkdir()

        # Set up: seed a profile so create_binding hits the validate-
        # then-rollback path. We supply a deliberately malformed
        # action so load_profile rejects.
        bindings_crud.create_binding(
            profile="good",
            name="b1",
            match={"subspace": "tasks"},
            action={"kind": "log", "marker": "ok"},
            profiles_dir=target_dir,
        )

        # Inject a rollback-time failure: monkey-patch _write_profile_dict
        # to raise on the SECOND call (the rollback write).
        real_write = bindings_crud._write_profile_dict
        call_count = {"n": 0}

        def _maybe_fail(path, payload):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise OSError("synthetic rollback failure")
            return real_write(path, payload)

        monkeypatch.setattr(bindings_crud, "_write_profile_dict", _maybe_fail)

        from nexus.cockpit.bindings import BindingProfileError

        # The bad action ("unknown" kind) triggers load_profile to
        # reject — rollback then fires, our mock raises, and the
        # caller should still see the BindingProfileError, not OSError.
        with pytest.raises(BindingProfileError):
            bindings_crud.create_binding(
                profile="good",
                name="b2",
                match={"subspace": "tasks"},
                action={"kind": "unknown"},
                profiles_dir=target_dir,
            )


# ---------------------------------------------------------------------------
# nexus-9pkn S1: _idempotency_now clamps backward wall-clock movement
# ---------------------------------------------------------------------------


class TestNpknIdempotencyClockFloor:
    def test_idempotency_now_never_moves_backwards(
        self, monkeypatch
    ) -> None:
        from nexus.cockpit import bindings as b

        # Reset the module floor so prior tests don't carry over.
        b._idempotency_clock_floor = 0.0

        clock_values = iter([1000.0, 800.0, 900.0, 1500.0])
        monkeypatch.setattr(b.time, "time", lambda: next(clock_values))

        assert b._idempotency_now() == 1000.0
        assert b._idempotency_now() == 1000.0  # 800 clamped to 1000
        assert b._idempotency_now() == 1000.0  # 900 still below floor
        assert b._idempotency_now() == 1500.0  # 1500 advances floor


# ---------------------------------------------------------------------------
# nexus-9pkn S2: fingerprint tuple includes file size, not just mtime
# ---------------------------------------------------------------------------


class TestNpknFingerprintIncludesSize:
    def test_size_change_detected_when_mtime_unchanged(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings import (
            BindingContext,
            BindingProfile,
            BindingWatcher,
        )

        prof_dir = tmp_path / "profiles"
        prof_dir.mkdir()
        yml = prof_dir / "p.yml"
        yml.write_text("profile: p\nbindings: []\n")
        # Freeze mtime so the only signal is file size.
        fixed_mtime = 1_700_000_000.0
        os.utime(yml, (fixed_mtime, fixed_mtime))

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(":memory:")
        watcher = BindingWatcher(
            conn=conn,
            profiles=[BindingProfile(name="p", bindings=())],
            context=BindingContext(conn=conn),
            profiles_dirs=[prof_dir],
        )
        # First snapshot via the constructor.
        snap1 = dict(watcher._profile_fingerprints)
        assert yml in snap1
        size1 = snap1[yml][1]

        # Append content but RESET mtime to the same fixed value.
        yml.write_text(yml.read_text() + "# comment\n")
        os.utime(yml, (fixed_mtime, fixed_mtime))
        watcher._fingerprint_profiles_dirs()
        snap2 = dict(watcher._profile_fingerprints)
        size2 = snap2[yml][1]

        assert size2 != size1, (
            f"fingerprint did not detect size change ({size1} vs {size2}) — "
            "mtime-only fingerprint regression"
        )
        assert snap1 != snap2, (
            "watcher fingerprint reported no change despite a size-only "
            "edit; reload would have been missed"
        )


# ---------------------------------------------------------------------------
# nexus-uf3w S1: SR-1 allowlist enforced on the load path
# ---------------------------------------------------------------------------


class TestUf3wLoadProfileAllowlist:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../etc",
            "a/b",
            "white space",
            "!shell",
            "",
        ],
    )
    def test_load_profile_rejects_disallowed_names(
        self, tmp_path: Path, bad_name: str
    ) -> None:
        from nexus.cockpit.bindings import (
            BindingProfileError,
            load_profile,
        )

        yml = tmp_path / "p.yml"
        yml.write_text(f"profile: {bad_name!r}\nbindings: []\n")
        with pytest.raises(BindingProfileError):
            load_profile(yml)

    @pytest.mark.parametrize(
        "good_name",
        ["A", "foo", "a_b-1", "user1"],
    )
    def test_load_profile_accepts_allowlisted_names(
        self, tmp_path: Path, good_name: str
    ) -> None:
        from nexus.cockpit.bindings import load_profile

        yml = tmp_path / "p.yml"
        yml.write_text(f"profile: {good_name}\nbindings: []\n")
        prof = load_profile(yml)
        assert prof.name == good_name


# ---------------------------------------------------------------------------
# nexus-uf3w S2: NFC normalisation collapses NFD/NFC duplicate bindings
# ---------------------------------------------------------------------------


class TestUf3wNFCNormalisationDedup:
    def test_load_profile_collapses_nfd_nfc_duplicate(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings import (
            BindingProfileError,
            load_profile,
        )

        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        assert nfc != nfd  # confirm the input forms differ

        yml = tmp_path / "p.yml"
        yml.write_text(
            "profile: p\n"
            "bindings:\n"
            f"  - name: {nfc!r}\n"
            "    match: {subspace: x}\n"
            "    action: {kind: log, marker: a}\n"
            f"  - name: {nfd!r}\n"
            "    match: {subspace: x}\n"
            "    action: {kind: log, marker: b}\n"
        )
        with pytest.raises(BindingProfileError, match="duplicate"):
            load_profile(yml)

    def test_loaded_binding_name_is_nfc(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings import load_profile

        nfd = unicodedata.normalize("NFD", "café")
        yml = tmp_path / "p.yml"
        yml.write_text(
            "profile: p\n"
            "bindings:\n"
            f"  - name: {nfd!r}\n"
            "    match: {subspace: x}\n"
            "    action: {kind: log, marker: a}\n"
        )
        prof = load_profile(yml)
        assert prof.bindings[0].name == unicodedata.normalize("NFC", nfd)
