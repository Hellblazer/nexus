# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI wiring pins for the lab-pool pytest job: bytecode on ``/dev/shm`` (nexus-m643n, "F3") and
the shared dependencies venv (nexus-dvk18, "F5"), which excludes F3.

F5 (second half of this file) is pinned by ``shared_venv_problems``: ``scripts/ci_shared_venv.sh``
runs once after setup-uv, the plain ``uv sync`` is skipped when it took the job, every step that
runs uv gets ``UV_PROJECT_ENVIRONMENT`` from its output (never job-wide, never empty) and
``UV_NO_SYNC`` as ``1``/``0`` (never empty), and F3 runs only when F5 did not.

nexus-m643n: every lab-pool pytest job keeps its bytecode on ``/dev/shm``.

A pool shard's collection writes ~116 MB of ``.pyc`` to the host's NVMe; with
``PYTHONPYCACHEPREFIX`` on a per-job ``/dev/shm`` directory the disk write is ~23 MB
(T2 ``nexus/analysis-ci-startup-bytes-2026-10-19``). The pin: each workflow job that
runs ``pytest`` on a ``lab-*`` runner has (1) a pool-only step, before the first step
that runs anything Python, that creates a per-job directory under ``/dev/shm`` and
exports it as ``PYTHONPYCACHEPREFIX`` through ``GITHUB_ENV``, and (2) a LAST step
guarded by ``always()`` that removes it. The job count is asserted non-zero: a scan
that found no pool pytest job passed nothing.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_LAB_RUNNER = re.compile(r"['\"]lab-[a-z0-9]+")
_PYTHON_USERS = re.compile(r"\b(uv|pytest|python3?)\b")


def pool_pytest_jobs(docs: dict[str, dict]) -> list[tuple[str, str, dict]]:
    """(file, job id, job spec) for every job that runs pytest on a ``lab-*`` runner."""
    found = []
    for fname, doc in docs.items():
        for jid, spec in (doc.get("jobs") or {}).items():
            if not _LAB_RUNNER.search(str(spec.get("runs-on", ""))):
                continue
            if any("pytest" in str(st.get("run", "")) for st in spec.get("steps", [])):
                found.append((fname, jid, spec))
    return found


def prefix_problems(spec: dict) -> list[str]:
    """Every way a pool pytest job's bytecode-prefix wiring is wrong; empty means wired."""
    steps = spec.get("steps", [])
    problems: list[str] = []
    setup = [
        i for i, st in enumerate(steps)
        if "PYTHONPYCACHEPREFIX=" in str(st.get("run", "")) and "GITHUB_ENV" in str(st.get("run", ""))
    ]
    if len(setup) != 1:
        return [f"expected exactly one step exporting PYTHONPYCACHEPREFIX via GITHUB_ENV, found {len(setup)}"]
    i = setup[0]
    st = steps[i]
    run = str(st["run"])
    cond = str(st.get("if", ""))
    if "self-hosted" not in cond or "Linux" not in cond:
        problems.append(f"setup step must be pool-only (self-hosted and Linux), if: {cond!r}")
    if "steps.sharedvenv.outputs.used != 'true'" not in cond:
        problems.append(f"F3 must be excluded when F5 took the job (PYTHONPYCACHEPREFIX hides the shared venv's pyc), if: {cond!r}")
    dirs = str(st.get("env", {}).get("NX_PYC_DIR", ""))
    if not dirs.startswith("/dev/shm/"):
        problems.append(f"the prefix directory must sit under /dev/shm/, got {dirs!r}")
    for part in ("github.run_id", "github.run_attempt", "matrix.python-version", "matrix.shard"):
        if part not in dirs:
            problems.append(f"the prefix directory is not unique per job: {part} missing from {dirs!r}")
    if "mkdir -m 0700" not in run:
        problems.append("the prefix directory must be created 0700 (private to the job user)")
    if "PYTHONPYCACHEPREFIX=$NX_PYC_DIR" not in run:
        problems.append("the exported prefix is not the per-job NX_PYC_DIR")
    # setup-uv and the F5 step (id sharedvenv) legitimately come first: F3 reads F5's output, and
    # neither imports project code under a bytecode prefix (F5 sets none; its job is the other case).
    early = [
        j for j, other in enumerate(steps[:i])
        if other.get("id") != "sharedvenv" and "setup-uv" not in str(other.get("uses", ""))
        and _PYTHON_USERS.search(str(other.get("run", "")))
    ]
    if early:
        problems.append(f"steps {early} run Python or uv before the prefix is exported")
    last = steps[-1]
    lcond = str(last.get("if", ""))
    lrun = str(last.get("run", ""))
    if "always()" not in lcond:
        problems.append(f"the last step must be guarded by always(), if: {lcond!r}")
    if "rm -rf" not in lrun or "PYTHONPYCACHEPREFIX" not in lrun or "/dev/shm/nx-pyc-" not in lrun:
        problems.append("the last step does not remove the nx-pyc-* prefix directory")
    return problems


def _load() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(WORKFLOWS.glob("*.yml"))}


def test_every_lab_pool_pytest_job_keeps_bytecode_on_dev_shm() -> None:
    jobs = pool_pytest_jobs(_load())
    # non-vacuity: today exactly the ci.yml `test` job; a scan that found none passed nothing
    assert [(f, j) for f, j, _ in jobs] == [("ci.yml", "test")], (
        f"pool pytest jobs found: {[(f, j) for f, j, _ in jobs]}; a new one needs the same wiring and a line here"
    )
    for fname, jid, spec in jobs:
        assert prefix_problems(spec) == [], f"{fname}:{jid}"


def _ci_test_job() -> dict:
    return copy.deepcopy(_load()["ci.yml"]["jobs"]["test"])


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda s: [st.pop("env") for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})], "under /dev/shm"),
        (lambda s: [st.pop("if") for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})], "pool-only"),
        (lambda s: s["steps"][-1].pop("if"), "always()"),
        (lambda s: s["steps"].pop(-1), "always()"),
        (
            lambda s: [st.update(env={"NX_PYC_DIR": "/dev/shm/nx-pyc-fixed"}) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "not unique per job",
        ),
        (lambda s: s["steps"].insert(0, {"run": "uv sync"}), "before the prefix is exported"),
        (
            lambda s: [st.update({"if": st["if"].replace(" && steps.sharedvenv.outputs.used != 'true'", "")}) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "excluded when F5 took the job",
        ),
        (
            lambda s: [st["env"].update(NX_PYC_DIR=st["env"]["NX_PYC_DIR"].replace("-py${{ matrix.python-version }}", "")) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "matrix.python-version missing",
        ),
        (
            lambda s: [st.update(run=st["run"].replace("mkdir -m 0700", "mkdir")) for st in s["steps"] if "NX_PYC_DIR" in st.get("env", {})],
            "created 0700",
        ),
    ],
    ids=["no-dir", "not-pool-only", "cleanup-not-always", "cleanup-missing", "not-unique", "uv-before-export", "f3-not-excluded-by-f5", "no-python-version", "not-0700"],
)
def test_each_wiring_break_is_caught(mutate, expect) -> None:  # noqa: ANN001
    spec = _ci_test_job()
    mutate(spec)
    problems = prefix_problems(spec)
    assert any(expect in p for p in problems), problems


def test_a_job_without_the_export_is_caught() -> None:
    spec = _ci_test_job()
    spec["steps"] = [st for st in spec["steps"] if "PYTHONPYCACHEPREFIX=" not in str(st.get("run", ""))]
    assert "found 0" in prefix_problems(spec)[0]


# ---- F5: the shared dependencies venv (nexus-dvk18) ---------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_shared_venv.sh"
_VENV_EXPR = "steps.sharedvenv.outputs.venv"
_USED_EXPR = "steps.sharedvenv.outputs.used"


def _runs_uv(step: dict) -> bool:
    return bool(re.search(r"\buv (run|sync)\b", str(step.get("run", ""))))


def shared_venv_problems(spec: dict, script_text: str) -> list[str]:
    """Every way a pool pytest job's F5 wiring is wrong; empty means wired."""
    steps = spec.get("steps", [])
    problems: list[str] = []
    f5 = [i for i, st in enumerate(steps) if st.get("id") == "sharedvenv"]
    if len(f5) != 1:
        return [f"expected exactly one step with id sharedvenv, found {len(f5)}"]
    i = f5[0]
    st = steps[i]
    if "scripts/ci_shared_venv.sh" not in str(st.get("run", "")):
        problems.append("the sharedvenv step does not run scripts/ci_shared_venv.sh")
    cond = str(st.get("if", ""))
    if "self-hosted" not in cond or "Linux" not in cond:
        problems.append(f"the sharedvenv step must be pool-only (self-hosted and Linux), if: {cond!r}")
    uvs = [j for j, o in enumerate(steps) if "setup-uv" in str(o.get("uses", ""))]
    if not uvs or min(uvs) > i:
        problems.append("the sharedvenv step must come after setup-uv (it needs uv and the interpreter)")

    syncs = [j for j, o in enumerate(steps) if re.search(r"\buv sync\b", str(o.get("run", "")))]
    if len(syncs) != 1:
        problems.append(f"expected exactly one `uv sync` step, found {len(syncs)}")
    for j in syncs:
        if j < i:
            problems.append("the `uv sync` step runs before the sharedvenv step")
        if f"{_USED_EXPR} != 'true'" not in str(steps[j].get("if", "")):
            problems.append(f"the `uv sync` step must be skipped when F5 took the job, if: {steps[j].get('if')!r}")

    uv_steps = [j for j, o in enumerate(steps) if _runs_uv(o)]
    if len(uv_steps) < 2:
        problems.append(f"non-vacuity: expected the sync and pytest steps to run uv, found {len(uv_steps)}")
    for j in uv_steps:
        senv = steps[j].get("env", {}) or {}
        pe = str(senv.get("UV_PROJECT_ENVIRONMENT", ""))
        # the F1 fallback: not-used jobs get the venv the script reported (disk under LAB_WS=tmpfs),
        # and a runner that skipped the step (github-hosted) falls back to uv's own default
        if _VENV_EXPR not in pe or "github.workspace" not in pe or "||" not in pe:
            problems.append(f"step {steps[j].get('name')!r}: UV_PROJECT_ENVIRONMENT must be ${{{{ {_VENV_EXPR} || <default> }}}}, got {pe!r}")
        if "uv sync" in str(steps[j].get("run", "")):
            continue
        ns = str(senv.get("UV_NO_SYNC", ""))
        if _USED_EXPR not in ns or "'1'" not in ns or "'0'" not in ns:
            problems.append(f"step {steps[j].get('name')!r}: UV_NO_SYNC must be 1 when F5 is used and '0' (never empty) otherwise, got {ns!r}")

    for key in ("UV_PROJECT_ENVIRONMENT", "UV_NO_SYNC"):
        if key in (spec.get("env") or {}):
            problems.append(f"{key} is set job-wide; a job-wide value pulls other projects' `uv run` into this venv")
    for j, o in enumerate(steps):
        run = str(o.get("run", ""))
        if "GITHUB_ENV" in run and ("UV_PROJECT_ENVIRONMENT" in run or "UV_NO_SYNC" in run):
            problems.append(f"step {j} writes a uv variable to GITHUB_ENV")
    live = [ln for ln in script_text.splitlines() if "GITHUB_ENV" in ln and not ln.lstrip().startswith("#")]
    if live:
        problems.append(f"scripts/ci_shared_venv.sh writes GITHUB_ENV: {live}")
    for needle in ("LAB_WS", "LAB_DISK_DIR", "GITHUB_OUTPUT", "--no-install-project", "--compile-bytecode"):
        if needle not in script_text:
            problems.append(f"scripts/ci_shared_venv.sh lost {needle!r}")
    # F1 gate: without a tmpfs workspace F5 would drop F3 (the prefix step is skipped whenever F5 is used)
    # and leave the thin venv and the project's pyc on NVMe
    if not re.search(r'(?m)^\[ "\$\{LAB_WS:-\}" = tmpfs \] \|\| fallback ', script_text):
        problems.append("scripts/ci_shared_venv.sh must fall back unless LAB_WS=tmpfs (F5 without F1 loses F3's saving)")
    # the interpreter of the shared entry must never be started: it may live under another job's RUNNER_TEMP
    if re.search(r'"\$shared/bin/python"', script_text):
        problems.append("scripts/ci_shared_venv.sh starts the shared entry's own interpreter (it can be gone)")
    return problems


def test_every_lab_pool_pytest_job_is_wired_for_the_shared_venv() -> None:
    jobs = pool_pytest_jobs(_load())
    assert [(f, j) for f, j, _ in jobs] == [("ci.yml", "test")]
    text = _SCRIPT.read_text(encoding="utf-8")
    for fname, jid, spec in jobs:
        assert shared_venv_problems(spec, text) == [], f"{fname}:{jid}"


def test_f3_and_f5_are_ordered_f5_first() -> None:
    steps = _ci_test_job()["steps"]
    f5 = next(i for i, st in enumerate(steps) if st.get("id") == "sharedvenv")
    f3 = next(i for i, st in enumerate(steps) if "PYTHONPYCACHEPREFIX=" in str(st.get("run", "")))
    assert f5 < f3, "F3 reads F5's output, so F5 must run first"


def _uv_steps(s: dict) -> list[dict]:
    return [st for st in s["steps"] if _runs_uv(st)]


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda s: [st.pop("id") for st in s["steps"] if st.get("id") == "sharedvenv"], "exactly one step with id"),
        (lambda s: [st.update(run="uv sync") for st in s["steps"] if st.get("id") == "sharedvenv"], "does not run scripts/ci_shared_venv.sh"),
        (lambda s: [st.update({"if": "true"}) for st in s["steps"] if st.get("id") == "sharedvenv"], "pool-only"),
        (lambda s: [st.pop("if") for st in s["steps"] if re.search(r"\buv sync\b", str(st.get("run", "")))], "skipped when F5 took the job"),
        (lambda s: [st["env"].pop("UV_PROJECT_ENVIRONMENT") for st in _uv_steps(s) if "pytest" in st["run"]], "UV_PROJECT_ENVIRONMENT must be"),
        (lambda s: [st["env"].update(UV_PROJECT_ENVIRONMENT="${{ steps.sharedvenv.outputs.venv }}") for st in _uv_steps(s) if "pytest" in st["run"]], "UV_PROJECT_ENVIRONMENT must be"),
        (lambda s: [st["env"].pop("UV_NO_SYNC") for st in _uv_steps(s) if "pytest" in st["run"]], "UV_NO_SYNC must be"),
        (lambda s: [st["env"].update(UV_NO_SYNC="${{ steps.sharedvenv.outputs.used == 'true' && '1' || '' }}") for st in _uv_steps(s) if "pytest" in st["run"]], "UV_NO_SYNC must be"),
        (lambda s: s.setdefault("env", {}).update(UV_PROJECT_ENVIRONMENT="x"), "set job-wide"),
        (lambda s: s["steps"].append({"run": 'echo "UV_PROJECT_ENVIRONMENT=x" >> "$GITHUB_ENV"'}), "writes a uv variable to GITHUB_ENV"),
    ],
    ids=["no-id", "wrong-script", "not-pool-only", "sync-not-skipped", "pytest-no-venv-env", "venv-env-without-default",
         "pytest-no-nosync", "nosync-empty", "job-wide-env", "github-env-write"],
)
def test_each_f5_wiring_break_is_caught(mutate, expect) -> None:  # noqa: ANN001
    spec = _ci_test_job()
    mutate(spec)
    problems = shared_venv_problems(spec, _SCRIPT.read_text(encoding="utf-8"))
    assert any(expect in p for p in problems), problems


def test_a_script_that_writes_github_env_is_caught() -> None:
    text = _SCRIPT.read_text(encoding="utf-8") + '\necho "UV_PROJECT_ENVIRONMENT=x" >> "$GITHUB_ENV"\n'
    assert any("writes GITHUB_ENV" in p for p in shared_venv_problems(_ci_test_job(), text))


def test_a_script_without_the_tmpfs_gate_is_caught() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    gate = '[ "${LAB_WS:-}" = tmpfs ] || fallback '
    assert gate in text
    assert any("unless LAB_WS=tmpfs" in p for p in shared_venv_problems(_ci_test_job(), text.replace(gate, ': ')))


def test_a_script_that_starts_the_shared_interpreter_is_caught() -> None:
    text = _SCRIPT.read_text(encoding="utf-8") + '\n"$shared/bin/python" -I -c pass\n'
    assert any("shared entry's own interpreter" in p for p in shared_venv_problems(_ci_test_job(), text))


def test_a_script_without_the_f1_fallback_is_caught() -> None:
    text = _SCRIPT.read_text(encoding="utf-8").replace("LAB_DISK_DIR", "X")
    assert any("lost 'LAB_DISK_DIR'" in p for p in shared_venv_problems(_ci_test_job(), text))
