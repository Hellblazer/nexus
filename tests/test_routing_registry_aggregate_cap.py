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

Currently in scope: ``nx/hooks/scripts/routing/registry.yaml`` and
``sn/hooks/scripts/routing/registry.yaml``. Extend ``_REGISTRY_PATHS``
when a third plugin ships a routing registry.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = pathlib.Path(__file__).parent.parent

_REGISTRY_PATHS: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "nx" / "hooks" / "scripts" / "routing" / "registry.yaml",
    REPO_ROOT / "sn" / "hooks" / "scripts" / "routing" / "registry.yaml",
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
