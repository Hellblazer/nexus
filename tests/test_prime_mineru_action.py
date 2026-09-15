# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ggonw: local-service-gate-nightly.yml's slow leg drove
test_mineru_path_indexes_table_values_as_text against MinerU's default
MINERU_MODEL_SOURCE ("huggingface"), so a cold cache pulled ~1-3GB of
pipeline weights straight from the HuggingFace hub on every miss (run
34975576187 timed out at 45 minutes inside that download). The fix mirrors
prime-docling: a composite action (.github/actions/prime-mineru) that
restores our own self-hosted release asset from actions/cache, writes
MinerU's local-models config, and exports MINERU_MODEL_SOURCE=local so the
client never resolves through the hub.

These are structural/mechanical checks (parse the YAML, confirm the cache
step exists and is keyed on the asset tag, confirm the nightly workflow
calls the action rather than caching ~/.cache/huggingface directly) — not a
functional test of the fetch itself, which needs a real GitHub Actions
runner.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._mineru_rearm import mineru_importorskip

REPO_ROOT = Path(__file__).parent.parent
ACTION_PATH = REPO_ROOT / ".github" / "actions" / "prime-mineru" / "action.yml"
MANIFEST_PATH = REPO_ROOT / ".github" / "actions" / "prime-mineru" / "models.manifest"
NIGHTLY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "local-service-gate-nightly.yml"

#: nexus-ggonw fix round: directories a lint sweep must never see
#: MINERU_FORMULA_CH_SUPPORT set in — see
#: test_manifest_omission_stays_a_decision below.
_FORMULA_CH_SUPPORT_SWEEP_ROOTS = ("src", ".github", "tests/e2e")

#: Files that legitimately NAME MINERU_FORMULA_CH_SUPPORT in prose while
#: explaining that nothing sets it (this action's own description and
#: manifest, and this test file itself) — excluded from the sweep so their
#: own documentation doesn't self-trigger the check it defines.
_FORMULA_CH_SUPPORT_SWEEP_EXCLUDES = (
    Path(".github/actions/prime-mineru/action.yml"),
    Path(".github/actions/prime-mineru/models.manifest"),
)


def _load_action() -> dict:
    return yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))


def _manifest_paths() -> set[str]:
    lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    return {
        stripped
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith("#")
    }


def test_prime_mineru_action_exists_and_parses() -> None:
    assert ACTION_PATH.is_file(), f"missing composite action: {ACTION_PATH}"
    action = _load_action()
    assert action["runs"]["using"] == "composite"


def test_prime_mineru_asset_tag_default_matches_known_release_asset() -> None:
    """Drift guard: the action's default asset-tag input must match the
    release asset the mirrored tarball actually uses."""
    action = _load_action()
    asset_tag = action["inputs"]["asset-tag"]["default"]
    assert asset_tag == "ci-assets-mineru-v1"


def test_prime_mineru_action_caches_before_fetching() -> None:
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


def test_prime_mineru_action_cache_key_uses_asset_tag_input() -> None:
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


def test_prime_mineru_action_restores_to_expected_path() -> None:
    """The cached path must be the exact directory the models-dir config
    points MinerU at, or a cache hit restores the artifacts nowhere useful."""
    action = _load_action()
    steps = action["runs"]["steps"]
    cache_step = next(s for s in steps if s.get("uses", "").startswith("actions/cache@"))
    assert cache_step["with"]["path"] == "~/.cache/nexus/mineru-models"

    config_step = next(
        s for s in steps if "MINERU_MODEL_SOURCE" in s.get("run", "")
    )
    assert "$HOME/.cache/nexus/mineru-models" in config_step["run"]


def test_prime_mineru_action_exports_model_source_local() -> None:
    """The action must export MINERU_MODEL_SOURCE=local and point
    MINERU_TOOLS_CONFIG_JSON at an absolute config path it writes itself —
    otherwise mineru/utils/models_download_utils.py falls through to its
    "huggingface" default and resolves through the hub regardless of a warm
    model cache."""
    action = _load_action()
    steps = action["runs"]["steps"]
    export_step = next(
        s for s in steps if "MINERU_MODEL_SOURCE" in s.get("run", "")
    )
    run_text = export_step["run"]
    assert "MINERU_MODEL_SOURCE=local" in run_text
    assert "MINERU_TOOLS_CONFIG_JSON=" in run_text
    assert "models-dir" in run_text
    assert '"pipeline"' in run_text


def test_prime_mineru_action_never_reaches_huggingface() -> None:
    """WE HOST THESE. No step in this action may issue a network call to the
    HuggingFace hub — that is precisely the network call nexus-ggonw exists
    to eliminate. The action's own prose is allowed to NAME huggingface
    (explaining what it avoids, same as prime-docling's own description);
    what must be absent is an actual host/URL a step would resolve."""
    action = _load_action()
    steps = action["runs"]["steps"]
    for step in steps:
        for field in ("run", "with"):
            value = step.get(field, "")
            text = value if isinstance(value, str) else " ".join(
                str(v) for v in value.values()
            )
            lowered = text.lower()
            assert "huggingface.co" not in lowered, step.get("name")
            assert "hf.co" not in lowered, step.get("name")
            assert "hf-mirror" not in lowered, step.get("name")
    # No step caches or writes into the HuggingFace hub's own cache dir —
    # that path belongs to prime-mineru's own ~/.cache/nexus/mineru-models,
    # never ~/.cache/huggingface.
    cache_step = next(s for s in steps if s.get("uses", "").startswith("actions/cache@"))
    assert cache_step["with"]["path"] == "~/.cache/nexus/mineru-models"


def test_nightly_workflow_calls_the_prime_mineru_action() -> None:
    """local-service-gate-nightly.yml must delegate to the composite action
    before the slow leg, rather than caching ~/.cache/huggingface directly —
    that old actions/cache step is exactly what nexus-ggonw removes."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["gate"]["steps"]
    assert any(s.get("uses", "") == "./.github/actions/prime-mineru" for s in steps)
    # The old HuggingFace cache step (an actions/cache step whose path is
    # the HF hub cache dir) must be gone.
    hf_cache_steps = [
        s
        for s in steps
        if s.get("uses", "").startswith("actions/cache@")
        and s.get("with", {}).get("path") == "~/.cache/huggingface"
    ]
    assert hf_cache_steps == [], f"stale HuggingFace cache step still present: {hf_cache_steps}"


def test_nightly_workflow_primes_mineru_before_the_slow_leg() -> None:
    """The prime-mineru step must run before the slow-marked test leg that
    actually needs the models, or the cache does nothing for it."""
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["gate"]["steps"]

    prime_idx = next(
        i for i, s in enumerate(steps) if s.get("uses", "") == "./.github/actions/prime-mineru"
    )
    slow_idx = next(
        i
        for i, s in enumerate(steps)
        if "slow" in s.get("name", "").lower() and "-m slow" in s.get("run", "")
    )
    assert prime_idx < slow_idx, (
        f"prime-mineru must run before the slow-marked test leg; "
        f"prime at {prime_idx}, slow leg at {slow_idx}"
    )


def test_prime_mineru_action_verifies_sha256_before_extraction() -> None:
    """The fetch step must sha256-verify the downloaded tarball with
    `shasum -a 256 -c -` BEFORE extracting it — same pattern as
    prime-bge-onnx / prime-crossencoder-onnx. A tampered/truncated asset
    must fail the step, not extract silently."""
    action = _load_action()
    steps = action["runs"]["steps"]
    fetch_step = next(
        s
        for s in steps
        if "fetch" in s.get("name", "").lower() and "cache miss" in s.get("name", "").lower()
    )
    env = fetch_step.get("env", {})
    assert "MODEL_SHA256" in env, "fetch step must pin an expected sha256"
    assert env["MODEL_SHA256"] == (
        "dc8c072b691ad38d5ab6b6474e9f009f5b673f248c1ff5f5d885016fc504305c"
    )

    run_text = fetch_step["run"]
    verify_idx = run_text.find("shasum -a 256 -c -")
    extract_idx = run_text.find("tar -xzf")
    assert verify_idx != -1, "fetch step must run `shasum -a 256 -c -`"
    assert extract_idx != -1, "fetch step must extract the tarball"
    assert verify_idx < extract_idx, (
        "sha256 verification must happen BEFORE extraction, not after"
    )


def test_config_step_runs_unconditionally() -> None:
    """The config-writing step must carry no `if:` guard. It must run on a
    cache HIT too — the models directory is already populated, but
    MINERU_MODEL_SOURCE / MINERU_TOOLS_CONFIG_JSON still need exporting for
    THIS job's steps, since actions/cache restores files, never env vars.
    A `cache-hit-only` guard here would silently strand a restored-from-
    cache job on MinerU's "huggingface" default.

    Fail-before evidence (nexus-ggonw fix round, recorded manually):
    adding `if: steps.cache.outputs.cache-hit != 'true'` to this step in
    action.yml and re-running this test fails with:
        assert step.get("if") is None
        AssertionError: config step must have no `if:` guard; found "steps.cache.outputs.cache-hit != 'true'"
    Reverting the added `if:` makes the test pass again.
    """
    action = _load_action()
    steps = action["runs"]["steps"]
    config_step = next(s for s in steps if "MINERU_MODEL_SOURCE" in s.get("run", ""))
    assert config_step.get("if") is None, (
        f'config step must have no `if:` guard; found {config_step.get("if")!r}'
    )


def test_manifest_matches_mineru_modelpath_pipeline_entries() -> None:
    """models.manifest (the single source the action's description and this
    test both read) must equal mineru.utils.enum_class.ModelPath's pipeline
    entries (every string value starting with "models/") minus
    pp_formulanet_plus_m — the one model this mirror deliberately omits.

    Fail-before evidence (nexus-ggonw fix round, recorded manually):
    appending a stray "models/bogus/entry" line to models.manifest and
    re-running this test fails with:
        AssertionError: assert {'models/Layout/PP-DocLayoutV2', ..., 'models/bogus/entry'} == {'models/Layout/PP-DocLayoutV2', ...}
    Removing the stray line makes the test pass again.
    """
    mineru_importorskip("mineru.utils.enum_class")
    from mineru.utils.enum_class import ModelPath

    pipeline_entries = {
        value
        for value in vars(ModelPath).values()
        if isinstance(value, str) and value.startswith("models/")
    }
    expected = pipeline_entries - {ModelPath.pp_formulanet_plus_m}
    assert _manifest_paths() == expected


def test_manifest_omission_stays_a_decision() -> None:
    """Nothing in src/, .github/, or tests/e2e/ may set
    MINERU_FORMULA_CH_SUPPORT. That is what keeps leaving
    pp_formulanet_plus_m out of the mirror manifest a DELIBERATE decision
    (this repo never asks mineru's MFR_MODEL to become "pp_formulanet_plus_m",
    see mineru/backend/pipeline/model_init.py) rather than a silent gap that
    would crash extraction at weight-load time with no re-fallback.

    Fail-before evidence (nexus-ggonw fix round, recorded manually):
    adding a line `MINERU_FORMULA_CH_SUPPORT=true` to
    .github/workflows/local-service-gate-nightly.yml and re-running this
    test fails with:
        AssertionError: MINERU_FORMULA_CH_SUPPORT set in: ['.github/workflows/local-service-gate-nightly.yml']
    Removing the added line makes the test pass again.
    """
    hits: list[str] = []
    for root in _FORMULA_CH_SUPPORT_SWEEP_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT)
            if rel in _FORMULA_CH_SUPPORT_SWEEP_EXCLUDES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "MINERU_FORMULA_CH_SUPPORT" in text:
                hits.append(str(rel))
    assert hits == [], f"MINERU_FORMULA_CH_SUPPORT set in: {hits}"
