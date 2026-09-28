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


def _write_marketplace_named(tmp_path: Path, name: str, plugin_names: list[str]) -> Path:
    """A marketplace.json fixture with an arbitrary top-level ``"name"``
    and plugin list -- used to build an UNRELATED marketplace (a
    different name from ``nexus.plugin_registry.MARKETPLACE_NAME``) that
    ``known_plugins()`` must refuse to trust."""
    mkt = tmp_path / f"{name}-marketplace.json"
    mkt.write_text(json.dumps({
        "name": name,
        "owner": {"name": "Test", "url": "https://example.invalid"},
        "plugins": [
            {
                "name": n,
                "source": {"source": "git-subdir", "url": "https://example.invalid/repo.git",
                           "path": n, "ref": "v0.0.0"},
                "description": "test fixture",
                "version": "0.0.0",
            }
            for n in plugin_names
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


class TestPluginRegistryRefusesAnUnrelatedMarketplace:
    """Code-review follow-up (2026-09-27): ``known_plugins()`` must not
    trust ANY well-shaped marketplace.json it happens to find -- only one
    whose own top-level ``"name"`` matches
    ``nexus.plugin_registry.MARKETPLACE_NAME`` ("nexus-plugins"). Before
    this fix the dev-checkout walk-up (and, since ``_read`` is shared,
    the env-override and installed-clone routes too) accepted ANY
    ``plugins`` list with non-empty names, including an unrelated
    marketplace's -- a real gap for a flatter/vendored install shape
    where an unrelated marketplace.json happens to sit two directories
    above this module."""

    def test_an_unrelated_marketplace_json_is_skipped(self, tmp_path, monkeypatch) -> None:
        import nexus.plugin_registry as pr

        unrelated = _write_marketplace_named(tmp_path, "some-other-marketplace", ["not-ours"])
        monkeypatch.setenv(pr.MARKETPLACE_JSON_ENV, str(unrelated))
        # Force the other two routes to miss too, so a WRONG accept of
        # the unrelated file above is the only way "not-ours" could
        # appear in the result.
        monkeypatch.setattr(
            pr, "_dev_checkout_path",
            lambda: tmp_path / "no-such-dev-checkout" / "marketplace.json",
        )

        names = pr.known_plugins(marketplaces_path=tmp_path / "no-such-known-marketplaces.json")
        assert "not-ours" not in names, (
            f"known_plugins() {names} trusted an unrelated marketplace.json (wrong "
            "top-level \"name\") instead of refusing it"
        )
        assert names == pr.FALLBACK_PLUGINS, (
            f"known_plugins() {names} should have fallen through to FALLBACK_PLUGINS "
            f"{pr.FALLBACK_PLUGINS} once the unrelated marketplace was refused and "
            "the other two routes were forced to miss"
        )


class TestPluginRegistryFallbackIsLogged:
    """Substantive-critique follow-up (2026-09-27, Issue 1): neither
    fallback leg had a test. Force every route in
    ``nexus.plugin_registry.known_plugins()`` to miss and assert the
    ``plugin_registry_marketplace_unreachable`` structlog warning fires
    -- never a silent substitution."""

    def test_unreachable_marketplace_falls_back_and_logs(self, tmp_path, monkeypatch) -> None:
        from structlog.testing import capture_logs

        import nexus.plugin_registry as pr

        monkeypatch.setenv(pr.MARKETPLACE_JSON_ENV, str(tmp_path / "missing.json"))
        monkeypatch.setattr(
            pr, "_dev_checkout_path",
            lambda: tmp_path / "no-such-dev-checkout" / "marketplace.json",
        )

        with capture_logs() as cap:
            names = pr.known_plugins(marketplaces_path=tmp_path / "no-such-known-marketplaces.json")

        assert names == pr.FALLBACK_PLUGINS
        assert any(e.get("event") == "plugin_registry_marketplace_unreachable" for e in cap), (
            f"known_plugins() fell back to {names} with no "
            f"plugin_registry_marketplace_unreachable log event -- captured: {cap}"
        )


class TestVersionLockstepHookFallbackIsNotSilent:
    """Substantive-critique follow-up (2026-09-27, Issue 1): the hook's
    fallback used to be reachable ONLY through ``debug()``, gated on
    ``NX_HOOK_DEBUG=1`` (off by default) -- contradicting the module's
    own "never a silent skip" claim. The fallback must now be
    unconditionally reported on stderr, with stdout (SessionStart's own
    JSON channel) left clean, regardless of ``NX_HOOK_DEBUG``."""

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

    def test_unreachable_marketplace_falls_back_and_warns_on_stderr(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        plugin_root = tmp_path / "conexus"  # no .claude-plugin/marketplace.json beside it
        plugin_root.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("NX_HOOK_DEBUG", raising=False)

        mod = self._load_module()
        capsys.readouterr()  # discard any import-time noise
        names = mod.known_plugins()

        assert names == mod._FALLBACK_PLUGINS
        captured = capsys.readouterr()
        assert captured.out == "", (
            "SessionStart's stdout must stay clean; the fallback notice belongs on stderr"
        )
        assert "falling back to built-in plugin set" in captured.err, (
            f"NX_HOOK_DEBUG is unset (default OFF) and the fallback still produced no "
            f"stderr notice -- stderr was: {captured.err!r}"
        )

    def test_unset_plugin_root_falls_back_and_warns(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("NX_HOOK_DEBUG", raising=False)

        mod = self._load_module()
        capsys.readouterr()
        names = mod.known_plugins()

        assert names == mod._FALLBACK_PLUGINS
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "CLAUDE_PLUGIN_ROOT unset" in captured.err


class TestFallbackPluginsStayInSync:
    """Substantive-critique follow-up (2026-09-27, Issue 2, "ironic given
    the bead's premise"): this fix introduced a NEW hand-kept pair --
    ``nexus.plugin_registry.FALLBACK_PLUGINS`` and the hook's own
    ``_FALLBACK_PLUGINS`` -- that must agree with each other AND with
    marketplace.json's real, CURRENT plugin set, read live here rather
    than hardcoded in this test. A plugin added to marketplace.json with
    neither fallback updated would otherwise go unnoticed on the one path
    both derivations take when marketplace.json is unreachable (an
    installed, non-dev-checkout box with no marketplace clone either)."""

    SCRIPT = (
        Path(__file__).resolve().parents[1]
        / "conexus" / "hooks" / "scripts" / "version_lockstep_hook.py"
    )

    def _load_hook_module(self):
        spec = importlib.util.spec_from_file_location("version_lockstep_hook", self.SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_two_fallback_tuples_agree(self) -> None:
        from nexus.plugin_registry import FALLBACK_PLUGINS

        hook = self._load_hook_module()
        assert set(hook._FALLBACK_PLUGINS) == set(FALLBACK_PLUGINS), (
            f"version_lockstep_hook._FALLBACK_PLUGINS {hook._FALLBACK_PLUGINS} and "
            f"nexus.plugin_registry.FALLBACK_PLUGINS {FALLBACK_PLUGINS} disagree -- "
            "both are read on the SAME unreachable-marketplace.json path and must "
            "name the same plugin set"
        )

    def test_both_fallbacks_match_the_repos_real_marketplace_json(self) -> None:
        from nexus.plugin_registry import FALLBACK_PLUGINS

        marketplace = json.loads(
            (Path(__file__).resolve().parents[1] / ".claude-plugin" / "marketplace.json")
            .read_text(encoding="utf-8")
        )
        real_names = {
            p["name"] for p in marketplace["plugins"]
            if isinstance(p, dict) and p.get("name")
        }

        hook = self._load_hook_module()
        assert set(FALLBACK_PLUGINS) == real_names, (
            f"nexus.plugin_registry.FALLBACK_PLUGINS {FALLBACK_PLUGINS} no longer "
            f"matches marketplace.json's real plugin set {sorted(real_names)} -- a "
            "plugin was added/removed there and the fallback needs updating"
        )
        assert set(hook._FALLBACK_PLUGINS) == real_names, (
            f"version_lockstep_hook._FALLBACK_PLUGINS {hook._FALLBACK_PLUGINS} no "
            f"longer matches marketplace.json's real plugin set {sorted(real_names)} "
            "-- a plugin was added/removed there and the fallback needs updating"
        )
