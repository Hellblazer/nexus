# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the purpose-alias registry loader (nexus-eesvy).

These tests deliberately exercise the REAL on-disk registry — no
``_registry_override`` — because the bug that motivated them was a path
that resolved only in the source tree and silently returned ``{}`` from
every installed artifact (uv-tool, ``.mcpb``). The pre-existing
``_registry_override``-based tests could never have caught it: they
bypass the file load entirely. Asserting against the real resource is
the point.
"""
from __future__ import annotations

import importlib

import nexus.plans.purposes as purposes
from nexus.plans.purposes import PURPOSES_YML, resolve_purpose


def test_purposes_yml_resolves_to_a_real_file() -> None:
    """The shipped registry path must exist in the active install layout.

    This is the direct regression for nexus-eesvy: ``PURPOSES_YML``
    must point at the force-included ``nexus/_resources/plans`` resource
    (or the repo-tree fallback), not a dev-only path that vanishes in
    the wheel.
    """
    assert PURPOSES_YML.exists(), (
        f"purposes registry not found at {PURPOSES_YML!r}; the resolver "
        "must locate the force-included resource or the repo-tree copy"
    )


def test_purposes_yml_uses_packaged_resource_not_source_tree() -> None:
    """Resolution must go through the packaged ``_resources`` location.

    Non-vacuity guard: the OLD resolver pointed at
    ``<repo>/conexus/plans/purposes.yml`` — a path that ALSO exists in
    the source checkout, so an ``.exists()``-only assertion passed under
    the bug in dev while production (the wheel, where ``conexus/`` is
    not shipped) silently broke. The packaged resource lives under
    ``nexus/_resources/plans`` in both the editable install
    (``src/nexus/_resources/...``) and the wheel
    (``site-packages/nexus/_resources/...``); the old ``conexus/plans``
    constant never goes through ``_resources``. Asserting the segment
    is present is what actually fails the regression.
    """
    parts = PURPOSES_YML.parts
    assert "_resources" in parts and "plans" in parts, (
        f"{PURPOSES_YML!r} did not resolve through the packaged "
        "nexus/_resources/plans resource — this is the nexus-eesvy "
        "source-tree-only path regression"
    )


def test_registry_loads_non_empty() -> None:
    """A resolved-but-empty registry is the exact production failure."""
    # Clear the module cache so we genuinely re-read the file.
    purposes._registry_cache = None
    registry = purposes._load_registry()
    assert registry, "registry loaded empty — every purpose would be 'unknown'"
    assert "find-implementations" in registry


def test_resolve_find_implementations_from_real_registry() -> None:
    """The canonical alias resolves to its catalog link types.

    No ``_registry_override`` — this is the end-to-end path a live
    ``traverse(purpose=...)`` call takes.
    """
    purposes._registry_cache = None
    assert resolve_purpose("find-implementations") == [
        "implements",
        "implements-heuristic",
    ]


def test_resolve_unknown_purpose_returns_empty() -> None:
    purposes._registry_cache = None
    assert resolve_purpose("no-such-purpose-alias") == []


def test_module_reimport_resolves_path() -> None:
    """Re-importing must not regress to a non-existent path constant."""
    reloaded = importlib.reload(purposes)
    assert reloaded.PURPOSES_YML.exists()
