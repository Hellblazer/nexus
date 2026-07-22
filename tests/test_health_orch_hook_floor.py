# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3xg21: orchestration-hook plugin-floor doctor check.

An installed conexus plugin predating v6.14.0 carries no RDR-184
orchestration hook registrations — zero coverage, silently (no
EXPECT/START rows, no stop guard). The pre-floor plugin cannot warn
about itself (its hooks.json predates any warning hook), so `nx doctor`
— delivered via PyPI, independent of the plugin pin — owns the check.
"""
from __future__ import annotations

import json
from pathlib import Path

from nexus.health import (
    _ORCH_HOOKS_PLUGIN_FLOOR,
    _check_orchestration_hook_floor,
    _installed_conexus_plugin_versions,
)


def _registry(tmp_path: Path, payload: dict | str) -> Path:
    p = tmp_path / "installed_plugins.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def _v2(conexus_version: str | None) -> dict:
    plugins: dict = {
        "sn@nexus-plugins": [{"installPath": "/x/sn/1.0.0", "version": "1.0.0"}],
    }
    if conexus_version is not None:
        plugins["conexus@nexus-plugins"] = [
            {"installPath": f"/x/conexus/{conexus_version}", "version": conexus_version},
        ]
    return {"version": 2, "plugins": plugins}


class TestFloorConstant:
    def test_floor_is_6_14_0(self) -> None:
        """The hook registrations landed in the v6.14.0 lineage
        (~78bb02b6/d613f2e7); moving this constant requires knowing which
        release first shipped BOTH subagent-start-stamp and subagent-stop
        in conexus/hooks/hooks.json."""
        assert _ORCH_HOOKS_PLUGIN_FLOOR == (6, 14, 0)


class TestRegistryParsing:
    def test_reads_v2_schema(self, tmp_path: Path) -> None:
        p = _registry(tmp_path, _v2("6.16.0"))
        assert _installed_conexus_plugin_versions(p) == ["6.16.0"]

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert _installed_conexus_plugin_versions(tmp_path / "nope.json") is None

    def test_malformed_json_is_none(self, tmp_path: Path) -> None:
        assert _installed_conexus_plugin_versions(_registry(tmp_path, "{not json")) is None

    def test_no_conexus_entry_is_none(self, tmp_path: Path) -> None:
        assert _installed_conexus_plugin_versions(_registry(tmp_path, _v2(None))) is None

    def test_marketplace_suffix_variants_match(self, tmp_path: Path) -> None:
        payload = {"version": 2, "plugins": {
            "conexus@other-marketplace": [{"version": "6.15.0"}],
        }}
        assert _installed_conexus_plugin_versions(_registry(tmp_path, payload)) == ["6.15.0"]


class TestFloorCheck:
    def test_at_floor_ok(self, tmp_path: Path) -> None:
        results = _check_orchestration_hook_floor(_registry(tmp_path, _v2("6.14.0")))
        assert len(results) == 1
        assert results[0].ok is True
        assert "hooks present" in results[0].detail

    def test_above_floor_ok(self, tmp_path: Path) -> None:
        results = _check_orchestration_hook_floor(_registry(tmp_path, _v2("6.16.0")))
        assert results[0].ok is True

    def test_below_floor_warns(self, tmp_path: Path) -> None:
        results = _check_orchestration_hook_floor(_registry(tmp_path, _v2("6.11.0")))
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].warn is True
        assert results[0].fatal is False
        assert "predates" in results[0].detail
        assert any("/plugin update conexus" in s for s in results[0].fix_suggestions)

    def test_multiple_entries_newest_wins(self, tmp_path: Path) -> None:
        payload = {"version": 2, "plugins": {
            "conexus@nexus-plugins": [
                {"version": "6.11.0"},
                {"version": "6.15.0"},
            ],
        }}
        results = _check_orchestration_hook_floor(_registry(tmp_path, payload))
        assert results[0].ok is True

    def test_no_plugin_box_is_ok_not_applicable(self, tmp_path: Path) -> None:
        """A box without the Claude Code plugin at all is not in scope —
        never a warning (fail-open, informational)."""
        results = _check_orchestration_hook_floor(tmp_path / "nope.json")
        assert results[0].ok is True
        assert "not applicable" in results[0].detail

    def test_unparseable_version_is_ok_cannot_verify(self, tmp_path: Path) -> None:
        results = _check_orchestration_hook_floor(_registry(tmp_path, _v2("unknown")))
        assert results[0].ok is True
        assert "cannot verify" in results[0].detail
