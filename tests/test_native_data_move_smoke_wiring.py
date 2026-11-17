"""nexus-f3yqd.2.22: the native data-move smoke is wired into the engine release, and its pieces agree.

The smoke runs only in the release workflow, on a runner nobody has locally, so what can be proven here is the
shape: the job exists, builds nothing, downloads the artifacts the two build jobs upload, gates promotion, runs
the script; the script boots both PostgreSQL shapes and hands each log to the checker with a secret per database
role; and the probe route the script requests is the route the checker looks for.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "engine-service-release.yml"
SMOKE = ROOT / "service" / "native-data-move-smoke.sh"
CHECKER = ROOT / "scripts" / "check_native_data_move_boot.py"

JOB = "smoke-native-data-move"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _checker():
    spec = importlib.util.spec_from_file_location("check_native_data_move_boot_wiring", CHECKER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_job_gates_the_promotion_and_covers_both_linux_arches() -> None:
    jobs = _jobs()
    job = jobs[JOB]
    assert set(job["needs"]) == {"build-publish", "build-publish-pg-bundle"}
    assert "needs.build-publish.result == 'success'" in job["if"]
    assert "needs.build-publish-pg-bundle.result == 'success'" in job["if"]
    arches = sorted(e["arch"] for e in job["strategy"]["matrix"]["include"])
    assert arches == ["linux-amd64", "linux-arm64"]
    promote = jobs["promote-release"]
    assert JOB in promote["needs"]
    assert f"needs.{JOB}.result == 'success'" in promote["if"]


def test_the_job_downloads_what_the_build_jobs_upload_and_builds_nothing() -> None:
    job = _jobs()[JOB]
    downloads = [s["with"]["name"] for s in job["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact")]
    # build-publish uploads name: ASSET (nexus-service-<arch>); build-publish-pg-bundle uploads nexus-pg-<arch>.
    assert downloads == ["nexus-service-${{ matrix.arch }}", "nexus-pg-${{ matrix.arch }}"]
    text = "\n".join(s.get("run", "") for s in job["steps"])
    assert "mvnw" not in text and "native-image" not in text and "docker" not in text.lower()
    smoke_steps = [s for s in job["steps"] if "native-data-move-smoke.sh" in s.get("run", "")]
    assert len(smoke_steps) == 1
    env = smoke_steps[0]["env"]
    assert set(env) == {"BIN", "PG_BUNDLE"}
    assert smoke_steps[0]["working-directory"] == "service"


def test_the_artifact_names_the_job_downloads_are_the_names_the_build_jobs_upload() -> None:
    jobs = _jobs()
    assert jobs["build-publish"]["env"]["ASSET"] == "nexus-service-${{ matrix.target.arch }}"
    assert jobs["build-publish-pg-bundle"]["env"]["ASSET"] == "nexus-pg-${{ matrix.target.arch }}"
    for name in ("build-publish", "build-publish-pg-bundle"):
        uploads = [s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
        assert any(u["with"]["name"] == "${{ env.ASSET }}" for u in uploads), name


def test_the_script_boots_both_shapes_and_hands_each_log_to_the_checker_with_both_secrets() -> None:
    text = SMOKE.read_text()
    assert re.search(r"for mode in with-pg-background without-pg-background", text)
    assert "check_native_data_move_boot.py" in text
    assert '--mode "$mode"' in text
    assert "--secret svc=SMOKE_SVC_PASS" in text and "--secret admin=SMOKE_ADMIN_PASS" in text
    # Non-vacuity of the two shapes: the without shape removes a control file it checked exists, the with shape needs one.
    assert "pg_background.control" in text
    # The engine has a separate admin role, or it would skip the libraries as the single-role posture.
    assert "NX_DB_ADMIN_USER" in text and "one role" in text


def test_the_probe_route_in_the_script_is_the_route_the_checker_looks_for() -> None:
    checker = _checker()
    m = re.search(r'^PROBE_PATH_PREFIX="([^"]+)"', SMOKE.read_text(), re.M)
    assert m, "the script must define PROBE_PATH_PREFIX"
    assert m.group(1) == checker.PROBE_PATH_PREFIX
    # The route must be one whose context carries the auth filter (an unmatched path is not logged).
    service = (ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "NexusService.java").read_text()
    assert f'server.createContext("{checker.PROBE_PATH_PREFIX.rstrip("/")}"' in service


def test_the_labels_the_script_probes_are_the_labels_the_checker_is_given() -> None:
    text = SMOKE.read_text()
    for label in ("svc", "admin"):
        assert f"${{PROBE_PATH_PREFIX}}{label}-" in text


def _pack_like_the_release(tmp_path: Path) -> Path:
    """A txz packed the way build-publish-pg-bundle packs it: tar -cJf -C <parent> <basename>, top directory kept."""
    import subprocess

    bundle = tmp_path / "src" / "pgsql"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "initdb").write_text("#!/bin/sh\n")
    (bundle / "share" / "extension").mkdir(parents=True)
    (bundle / "share" / "extension" / "pg_background.control").write_text("")
    txz = tmp_path / "nexus-pg-linux-amd64.txz"
    subprocess.run(["tar", "-cJf", str(txz), "-C", str(bundle.parent), bundle.name], check=True)
    return txz


def test_the_script_finds_the_bundle_root_in_the_release_layout(tmp_path):
    """The release keeps the bundle's top directory, so initdb is three levels below the extraction dir; a lookup
    that stops at two finds nothing and fails the job on every tag (review of 9a334f58c)."""
    import subprocess

    txz = _pack_like_the_release(tmp_path)
    out = tmp_path / "x"
    out.mkdir()
    r = subprocess.run(["bash", str(SMOKE), "--locate-bundle", str(txz), str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    root = Path(r.stdout.strip())
    assert root == out / "pgsql", r.stdout
    assert (root / "bin" / "initdb").is_file()
    assert (root / "share" / "extension" / "pg_background.control").is_file()


def test_the_script_fails_loud_on_a_bundle_with_no_initdb(tmp_path):
    import subprocess

    empty = tmp_path / "src" / "pgsql" / "lib"
    empty.mkdir(parents=True)
    txz = tmp_path / "b.txz"
    subprocess.run(["tar", "-cJf", str(txz), "-C", str(empty.parent.parent), "pgsql"], check=True)
    out = tmp_path / "x"
    out.mkdir()
    r = subprocess.run(["bash", str(SMOKE), "--locate-bundle", str(txz), str(out)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "no bin/initdb" in r.stdout
