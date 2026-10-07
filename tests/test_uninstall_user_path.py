# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx uninstall`` takes back the Windows user PATH entry (RDR-224, nexus-7xzc1).

Measured on nx-clean-win11 (T2 ``nexus_rdr/224-windows-self-install``):
``nx self install`` put ``<tools>\\current\\bin`` first on ``HKCU\\Environment``
``Path`` and ``nx uninstall --yes --remove-data`` left it there. The platform
is injected and a file stands in for the registry (``NX_USER_PATH_STORE``), so
these run on every host and never touch a real registry.
"""
from __future__ import annotations

import functools
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus._install import generation_core as gen_core
from nexus.commands import uninstall as uninstall_mod


@pytest.fixture
def path_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    tools = tmp_path / "tools"
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    store = tmp_path / "userpath.txt"
    monkeypatch.setenv(gen_core.USER_PATH_STORE_ENV, str(store))
    entry = str(gen_core.current_launcher_dir())
    assert entry == str(tools / "current" / "bin")
    return store, entry


class TestTeardownUserPath:
    def test_confirm_removes_only_the_generation_entry(self, path_store) -> None:
        store, entry = path_store
        store.write_text(f"{entry};C:\\Windows;%USERPROFILE%\\bin\n")
        lines, warnings = uninstall_mod._teardown_user_path(confirm=True, platform="win32")
        assert warnings == []
        assert len(lines) == 1 and f"removed {entry}" in lines[0]
        assert store.read_text().strip() == "C:\\Windows;%USERPROFILE%\\bin"

    def test_dry_run_previews_and_writes_nothing(self, path_store) -> None:
        store, entry = path_store
        store.write_text(f"{entry};C:\\Windows\n")
        lines, _ = uninstall_mod._teardown_user_path(confirm=False, platform="win32")
        assert len(lines) == 1 and f"would remove {entry}" in lines[0]
        assert store.read_text().strip() == f"{entry};C:\\Windows"

    def test_absent_entry_is_silent(self, path_store) -> None:
        store, _ = path_store
        store.write_text("C:\\Windows\n")
        assert uninstall_mod._teardown_user_path(confirm=True, platform="win32") == ([], [])

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_posix_has_no_path_edit_to_revert(self, path_store, platform: str) -> None:
        """The POSIX layout puts shims in uv's bin dir and edits no PATH and no
        shell rc file; uninstall touches nothing here."""
        store, entry = path_store
        store.write_text(f"{entry}\n")
        assert uninstall_mod._teardown_user_path(confirm=True, platform=platform) == ([], [])
        assert store.read_text().strip() == entry

    def test_an_unwritable_store_is_a_warning_naming_the_entry(
        self, path_store, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, entry = path_store

        def _refuse(*_a, **_kw):
            raise gen_core.GenerationError("could not write the user PATH (x): denied")

        monkeypatch.setattr(gen_core, "remove_user_path", _refuse)
        lines, warnings = uninstall_mod._teardown_user_path(confirm=True, platform="win32")
        assert lines == []
        assert len(warnings) == 1 and entry in warnings[0]


class TestUninstallCommandWiring:
    def test_nx_uninstall_yes_removes_the_entry_and_says_so(
        self, path_store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        store, entry = path_store
        store.write_text(f"C:\\a;{entry}\n")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr(uninstall_mod, "_local_service_present", lambda: False)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: tmp_path / "PRIME.md")
        monkeypatch.setattr(
            uninstall_mod, "_teardown_user_path",
            functools.partial(uninstall_mod._teardown_user_path, platform="win32"),
        )
        result = CliRunner().invoke(uninstall_mod.uninstall_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert f"User PATH: removed {entry}" in result.output
        assert "Nothing to uninstall" not in result.output
        assert store.read_text().strip() == "C:\\a"
