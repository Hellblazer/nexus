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


def test_the_fetch_writes_its_tarball_under_the_runner_temp_dir_not_a_fixed_tmp_path(tmp_path) -> None:
    """On a self-hosted box shared by several users a fixed /tmp name collides across users (and is
    a symlink-replacement target). Run the real fetch step with a stub curl and see where it writes."""
    import os
    import subprocess
    import tarfile

    action = _load_action()
    step = next(s for s in action["runs"]["steps"] if "releases/download" in s.get("run", ""))
    assert "/tmp/docling-artifacts" not in step["run"]

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "model.bin").write_text("x")
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(payload / "model.bin", arcname="model.bin")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    seen = tmp_path / "curl-target"
    stub = bindir / "curl"
    stub.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
        f'printf %s "$out" > {seen}\n'
        f'cp {bundle} "$out"\n'
    )
    stub.chmod(0o755)
    home, runner_temp = tmp_path / "home", tmp_path / "runner_temp"
    home.mkdir()
    runner_temp.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "RUNNER_TEMP": str(runner_temp),
           "ASSET_TAG": "ci-assets-docling-v1", "GITHUB_REPOSITORY": "o/r"}
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    target = seen.read_text()
    assert target.startswith(str(runner_temp) + os.sep) and "docling-artifacts" in target
    assert not os.path.exists(target), "the tarball is removed when the step ends"
    assert (home / ".cache/nexus/docling-artifacts/model.bin").read_text() == "x"


def _run_fetch_step(tmp_path, *, persistent: bool, home=None):
    """Run the real fetch step with a stub curl that counts its calls. Returns (proc, home, calls)."""
    import subprocess
    import tarfile

    action = _load_action()
    step = next(s for s in action["runs"]["steps"] if "releases/download" in s.get("run", ""))
    work = tmp_path / f"work-{len(list(tmp_path.iterdir()))}"
    work.mkdir()
    (work / "model.bin").write_text("x")
    bundle = work / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(work / "model.bin", arcname="model.bin")
    bindir = work / "bin"
    bindir.mkdir()
    calls = work / "curl-calls"
    stub = bindir / "curl"
    stub.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
        f"echo call >> {calls}\n"
        f'cp {bundle} "$out"\n'
    )
    stub.chmod(0o755)
    if home is None:
        home = tmp_path / "home"
        home.mkdir()
    runner_temp = work / "runner_temp"
    runner_temp.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "RUNNER_TEMP": str(runner_temp),
           "ASSET_TAG": "ci-assets-docling-v1", "GITHUB_REPOSITORY": "o/r",
           "PERSISTENT_HOME": "true" if persistent else "false"}
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=env)
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    return proc, home, n


def test_the_cache_step_is_skipped_on_a_persistent_home() -> None:
    """nexus-480wm: on the lab pool $HOME is shared with jobs running at the same time, and a
    cache restore would rewrite files a sibling job is reading."""
    action = _load_action()
    assert action["inputs"]["persistent-home"]["default"] == "false"
    cache_step = next(s for s in action["runs"]["steps"] if s.get("uses", "").startswith("actions/cache@"))
    assert cache_step.get("if") == "inputs.persistent-home != 'true'"
    fetch_step = next(s for s in action["runs"]["steps"] if "releases/download" in s.get("run", ""))
    assert fetch_step["env"]["PERSISTENT_HOME"] == "${{ inputs.persistent-home }}"


def test_a_persistent_home_fetches_once_and_installs_a_complete_directory(tmp_path) -> None:
    proc, home, calls = _run_fetch_step(tmp_path, persistent=True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    target = home / ".cache/nexus/docling-artifacts"
    assert calls == 1
    assert (target / "model.bin").read_text() == "x"
    assert (target / ".complete-ci-assets-docling-v1").is_file()
    assert [p.name for p in target.parent.iterdir()] == ["docling-artifacts"], "no staging directory is left"

    again, _, calls = _run_fetch_step(tmp_path, persistent=True, home=home)
    assert again.returncode == 0, (again.stdout, again.stderr)
    assert calls == 0, "a complete directory on disk is used, not downloaded again"
    assert "already on disk" in again.stdout


def test_a_persistent_home_replaces_a_directory_with_no_completion_marker(tmp_path) -> None:
    """A directory without the marker is another asset tag's files or an extract that died."""
    home = tmp_path / "home"
    target = home / ".cache/nexus/docling-artifacts"
    target.mkdir(parents=True)
    (target / "stale.bin").write_text("old")
    proc, _, calls = _run_fetch_step(tmp_path, persistent=True, home=home)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert calls == 1
    assert not (target / "stale.bin").exists()
    assert (target / "model.bin").read_text() == "x"


def test_a_sibling_job_that_installed_first_wins_and_the_loser_cleans_up(tmp_path) -> None:
    """The race: both jobs saw no directory, the sibling renamed its copy in while this one
    downloaded. A stub tar makes the sibling's directory appear during this job's extract."""
    import subprocess
    import tarfile

    action = _load_action()
    step = next(s for s in action["runs"]["steps"] if "releases/download" in s.get("run", ""))
    home = tmp_path / "home"
    target = home / ".cache/nexus/docling-artifacts"
    (tmp_path / "model.bin").write_text("x")
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(tmp_path / "model.bin", arcname="model.bin")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
        f'cp {bundle} "$out"\n'
    )
    (bindir / "tar").write_text(
        "#!/bin/sh\n"
        '/usr/bin/tar "$@" || exit 1\n'
        f'mkdir -p {target}\n'
        f'echo sibling > {target}/model.bin\n'
        f'touch {target}/.complete-ci-assets-docling-v1\n'
    )
    for name in ("curl", "tar"):
        (bindir / name).chmod(0o755)
    home.mkdir()
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "RUNNER_TEMP": str(runner_temp),
           "ASSET_TAG": "ci-assets-docling-v1", "GITHUB_REPOSITORY": "o/r", "PERSISTENT_HOME": "true"}
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "sibling job provisioned" in proc.stdout
    assert (target / "model.bin").read_text() == "sibling\n"
    assert [p.name for p in target.parent.iterdir()] == ["docling-artifacts"], "the loser removes its staging directory"
