# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-125 cross-plugin aggregate-cap lint.

RDR-121 § Performance Expectations established a 4-hook cap on the
PreToolUse:Bash matcher: the <300ms p95 cumulative budget assumes at
most four routing hooks fire sequentially per Bash call. After
RDR-125 migrates rules into the plugin that owns each redirect
target, that cap becomes an aggregate across all installed plugins,
not a per-plugin count -- Claude Code merges hook registrations from
every plugin and fires them in sequence.

This lint computes the union and refuses to commit when it exceeds
four. Adding a fifth routing hook in ANY plugin (nx, sn, or a future
plugin) requires either consolidation or a budget revision in a
successor RDR.

Currently in scope: ``conexus/hooks/scripts/routing/registry.yaml``. sn's
registry was deleted with its unregistered grep redirect (nexus-jbt5x; the
hook itself left hooks.json at a69bea883, the registry kept claiming a
slot for two months). Extend ``_REGISTRY_PATHS`` when another plugin ships
a routing registry; ``_HOOKS_JSON_PATHS`` below still counts sn's manifest.

CORRECTION (2026-08-23). The registry count alone does not measure the
budget. ``src/nexus/commands/hook.py`` says it outright -- "registry.yaml
is documentation, hooks.json is the registration surface" -- and the two
disagree: ``conexus/hooks/hooks.json`` fires THREE hooks on the
``PreToolUse: Bash`` matcher (``pre_close_verification_hook.sh`` plus the
two routing hooks) while the registry lists two rules. A hook registered
in hooks.json with no registry entry costs latency on every Bash call and
was invisible to this cap, so a fifth could land while the lint reported
2/4. The budget is cumulative wall-clock per Bash call, so the count that
matters is every hook on that matcher, routing or not.
"""
from __future__ import annotations

import json
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = pathlib.Path(__file__).parent.parent

_REGISTRY_PATHS: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml",
)

AGGREGATE_CAP = 4


def _load_rules(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    parsed = yaml.safe_load(path.read_text()) or {}
    rules = parsed.get("rules") if isinstance(parsed, dict) else None
    return rules if isinstance(rules, dict) else {}


def test_aggregate_routing_rule_count_within_cap() -> None:
    """The union of all plugins' routing rules must respect the cap."""
    per_plugin: dict[str, int] = {}
    rule_names: list[str] = []
    for registry_path in _REGISTRY_PATHS:
        plugin_name = registry_path.relative_to(REPO_ROOT).parts[0]
        rules = _load_rules(registry_path)
        per_plugin[plugin_name] = len(rules)
        rule_names.extend(f"{plugin_name}/{name}" for name in rules)

    aggregate = sum(per_plugin.values())
    breakdown = ", ".join(
        f"{plugin}={count}" for plugin, count in sorted(per_plugin.items())
    )
    assert aggregate <= AGGREGATE_CAP, (
        f"Cross-plugin routing-rule aggregate {aggregate} exceeds cap "
        f"{AGGREGATE_CAP}. Breakdown: {breakdown}. Active rules: "
        f"{sorted(rule_names)}. RDR-121 § Performance Expectations sets "
        "the 4-hook cap to honor the <300ms p95 cumulative budget; "
        "RDR-125 made it cross-plugin. Adding a fifth rule requires "
        "consolidation or a budget revision in a successor RDR."
    )


def test_no_duplicate_rule_names_across_plugins() -> None:
    """Each rule name must be unique across the aggregate registry."""
    seen: dict[str, str] = {}  # rule_name -> first plugin that defined it
    duplicates: list[str] = []
    for registry_path in _REGISTRY_PATHS:
        plugin_name = registry_path.relative_to(REPO_ROOT).parts[0]
        for rule_name in _load_rules(registry_path):
            if rule_name in seen:
                duplicates.append(
                    f"{rule_name!r} appears in both {seen[rule_name]} and "
                    f"{plugin_name}"
                )
            else:
                seen[rule_name] = plugin_name
    assert not duplicates, (
        "Duplicate routing rule names across plugins: " + "; ".join(duplicates)
        + ". RDR-125 ownership rule says each rule lives in exactly one "
        "plugin; a duplicate means the migration is half-done or two "
        "plugins claim the same rule."
    )


# ── The registration surface, which is what actually costs latency ─────────

_HOOKS_JSON_PATHS: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "conexus" / "hooks" / "hooks.json",
    REPO_ROOT / "sn" / "hooks" / "hooks.json",
)


def _bash_hook_count(manifest: dict) -> int:
    """Hooks registered against a matcher that includes Bash, in *manifest*.

    Pure, so the cap logic below is falsifiable without editing a real
    hooks.json.
    """
    total = 0
    for entries in (manifest.get("hooks") or {}).values():
        for entry in entries or []:
            if "Bash" in str(entry.get("matcher", "")):
                total += len(entry.get("hooks") or [])
    return total


def test_bash_hook_counter_is_falsifiable() -> None:
    """Non-vacuity: the counter must actually count."""
    assert _bash_hook_count({}) == 0
    assert _bash_hook_count({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{}, {}, {}]}]}
    }) == 3
    assert _bash_hook_count({
        "hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{}, {}]}]}
    }) == 0, "a non-Bash matcher does not spend the Bash budget"
    over = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{}] * 5}]}}
    assert _bash_hook_count(over) > AGGREGATE_CAP, (
        "a 5-hook manifest must exceed the cap, or the assertion below can "
        "never fail"
    )


def test_registered_bash_hooks_within_cap() -> None:
    """The cap against the REGISTRATION SURFACE, not the documentation.

    This is the assertion the registry-based one above was standing in for.
    """
    per_plugin: dict[str, int] = {}
    for manifest_path in _HOOKS_JSON_PATHS:
        if not manifest_path.exists():
            continue
        plugin = manifest_path.relative_to(REPO_ROOT).parts[0]
        per_plugin[plugin] = _bash_hook_count(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )

    assert per_plugin, (
        "no hooks.json found in any plugin -- the paths are wrong and this "
        "lint is proving nothing"
    )
    aggregate = sum(per_plugin.values())
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(per_plugin.items()))
    assert aggregate <= AGGREGATE_CAP, (
        f"{aggregate} hooks fire on PreToolUse:Bash across plugins "
        f"({breakdown}), over the RDR-121 cap of {AGGREGATE_CAP}. That "
        "budget is cumulative wall-clock on EVERY Bash call the user makes. "
        "Consolidate, or revise the budget in a successor RDR."
    )
