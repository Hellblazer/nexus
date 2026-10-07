# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 P2.2 (nexus-f9bgu.14) and P0.5a (nexus-f9bgu.5): the Windows release legs.

The legs run on ``win-release``, a self-hosted runner on a host that is inside the
release trust boundary, and they only ever have a runner once Sam registers one.
So the properties that matter are structural and are pinned here:

* every job on that label, in ANY workflow under .github/workflows, is behind the
  NX_WINDOWS_RELEASE_LEGS switch: its `if` is EVALUATED with the switch off, for every
  result of every job it needs, and must be False. (Asserting that the switch's text
  appears in the `if` cannot catch `||` for `&&`.) So with the variable unset a tag run
  queues nothing and promotion expects no Windows asset;
* no workflow that names the label can run on ``pull_request`` (public repo, self-hosted
  box), and no composite action that names it can be called from a job that is not on it;
* the release and seed legs compute the cache key the same way, so a tag restores what
  main seeded;
* promotion is skipped-tolerant only while the switch is off.

A workflow that is YAML-valid and has none of these still runs; these tests are what
would notice.
"""
from __future__ import annotations

import itertools
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
#: The files known to carry win-release jobs: the floor of the discovery below, never its ceiling.
KNOWN_WINDOWS_WORKFLOWS = (RELEASE, SEED, REHEARSAL)
ACTIONS = REPO / ".github" / "actions"


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    on = doc.get("on", doc.get(True))
    assert isinstance(on, dict), "workflow has no `on:` mapping"
    return on


def _names_label(job: dict) -> bool:
    """True when the job can land on the label: in runs-on (string, list, mapping or expression) or in a matrix."""
    return LABEL in yaml.safe_dump({"runs-on": job.get("runs-on"), "strategy": job.get("strategy")})


def _win_jobs(name: str) -> dict[str, dict]:
    return {job_id: job for job_id, job in _doc(name)["jobs"].items() if _names_label(job)}


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _action_files() -> list[Path]:
    return sorted([*ACTIONS.rglob("action.yml"), *ACTIONS.rglob("action.yaml")])


def _discover_windows_workflows() -> tuple[str, ...]:
    """Every workflow file with at least one job that can land on the label. A fourth file added tomorrow
    is picked up here, so the checks below cannot be bypassed by not being on a fixed list."""
    found = []
    for path in _workflow_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and any(_names_label(j) for j in (doc.get("jobs") or {}).values() if isinstance(j, dict)):
            found.append(path.name)
    return tuple(found)


#: Computed at collection: every parametrised check below runs once per file that names the label.
WINDOWS_WORKFLOWS = _discover_windows_workflows()


def _run_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_the_discovery_finds_at_least_the_known_windows_workflows() -> None:
    """Non-vacuity: every parametrised check below runs over WINDOWS_WORKFLOWS, so an empty or shrunken
    discovery would pass all of them."""
    assert set(KNOWN_WINDOWS_WORKFLOWS) <= set(WINDOWS_WORKFLOWS), WINDOWS_WORKFLOWS
    assert all(_win_jobs(name) for name in WINDOWS_WORKFLOWS)


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_each_workflow_really_has_windows_jobs(name: str) -> None:
    assert _win_jobs(name), f"{name} has no job on {LABEL}"


_RESULTS = ("success", "failure", "skipped", "cancelled")


def _eval_if(cond: object, *, switch: str, actor: str = "Hellblazer", results: dict[str, str] | None = None) -> bool:
    """A job-level `if` evaluated as GitHub would, for the expression subset these workflows use:
    `!cancelled()`, `always()`, `github.actor`, `needs.<job>.result`, `vars.NX_WINDOWS_RELEASE_LEGS`,
    startsWith(github.ref, ...), == != && || !. Anything else raises: an `if` this cannot evaluate is
    not silently treated as False (that would pass every check below)."""
    text = str(cond).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    expr = re.sub(r"startsWith\(github\.ref, '[^']*'\)", "True", text)
    expr = expr.replace("!cancelled()", "True").replace("always()", "True")
    expr = re.sub(r"needs\.([\w-]+)\.result", lambda m: f"R[{m.group(1)!r}]", expr)
    expr = expr.replace("vars.NX_WINDOWS_RELEASE_LEGS", "SWITCH").replace("github.actor", "ACTOR")
    expr = re.sub(r"!(?!=)", " not ", expr).replace("&&", " and ").replace("||", " or ")
    try:
        return bool(eval(expr, {"__builtins__": {}}, {"R": results or {}, "SWITCH": switch, "ACTOR": actor, "True": True}))  # noqa: S307 - the repo's own expression
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"cannot evaluate the job `if` {text!r} (as {expr!r}): {exc!r}") from exc


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_every_win_release_job_is_off_when_the_switch_is_off(name: str) -> None:
    """EVALUATED, not grepped: with the variable unset, off, or anything but 'on', the job does not run,
    whatever the jobs it needs did and whoever the actor is. `||` where `&&` belongs flips this."""
    for job_id, job in _win_jobs(name).items():
        assert "if" in job, f"{name}:{job_id} runs on {LABEL} with no job-level `if` at all"
        needed = _needs(job)
        for switch in ("", "off", "false", "0"):
            for combo in itertools.product(_RESULTS, repeat=len(needed)):
                for actor in ("Hellblazer", "someone-else"):
                    assert _eval_if(job["if"], switch=switch, actor=actor, results=dict(zip(needed, combo))) is False, (
                        f"{name}:{job_id} would run with NX_WINDOWS_RELEASE_LEGS={switch!r}, actor={actor!r}, "
                        f"needs={dict(zip(needed, combo))}: it would queue for a runner that is not there"
                    )


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_every_win_release_job_can_run_when_the_switch_is_on_so_the_off_check_is_not_vacuous(name: str) -> None:
    """The control for the test above: the evaluator does not just answer False. With the switch on, the
    owner as actor and every needed job green, each of these jobs is enabled."""
    for job_id, job in _win_jobs(name).items():
        ok = {n: "success" for n in _needs(job)}
        assert _eval_if(job["if"], switch="on", results=ok) is True, f"{name}:{job_id} cannot run even with the switch on"


@pytest.mark.parametrize("name", WINDOWS_WORKFLOWS)
def test_every_win_release_job_is_off_for_anyone_but_the_owner_even_with_the_switch_on(name: str) -> None:
    """nexus-f9bgu.27 (code review S2): the actor guard is on every Windows job, not only the rehearsal's.
    Evaluated: switch on, every needed job green, a different actor -> the job does not run."""
    for job_id, job in _win_jobs(name).items():
        ok = {n: "success" for n in _needs(job)}
        assert _eval_if(job["if"], switch="on", actor="someone-else", results=ok) is False, (
            f"{name}:{job_id} runs on {LABEL} for an actor who is not the owner"
        )


def test_the_evaluator_itself_tells_a_guarded_if_from_a_broken_one() -> None:
    good = "${{ !cancelled() && github.actor == 'Hellblazer' && needs.a.result == 'success' && vars.NX_WINDOWS_RELEASE_LEGS == 'on' }}"
    broken = "${{ !cancelled() && github.actor == 'Hellblazer' && needs.a.result == 'success' || vars.NX_WINDOWS_RELEASE_LEGS == 'on' }}"
    ok = {"a": "success"}
    assert _eval_if(good, switch="off", results=ok) is False and _eval_if(good, switch="on", results=ok) is True
    assert _eval_if(broken, switch="off", results=ok) is True, "the broken form runs with the switch off: the check must see it"
    assert _eval_if(good, switch="on", actor="other", results=ok) is False
    assert _eval_if(good, switch="on", results={"a": "failure"}) is False
    with pytest.raises(AssertionError, match="cannot evaluate"):
        _eval_if("${{ github.event_name == 'push' && vars.NX_WINDOWS_RELEASE_LEGS == 'on' }}", switch="off")


def test_the_label_is_a_bare_custom_label_never_an_array_or_self_hosted() -> None:
    for name in WINDOWS_WORKFLOWS:
        for job_id, job in _win_jobs(name).items():
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


@pytest.mark.parametrize("path", _action_files(), ids=lambda p: p.parent.name)
def test_an_action_that_names_the_label_is_called_only_from_jobs_on_it(path: Path) -> None:
    """A composite action cannot declare where it runs; one written for win-release that a hosted-runner job
    (or a pull_request workflow) calls would run Windows-only steps, or worse the other way round. Every
    caller must be a job on the label. Actions that do not name the label are not constrained."""
    text = path.read_text(encoding="utf-8")
    if LABEL not in text:
        return
    ref = f"./.github/actions/{path.parent.name}"
    callers = 0
    for wf in _workflow_files():
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            if any(str(step.get("uses", "")) == ref for step in job.get("steps") or []):
                callers += 1
                assert _names_label(job), f"{wf.name}:{job_id} calls {ref}, which is for {LABEL}, from a job that is not on it"
    assert callers, f"{ref} names {LABEL} and nothing calls it: a dead action, or a caller this test cannot see"


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
    # The conformance suite is inside tests/daemon/, which the widened Windows test set runs whole.
    assert "tests/daemon/" in _run_text(jobs["conformance"])
    assert (REPO / "tests" / "daemon" / "test_rdr149_lifecycle_conformance.py").exists()


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


def _assert_cosign_installs_on_every_run_with_gits_bash(steps: list[dict], install: int) -> None:
    # The first tag run (engine-service-v0.1.148, run 37425299859) failed installing
    # cosign: the installer's `shell: bash` step reached System32\bash.exe, the WSL
    # launcher. Git's bin goes first, and the install runs on every run so a
    # workflow_dispatch proves it before a tag; only signing and upload are tag-gated.
    names = [s.get("name", "") for s in steps]
    assert "if" not in steps[install], "the install must run on a dispatch too, or only a tag can prove it"
    path = steps[install - 1]
    assert path.get("name", "").startswith("Put Git's bin first on PATH"), names[install - 1]
    assert "'C:\\Program Files\\Git\\bin'" in path["run"] and "GITHUB_PATH" in path["run"]
    check = steps[install + 1]
    assert "if" not in check and "cosign version" in check["run"] and "Get-Command bash" in check["run"], names[install + 1]


def test_the_engine_job_signs_and_publishes_on_tags_only_after_the_leg_passed() -> None:
    steps = _engine()["steps"]
    names = [s.get("name", "") for s in steps]
    leg = _step_index(steps, "./.github/actions/windows-engine-leg")
    sign = next(i for i, n in enumerate(names) if n.startswith("Sign release asset"))
    publish = next(i for i, n in enumerate(names) if n.startswith("Upload the engine archive"))
    install = next(i for i, n in enumerate(names) if n.startswith("Install cosign"))
    assert leg < install < sign < publish, names
    for i in (sign, publish):
        assert "refs/tags/engine-service-v" in steps[i]["if"], names[i]
    assert "if" not in steps[leg], "the build, checks and smoke run on every trigger, tag or not"
    _assert_cosign_installs_on_every_run_with_gits_bash(steps, install)


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


PG_JOB = "build-publish-pg-bundle-windows"
PG_ASSETS = (
    "nexus-pg-windows-x64.txz",
    "nexus-pg-windows-x64.txz.sha256",
    "nexus-pg-windows-x64.txz.sigstore.json",
)


@pytest.mark.parametrize(
    "job_name, asset, sign_prefix, upload_prefix, expected",
    [
        (ENGINE, "nexus-service-windows-x64", "Sign release asset", "Upload the engine archive", ENGINE_ASSETS),
        (PG_JOB, "nexus-pg-windows-x64", "Sign PG bundle", "Publish PG bundle", PG_ASSETS),
    ],
    ids=["engine", "pg-bundle"],
)
def test_each_windows_release_job_signs_verifies_and_uploads_its_pinned_asset_set(
    job_name: str, asset: str, sign_prefix: str, upload_prefix: str, expected: tuple[str, ...]
) -> None:
    """The PG job is pinned exactly as the engine job is: the sign step self-verifies (verify-blob,
    this workflow at this ref as the identity, the GitHub OIDC issuer, both calls in the new bundle
    format, both exit codes checked) and the upload names the txz, its digest and its .sigstore.json."""
    job = _doc(RELEASE)["jobs"][job_name]
    assert job["env"]["ASSET"] == asset
    steps = job["steps"]
    names = [s.get("name", "") for s in steps]
    install = next(i for i, n in enumerate(names) if n.startswith("Install cosign"))
    sign_i = next(i for i, n in enumerate(names) if n.startswith(sign_prefix))
    publish_i = next(i for i, n in enumerate(names) if n.startswith(upload_prefix))
    assert install < sign_i < publish_i, names
    for i in (sign_i, publish_i):
        assert "refs/tags/engine-service-v" in steps[i]["if"], names[i]
    _assert_cosign_installs_on_every_run_with_gits_bash(steps, install)
    sign = _code(steps[sign_i]["run"])
    assert "cosign sign-blob" in sign
    assert "cosign verify-blob" in sign, "a signature published without being verified in the same step"
    assert sign.count("--new-bundle-format") == 2, "sign and self-verify both use the format the client verifies"
    assert ".cosign.bundle" not in sign
    assert "engine-service-release.yml@$env:GITHUB_REF" in sign
    assert '--certificate-oidc-issuer "https://token.actions.githubusercontent.com"' in sign
    assert sign.count("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }") == 2, "a failed sign or verify must fail the step"
    assert 'dist\\$env:ASSET.txz"' in sign
    assert steps[sign_i]["env"]["COSIGN_YES"] == "true"
    publish = _code(steps[publish_i]["run"])
    assert "gh release upload" in publish and "--clobber" in publish
    uploaded = re.findall(r'"dist\\\$env:ASSET(\.txz[\w.]*)"', publish)
    assert [f"{asset}{u}" for u in uploaded] == list(expected)


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
    assert build["env"]["NATIVE_IMAGE_OPTIONS"] == (
        "-H:+UnlockExperimentalVMOptions -H:+GenerateEmbeddedResourcesFile --parallelism=8"
    ), "the Windows build keeps the 8-thread cap that protects the box's llama-server (T2 nexus_rdr/224-windows-o2-build-timing)"
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


def test_the_rehearsals_windows_unit_test_steps_do_not_load_the_posix_only_conftest() -> None:
    """tests/conftest.py imports tests/_engine_substrate.py, whose import calls os.getuid(): on Windows
    `pytest` dies loading the conftest before collecting anything (measured on qwentescence,
    nexus-f9bgu.9). The two unit-test steps use no conftest fixture, so they run with --noconftest;
    without it the rehearsal's first step is red for a reason that has nothing to do with the change."""
    jobs = _doc(REHEARSAL)["jobs"]
    steps = [s for job in ("bundle", "engine") for s in jobs[job]["steps"] if "pytest" in s.get("run", "")]
    assert len(steps) == 2, "non-vacuity: one unit-test step in each of the bundle and engine jobs"
    for step in steps:
        assert "--noconftest" in _code(step["run"]), step["name"]
    substrate = (REPO / "tests" / "_engine_substrate.py").read_text(encoding="utf-8")
    assert "os.getuid()" in substrate, (
        "the premise is gone: tests/_engine_substrate.py no longer needs a POSIX uid at import; "
        "re-measure on Windows and drop --noconftest if conftest loads there now"
    )


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


# --------------------------------------------------------------------------- #
# The widened Windows test set (nexus-f9bgu.33/.34, critique S3)
# --------------------------------------------------------------------------- #

#: The Phase 3 Windows modules: a change to any of them must re-run the Windows tests.
PHASE3_WINDOWS_MODULES = (
    "src/nexus/util/win_console.py", "src/nexus/util/win_job.py", "src/nexus/_winsec.py",
    "src/nexus/daemon/windows_autostart.py", "src/nexus/daemon/session_end.py", "src/nexus/daemon/replace_*.py",
    "src/nexus/_install/winproc_core.py", "src/nexus/daemon/binary_install.py",
    "src/nexus/mcp/_win_stdin.py",
    "src/nexus/_install/generation_core.py", "src/nexus/_install/layout_core.py",
    "src/nexus/_install/gc_core.py", "src/nexus/_install/census_core.py",
    "src/nexus/db/pg_provision.py", "src/nexus/db/pg_bundle.py", "src/nexus/commands/daemon.py",
    "src/nexus/daemon/storage_service_daemon.py", "src/nexus/daemon/service_registry.py",
    "src/nexus/daemon/aspect_worker_daemon.py",
)
#: What .44 proved green on native Windows, run as one pytest process.
WINDOWS_TEST_SET = (
    "tests/daemon/", "tests/test_process_group_safety.py", "tests/test_session_sweep_orphan_trackers.py",
    "tests/test_win_console.py", "tests/test_winsec.py", "tests/test_winproc_core.py",
    "tests/test_install_generation_real_windows.py", "tests/test_session_end_real_windows.py",
    "tests/test_mcp_win_stdin.py", "tests/test_mcp_win_stdin_real_windows.py",
    "tests/test_os_trust_store.py", "tests/hooks/test_endpoint_resolve_lease_retry.py",
    # Phase 4 (nexus-f9bgu.38 finding C): the desktop bootstrap and the uv hook launcher.
    "tests/test_mcpb_bootstrap.py", "tests/hooks/test_hook_launcher_uv.py",
)
#: The Phase 4 code that only a native Windows run exercises, and the helper its tests import.
PHASE4_WINDOWS_PATHS = (
    "mcpb/src/bootstrap.py", "conexus/hooks/**", "tests/_hook_wiring.py",
)


def test_the_rehearsal_triggers_on_every_phase3_windows_module_and_its_tests() -> None:
    paths = _triggers(_doc(REHEARSAL))["push"]["paths"]
    for module in PHASE3_WINDOWS_MODULES:
        assert module in paths, module
    assert "tests/daemon/**" in paths
    for test in WINDOWS_TEST_SET[1:]:
        assert test in paths, test


def test_the_rehearsal_triggers_on_the_phase4_bootstrap_and_hook_launcher() -> None:
    paths = _triggers(_doc(REHEARSAL))["push"]["paths"]
    for path in PHASE4_WINDOWS_PATHS:
        assert path in paths, path


def test_the_windows_job_runs_the_whole_set_in_one_pytest_with_a_junit_floor() -> None:
    job = _doc(REHEARSAL)["jobs"]["conformance"]
    run_steps = [s["run"] for s in job["steps"] if "pytest" in s.get("run", "")]
    assert len(run_steps) == 1, "non-vacuity: exactly one pytest step in the job"
    for target in WINDOWS_TEST_SET:
        assert target in run_steps[0], target
    assert "--junitxml=windows-tests-junit.xml" in run_steps[0]
    # default addopts stay: integration, slow and lint tests need services a runner step does not have.
    assert 'addopts=""' not in run_steps[0]
    floor = next(s for s in job["steps"] if "check_junit_floor.py" in s.get("run", ""))
    assert "windows-tests-junit.xml" in floor["run"]
    # a floor that can be passed by a mostly-skipped or much smaller run is no floor
    passed = int(re.search(r"--min-passed (\d+)", floor["run"]).group(1))
    skipped = int(re.search(r"--max-skipped (\d+)", floor["run"]).group(1))
    assert passed >= 1000, passed
    assert skipped <= passed // 20, f"{skipped} skips against a {passed} pass floor is a mostly-skipped run"


#: The tests only a real Windows run exercises. Each is a ``--require-passed`` pattern in the floor
#: step, and each must exist in the suite: a pattern that matches nothing fails the gate on its first
#: run, so a rename would otherwise surface only on the Windows runner.
REAL_KERNEL_PATTERNS = (
    "tests.test_winsec.TestRealWindows",
    "tests.test_winproc_core.TestRealWindowsKernel",
    "tests.test_os_trust_store.TestRealWindows",
    "tests.test_install_generation_real_windows.TestRealWindows",
    "tests.daemon.test_pid_alive_windows.TestRealWindowsKernel",
    "tests.test_session_end_real_windows.TestRealWindows",
    "tests.test_mcp_win_stdin_real_windows.TestRealWindows",
    "test_hard_killing_the_supervisor_takes_its_job_engine_with_it",
    "tests.test_mcpb_bootstrap.TestRealWindows",
    "test_the_credential_guard_still_denies_through_the_launcher",
)


def test_the_floor_names_the_real_kernel_tests_that_must_have_passed() -> None:
    job = _doc(REHEARSAL)["jobs"]["conformance"]
    floor = next(s for s in job["steps"] if "check_junit_floor.py" in s.get("run", ""))
    named = re.findall(r"--require-passed (\S+)", floor["run"])
    assert tuple(named) == REAL_KERNEL_PATTERNS
    root = Path(__file__).parent.parent
    pytest_run = next(s["run"] for s in job["steps"] if "pytest" in s.get("run", ""))
    for pattern in named:
        if pattern.startswith("tests."):
            parts = pattern.split(".")
            file = root.joinpath(*parts[:-1]).with_suffix(".py")
            assert file.is_file(), f"{pattern}: no module {file}"
            # The module must be in the run, or the floor requires a test that never
            # ran (rehearsal run 37411668705: tests/test_winproc_core.py was missing).
            rel = file.relative_to(root).as_posix()
            assert rel in pytest_run or any(
                rel.startswith(t) for t in pytest_run.split() if t.endswith("/")
            ), f"{pattern}: {rel} is not in the Windows test set's pytest command"
            assert f"class {parts[-1]}" in file.read_text(encoding="utf-8"), f"{pattern}: no such class"
        else:
            hits = [
                p for p in (root / "tests").rglob("*.py")
                if f"def {pattern}" in p.read_text(encoding="utf-8")
            ]
            assert hits, f"{pattern}: no such test function"


def test_the_windows_job_keeps_the_switch_the_actor_guard_and_never_runs_on_pull_request() -> None:
    doc = _doc(REHEARSAL)
    job = doc["jobs"]["conformance"]
    assert "github.actor == 'Hellblazer'" in str(job["if"])
    assert SWITCH in str(job["if"])
    assert "pull_request" not in _triggers(doc)
    assert doc["concurrency"]["cancel-in-progress"] is True


def test_the_python_resolver_runs_native_commands_with_both_streams_redirected() -> None:
    # pwsh 7.6 reported an "Access is denied" starting uv.exe under the runner
    # service as "StandardOutputEncoding is only supported when standard output
    # is redirected" (seed run 37408239980). Driving every native call through
    # Process with both streams redirected surfaces the OS error instead.
    action = yaml.safe_load((ACTIONS / "resolve-windows-python" / "action.yml").read_text())
    run = _code(action["runs"]["steps"][0]["run"])
    assert "2>$null" not in run and "2>&1" not in run
    assert "RedirectStandardOutput = $true" in run and "RedirectStandardError = $true" in run
    assert not re.search(r"&\s*(?:uv|\$py)\b", run), "a bare `& uv`/`& $py` reintroduces pwsh's own native-command path"
    assert "'python', 'install', '3.13'" in run, "uv keeps Pythons per user; a fresh service account has none"


def test_the_windows_test_job_repairs_the_checkout_before_pytest() -> None:
    # Git for Windows' system config (autocrlf=true, symlinks=false) and an account
    # without the symlink privilege leave CRLF text and symlink stub files; 32
    # tests failed on the missing daemon resources (rehearsal run 37411668705).
    steps = _doc(REHEARSAL)["jobs"]["conformance"]["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    fix = names.index("Checkout fixups (LF, symlinks materialised)")
    assert names[fix - 1].startswith("actions/checkout@")
    assert fix < names.index("Windows test set")
    run = steps[fix]["run"]
    assert "git config core.autocrlf false" in run and "git reset -q --hard HEAD" in run
    assert "^120000 " in run, "tracked symlinks are found by their git mode"
    assert "throw 'no tracked symlinks found" in run, "non-vacuity: a step with nothing to do fails"
    attrs = (REPO / ".gitattributes").read_text()
    assert "tests/fixtures/** -text" in attrs


@pytest.mark.parametrize(("name", "job"), [(RELEASE, "build-publish-pg-bundle-windows"), (SEED, "seed-windows")])
def test_the_windows_cache_jobs_append_gits_usr_bin_before_the_cache_step(name: str, job: str) -> None:
    # Git's tar -z runs gzip from Git's usr\bin, which the runner service's PATH
    # lacks: seed run 37421965631 saved nothing ("gzip: command not found").
    steps = _doc(name)["jobs"][job]["steps"]
    idx = next(i for i, s in enumerate(steps) if "usr\\bin to PATH" in s.get("name", ""))
    cache = next(i for i, s in enumerate(steps) if "actions/cache" in s.get("uses", ""))
    assert idx < cache, "the PATH must hold gzip before the restore, which unpacks with it too"
    run = steps[idx]["run"]
    assert '"PATH=$env:PATH;$d" >> $env:GITHUB_ENV' in run, "appended through GITHUB_ENV"
    assert "GITHUB_PATH" not in run, "GITHUB_PATH prepends: msys perl/link/find would shadow the build's"
    assert "gzip.exe" in run, "fails loud when Git's gzip is absent"
