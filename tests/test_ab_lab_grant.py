# SPDX-License-Identifier: AGPL-3.0-or-later
"""The A/B harness sizes its containers to the lab-run grant (nexus-kani0).

dockerd starts a container outside the grant's cgroup, so under lab-run each
batch script passes the granted cores and an equal share of LAB_MEM_MB to
``docker run``; outside lab-run it passes nothing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

AB = Path(__file__).resolve().parent / "e2e" / "prefix-capture" / "ab"
HELPER = AB / "lab_grant.sh"

#: Every script that runs a container, one at a time and in the foreground.
#: skillroute/pe_detach.sh detaches lab-run around itself, never the container.
BATCH_SCRIPTS = (
    "ab_batch.sh",
    "skillroute/pe_detach.sh",
    "tuplespace/ts_batch.sh",
    "seqthink/sq_batch.sh",
    "skillroute/sr_batch.sh",
)


def _flags(env: dict[str, str], n: int = 1) -> list[str]:
    script = f'. "{HELPER}"; lab_docker_flags {n}; printf "%s\\n" "${{LAB_DOCKER_FLAGS[@]+"${{LAB_DOCKER_FLAGS[@]}}"}}"'
    out = subprocess.run(
        ["bash", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_outside_lab_run_the_docker_run_is_unchanged() -> None:
    assert _flags({}) == []


@pytest.mark.parametrize("n, share", [(1, 4096), (2, 2048), (3, 1365)])
def test_under_lab_run_each_container_gets_its_share_with_no_swap(n: int, share: int) -> None:
    flags = _flags({"LAB_MEM_MB": "4096"}, n)
    assert f"--memory={share}m" in flags
    assert f"--memory-swap={share}m" in flags


def test_the_cores_come_from_the_grants_cpuset_when_there_is_one() -> None:
    cgroup = Path("/proc/self/cgroup")
    flags = _flags({"LAB_MEM_MB": "4096"})
    cpuset = [f for f in flags if f.startswith("--cpuset-cpus=")]
    if not cgroup.exists():
        # macOS: no cgroup, so no core pinning, and the memory cap still applies.
        assert cpuset == []
        return
    rel = cgroup.read_text().strip().split(":", 2)[-1]
    eff = Path(f"/sys/fs/cgroup{rel}/cpuset.cpus.effective")
    if eff.exists():
        assert cpuset == [f"--cpuset-cpus={eff.read_text().strip()}"]
    else:
        assert cpuset == []


@pytest.mark.parametrize("rel", BATCH_SCRIPTS)
def test_every_batch_docker_run_carries_the_grant_flags(rel: str) -> None:
    text = (AB / rel).read_text(encoding="utf-8")
    runs = re.findall(r"^\s*docker run .*$", text, re.M)
    assert runs, f"{rel}: no docker run found; the census would be vacuous"
    for run in runs:
        assert '"${LAB_DOCKER_FLAGS[@]+"${LAB_DOCKER_FLAGS[@]}"}"' in run, f"{rel}: {run.strip()}"
    assert re.search(r"^lab_docker_flags \d+", text, re.M), f"{rel}: never calls lab_docker_flags"


def test_the_census_names_every_foreground_launcher() -> None:
    """A new batch script that starts containers must join BATCH_SCRIPTS, and
    none may detach the container itself: it would outlive the grant."""
    found = {
        str(p.relative_to(AB)) for p in AB.rglob("*.sh")
        if re.search(r"^[^#\n]*docker run ", p.read_text(encoding="utf-8"), re.M)
    }
    assert found == set(BATCH_SCRIPTS), found
    for rel in found:
        for run in re.findall(r"^[^#\n]*docker run .*$", (AB / rel).read_text(encoding="utf-8"), re.M):
            assert not re.search(r"setsid|nohup|\s-d\b|--detach", run), f"{rel}: {run.strip()}"
