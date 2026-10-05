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


def test_promote_release_waits_for_the_windows_legs_only_while_the_switch_is_on() -> None:
    promote = _doc(RELEASE)["jobs"]["promote-release"]
    assert "build-publish-pg-bundle-windows" in promote["needs"]
    assert "build-publish-engine-windows" in promote["needs"]
    cond = promote["if"]
    # on: both legs must have succeeded. off: their skip must not hold the release.
    assert (
        "(vars.NX_WINDOWS_RELEASE_LEGS != 'on' || "
        "(needs.build-publish-pg-bundle-windows.result == 'success' && "
        "needs.build-publish-engine-windows.result == 'success'))"
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


# --------------------------------------------------------------------------- #
# The engine leg (RDR-224 P1.2, nexus-f9bgu.9)
# --------------------------------------------------------------------------- #

ENGINE = "build-publish-engine-windows"
ACTION = REPO / ".github" / "actions" / "windows-engine-leg" / "action.yml"
ENGINE_ASSETS = (
    "nexus-service-windows-x64.txz",
    "nexus-service-windows-x64.txz.sha256",
    "nexus-service-windows-x64.txz.sigstore.json",
)


def _engine() -> dict:
    return _doc(RELEASE)["jobs"][ENGINE]


def _action_steps() -> list[dict]:
    return yaml.safe_load(ACTION.read_text())["runs"]["steps"]


def _code(script: str) -> str:
    """The script with PowerShell (#) and cmd (rem) comment lines removed: a pin a comment can satisfy is not a pin."""
    return "\n".join(
        ln for ln in script.splitlines() if not ln.lstrip().startswith("#") and not ln.lstrip().lower().startswith("rem ")
    )


def _step_index(steps: list[dict], needle: str) -> int:
    for i, s in enumerate(steps):
        if needle in str(s.get("name", "")) or needle in str(s.get("run", "")) or needle in str(s.get("uses", "")):
            return i
    raise AssertionError(f"no step mentions {needle!r}: {[s.get('name') for s in steps]}")


def test_the_engine_job_is_a_job_of_its_own_and_the_matrix_is_untouched() -> None:
    jobs = _doc(RELEASE)["jobs"]
    assert ENGINE in jobs
    matrix = jobs["build-publish"]["strategy"]["matrix"]["target"]
    assert [t["arch"] for t in matrix] == ["linux-amd64", "linux-arm64", "mac-arm64"], (
        "windows-x64 is its own job: a matrix entry cannot be skipped alone and would pull in the apple-signing environment"
    )
    assert jobs["build-publish"]["needs"] == ["jooq-codegen", "create-release"]
    assert "windows" not in str(jobs["build-publish"]["runs-on"]).lower()


def test_the_engine_job_waits_for_codegen_the_release_and_the_pg_bundle_and_is_behind_the_switch() -> None:
    job = _engine()
    assert job["runs-on"] == LABEL
    assert set(job["needs"]) == {"jooq-codegen", "create-release", "build-publish-pg-bundle-windows"}
    cond = job["if"]
    assert SWITCH in cond
    for need in ("jooq-codegen", "build-publish-pg-bundle-windows"):
        assert f"needs.{need}.result == 'success'" in cond, need
    assert "needs.create-release.result != 'failure'" in cond
    assert job["defaults"]["run"]["shell"] == "pwsh"
    assert job["timeout-minutes"] >= 60, "an -O2 native build plus the model download needs room"


def test_the_engine_job_takes_the_pg_bundle_from_this_runs_artifact_not_a_rebuild() -> None:
    steps = _engine()["steps"]
    download = steps[_step_index(steps, "Download the Windows PG bundle")]
    assert download["with"]["name"] == "nexus-pg-windows-x64"
    pg_upload = next(
        s for s in _doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"]["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert pg_upload["with"]["name"] == "${{ env.ASSET }}"
    assert _doc(RELEASE)["jobs"]["build-publish-pg-bundle-windows"]["env"]["ASSET"] == "nexus-pg-windows-x64"
    leg = steps[_step_index(steps, "./.github/actions/windows-engine-leg")]
    assert leg["with"]["pg-archive"] == f"{download['with']['path']}/nexus-pg-windows-x64.txz"


def test_the_engine_job_signs_and_publishes_on_tags_only_after_the_leg_passed() -> None:
    steps = _engine()["steps"]
    names = [s.get("name", "") for s in steps]
    leg = _step_index(steps, "./.github/actions/windows-engine-leg")
    sign = next(i for i, n in enumerate(names) if n.startswith("Sign release asset"))
    publish = next(i for i, n in enumerate(names) if n.startswith("Upload the engine archive"))
    install = next(i for i, n in enumerate(names) if n.startswith("Install cosign"))
    assert leg < install < sign < publish, names
    for i in (install, sign, publish):
        assert "refs/tags/engine-service-v" in steps[i]["if"], names[i]
    assert "if" not in steps[leg], "the build, checks and smoke run on every trigger, tag or not"


def test_the_engine_job_signs_the_txz_with_the_new_bundle_format_only_and_uploads_exactly_the_three_assets() -> None:
    job = _engine()
    assert job["env"]["ASSET"] == "nexus-service-windows-x64"
    steps = job["steps"]
    sign = _code(steps[_step_index(steps, "Sign release asset")]["run"])
    assert "cosign sign-blob" in sign and "--new-bundle-format" in sign
    assert sign.count("--new-bundle-format") == 2, "sign and self-verify both use the format the client verifies"
    assert ".cosign.bundle" not in sign, "the client needs only the protobuf bundle; nothing else consumes this asset"
    assert "engine-service-release.yml@$env:GITHUB_REF" in sign
    assert 'dist\\$env:ASSET.txz"' in sign
    publish = steps[_step_index(steps, "Upload the engine archive")]["run"]
    uploaded = re.findall(r'"dist\\\$env:ASSET(\.txz[\w.]*)"', publish)
    assert [f"nexus-service-windows-x64{u}" for u in uploaded] == list(ENGINE_ASSETS)


def test_the_assets_the_engine_job_uploads_are_the_ones_promotion_expects() -> None:
    text = (REPO / "scripts" / "promote_engine_release.sh").read_text()
    on_block = text[text.index('if [ "$windows" = "on" ]'):]
    assert "nexus-service-windows-x64.txz" in on_block and "$p.sha256" in on_block and "$p.sigstore.json" in on_block
    off_block = text[text.index("for arch in"): text.index('if [ "$windows" = "on" ]')]
    assert "for arch in linux-amd64 linux-arm64 mac-arm64; do" in off_block
    assert "windows" not in off_block, "the off set must stay the three platforms"


def test_the_engine_job_stamps_the_tag_version_through_the_leg_and_a_dispatch_run_stamps_nothing() -> None:
    steps = _engine()["steps"]
    ver = steps[_step_index(steps, "Resolve engine-service version")]
    assert "refs/tags/engine-service-v" in ver["run"] and "engine-service-v'.Length" in ver["run"]
    assert ver["env"] == {"REF": "${{ github.ref }}", "REF_NAME": "${{ github.ref_name }}"}, (
        "the tag reaches the script through env, never interpolated: a tag can hold shell metacharacters"
    )
    leg = steps[_step_index(steps, "./.github/actions/windows-engine-leg")]
    assert leg["with"]["version"] == "${{ steps.ver.outputs.version }}"
    stamp = _action_steps()[_step_index(_action_steps(), "Stamp release_version")]
    assert stamp["if"] == "inputs.version != ''"
    assert stamp["env"]["RELEASE_VERSION"] == "${{ inputs.version }}"
    assert "$env:RELEASE_VERSION" in stamp["run"] and "${{" not in stamp["run"]


def test_the_composite_runs_the_leg_in_the_order_that_makes_each_check_mean_something() -> None:
    steps = _action_steps()
    order = [
        _step_index(steps, "Download pre-generated jOOQ sources"),
        _step_index(steps, "Stamp release_version"),
        _step_index(steps, "Load the MSVC build environment"),
        _step_index(steps, "Build native image"),
        _step_index(steps, "check_native_embedded_resources.py"),
        _step_index(steps, "windows_engine_release.py check-deps"),
        _step_index(steps, "windows_engine_release.py package"),
        _step_index(steps, "engine_windows_smoke.py"),
    ]
    assert order == sorted(order) and len(set(order)) == len(order), order
    assert steps[order[0]]["with"] == {"name": "jooq-sources", "path": "service/target/generated-sources/jooq"}


def test_the_composite_builds_once_without_docker_and_reports_embedded_resources() -> None:
    steps = _action_steps()
    build = steps[_step_index(steps, "Build native image")]
    assert build["shell"] == "cmd", "Maven warnings go to stderr; cmd redirects them without PowerShell turning them into errors"
    assert build["working-directory"] == "service"
    assert build["env"]["NATIVE_IMAGE_OPTIONS"] == "-H:+UnlockExperimentalVMOptions -H:+GenerateEmbeddedResourcesFile"
    run = _code(build["run"])
    assert len(re.findall(r"-Pnative\b", run)) == 1
    assert "-Pprebuilt-jooq" in run, "codegen needs Docker (testcontainers); this runner has none"
    assert not re.search(r"\s-q(?:\s|$)", run), "-q would blind the checker's read of the Maven log"
    assert "native-build.log" in run
    assert "exit /b %BUILD_RC%" in run, "the build's status must survive the `type` that prints the log"
    assert " clean" not in run, "clean would wipe the downloaded jOOQ sources"


def test_the_composite_runs_every_check_with_the_windows_platform_and_the_builds_own_outputs() -> None:
    steps = _action_steps()
    emb = steps[_step_index(steps, "check_native_embedded_resources.py")]["run"]
    assert "--platform windows-x64" in emb
    assert "--report service\\target\\embedded-resources.json" in emb and "native-build.log" in emb
    deps = steps[_step_index(steps, "windows_engine_release.py check-deps")]["run"]
    assert "--exe service\\target\\nexus-service.exe" in deps and "--report service\\target\\embedded-resources.json" in deps
    smoke = steps[_step_index(steps, "engine_windows_smoke.py")]["run"]
    assert '--engine-archive "$env:OUT_DIR\\nexus-service-windows-x64.txz"' in smoke
    assert "--pg-archive $env:PG_ARCHIVE" in smoke
    for step in steps:
        if step.get("shell") == "pwsh" and "PYTHON" in step.get("run", ""):
            assert "$LASTEXITCODE" in step["run"], f"{step.get('name')}: a failing script must fail the step"


def test_every_script_the_engine_leg_runs_exists() -> None:
    text = ACTION.read_text() + "\n".join(_run_text(j) for j in (_engine(), _doc(REHEARSAL)["jobs"]["engine"]))
    scripts = set(re.findall(r"scripts[\\/]([\w]+\.py)", text))
    assert {"windows_engine_release.py", "engine_windows_smoke.py", "check_native_embedded_resources.py"} <= scripts
    for name in scripts:
        assert (REPO / "scripts" / name).is_file(), name


# The `if` of promote-release, evaluated for the cases that matter: the claim "with the switch off a tag run
# behaves exactly as today, with it on a Windows failure keeps the release a draft" is a property of this
# expression, so it is run, not just read.


def _promotes(switch: str, **results: str) -> bool:
    cond = _doc(RELEASE)["jobs"]["promote-release"]["if"].strip()
    assert cond.startswith("${{") and cond.endswith("}}")
    expr = cond[3:-2].strip()
    expr = expr.replace("!cancelled()", "True")
    expr = re.sub(r"startsWith\(github\.ref, '[^']*'\)", "True", expr)
    expr = re.sub(r"needs\.([\w-]+)\.result", lambda m: f"R[{m.group(1)!r}]", expr)
    expr = expr.replace("vars.NX_WINDOWS_RELEASE_LEGS", "SWITCH").replace("&&", " and ").replace("||", " or ")
    return bool(eval(expr, {"R": results, "SWITCH": switch}))  # noqa: S307 - the expression is the repo's own


BASE = {"create-release": "success", "build-publish": "success", "build-publish-pg-bundle": "success"}


def test_promotion_with_the_switch_off_is_what_it_was_before_the_windows_legs_existed() -> None:
    skipped = {"build-publish-pg-bundle-windows": "skipped", "build-publish-engine-windows": "skipped"}
    assert _promotes("", **BASE, **skipped) is True
    assert _promotes("off", **BASE, **skipped) is True
    assert _promotes("", **{**BASE, "build-publish": "failure"}, **skipped) is False
    assert _promotes("", **{**BASE, "build-publish-pg-bundle": "failure"}, **skipped) is False


def test_promotion_with_the_switch_on_needs_both_windows_legs() -> None:
    ok = {"build-publish-pg-bundle-windows": "success", "build-publish-engine-windows": "success"}
    assert _promotes("on", **BASE, **ok) is True
    for leg in ok:
        for bad in ("failure", "skipped", "cancelled"):
            assert _promotes("on", **BASE, **{**ok, leg: bad}) is False, (leg, bad)
    assert _promotes("on", **{**BASE, "build-publish": "failure"}, **ok) is False


# --------------------------------------------------------------------------- #
# The rehearsal's engine job
# --------------------------------------------------------------------------- #


def test_the_rehearsal_engine_job_runs_the_same_composite_and_ships_nothing() -> None:
    job = _doc(REHEARSAL)["jobs"]["engine"]
    assert job["runs-on"] == LABEL
    assert set(job["needs"]) == {"bundle", "jooq-codegen"}
    steps = job["steps"]
    leg = steps[_step_index(steps, "./.github/actions/windows-engine-leg")]
    assert "version" not in leg["with"], "a rehearsal stamps no release version"
    release_leg = _engine()["steps"][_step_index(_engine()["steps"], "./.github/actions/windows-engine-leg")]
    assert leg["uses"] == release_leg["uses"], "the rehearsal must run the release leg's own steps"
    text = "\n".join(str(s) for s in steps)
    assert "cosign" not in text and "gh release" not in text and "upload-artifact" not in text
    assert any("test_windows_engine_release.py" in s.get("run", "") and "test_engine_windows_smoke.py" in s.get("run", "") for s in steps)


def test_the_rehearsal_engine_job_gets_its_pg_bundle_and_jooq_sources_from_jobs_in_the_same_run() -> None:
    doc = _doc(REHEARSAL)
    bundle_upload = next(s for s in doc["jobs"]["bundle"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    download = next(s for s in doc["jobs"]["engine"]["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert bundle_upload["with"]["name"] == download["with"]["name"]
    assert bundle_upload["with"]["path"].endswith("nexus-pg-windows-x64.txz")
    codegen = doc["jobs"]["jooq-codegen"]
    assert codegen["runs-on"] == "ubuntu-latest"
    assert "github.actor == 'Hellblazer'" in codegen["if"] and SWITCH in codegen["if"], (
        "with the switch off the rehearsal creates no job at all, the codegen job included"
    )
    up = next(s for s in codegen["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert up["with"]["name"] == "jooq-sources"


def test_the_rehearsal_is_triggered_by_the_engine_inputs_too() -> None:
    paths = _triggers(_doc(REHEARSAL))["push"]["paths"]
    for needed in (
        "scripts/windows_engine_release.py", "scripts/engine_windows_smoke.py",
        "scripts/check_native_embedded_resources.py", "service/pom.xml",
        ".github/actions/windows-engine-leg/**", "tests/test_windows_engine_release.py",
        "tests/test_engine_windows_smoke.py",
    ):
        assert needed in paths, needed


def test_every_file_the_rehearsal_triggers_on_exists_or_is_a_glob_over_something_real() -> None:
    for path in _triggers(_doc(REHEARSAL))["push"]["paths"]:
        if "*" in path:
            assert list(REPO.glob(path)), f"{path} matches nothing: a dead trigger"
        else:
            assert (REPO / path).exists(), path
