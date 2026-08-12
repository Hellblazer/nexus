# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hak6p: real test coverage for the nexus-cqquo dorny fail-loud guards.

Five sites in ``ci.yml`` / ``service-ci.yml`` gate expensive (or REQUIRED)
jobs on a boolean `dorny/paths-filter` output. Before this fix, an empty,
absent, or malformed output silently read as "no changes" and skipped the
job, which branch protection then treats as SUCCESS. The fix added a
``case`` statement fail-loud arm at each site — this module is the coverage
that MANDATORY ACCEPTANCE CRITERION demands (a fixture/self-test/RED-GREEN
transcript per site), extracted straight from review: the sites were fixed
and manually demonstrated in a chat transcript, but had ZERO test fixture,
so a guard deleted tomorrow would regress silently.

APPROACH: extract each site's literal ``run:`` shell script from the parsed
workflow YAML (so a future edit to the ACTUAL script under test, not a
hand-copied duplicate that could drift), substitute its
``${{ ... }}`` GitHub Actions expression placeholders with test values the
same way GitHub's runner would inline them before invoking the shell, then
execute the substituted script for real via ``bash -c`` and assert the exit
code / GITHUB_OUTPUT contents. This fails the moment the guard's ``case``
arm is removed, renamed, or loosened — it exercises the SAME text `git`
would push to a runner, not a hand-maintained copy of the logic.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SERVICE_CI_YML = REPO_ROOT / ".github" / "workflows" / "service-ci.yml"

_GH_EXPR_RE = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _find_step(doc: dict, job: str, name_prefix: str) -> dict:
    steps = doc["jobs"][job]["steps"]
    for step in steps:
        if step.get("name", "").startswith(name_prefix):
            return step
    raise AssertionError(
        f"no step named like {name_prefix!r} found in job {job!r} of "
        f"{list(doc['jobs'])} -- the workflow was restructured; update this "
        "test's (job, name_prefix) pointer to match"
    )


def _render(run_text: str, values: dict[str, str]) -> str:
    """Substitute every ``${{ <expr> }}`` in *run_text* with *values[<expr>]*,
    the same textual inlining GitHub Actions performs before handing the
    script to the shell. Raises if a script references an expression this
    test forgot to supply a value for -- fail loud on a stale fixture,
    never silently skip a substitution.
    """
    def repl(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        if expr not in values:
            raise KeyError(
                f"script references {expr!r} but the test fixture supplied "
                f"no value for it (have: {sorted(values)}) -- the guard's "
                "run: block changed shape; update this test"
            )
        return values[expr]

    return _GH_EXPR_RE.sub(repl, run_text)


def _run(script: str, github_output: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if github_output is not None:
        import os
        env = dict(os.environ)
        env["GITHUB_OUTPUT"] = str(github_output)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env,
    )


def _read_output(github_output: Path) -> dict[str, str]:
    if not github_output.exists():
        return {}
    out = {}
    for line in github_output.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


# Every input a `case "$x" in true|false) ;; *) ... esac`-shaped guard must
# accept as clean and reject as unproven, per the bead's own scope: "assert
# each of the 5 sites' guard logic rejects "", "garbage", "TRUE", "1", "null"
# and accepts only true/false".
MALFORMED_INPUTS = ("", "garbage", "TRUE", "1", "null")
VALID_INPUTS = ("true", "false")


class TestEngineServiceBuildSvcGuard:
    """ci.yml, job `engine-service-build`, step 'Verify the svc predicate
    is usable' -- gates the native-build trip-wire, ci.yml's own comment
    says this job is a REQUIRED check (nexus-a5rj8)."""

    @staticmethod
    def _script(svc: str) -> str:
        doc = _load(CI_YML)
        step = _find_step(doc, "engine-service-build", "Verify the svc predicate")
        return _render(step["run"], {"steps.changes.outputs.svc": svc})

    @pytest.mark.parametrize("bad", MALFORMED_INPUTS)
    def test_rejects_malformed_svc(self, bad: str) -> None:
        proc = _run(self._script(bad))
        assert proc.returncode == 1, (bad, proc.stdout, proc.stderr)
        assert "::error::" in proc.stdout or "::error::" in proc.stderr

    @pytest.mark.parametrize("good", VALID_INPUTS)
    def test_accepts_true_false_svc(self, good: str) -> None:
        proc = _run(self._script(good))
        assert proc.returncode == 0, (good, proc.stdout, proc.stderr)


class TestWriteSeamGateGuard:
    """ci.yml, job `write-seam-gate`, step 'Verify the seam predicate is
    usable' -- gates the write-seam behavioural gate."""

    @staticmethod
    def _script(seam: str) -> str:
        doc = _load(CI_YML)
        step = _find_step(doc, "write-seam-gate", "Verify the seam predicate")
        return _render(step["run"], {"steps.changes.outputs.seam": seam})

    @pytest.mark.parametrize("bad", MALFORMED_INPUTS)
    def test_rejects_malformed_seam(self, bad: str) -> None:
        proc = _run(self._script(bad))
        assert proc.returncode == 1, (bad, proc.stdout, proc.stderr)
        assert "::error::" in proc.stdout or "::error::" in proc.stderr

    @pytest.mark.parametrize("good", VALID_INPUTS)
    def test_accepts_true_false_seam(self, good: str) -> None:
        proc = _run(self._script(good))
        assert proc.returncode == 0, (good, proc.stdout, proc.stderr)


class TestCa3GateGuard:
    """ci.yml, jobs `ca3-pgvector-bundle` (linux) and
    `ca3-pgvector-bundle-macos` -- both carry a BYTE-IDENTICAL 'Decide
    whether the CA-3 build runs' step (replace_all fix), so this is
    parametrized over both job names to prove neither silently diverged."""

    @staticmethod
    def _script(job: str, ca3_core: str, ca3_deps: str, event_name: str = "push") -> str:
        doc = _load(CI_YML)
        step = _find_step(doc, job, "Decide whether the CA-3 build runs")
        return _render(step["run"], {
            "steps.changes.outputs.ca3_core": ca3_core,
            "steps.changes.outputs.ca3_deps": ca3_deps,
            "github.event_name": event_name,
        })

    @pytest.mark.parametrize("job", ["ca3-pgvector-bundle", "ca3-pgvector-bundle-macos"])
    @pytest.mark.parametrize("bad", MALFORMED_INPUTS)
    def test_rejects_malformed_ca3_core(self, job: str, bad: str) -> None:
        proc = _run(self._script(job, bad, "true"))
        assert proc.returncode == 1, (job, bad, proc.stdout, proc.stderr)
        assert "::error::" in proc.stdout or "::error::" in proc.stderr

    @pytest.mark.parametrize("job", ["ca3-pgvector-bundle", "ca3-pgvector-bundle-macos"])
    @pytest.mark.parametrize("bad", MALFORMED_INPUTS)
    def test_rejects_malformed_ca3_deps(self, job: str, bad: str) -> None:
        proc = _run(self._script(job, "false", bad))
        assert proc.returncode == 1, (job, bad, proc.stdout, proc.stderr)
        assert "::error::" in proc.stdout or "::error::" in proc.stderr

    @pytest.mark.parametrize("job", ["ca3-pgvector-bundle", "ca3-pgvector-bundle-macos"])
    def test_accepts_clean_true_false_and_computes_run_true(self, job: str) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "gh_output"
            proc = _run(self._script(job, "true", "false"), github_output=out)
            assert proc.returncode == 0, (job, proc.stdout, proc.stderr)
            assert _read_output(out).get("run") == "true"

    @pytest.mark.parametrize("job", ["ca3-pgvector-bundle", "ca3-pgvector-bundle-macos"])
    def test_accepts_clean_false_false_and_computes_run_false(self, job: str) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "gh_output"
            proc = _run(self._script(job, "false", "false"), github_output=out)
            assert proc.returncode == 0, (job, proc.stdout, proc.stderr)
            assert _read_output(out).get("run") == "false"


class TestServiceCiChangesGuard:
    """service-ci.yml, job `changes`, step 'Report the decision' -- the
    job whose own comment says it 'MUST ITSELF BE A REQUIRED CHECK' so a
    broken detector cannot silently green-light every PR (nexus-hq9na
    tracks whether that is ACTUALLY true in branch protection separately;
    this test covers the guard's own logic regardless)."""

    @staticmethod
    def _script(service: str) -> str:
        doc = _load(SERVICE_CI_YML)
        step = _find_step(doc, "changes", "Report the decision")
        return _render(step["run"], {"steps.filter.outputs.service": service})

    @pytest.mark.parametrize("bad", MALFORMED_INPUTS)
    def test_rejects_malformed_service(self, bad: str) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "gh_output"
            proc = _run(self._script(bad), github_output=out)
            assert proc.returncode == 1, (bad, proc.stdout, proc.stderr)
            assert "::error::" in proc.stdout or "::error::" in proc.stderr
            # nexus-hak6p: on the malformed path, the step must not have
            # emitted the "guard in the path" GITHUB_OUTPUT at all -- a
            # written-then-ignored output would still be a side channel.
            assert "service" not in _read_output(out)

    @pytest.mark.parametrize("good", VALID_INPUTS)
    def test_accepts_and_republishes_true_false_service(self, good: str) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "gh_output"
            proc = _run(self._script(good), github_output=out)
            assert proc.returncode == 0, (good, proc.stdout, proc.stderr)
            # THE side-channel fix under test: the validated step must
            # itself republish the SAME value the job's `outputs:` block
            # is wired to (steps.decide.outputs.service) -- not merely
            # print a message and let the raw dorny value flow around it.
            assert _read_output(out).get("service") == good

    def test_job_output_is_wired_to_the_validated_step_not_the_raw_filter(self) -> None:
        """Structural pin for the side-channel fix: `changes.outputs.service`
        must read from `steps.decide` (the validated step), never
        `steps.filter` (the raw, unvalidated dorny output) directly."""
        doc = _load(SERVICE_CI_YML)
        job_outputs = doc["jobs"]["changes"]["outputs"]
        assert job_outputs["service"] == "${{ steps.decide.outputs.service }}", (
            "changes.outputs.service must be sourced from the validated "
            "'decide' step -- routing it straight from steps.filter makes "
            "the guard a side-channel observer instead of the value's "
            "actual gate (nexus-hak6p)"
        )
        # And the step the output claims to read from must actually BE the
        # guarded one, not a same-named coincidence.
        step = _find_step(doc, "changes", "Report the decision")
        assert step.get("id") == "decide"
