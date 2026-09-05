# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tl7bu: the docling artifacts fetch ('Fetch docling models from our
own mirror') had no actions/cache in front of it and re-downloaded the ~1GB
release tarball on EVERY job — measured 2026-09-03: 20 minutes on a single
shard, cancelling it under the job timeout. The fix wraps the extracted
directory in a composite action (.github/actions/prime-docling), same shape
as .github/actions/prime-bge-onnx: restore from actions/cache keyed on the
asset tag, fetch only on a cache miss.

These are structural/mechanical checks (parse the YAML, confirm the cache
step exists and is keyed on the asset tag, confirm ci.yml calls the action
rather than inlining the curl/tar) — not a functional test of the fetch
itself, which needs a real GitHub Actions runner.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
ACTION_PATH = REPO_ROOT / ".github" / "actions" / "prime-docling" / "action.yml"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_action() -> dict:
    return yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))


def test_prime_docling_action_exists_and_parses() -> None:
    assert ACTION_PATH.is_file(), f"missing composite action: {ACTION_PATH}"
    action = _load_action()
    assert action["runs"]["using"] == "composite"


def test_prime_docling_asset_tag_default_matches_known_release_asset() -> None:
    """Drift guard: the action's default asset-tag input must match the
    release asset ci_warm_docling.py and the mirrored tarball actually use."""
    action = _load_action()
    asset_tag = action["inputs"]["asset-tag"]["default"]
    assert asset_tag == "ci-assets-docling-v1"


def test_prime_docling_action_caches_before_fetching() -> None:
    """The restore step must run before the fetch step, and the fetch step
    must be conditioned on a cache miss — the whole point of the fix is that
    a cache hit skips the network call entirely."""
    action = _load_action()
    steps = action["runs"]["steps"]
    step_names = [s.get("name", "") for s in steps]

    cache_idx = next(
        i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/cache@")
    )
    fetch_idx = next(
        i
        for i, s in enumerate(steps)
        if "fetch" in s.get("name", "").lower() and "cache miss" in s.get("name", "").lower()
    )
    assert cache_idx < fetch_idx, (
        f"cache restore must precede the conditional fetch step; got order {step_names}"
    )

    fetch_step = steps[fetch_idx]
    assert fetch_step.get("if") == "steps.cache.outputs.cache-hit != 'true'", (
        "fetch step must be gated on a cache miss, not run unconditionally"
    )


def test_prime_docling_action_cache_key_uses_asset_tag_input() -> None:
    """The cache key must be derived from the same asset-tag input the fetch
    step consumes, so a -v2 tag bump (per the action's own regeneration
    instructions) also busts the cache instead of silently restoring stale
    v1 artifacts under a -v2 tag."""
    action = _load_action()
    steps = action["runs"]["steps"]
    cache_step = next(s for s in steps if s.get("uses", "").startswith("actions/cache@"))
    key = cache_step["with"]["key"]
    assert "inputs.asset-tag" in key, f"cache key must derive from inputs.asset-tag: {key}"
    assert "cache-key" in key, f"cache key should still honour an explicit inputs.cache-key: {key}"

    cache_key_default = action["inputs"]["cache-key"]["default"]
    assert cache_key_default == "", "an explicit cache-key default would bypass the asset-tag derivation"


def test_prime_docling_action_restores_to_expected_path() -> None:
    """The cached path must be the exact directory
    NEXUS_DOCLING_ARTIFACTS_PATH points docling at, or a cache hit restores
    the artifacts nowhere useful."""
    action = _load_action()
    steps = action["runs"]["steps"]
    cache_step = next(s for s in steps if s.get("uses", "").startswith("actions/cache@"))
    assert cache_step["with"]["path"] == "~/.cache/nexus/docling-artifacts"

    export_step = next(
        s for s in steps if "NEXUS_DOCLING_ARTIFACTS_PATH" in s.get("run", "")
    )
    assert "$HOME/.cache/nexus/docling-artifacts" in export_step["run"]


def test_ci_workflow_calls_the_composite_action_not_an_inline_fetch() -> None:
    """ci.yml must delegate to the composite action rather than reverting to
    an inline curl/tar step with no cache in front of it — that regression
    is exactly what nexus-tl7bu fixed."""
    text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "uses: ./.github/actions/prime-docling" in text
    # The old uncached inline fetch step must be gone.
    assert "docling-artifacts-v1.tar.gz" not in text, (
        "ci.yml should no longer inline the docling tarball fetch; "
        "it belongs in .github/actions/prime-docling"
    )
