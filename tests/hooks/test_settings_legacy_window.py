# SPDX-License-Identifier: AGPL-3.0-or-later
"""The legacy-marker window closes on schedule (nexus-sby49.8, RDR-259 Decision 7).

``_audience.py`` reads two old markers as ``subagent_git: strict`` for two client releases after the one that ships RDR-259.
``LEGACY_ACCEPTED_UNTIL`` is that version. This test reads the plugin's version from ``conexus/.claude-plugin/plugin.json`` and
fails once it reaches the constant while a marker is still read, so removing the markers (Stage 4), or moving the constant on
purpose, is a visible diff. The check itself is a function, exercised below with made-up versions so a test that stopped
looking would fail here.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_JSON = ROOT / "conexus" / ".claude-plugin" / "plugin.json"
COPIES = {
    "plugin": ROOT / "conexus" / "hooks" / "scripts" / "_audience.py",
    "src": ROOT / "src" / "nexus" / "hooks" / "_audience.py",
}


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def window_violation(version: str, until: str, markers: "tuple[str, ...]") -> "str | None":
    """Why the window is overdue, or None: the plugin has reached *until* and a marker is still read."""
    if markers and _version(version) >= _version(until):
        return (
            f"plugin {version} has reached LEGACY_ACCEPTED_UNTIL {until} and {len(markers)} legacy marker(s) are still read "
            f"({', '.join(markers)}): remove them (RDR-259 Stage 4) or move the constant on purpose"
        )
    return None


@pytest.fixture(params=sorted(COPIES))
def audience(request):
    spec = importlib.util.spec_from_file_location("_audience_window_" + request.param, COPIES[request.param])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_check_fails_on_and_after_the_version_and_passes_before_it() -> None:
    markers = (".nexus/dev-process.json",)
    assert window_violation("8.5.10", "8.5.11", markers) is None
    assert window_violation("8.5.11", "8.5.11", markers) is not None
    assert window_violation("8.6.0", "8.5.11", markers) is not None
    assert window_violation("9.0.0", "8.5.11", markers) is not None
    assert window_violation("8.5.9", "8.5.11", markers) is None
    assert window_violation("8.5.11", "8.5.11", ()) is None, "markers removed: the window is closed on schedule"
    assert window_violation("8.5.100", "8.5.11", markers) is not None, "versions compare as numbers, not strings"


def test_the_plugin_has_not_outlived_the_window_while_a_marker_is_still_read(audience) -> None:
    version = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    assert window_violation(version, audience.LEGACY_ACCEPTED_UNTIL, tuple(audience.LEGACY_MARKERS)) is None


def test_the_constant_is_a_version_two_client_releases_past_the_shipping_one(audience) -> None:
    """8.5.8 is the plugin-only fast lane; Stages 1 to 3 ship in the next client release (8.5.9, patch cadence); two client
    releases after that is 8.5.11 (coordinator decision, nexus-sby49.8; nexus-sby49.18 rechecks it at the cut)."""
    assert audience.LEGACY_ACCEPTED_UNTIL == "8.5.11"
    assert _version(audience.LEGACY_ACCEPTED_UNTIL) > _version(json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"])


def test_the_two_markers_are_the_ones_the_window_covers(audience) -> None:
    assert tuple(audience.LEGACY_MARKERS) == (".nexus/dev-process.json", "scripts/agent-worktree-preflight.sh")
