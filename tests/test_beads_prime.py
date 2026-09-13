# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-level ``beads`` PRIME.md management (nexus-cnzei.8).

Covers path resolution per platform (Go ``os.UserConfigDir`` semantics),
beads detection (``bd`` on PATH or the beads Claude Code plugin), the four
:class:`~nexus.beads_prime.PrimeStatus` states, install idempotence, the
never-overwrite-user-authored guarantee, and the marker-version upgrade
path. All filesystem interaction is confined to ``tmp_path`` — nothing here
touches a real ``HOME``/``~/.config``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.beads_prime import (
    PrimeStatus,
    beads_detected,
    install,
    load_template,
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
        # No platform/home/environ passed — must resolve without raising,
        # using the real ambient values.
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
        claude_dir = tmp_path / ".claude"
        plugin_dir = claude_dir / "plugins" / "marketplaces" / "beads-marketplace" / "beads" / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text("{}")
        present, reason = beads_detected(
            which=lambda _name: None, claude_config_dir=claude_dir
        )
        assert present is True
        assert "plugin" in reason

    def test_plugin_under_cache_versioned_detected(self, tmp_path: Path) -> None:
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

    def test_marker_with_stale_content_is_stale(self, tmp_path: Path) -> None:
        p = tmp_path / "PRIME.md"
        template = load_template()
        first_line = template.split("\n", 1)[0]
        p.write_text(first_line + "\nSTALE BODY, not the packaged template.\n")
        assert status(p) is PrimeStatus.MANAGED_STALE

    def test_marker_older_version_is_stale(self, tmp_path: Path) -> None:
        p = tmp_path / "PRIME.md"
        p.write_text("<!-- conexus-managed beads PRIME v0 -->\nold body\n")
        assert status(p) is PrimeStatus.MANAGED_STALE


# ── install ───────────────────────────────────────────────────────────────────


class TestInstall:
    def test_installs_when_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "beads" / "PRIME.md"
        action, path = install(target)
        assert action == "installed"
        assert path == target
        assert target.read_text() == load_template()

    def test_idempotent_second_call_is_up_to_date(self, tmp_path: Path) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        action, _path = install(target)
        assert action == "up to date"
        assert target.read_text() == load_template()

    def test_never_overwrites_user_authored_file(self, tmp_path: Path) -> None:
        target = tmp_path / "PRIME.md"
        target.write_text("# hand-written, do not touch\n")
        action, _path = install(target)
        assert action == "left alone (user-authored)"
        assert target.read_text() == "# hand-written, do not touch\n"

    def test_marker_version_upgrade_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        target.write_text("<!-- conexus-managed beads PRIME v1 -->\nold body\n")

        new_template = "<!-- conexus-managed beads PRIME v2 -->\nnew body\n"
        monkeypatch.setattr(
            "nexus.beads_prime.load_template", lambda: new_template
        )

        assert status(target) is PrimeStatus.MANAGED_STALE
        action, _path = install(target)
        assert action == "updated"
        assert target.read_text() == new_template

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
        action, path = install()
        assert action == "installed"
        assert path == tmp_path / "PRIME.md"


# ── packaged template sanity ─────────────────────────────────────────────────


class TestPackagedTemplate:
    def test_under_1024_bytes(self) -> None:
        assert len(load_template().encode("utf-8")) < 1024

    def test_no_git_add(self) -> None:
        assert "git add" not in load_template()

    def test_starts_with_recognized_marker(self) -> None:
        first_line = load_template().split("\n", 1)[0]
        assert first_line.startswith("<!-- conexus-managed beads PRIME v")
        assert first_line.endswith("-->")

    def test_no_nexus_specific_content(self) -> None:
        text = load_template().lower()
        for token in ("develop branch", "git-push-develop", "scripts/", "nexus-cnzei"):
            assert token not in text
