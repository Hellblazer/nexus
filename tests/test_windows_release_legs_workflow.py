# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 P2.2 (nexus-f9bgu.14) and P0.5a (nexus-f9bgu.5): the Windows release legs.

The legs run on ``win-release``, a self-hosted runner on a host that is inside the
release trust boundary, and they only ever have a runner once Sam registers one.
So the properties that matter are structural and are pinned here:

* every job on that label is behind the NX_WINDOWS_RELEASE_LEGS switch, so with the
  variable unset a tag run queues nothing and promotion expects no Windows asset;
* none of the three workflows can run on ``pull_request`` (public repo, self-hosted box);
* the release and seed legs compute the cache key the same way, so a tag restores what
  main seeded;
* promotion is skipped-tolerant only while the switch is off.

A workflow that is YAML-valid and has none of these still runs; these tests are what
would notice.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
LABEL = "win-release"
SWITCH = "vars.NX_WINDOWS_RELEASE_LEGS == 'on'"

RELEASE = "engine-service-release.yml"
SEED = "pg-bundle-cache-seed.yml"
REHEARSAL = "windows-pg-bundle-rehearsal.yml"
WINDOWS_WORKFLOWS = (RELEASE, SEED, REHEARSAL)


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    on = doc.get("on", doc.get(True))
    assert isinstance(on, dict), "workflow has no `on:` mapping"
    return on


def _win_jobs(name: str) -> dict[str, dict]:
    return {
        job_id: job
        for job_id, job in _doc(name)["jobs"].items()
        if job.get("runs-on") == LABEL
    }


def _run_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_each_workflow_really_has_windows_jobs(name: str) -> None:
    """Non-vacuity: every assertion below loops over these jobs, so an empty set passes all of them."""
    assert _win_jobs(name), f"{name} has no job on {LABEL}"


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_every_win_release_job_is_behind_the_switch(name: str) -> None:
    for job_id, job in _win_jobs(name).items():
        assert SWITCH in str(job.get("if", "")), (
            f"{name}:{job_id} runs on {LABEL} without `{SWITCH}` in its job-level `if`: with the "
            "variable unset it would queue for a runner that is not there"
        )


def test_the_label_is_a_bare_custom_label_never_an_array_or_self_hosted() -> None:
    for name in WINDOWS_WORKFLOWS:
        for job_id, job in _doc(name)["jobs"].items():
            runs_on = job.get("runs-on")
            if isinstance(runs_on, list) or (isinstance(runs_on, str) and "self-hosted" in runs_on):
                pytest.fail(f"{name}:{job_id} routes through {runs_on!r}; use the bare label")
            assert not (isinstance(runs_on, str) and re.search(r"qwen|llama", runs_on, re.I)), (
                f"{name}:{job_id}: the runner's service name derives from its name, and the host "
                "forbids any Windows service named with qwen or llama"
            )


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_no_windows_workflow_runs_on_a_pull_request(name: str) -> None:
    triggers = _triggers(_doc(name))
    assert "pull_request" not in triggers and "pull_request_target" not in triggers, (
        f"{name} must never run on a pull request: public repo, self-hosted runner"
    )


def test_actionlint_knows_the_label() -> None:
    cfg = yaml.safe_load((REPO / ".github" / "actionlint.yaml").read_text())
    assert LABEL in cfg["self-hosted-runner"]["labels"]


# --------------------------------------------------------------------------- #
# The release leg and promotion
# --------------------------------------------------------------------------- #


def test_the_release_leg_gates_publish_on_the_relocation_smoke_and_signs_on_tags_only() -> None:
    job = _doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"]
    names = [step.get("name", "") for step in job["steps"]]
    smoke = next(i for i, n in enumerate(names) if "relocat" in n.lower())
    sign = next(i for i, n in enumerate(names) if n.startswith("Sign PG bundle"))
    publish = next(i for i, n in enumerate(names) if n.startswith("Publish PG bundle"))
    assert smoke < sign < publish, names
    assert "pg_bundle_windows_smoke.py --archive" in job["steps"][smoke]["run"]
    for i in (sign, publish):
        assert "refs/tags/engine-service-v" in job["steps"][i]["if"], names[i]
    assert "needs" in job and job["needs"] == "create-release"
    assert job["defaults"]["run"]["shell"] == "pwsh"


def test_a_cache_hit_refreshes_the_vc_runtime_before_packaging() -> None:
    """P0.6 condition 8: the four DLLs are not a key input, so every restored prefix is refreshed."""
    steps = _doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"]["steps"]
    refresh = next(i for i, s in enumerate(steps) if "refresh-runtime" in s.get("run", ""))
    package = next(i for i, s in enumerate(steps) if " package " in s.get("run", ""))
    assert refresh < package
    assert "cache-hit == 'true'" in steps[refresh]["if"]
    build = next(s for s in steps if " build " in s.get("run", "") and "refresh" not in s["run"])
    assert "cache-hit != 'true'" in build["if"]


def test_the_release_leg_only_restores_the_cache_and_the_seed_leg_saves_it() -> None:
    release = _doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"]["steps"]
    seed = _doc(SEED)["jobs"]["seed-windows"]["steps"]
    assert any(str(s.get("uses", "")).startswith("actions/cache/restore@") for s in release)
    assert not any(str(s.get("uses", "")).startswith("actions/cache@") for s in release), (
        "a tag-scoped cache save can never be restored and only burns quota"
    )
    assert any(str(s.get("uses", "")).startswith("actions/cache@") for s in seed)


def _cache_key_step(steps: list[dict]) -> dict:
    return next(s for s in steps if s.get("id") == "key")


def test_release_and_seed_compute_the_windows_cache_key_the_same_way() -> None:
    """The key is the script's own `cache-key` output, with one runner label, in both workflows."""
    rel = _doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"]
    seed = _doc(SEED)["jobs"]["seed-windows"]
    assert _cache_key_step(rel["steps"])["run"] == _cache_key_step(seed["steps"])["run"]
    assert "build_pg_bundle_windows.py cache-key" in _cache_key_step(rel["steps"])["run"]
    assert rel["env"]["PG_BUNDLE_RUNNER"] == seed["env"]["PG_BUNDLE_RUNNER"] == LABEL
    for job in (rel, seed):
        cache = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/cache"))
        assert cache["with"]["key"] == "${{ steps.key.outputs.key }}"
        assert cache["with"]["path"] == "bundle"


def test_the_seed_workflow_reseeds_when_the_windows_script_changes() -> None:
    paths = _triggers(_doc(SEED))["push"]["paths"]
    assert "scripts/build_pg_bundle_windows.py" in paths


def test_promote_release_waits_for_the_windows_leg_only_while_the_switch_is_on() -> None:
    promote = _doc(RELEASE)["jobs"]["promote-release"]
    assert "build-publish-pg-bundle-windows" in promote["needs"]
    cond = promote["if"]
    # on: the leg must have succeeded. off: its skip must not hold the release.
    assert (
        "(vars.NX_WINDOWS_RELEASE_LEGS != 'on' || "
        "needs.build-publish-pg-bundle-windows.result == 'success')"
    ) in cond
    # A job whose needs include a skipped job is skipped unless the `if` carries a status
    # function; without this the off state would silently stop promoting at all.
    assert "!cancelled()" in cond
    # ...and the explicit result checks stay, so `!cancelled()` does not promote past a failure.
    for job in ("create-release", "build-publish", "build-publish-pg-bundle"):
        assert f"needs.{job}.result == 'success'" in cond


def test_promote_passes_the_switch_to_the_script_normalised_to_on_or_off() -> None:
    step = next(
        s for s in _doc(RELEASE)["jobs"]["promote-release"]["steps"]
        if "promote_engine_release.sh" in s.get("run", "")
    )
    assert step["env"]["WINDOWS_LEGS"] == "${{ vars.NX_WINDOWS_RELEASE_LEGS == 'on' && 'on' || 'off' }}"
    assert re.search(r'promote_engine_release\.sh .*"\$\{WINDOWS_LEGS\}"', step["run"])


# --------------------------------------------------------------------------- #
# The rehearsal (P0.5a)
# --------------------------------------------------------------------------- #


def test_the_rehearsal_has_the_decided_shape() -> None:
    doc = _doc(REHEARSAL)
    triggers = _triggers(doc)
    assert triggers["push"]["branches"] == ["develop"]
    assert triggers["push"]["paths"], "an unfiltered push trigger would build on every commit"
    assert "workflow_dispatch" in triggers
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is True
    assert doc["concurrency"]["group"]
    for job_id, job in _win_jobs(REHEARSAL).items():
        assert "github.actor == 'Hellblazer'" in str(job["if"]), job_id


def test_the_rehearsal_runs_all_three_decided_checks() -> None:
    jobs = _doc(REHEARSAL)["jobs"]
    bundle = _run_text(jobs["bundle"])
    assert "tests/test_pg_bundle_windows.py" in bundle  # the build-script check
    assert "build_pg_bundle_windows.py build" in bundle
    assert "pg_bundle_windows_smoke.py --archive" in bundle  # the relocation smoke
    assert "test_rdr149_lifecycle_conformance.py" in _run_text(jobs["conformance"])


def test_the_rehearsal_deletes_the_build_tree_before_the_smoke() -> None:
    """The smoke refuses while the recorded build prefix exists; a rehearsal that passed
    --allow-build-prefix-present would prove less than the release gate does."""
    text = _run_text(_doc(REHEARSAL)["jobs"]["bundle"])
    assert "--allow-build-prefix-present" not in text
    assert text.index("Remove-Item -Recurse -Force (Join-Path $env:RUNNER_TEMP 'rehearsal-bundle')") < text.index(
        "pg_bundle_windows_smoke.py"
    )


def test_the_release_leg_deletes_the_build_tree_before_the_smoke() -> None:
    text = _run_text(_doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"])
    assert "--allow-build-prefix-present" not in text
    assert text.index("Remove-Item") < text.index("pg_bundle_windows_smoke.py")
