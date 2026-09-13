# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-level ``beads`` PRIME.md management (nexus-cnzei.8).

Covers path resolution per platform (Go ``os.UserConfigDir`` semantics),
beads detection (``bd`` on PATH or the beads Claude Code plugin, in its
two REAL layouts), the four :class:`~nexus.beads_prime.PrimeStatus`
states (hash-verified: a hand-edited body under an intact marker is
``USER_AUTHORED``, never silently overwritten), install idempotence, the
never-overwrite-user-authored guarantee, the marker-version upgrade path,
the downgrade-protection guard, the backup-before-overwrite guarantee,
and the persistent opt-out. All filesystem interaction is confined to
``tmp_path`` -- nothing here touches a real ``HOME``/``~/.config`` (the
conftest-level fence in ``tests/conftest.py::_fence_beads_prime_user_path``
also guarantees this for any call that reaches ``user_prime_path()`` with
no explicit args, but every test below still passes explicit tmp paths
directly, belt and suspenders).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus.beads_prime import (
    InstallOutcome,
    PrimeStatus,
    beads_detected,
    install,
    load_template,
    manage_enabled,
    status,
    user_prime_path,
)


# ── path resolution ──────────────────────────────────────────────────────────


class TestUserPrimePath:
    def test_darwin(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        got = user_prime_path(platform="darwin", home=home, environ={})
        assert got == home / "Library" / "Application Support" / "beads" / "PRIME.md"

    def test_windows_with_appdata(self, tmp_path: Path) -> None:
        appdata = tmp_path / "AppData" / "Roaming"
        home = tmp_path / "home"
        got = user_prime_path(
            platform="win32", home=home, environ={"APPDATA": str(appdata)}
        )
        assert got == appdata / "beads" / "PRIME.md"

    def test_windows_without_appdata_falls_back_to_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        got = user_prime_path(platform="win32", home=home, environ={})
        assert got == home / "AppData" / "Roaming" / "beads" / "PRIME.md"

    def test_linux_with_xdg_config_home(self, tmp_path: Path) -> None:
        xdg = tmp_path / "xdg-config"
        home = tmp_path / "home"
        got = user_prime_path(
            platform="linux", home=home, environ={"XDG_CONFIG_HOME": str(xdg)}
        )
        assert got == xdg / "beads" / "PRIME.md"

    def test_linux_without_xdg_config_home_falls_back_to_dot_config(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        got = user_prime_path(platform="linux", home=home, environ={})
        assert got == home / ".config" / "beads" / "PRIME.md"

    def test_unrecognized_platform_treated_like_linux(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        got = user_prime_path(platform="freebsd13", home=home, environ={})
        assert got == home / ".config" / "beads" / "PRIME.md"

    def test_defaults_are_ambient_when_not_injected(self) -> None:
        # No platform/home/environ passed — must resolve without raising.
        # Read-only: this computes a path, it does not touch the
        # filesystem, so exercising the true ambient fallback here is safe
        # even outside the conftest fence.
        got = user_prime_path()
        assert got.name == "PRIME.md"
        assert got.parent.name == "beads"


# ── beads detection ──────────────────────────────────────────────────────────


class TestBeadsDetected:
    def test_bd_on_path_wins_immediately(self, tmp_path: Path) -> None:
        present, reason = beads_detected(
            which=lambda _name: "/opt/homebrew/bin/bd",
            claude_config_dir=tmp_path / "nonexistent",
        )
        assert present is True
        assert "bd" in reason
        assert "/opt/homebrew/bin/bd" in reason

    def test_plugin_under_marketplaces_detected(self, tmp_path: Path) -> None:
        # Real layout (verified against a live Claude Code install,
        # nexus-cnzei.8 CRE fix round): the marketplace is a checkout of
        # its OWN repo, which nests the plugin under a `plugins/` dir —
        # <marketplace>/plugins/beads/.claude-plugin/plugin.json.
        claude_dir = tmp_path / ".claude"
        plugin_dir = (
            claude_dir / "plugins" / "marketplaces" / "beads-marketplace"
            / "plugins" / "beads" / ".claude-plugin"
        )
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text("{}")
        present, reason = beads_detected(
            which=lambda _name: None, claude_config_dir=claude_dir
        )
        assert present is True
        assert "plugin" in reason

    def test_plugin_under_cache_versioned_detected(self, tmp_path: Path) -> None:
        # Real layout: the cache flattens straight to <plugin>/<version>,
        # no `plugins/` segment.
        claude_dir = tmp_path / ".claude"
        plugin_dir = (
            claude_dir / "plugins" / "cache" / "beads-marketplace" / "beads" / "1.2.2"
            / ".claude-plugin"
        )
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text("{}")
        present, reason = beads_detected(
            which=lambda _name: None, claude_config_dir=claude_dir
        )
        assert present is True

    def test_marketplace_layout_without_plugins_segment_not_detected(
        self, tmp_path: Path
    ) -> None:
        # The WRONG (pre-fix) shape this module used to glob for must NOT
        # match — regression pin for the CRE fix round.
        claude_dir = tmp_path / ".claude"
        plugin_dir = (
            claude_dir / "plugins" / "marketplaces" / "beads-marketplace"
            / "beads" / ".claude-plugin"
        )
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text("{}")
        present, _reason = beads_detected(
            which=lambda _name: None, claude_config_dir=claude_dir
        )
        assert present is False

    def test_neither_present(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        present, reason = beads_detected(
            which=lambda _name: None, claude_config_dir=claude_dir
        )
        assert present is False
        assert reason


# ── status ────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_absent(self, tmp_path: Path) -> None:
        assert status(tmp_path / "does-not-exist" / "PRIME.md") is PrimeStatus.ABSENT

    def test_unreadable_path_is_user_authored(self, tmp_path: Path) -> None:
        # A directory at the target path exists() but cannot be read_text()'d —
        # must degrade to the safe (never-overwrite) branch, not crash.
        p = tmp_path / "PRIME.md"
        p.mkdir()
        assert status(p) is PrimeStatus.USER_AUTHORED

    def test_no_marker_is_user_authored(self, tmp_path: Path) -> None:
        p = tmp_path / "PRIME.md"
        p.write_text("# My own notes\nDo not touch.\n")
        assert status(p) is PrimeStatus.USER_AUTHORED

    def test_marker_and_matching_content_is_current(self, tmp_path: Path) -> None:
        p = tmp_path / "PRIME.md"
        p.write_text(load_template())
        assert status(p) is PrimeStatus.MANAGED_CURRENT

    def test_marker_present_hash_matches_but_content_stale_is_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates an older-version managed install: the marker's
        # recorded hash matches ITS OWN body (never hand-edited), but the
        # packaged template has since moved on.
        body = "old body\n"
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        p = tmp_path / "PRIME.md"
        p.write_text(f"<!-- conexus-managed beads PRIME v1 sha256:{digest} -->\n{body}")
        monkeypatch.setattr(
            "nexus.beads_prime.load_template",
            lambda: "<!-- conexus-managed beads PRIME v2 sha256:whatever -->\nnew body\n",
        )
        assert status(p) is PrimeStatus.MANAGED_STALE

    def test_marker_present_but_hash_mismatch_is_user_authored(
        self, tmp_path: Path
    ) -> None:
        # The ship-blocker this fix round closes: a human edited the body
        # but left the marker line (and its now-stale recorded hash)
        # intact. Must be treated as user-authored, never overwritten.
        p = tmp_path / "PRIME.md"
        wrong_digest = "0" * 64
        p.write_text(
            f"<!-- conexus-managed beads PRIME v1 sha256:{wrong_digest} -->\n"
            "a human edited this body\n"
        )
        assert status(p) is PrimeStatus.USER_AUTHORED

    def test_marker_with_malformed_hash_is_user_authored(self, tmp_path: Path) -> None:
        p = tmp_path / "PRIME.md"
        p.write_text("<!-- conexus-managed beads PRIME v1 -->\nold body (no hash)\n")
        assert status(p) is PrimeStatus.USER_AUTHORED


# ── install ───────────────────────────────────────────────────────────────────


class TestInstall:
    def test_installs_when_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "beads" / "PRIME.md"
        outcome = install(target)
        assert outcome == InstallOutcome("installed", target)
        assert target.read_text() == load_template()

    def test_idempotent_second_call_is_up_to_date(self, tmp_path: Path) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        outcome = install(target)
        assert outcome.action == "up to date"
        assert target.read_text() == load_template()

    def test_never_overwrites_user_authored_file(self, tmp_path: Path) -> None:
        target = tmp_path / "PRIME.md"
        target.write_text("# hand-written, do not touch\n")
        outcome = install(target)
        assert outcome.action == "left alone (user-authored)"
        assert target.read_text() == "# hand-written, do not touch\n"

    def test_never_overwrites_hand_edited_body_under_intact_marker(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "PRIME.md"
        wrong_digest = "1" * 64
        original = (
            f"<!-- conexus-managed beads PRIME v1 sha256:{wrong_digest} -->\n"
            "a human edited this body after install\n"
        )
        target.write_text(original)
        outcome = install(target)
        assert outcome.action == "left alone (user-authored)"
        assert target.read_text() == original

    def test_marker_version_upgrade_path_backs_up_and_updates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        old_body = "old body\n"
        old_digest = hashlib.sha256(old_body.encode("utf-8")).hexdigest()
        old_text = f"<!-- conexus-managed beads PRIME v1 sha256:{old_digest} -->\n{old_body}"
        target.write_text(old_text)

        new_body = "new body\n"
        new_digest = hashlib.sha256(new_body.encode("utf-8")).hexdigest()
        new_template = f"<!-- conexus-managed beads PRIME v2 sha256:{new_digest} -->\n{new_body}"
        monkeypatch.setattr("nexus.beads_prime.load_template", lambda: new_template)

        assert status(target) is PrimeStatus.MANAGED_STALE
        outcome = install(target)
        assert outcome.action == "updated"
        assert target.read_text() == new_template
        assert outcome.backup_path == target.with_name("PRIME.md.bak")
        assert outcome.backup_path.read_text() == old_text

    def test_downgrade_does_not_regress_a_newer_managed_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        newer_body = "newer body, v5\n"
        newer_digest = hashlib.sha256(newer_body.encode("utf-8")).hexdigest()
        newer_text = f"<!-- conexus-managed beads PRIME v5 sha256:{newer_digest} -->\n{newer_body}"
        target.write_text(newer_text)

        # This install only ships an OLDER template (v2) — a downgrade.
        older_body = "older body, v2\n"
        older_digest = hashlib.sha256(older_body.encode("utf-8")).hexdigest()
        older_template = f"<!-- conexus-managed beads PRIME v2 sha256:{older_digest} -->\n{older_body}"
        monkeypatch.setattr("nexus.beads_prime.load_template", lambda: older_template)

        outcome = install(target)
        assert outcome.action == "left alone (installed is newer)"
        assert target.read_text() == newer_text  # untouched
        assert not target.with_name("PRIME.md.bak").exists()  # no backup taken

    def test_write_is_atomic_no_partial_file_left_on_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "beads" / "PRIME.md"

        def _boom(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
            raise OSError("disk full (simulated)")

        monkeypatch.setattr("os.replace", _boom)
        with pytest.raises(OSError):
            install(target)
        # No half-written target and no leftover temp file.
        assert not target.exists()
        leftovers = list(target.parent.glob(".prime-*.tmp")) if target.parent.exists() else []
        assert leftovers == []

    def test_default_path_used_when_none_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nexus.beads_prime.user_prime_path", lambda: tmp_path / "PRIME.md"
        )
        outcome = install()
        assert outcome.action == "installed"
        assert outcome.path == tmp_path / "PRIME.md"


# ── opt-out (persistent config key) ─────────────────────────────────────────


class TestManageEnabled:
    def test_default_true_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nexus.config.load_config", lambda: {})
        assert manage_enabled() is True

    def test_false_when_explicitly_declined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "nexus.config.load_config", lambda: {"beads_prime": {"manage": False}}
        )
        assert manage_enabled() is False

    def test_true_when_explicitly_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "nexus.config.load_config", lambda: {"beads_prime": {"manage": True}}
        )
        assert manage_enabled() is True

    def test_config_read_failure_defaults_to_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("nexus.config.load_config", _boom)
        assert manage_enabled() is True


class TestInstallAndDescribe:
    def test_disabled_flag_skips_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.beads_prime import install_and_describe

        called: list[str] = []
        monkeypatch.setattr(
            "nexus.beads_prime.beads_detected", lambda **_kw: called.append("x") or (True, "x")
        )
        message = install_and_describe(disabled=True)
        assert message == "Beads PRIME.md: skipped (--no-beads-prime)"
        assert called == []  # never even reached detection

    def test_manage_disabled_config_skips_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.beads_prime import install_and_describe

        monkeypatch.setattr("nexus.beads_prime.manage_enabled", lambda: False)
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.beads_prime.beads_detected", lambda **_kw: called.append("x") or (True, "x")
        )
        message = install_and_describe()
        assert message == "Beads PRIME.md: skipped (beads_prime.manage is set to false)"
        assert called == []

    def test_message_names_path_and_undo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.beads_prime import install_and_describe

        target = tmp_path / "PRIME.md"
        monkeypatch.setattr("nexus.beads_prime.manage_enabled", lambda: True)
        monkeypatch.setattr(
            "nexus.beads_prime.beads_detected", lambda **_kw: (True, "test")
        )
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)
        message = install_and_describe()
        assert message is not None
        assert str(target) in message
        assert "machine-wide" in message
        assert "--no-beads-prime" in message
        assert "beads_prime.manage" in message
        assert "restore bd" in message

    def test_not_detected_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.beads_prime import install_and_describe

        monkeypatch.setattr("nexus.beads_prime.manage_enabled", lambda: True)
        monkeypatch.setattr(
            "nexus.beads_prime.beads_detected", lambda **_kw: (False, "not found")
        )
        assert install_and_describe() is None


# ── conftest fence (nexus-cnzei.8 CRE fix round) ────────────────────────────


class TestConftestHomeFence:
    """Proves the shared conftest fence (``tests/conftest.py::
    _fence_beads_prime_user_path``) is actually armed in an ordinary
    test — not merely present in conftest.py's source. This is the guard
    test the CRE finding asked for: it fails loud if the fence is ever
    removed or bypassed."""

    def test_user_prime_path_resolves_under_tmp_not_the_real_home(self) -> None:
        import nexus.beads_prime as bp

        resolved = bp.user_prime_path()
        home = Path.home()
        assert not str(resolved).startswith(str(home / "Library"))
        assert resolved != home / ".config" / "beads" / "PRIME.md"
        assert not str(resolved).startswith(str(home / "AppData"))

    def test_explicit_args_bypass_the_fence(self, tmp_path: Path) -> None:
        import nexus.beads_prime as bp

        home = tmp_path / "explicit-home"
        got = bp.user_prime_path(platform="darwin", home=home, environ={})
        assert got == home / "Library" / "Application Support" / "beads" / "PRIME.md"


# ── packaged template sanity ─────────────────────────────────────────────────


class TestPackagedTemplate:
    def test_under_1024_bytes(self) -> None:
        assert len(load_template().encode("utf-8")) < 1024

    def test_no_git_add(self) -> None:
        assert "git add" not in load_template()

    def test_starts_with_recognized_marker(self) -> None:
        first_line = load_template().split("\n", 1)[0]
        assert first_line.startswith("<!-- conexus-managed beads PRIME v")
        assert "sha256:" in first_line
        assert first_line.endswith("-->")

    def test_marker_hash_is_self_consistent(self) -> None:
        # The packaged file's declared hash must equal its actual body's
        # hash -- catches "edited the wording, forgot to recompute the
        # hash" at test time instead of shipping a template that can
        # never classify as MANAGED_CURRENT against itself.
        text = load_template()
        first_line, body = text.split("\n", 1)
        recorded = first_line.rsplit("sha256:", 1)[1].split(" ")[0]
        assert recorded == hashlib.sha256(body.encode("utf-8")).hexdigest()

    def test_no_nexus_specific_content(self) -> None:
        text = load_template().lower()
        for token in ("develop branch", "git-push-develop", "scripts/", "nexus-cnzei"):
            assert token not in text

    def test_installing_the_real_template_round_trips_to_current(
        self, tmp_path: Path
    ) -> None:
        # End-to-end sanity: install() against the REAL packaged template
        # (not a monkeypatched stand-in) must classify as MANAGED_CURRENT
        # immediately after.
        target = tmp_path / "PRIME.md"
        install(target)
        assert status(target) is PrimeStatus.MANAGED_CURRENT
