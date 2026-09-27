# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-smsau: marketplace.json is the single source of truth for the
plugin set. Before this bead, three sites hand-kept a fixed
``("conexus", "sn")`` tuple/pair and silently dropped any plugin added to
``.claude-plugin/marketplace.json`` without a matching source edit:

* the ``PLUGINS`` tuple in ``nexus.plugin_lockstep`` (consumed with
  ``if plugin_short not in PLUGINS: continue``)
* the identical tuple in
  ``conexus/hooks/scripts/version_lockstep_hook.py`` (a stdlib-only copy
  that cannot import ``nexus`` at all)
* the fixed ``for plugin in ("conexus", "sn"):`` loop in
  ``nexus.routing_stats.registered_rules``

This file builds ONE synthetic marketplace.json naming a THIRD plugin
and asserts each site's derived plugin set includes it. Restoring any of
the three old hardcoded tuples/loops makes the corresponding test below
fail (verified: see the docstring on each test).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SYNTHETIC_PLUGIN = "zzz-synthetic"


def _write_marketplace(tmp_path: Path) -> Path:
    """A marketplace.json fixture naming conexus, sn, and one synthetic
    third plugin -- same shape ``.claude-plugin/marketplace.json`` uses
    in this repo (a git-subdir source object), trimmed to what
    ``_plugin_names`` / ``known_plugins`` actually read (each plugin's
    ``name``)."""
    mkt = tmp_path / "marketplace.json"
    mkt.write_text(json.dumps({
        "name": "nexus-plugins",
        "owner": {"name": "Test", "url": "https://example.invalid"},
        "plugins": [
            {
                "name": "conexus",
                "source": {"source": "git-subdir", "url": "https://example.invalid/repo.git",
                           "path": "conexus", "ref": "v0.0.0"},
                "description": "test fixture",
                "version": "0.0.0",
            },
            {
                "name": "sn",
                "source": {"source": "git-subdir", "url": "https://example.invalid/repo.git",
                           "path": "sn", "ref": "v0.0.0"},
                "description": "test fixture",
                "version": "0.0.0",
            },
            {
                "name": SYNTHETIC_PLUGIN,
                "source": {"source": "git-subdir", "url": "https://example.invalid/repo.git",
                           "path": SYNTHETIC_PLUGIN, "ref": "v0.0.0"},
                "description": "synthetic third plugin (nexus-smsau acceptance test)",
                "version": "0.0.0",
            },
        ],
    }))
    return mkt


class TestPluginRegistryDerivesTheThirdPlugin:
    """``nexus.plugin_registry.known_plugins`` -- the wheel-side helper
    ``nexus.plugin_lockstep`` and ``nexus.routing_stats`` share."""

    def test_known_plugins_includes_a_synthetic_third_plugin(self, tmp_path, monkeypatch) -> None:
        mkt = _write_marketplace(tmp_path)
        monkeypatch.setenv("NX_MARKETPLACE_JSON", str(mkt))
        from nexus.plugin_registry import known_plugins

        names = known_plugins()
        assert SYNTHETIC_PLUGIN in names, (
            f"known_plugins() {names} dropped {SYNTHETIC_PLUGIN!r} even though "
            "marketplace.json lists it"
        )


class TestPluginLockstepKeepsTheThirdPlugin:
    """``nexus.plugin_lockstep.registry_entries`` -- consumed with
    ``if plugin_short not in <derived set>: continue``. With the old
    hardcoded ``PLUGINS = ("conexus", "sn")`` tuple this test fails: the
    synthetic plugin's registry key is silently dropped."""

    def test_registry_entries_keeps_a_synthetic_third_plugin(self, tmp_path, monkeypatch) -> None:
        mkt = _write_marketplace(tmp_path)
        monkeypatch.setenv("NX_MARKETPLACE_JSON", str(mkt))

        registry = tmp_path / "installed_plugins.json"
        registry.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "conexus@nexus-plugins": [{"version": "0.0.0"}],
                f"{SYNTHETIC_PLUGIN}@nexus-plugins": [{"version": "0.0.0"}],
            },
        }))

        from nexus import plugin_lockstep as pl

        entries = pl.registry_entries(registry)
        assert entries is not None
        assert f"{SYNTHETIC_PLUGIN}@nexus-plugins" in entries, (
            f"registry_entries() {sorted(entries)} dropped a plugin marketplace.json "
            "lists (nexus-smsau); the old hardcoded PLUGINS tuple only ever "
            "recognised conexus/sn"
        )


class TestVersionLockstepHookKeepsTheThirdPlugin:
    """The stdlib-only SessionStart hook's own copy of the derivation
    (it cannot import ``nexus`` at all -- see the hook's module
    docstring). With the old hardcoded ``PLUGINS = ("conexus", "sn")``
    tuple this test fails identically to the plugin_lockstep test above."""

    SCRIPT = (
        Path(__file__).resolve().parents[1]
        / "conexus" / "hooks" / "scripts" / "version_lockstep_hook.py"
    )

    def _load_module(self):
        spec = importlib.util.spec_from_file_location("version_lockstep_hook", self.SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_known_plugins_includes_a_synthetic_third_plugin(self, tmp_path, monkeypatch) -> None:
        mkt_dir = tmp_path / "clone"
        (mkt_dir / ".claude-plugin").mkdir(parents=True)
        # Reuse the real fixture writer, then move it into place under
        # <clone>/.claude-plugin/marketplace.json (the shape
        # _marketplace_json_path derives from CLAUDE_PLUGIN_ROOT).
        mkt = _write_marketplace(tmp_path)
        (mkt_dir / ".claude-plugin" / "marketplace.json").write_text(mkt.read_text())

        plugin_root = mkt_dir / "conexus"
        plugin_root.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        mod = self._load_module()
        names = mod.known_plugins()
        assert SYNTHETIC_PLUGIN in names, (
            f"version_lockstep_hook.known_plugins() {names} dropped "
            f"{SYNTHETIC_PLUGIN!r} even though marketplace.json lists it"
        )

    def test_our_plugin_shas_keeps_a_synthetic_third_plugin(self, tmp_path, monkeypatch) -> None:
        mkt_dir = tmp_path / "clone"
        (mkt_dir / ".claude-plugin").mkdir(parents=True)
        mkt = _write_marketplace(tmp_path)
        (mkt_dir / ".claude-plugin" / "marketplace.json").write_text(mkt.read_text())

        plugin_root = mkt_dir / "conexus"
        plugin_root.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        registry = tmp_path / "installed_plugins.json"
        registry.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "conexus@nexus-plugins": [{"version": "0.0.0", "gitCommitSha": "a" * 40}],
                f"{SYNTHETIC_PLUGIN}@nexus-plugins": [{"version": "0.0.0", "gitCommitSha": "b" * 40}],
            },
        }))
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))

        mod = self._load_module()
        shas = mod._our_plugin_shas()
        assert f"{SYNTHETIC_PLUGIN}@nexus-plugins" in shas, (
            f"_our_plugin_shas() {sorted(shas)} dropped a plugin marketplace.json "
            "lists (nexus-smsau); the old hardcoded PLUGINS tuple only ever "
            "recognised conexus/sn"
        )


class TestRoutingStatsFindsTheThirdPluginsHooks:
    """``nexus.routing_stats.registered_rules`` -- used to loop a fixed
    ``("conexus", "sn")`` pair, so a routing rule shipped by a third
    plugin would never be aggregated. With that fixed loop restored this
    test fails: the synthetic plugin's hooks.json is never even probed."""

    def test_registered_rules_finds_a_synthetic_third_plugins_routing_rule(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("NX_MARKETPLACE_JSON", str(_write_marketplace(tmp_path)))

        repo_root = tmp_path / "repo"
        hooks_dir = repo_root / SYNTHETIC_PLUGIN / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"command": "python3 conexus/hooks/scripts/routing/zzz_rule.py"}]}
                ]
            }
        }))

        from nexus.routing_stats import registered_rules

        rules = registered_rules(
            repo_root=repo_root,
            marketplace_dir=tmp_path / "no-such-marketplace-clone",
        )
        assert rules is not None and "zzz_rule" in rules, (
            f"registered_rules() {rules} ignored a plugin marketplace.json lists "
            "(nexus-smsau); the old hardcoded ('conexus', 'sn') loop never looked "
            "inside a third plugin's directory at all"
        )
