# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7lb9: Bindings CRUD MCP tools.

Three concerns under one bead:

1. ``Binding`` gains an ``enabled: bool`` field (default True) so
   ``binding_toggle`` has a real flag to flip. Disabled bindings are
   loaded but skipped in ``_BindingWatcher._dispatch_event``.

2. New helper module ``nexus.cockpit.bindings_crud`` with four
   functions (``create_binding``, ``list_bindings``,
   ``toggle_binding``, ``delete_binding``) that operate on YAML files
   in a user-profiles directory, separate from the shipped builtin
   profiles under ``nx/tuplespace/builtin/bindings/profiles/`` so
   operators can edit without clobbering checked-in defaults.

3. ``_BindingWatcher`` reloads profiles when any source file's mtime
   changes, so CRUD writes take effect without a daemon restart.

The MCP tool wrappers in ``nexus.mcp.core`` are thin (JSON in / JSON
out around the helper functions) and tested via integration.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Concern 1: Binding gains an ``enabled`` field
# ---------------------------------------------------------------------------


class TestBindingEnabledField:
    """Binding dataclass exposes ``enabled``; default True; YAML round-trips."""

    def test_binding_default_enabled_is_true(self) -> None:
        from nexus.cockpit.bindings import Action, Binding
        b = Binding(
            name="b1",
            match={"subspace": "tasks/*"},
            action=Action(kind="log", target="hello"),
        )
        assert b.enabled is True

    def test_binding_enabled_false_round_trip(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings import load_profile

        yml = tmp_path / "p.yml"
        yml.write_text(yaml.safe_dump({
            "profile": "p1",
            "bindings": [
                {
                    "name": "b-on",
                    "match": {"subspace": "tasks/x"},
                    "action": {"kind": "log", "marker": "ON"},
                },
                {
                    "name": "b-off",
                    "enabled": False,
                    "match": {"subspace": "tasks/y"},
                    "action": {"kind": "log", "marker": "OFF"},
                },
            ],
        }))
        prof = load_profile(yml)
        by_name = {b.name: b for b in prof.bindings}
        assert by_name["b-on"].enabled is True
        assert by_name["b-off"].enabled is False

    def test_disabled_binding_does_not_dispatch(
        self, tmp_path: Path
    ) -> None:
        """``_BindingWatcher._dispatch_event`` skips bindings with enabled=False."""
        from nexus.cockpit.bindings import (
            Action,
            Binding,
            BindingContext,
            BindingProfile,
            EventRecord,
            BindingWatcher,
        )

        fired: list[str] = []

        # Use a python action so we can verify firing.
        import sys as _sys

        def _record(event, binding, context):  # noqa: ARG001
            fired.append(binding.name)

        # Stash the callable so the YAML resolver can find it.
        mod = type(_sys)("_test_bindings_crud_disabled")
        mod.record = _record  # type: ignore[attr-defined]
        _sys.modules["_test_bindings_crud_disabled"] = mod

        b_on = Binding(
            name="b-on",
            match={"subspace": "tasks/x"},
            action=Action(
                kind="python", target="_test_bindings_crud_disabled:record"
            ),
            enabled=True,
        )
        b_off = Binding(
            name="b-off",
            match={"subspace": "tasks/x"},
            action=Action(
                kind="python", target="_test_bindings_crud_disabled:record"
            ),
            enabled=False,
        )
        prof = BindingProfile(name="p1", bindings=(b_on, b_off))

        import sqlite3 as _sqlite3
        tuples_conn = _sqlite3.connect(":memory:")
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[prof],
            context=BindingContext(conn=tuples_conn, index=None, registry=None),
        )
        evt = EventRecord(
            cursor=1, subspace="tasks/x", op="out", tuple_id="t1",
            payload_summary=None, category="data", ts=0.0,
        )

        import asyncio
        asyncio.run(watcher._dispatch_event(prof, evt))

        assert fired == ["b-on"], (
            f"only the enabled binding should fire; got {fired}"
        )


# ---------------------------------------------------------------------------
# Concern 2: bindings_crud helpers
# ---------------------------------------------------------------------------


class TestBindingsCrudHelpers:
    """``create_binding`` / ``list_bindings`` / ``toggle_binding`` /
    ``delete_binding`` operate on YAML files in a user-profiles dir.
    """

    def test_user_profiles_dir_under_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``user_profiles_dir()`` returns a path under ~/.config/nexus."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from nexus.cockpit.bindings import user_profiles_dir
        path = user_profiles_dir()
        # Resolve to handle macOS /var → /private/var symlink.
        assert path.resolve() == (
            tmp_path / ".config" / "nexus" / "bindings" / "profiles"
        ).resolve()

    def test_create_binding_writes_yaml(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings_crud import create_binding

        create_binding(
            profile="ops",
            name="alert-on-test-fail",
            match={"subspace": "hook_events/posttooluse", "category": "data"},
            action={"kind": "log", "marker": "test-failure-detected"},
            profiles_dir=tmp_path,
        )
        ops_yml = tmp_path / "ops.yml"
        assert ops_yml.exists()
        body = yaml.safe_load(ops_yml.read_text())
        assert body["profile"] == "ops"
        assert len(body["bindings"]) == 1
        assert body["bindings"][0]["name"] == "alert-on-test-fail"
        assert body["bindings"][0]["enabled"] is True

    def test_create_binding_appends_to_existing_profile(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings_crud import create_binding

        for name in ("b1", "b2"):
            create_binding(
                profile="ops",
                name=name,
                match={"subspace": "tasks/*"},
                action={"kind": "log", "marker": name},
                profiles_dir=tmp_path,
            )
        body = yaml.safe_load((tmp_path / "ops.yml").read_text())
        assert [b["name"] for b in body["bindings"]] == ["b1", "b2"]

    def test_create_binding_rejects_duplicate_name(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings import BindingProfileError
        from nexus.cockpit.bindings_crud import create_binding

        create_binding(
            profile="ops", name="dup",
            match={"subspace": "x/y"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        with pytest.raises(BindingProfileError, match="duplicate"):
            create_binding(
                profile="ops", name="dup",
                match={"subspace": "x/y"},
                action={"kind": "log", "marker": "m"},
                profiles_dir=tmp_path,
            )

    def test_list_bindings_returns_all_with_attribution(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings_crud import create_binding, list_bindings

        create_binding(
            profile="ops", name="b1",
            match={"subspace": "tasks/x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        create_binding(
            profile="dev", name="b2",
            match={"subspace": "tasks/y"},
            action={"kind": "log", "marker": "m"},
            enabled=False,
            profiles_dir=tmp_path,
        )
        result = list_bindings(profiles_dir=tmp_path)
        assert {(b["profile"], b["name"]) for b in result} == {
            ("ops", "b1"), ("dev", "b2"),
        }
        for b in result:
            assert "enabled" in b
            assert "match" in b
            assert "action" in b

    def test_list_bindings_filter_by_profile(
        self, tmp_path: Path
    ) -> None:
        from nexus.cockpit.bindings_crud import create_binding, list_bindings
        create_binding(
            profile="ops", name="b1",
            match={"subspace": "tasks/x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        create_binding(
            profile="dev", name="b2",
            match={"subspace": "tasks/y"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        result = list_bindings(profile="ops", profiles_dir=tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "b1"

    def test_list_bindings_enabled_only(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings_crud import create_binding, list_bindings
        create_binding(
            profile="ops", name="b-on",
            match={"subspace": "x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        create_binding(
            profile="ops", name="b-off",
            match={"subspace": "x"},
            action={"kind": "log", "marker": "m"},
            enabled=False,
            profiles_dir=tmp_path,
        )
        result = list_bindings(enabled_only=True, profiles_dir=tmp_path)
        assert [b["name"] for b in result] == ["b-on"]

    def test_toggle_binding_flips_enabled(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings_crud import (
            create_binding,
            list_bindings,
            toggle_binding,
        )
        create_binding(
            profile="ops", name="b1",
            match={"subspace": "x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        toggle_binding("ops", "b1", enabled=False, profiles_dir=tmp_path)
        result = list_bindings(profiles_dir=tmp_path)
        assert result[0]["enabled"] is False
        toggle_binding("ops", "b1", enabled=True, profiles_dir=tmp_path)
        result = list_bindings(profiles_dir=tmp_path)
        assert result[0]["enabled"] is True

    def test_toggle_binding_unknown_raises(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings_crud import toggle_binding
        with pytest.raises(KeyError, match="ops"):
            toggle_binding("ops", "b1", enabled=True, profiles_dir=tmp_path)

    def test_delete_binding_removes_row(self, tmp_path: Path) -> None:
        from nexus.cockpit.bindings_crud import (
            create_binding,
            delete_binding,
            list_bindings,
        )
        create_binding(
            profile="ops", name="b1",
            match={"subspace": "x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        create_binding(
            profile="ops", name="b2",
            match={"subspace": "x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        delete_binding("ops", "b1", profiles_dir=tmp_path)
        result = list_bindings(profiles_dir=tmp_path)
        assert [b["name"] for b in result] == ["b2"]

    def test_delete_last_binding_removes_profile_file(
        self, tmp_path: Path
    ) -> None:
        """When the last binding in a profile is deleted, the YAML file
        is removed so ``load_profiles_dir`` doesn't surface an empty
        profile (which would fail validation).
        """
        from nexus.cockpit.bindings_crud import create_binding, delete_binding
        create_binding(
            profile="ops", name="solo",
            match={"subspace": "x"},
            action={"kind": "log", "marker": "m"},
            profiles_dir=tmp_path,
        )
        delete_binding("ops", "solo", profiles_dir=tmp_path)
        assert not (tmp_path / "ops.yml").exists()


# ---------------------------------------------------------------------------
# Concern 3: _BindingWatcher reloads on mtime change
# ---------------------------------------------------------------------------


class TestBindingWatcherReload:
    """Watcher re-loads profiles when any source file mtime changes."""

    def test_reload_picks_up_new_profile_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drop a new profile YAML into the user dir; watcher reloads it."""
        from nexus.cockpit.bindings import (
            BindingContext,
            BindingWatcher,
        )

        tuples_conn = sqlite3.connect(":memory:")
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[],
            context=BindingContext(conn=tuples_conn, index=None, registry=None),
            profiles_dirs=[tmp_path],
        )
        # Initial: no profiles.
        assert watcher._profiles == ()

        # Create a new profile file.
        (tmp_path / "ops.yml").write_text(yaml.safe_dump({
            "profile": "ops",
            "bindings": [{
                "name": "b1",
                "match": {"subspace": "tasks/x"},
                "action": {"kind": "log", "marker": "m"},
            }],
        }))
        # Force at least 1ms mtime granularity bump.
        time.sleep(0.01)
        watcher._reload_if_changed()

        names = [p.name for p in watcher._profiles]
        assert names == ["ops"]

    def test_reload_picks_up_modified_profile(
        self, tmp_path: Path
    ) -> None:
        """Modify an existing profile; watcher reloads with new contents."""
        from nexus.cockpit.bindings import (
            BindingContext,
            BindingWatcher,
            load_profile,
        )

        yml = tmp_path / "ops.yml"
        yml.write_text(yaml.safe_dump({
            "profile": "ops",
            "bindings": [{
                "name": "b1",
                "match": {"subspace": "x"},
                "action": {"kind": "log", "marker": "v1"},
            }],
        }))

        tuples_conn = sqlite3.connect(":memory:")
        prof = load_profile(yml)
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[prof],
            context=BindingContext(conn=tuples_conn, index=None, registry=None),
            profiles_dirs=[tmp_path],
        )
        assert watcher._profiles[0].bindings[0].action.target == "v1"

        # Update content and bump mtime.
        time.sleep(0.01)
        yml.write_text(yaml.safe_dump({
            "profile": "ops",
            "bindings": [{
                "name": "b1",
                "match": {"subspace": "x"},
                "action": {"kind": "log", "marker": "v2"},
            }],
        }))
        watcher._reload_if_changed()
        assert watcher._profiles[0].bindings[0].action.target == "v2"

    def test_reload_tolerates_malformed_yaml(self, tmp_path: Path) -> None:
        """nexus-0cf1.2 (TR-2): a broken YAML in the user dir must not
        brick the watcher. Previous profile list is retained; a
        warning logs.
        """
        from nexus.cockpit.bindings import (
            BindingContext,
            BindingWatcher,
            load_profile,
        )

        yml = tmp_path / "ops.yml"
        yml.write_text(yaml.safe_dump({
            "profile": "ops",
            "bindings": [{
                "name": "b1",
                "match": {"subspace": "x"},
                "action": {"kind": "log", "marker": "v1"},
            }],
        }))

        tuples_conn = sqlite3.connect(":memory:")
        prof = load_profile(yml)
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[prof],
            context=BindingContext(conn=tuples_conn, index=None, registry=None),
            profiles_dirs=[tmp_path],
        )
        assert len(watcher._profiles) == 1
        original_target = watcher._profiles[0].bindings[0].action.target
        assert original_target == "v1"

        # Now corrupt the YAML: invalid syntax (unbalanced bracket).
        time.sleep(0.01)
        yml.write_text("profile: ops\nbindings: [{name: b1, match: {\n")
        watcher._reload_if_changed()

        # Previous profile list is retained (the watcher did NOT brick).
        assert len(watcher._profiles) == 1, (
            "malformed YAML must NOT clear the profile list; the "
            "watcher should retain the prior good state."
        )
        # The retained profile is the SAME one (action.target unchanged).
        assert watcher._profiles[0].bindings[0].action.target == "v1"

    def test_reload_no_op_when_unchanged(self, tmp_path: Path) -> None:
        """No mtime change → no reload (cheap repeated calls)."""
        from nexus.cockpit.bindings import BindingContext, BindingWatcher

        tuples_conn = sqlite3.connect(":memory:")
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[],
            context=BindingContext(conn=tuples_conn, index=None, registry=None),
            profiles_dirs=[tmp_path],
        )
        # Initial reload caches the mtime fingerprint.
        watcher._reload_if_changed()
        # Track how many times load_profile gets invoked.
        from nexus.cockpit import bindings as _bindings
        original = _bindings.load_profile
        call_count = {"n": 0}

        def _counting(path):
            call_count["n"] += 1
            return original(path)

        try:
            _bindings.load_profile = _counting  # type: ignore[assignment]
            watcher._reload_if_changed()
            watcher._reload_if_changed()
        finally:
            _bindings.load_profile = original  # type: ignore[assignment]

        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# Concern 4: MCP tool wrappers (light smoke; full coverage via helpers above)
# ---------------------------------------------------------------------------


class TestMcpToolWrappers:
    """Smoke tests: MCP tool functions exist and round-trip JSON."""

    def test_binding_mcp_tools_registered(self) -> None:
        from nexus.mcp import core as mcp_core
        for name in (
            "binding_create",
            "binding_list",
            "binding_toggle",
            "binding_delete",
        ):
            assert hasattr(mcp_core, name), (
                f"missing MCP tool: {name}"
            )

    def test_binding_create_then_list_via_mcp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end MCP-level round-trip: create → list."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from nexus.mcp.core import binding_create, binding_list

        raw = binding_create(
            profile="ops",
            name="alert",
            match='{"subspace": "x"}',
            action='{"kind": "log", "marker": "m"}',
        )
        result = json.loads(raw)
        assert result["created"] is True
        assert result["profile"] == "ops"
        assert result["name"] == "alert"

        listing = json.loads(binding_list())
        assert any(
            b["profile"] == "ops" and b["name"] == "alert" for b in listing
        )

    def test_binding_toggle_then_delete_via_mcp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        from nexus.mcp.core import (
            binding_create,
            binding_delete,
            binding_list,
            binding_toggle,
        )

        binding_create(
            profile="ops",
            name="alert",
            match='{"subspace": "x"}',
            action='{"kind": "log", "marker": "m"}',
        )
        # Toggle off
        toggled = json.loads(
            binding_toggle(profile="ops", name="alert", enabled=False)
        )
        assert toggled["enabled"] is False

        listing = json.loads(binding_list(enabled_only=True))
        assert listing == []

        # Delete
        deleted = json.loads(binding_delete(profile="ops", name="alert"))
        assert deleted["deleted"] is True
        listing = json.loads(binding_list())
        assert listing == []
