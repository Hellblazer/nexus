#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-tag check: two engine-service-release.yml procedures that differ from
every LOCAL pre-tag gate, and rotted silently because nothing local ran them
in the shape the release workflow does (nexus-xihsm).

Three engine-service tags burned in one day (2026-09-05) on defects a green
``--shakeout`` never saw, because ``--shakeout`` runs in a DIFFERENT shape
from the release workflow on both axes this script checks:

(a) **Checkout vs. installed wheel.** ``service/native-smoke.sh``'s real-
    client probes (``uv run python service/smoke-probes/t1_real_client.py``
    etc.) run from the release workflow's CI CHECKOUT, which
    ``nexus.db.service_endpoint.is_dev_checkout_process()`` (the nexus-a2qhz
    production-write guard) classifies as a dev checkout -- every HTTP write
    the probes make then needs the reason-bearing ``NX_ALLOW_PROD_WRITE``
    opt-in or it is refused. ``--shakeout`` drives the byte-identical script
    from a ``uv``-tool-installed WHEEL inside its container, where the guard
    is inert by construction (no ``.git``/``pyproject.toml`` ancestor exists
    in that filesystem), so a defect in this class is invisible there no
    matter how green the run.

(b) **Docker present vs. absent.** The release build is
    ``./mvnw -Pnative -Pprebuilt-jooq -DskipTests package`` on GitHub-hosted
    runners WITHOUT Docker (mac-arm64 has none at all; the point of
    ``-Pprebuilt-jooq`` is to need none anywhere). Every LOCAL pre-tag leg
    (the host JVM suite, ``--shakeout``'s own ``-Ob`` native build,
    ``--candidate-migration``) runs with Docker present, so a stray
    Docker-only Maven execution slipping past ``-Pprebuilt-jooq`` -- exactly
    what burned v0.1.102's ``generate-jooq-test-sources`` and then v0.1.103's
    ``testCompile`` -- was invisible to every one of them.

Phase (a) is ``check_checkout_shape_smoke`` below; phase (b) is
``check_dockerless_build``. Both are exposed as functions so the unit suite
(``tests/scripts/test_check_release_workflow_shape.py``) can drive them with
stubs; ``main()`` wires them into a CLI for the pre-tag battery.

The release's EXACT Maven invocation is PARSED from
``.github/workflows/engine-service-release.yml`` (``extract_release_native_build_argv``)
rather than retyped here, so this script and the workflow cannot drift apart
silently -- a pinned copy-paste is exactly the kind of "release-only
procedure rots silently" defect this check exists to close (nexus-1e2eh).
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

#: A GH Actions template expression (``${{ matrix.target.maxheap }}``)
#: contains internal whitespace that ``shlex.split`` would otherwise treat
#: as a token boundary -- GH Actions itself substitutes the expression
#: BEFORE bash ever sees the line, so at runtime it is never actually
#: multiple shell words. Collapse the internal whitespace before splitting
#: so the expression survives as one token; substitution then matches on
#: the (now-fixed) collapsed form.
_TEMPLATE_RE = re.compile(r"\$\{\{\s*([^{}]*?)\s*\}\}")


def _collapse_templates(line: str) -> str:
    return _TEMPLATE_RE.sub(lambda m: "${{" + m.group(1).replace(" ", "") + "}}", line)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "engine-service-release.yml"
SERVICE_DIR = REPO_ROOT / "service"
NATIVE_SMOKE_SH = SERVICE_DIR / "native-smoke.sh"
MVNW_LEASED = REPO_ROOT / "scripts" / "mvnw-leased.sh"

#: Where the release's SEPARATE ``jooq-codegen`` job uploads its output and
#: the native-build job downloads it BEFORE running ``-Pprebuilt-jooq``
#: (``.github/workflows/engine-service-release.yml`` lines 76/308, both
#: ``path: service/target/generated-sources/jooq``) -- the exact directory
#: this script's phase (b) needs already populated. One file under this
#: path (any of the always-generated jOOQ table classes) is enough evidence
#: that a real codegen ran here, not merely that the directory exists.
_JOOQ_GENERATED_MARKER = SERVICE_DIR / "target" / "generated-sources" / "jooq" / "dev" / "nexus" / "service" / "jooq" / "DefaultCatalog.java"

#: The step name anchor in engine-service-release.yml. Matched verbatim so a
#: renamed/restructured step fails this parse LOUDLY instead of silently
#: falling through to some other ``run:`` block.
_BUILD_STEP_NAME = "Build native image (-Pnative -Pprebuilt-jooq)"

#: A path that cannot resolve to a live Docker daemon on any real machine --
#: not merely an unused port (which some other process could still bind
#: between check and use), a filesystem path Docker's client library will
#: fail to connect(2) against every time.
DOCKER_HOST_NONEXISTENT = "unix:///nonexistent/nexus-xihsm-docker.sock"


class WorkflowShapeError(RuntimeError):
    """The workflow YAML no longer has the shape this script parses."""


def extract_release_native_build_argv(workflow_text: str) -> list[str]:
    """Parse the exact ``run:`` command of the release's native-build step.

    Returns the shell-split argv, e.g.::

        ["./mvnw", "-q", "-Pnative", "-Pprebuilt-jooq", "-DskipTests",
         "-Dnative.image.maxheap=${{ matrix.target.maxheap }}", "package"]

    The templated ``${{ matrix.target.maxheap }}`` token is returned
    VERBATIM -- callers that need to actually run this substitute a literal
    value (see :func:`check_dockerless_build`). Raises
    :class:`WorkflowShapeError` if the anchor step is missing or its
    ``run:`` is not a single-line string, rather than silently returning an
    empty/wrong invocation.
    """
    doc = yaml.safe_load(workflow_text)
    try:
        jobs = doc["jobs"]
    except (KeyError, TypeError) as exc:
        raise WorkflowShapeError("workflow YAML has no top-level 'jobs' key") from exc
    for job in jobs.values():
        for step in job.get("steps", []) or []:
            if step.get("name") == _BUILD_STEP_NAME:
                run = step.get("run")
                if not isinstance(run, str):
                    raise WorkflowShapeError(
                        f"step {_BUILD_STEP_NAME!r} has no string 'run:' block"
                    )
                line = run.strip()
                if "\n" in line:
                    raise WorkflowShapeError(
                        f"step {_BUILD_STEP_NAME!r}'s run: block is multi-line; "
                        "this parser expects a single command"
                    )
                return shlex.split(_collapse_templates(line))
    raise WorkflowShapeError(
        f"no step named {_BUILD_STEP_NAME!r} found in {WORKFLOW_PATH} -- "
        "the workflow was restructured; update _BUILD_STEP_NAME or the parse"
    )


def _substitute_maxheap(argv: list[str], maxheap: str) -> list[str]:
    """Replace the ``${{ matrix.target.maxheap }}`` GH Actions expression
    with a literal *maxheap* value (a local run has no matrix)."""
    out = []
    for arg in argv:
        if arg.startswith("-Dnative.image.maxheap="):
            out.append(f"-Dnative.image.maxheap={maxheap}")
        else:
            out.append(arg)
    return out


def _jooq_sources_present() -> bool:
    return _JOOQ_GENERATED_MARKER.is_file()


def _ensure_jooq_sources(*, jooq_runner=subprocess.run) -> tuple[bool, str]:
    """Populate ``service/target/generated-sources/jooq`` when it is not
    already there, via the SAME command the release's separate
    ``jooq-codegen`` job runs (``./mvnw -q generate-sources``, unmodified --
    this stage genuinely NEEDS Docker for its Testcontainers pgvector, same
    as that job).

    This is what makes phase (b) self-sufficient: a caller with a cold
    ``service/target`` does not need to know about this precondition and
    run a separate command first -- a check that needs a manual step before
    it can pass gets skipped in practice (the exact class of gap this
    script exists to close for OTHER procedures). Routed through
    ``scripts/mvnw-leased.sh`` (never a bare ``./mvnw``), so this respects
    the project's one-builder-per-box lease exactly like every other engine
    build in this repo (AGENTS.md hot rule, nexus-c00dw) -- a concurrent
    raw ``./mvnw`` here could corrupt a peer's in-flight jOOQ codegen.

    Returns ``(ok, message)``; ``ok=True, message=""`` when sources were
    already present and nothing ran.
    """
    if _jooq_sources_present():
        return True, ""
    if not MVNW_LEASED.is_file():
        raise WorkflowShapeError(f"mvnw-leased.sh missing: {MVNW_LEASED}")
    proc = jooq_runner(
        [str(MVNW_LEASED), "-q", "generate-sources"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    ok = proc.returncode == 0 and _jooq_sources_present()
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
    message = (
        f"jOOQ prerequisite generation (mvnw-leased.sh -q generate-sources, "
        f"Docker required here -- same as the release's jooq-codegen job) "
        f"{'PASSED' if ok else 'FAILED'} rc={proc.returncode}\n--- tail ---\n{tail}"
    )
    return ok, message


def check_dockerless_build(
    *,
    goal: str = "test-compile",
    maxheap: str = "3g",
    mvnw_runner=subprocess.run,
    jooq_runner=subprocess.run,
) -> tuple[bool, str]:
    """Run the release's EXACT Maven flags (minus the templated heap value,
    minus the trailing ``package`` goal, which *goal* overrides) with
    ``DOCKER_HOST`` pointed at a nonexistent socket.

    Self-sufficient: if ``service/target/generated-sources/jooq`` is not
    already populated, this first runs the release's SEPARATE
    ``jooq-codegen`` step (:func:`_ensure_jooq_sources`, WITH Docker -- the
    one prerequisite this whole check has, matching the release workflow's
    own two-job split) before the Docker-less invocation below. A caller
    never has to remember a manual pre-step for this to pass.

    *goal* defaults to ``test-compile`` -- upstream of the actual
    native-image link step and downstream of both incidents this check
    exists for (``generate-jooq-test-sources``'s Testcontainers Docker
    dependency, and the ``testCompile`` phase that broke once that codegen
    was disabled without disabling test compilation). Pass ``goal="package"``
    for the exhaustive form, which also performs the real (multi-minute)
    native-image link -- native-image itself needs no Docker, so this is
    strictly slower, never a different verdict on the Docker axis.

    Returns ``(ok, message)`` -- never raises for a Maven failure (that IS
    the signal under test); raises only on a genuinely missing workflow/mvnw.
    """
    if not WORKFLOW_PATH.is_file():
        raise WorkflowShapeError(f"workflow file missing: {WORKFLOW_PATH}")
    argv = extract_release_native_build_argv(WORKFLOW_PATH.read_text(encoding="utf-8"))
    argv = _substitute_maxheap(argv, maxheap)
    if argv[-1] != "package":
        raise WorkflowShapeError(
            f"expected the parsed invocation to end in 'package', got: {argv}"
        )
    argv = argv[:-1] + [goal]

    if not MVNW_LEASED.is_file():
        raise WorkflowShapeError(f"mvnw-leased.sh missing: {MVNW_LEASED}")

    jooq_ok, jooq_message = _ensure_jooq_sources(jooq_runner=jooq_runner)
    if not jooq_ok:
        return False, (
            "FAILED: jOOQ prerequisite generation did not produce "
            f"{_JOOQ_GENERATED_MARKER} -- cannot run the Docker-less check "
            f"without it\n{jooq_message}"
        )
    prereq_note = jooq_message + "\n" if jooq_message else ""

    env = dict(os.environ)
    env["DOCKER_HOST"] = DOCKER_HOST_NONEXISTENT
    # Belt-and-braces: some Docker client libraries fall back to other
    # discovery mechanisms (a Unix socket at the default path, Colima's
    # context) when DOCKER_HOST is unset but NOT when it is set to garbage --
    # unset the context-selection var too so nothing else hands testcontainers
    # a working daemon out from under this env.
    env.pop("DOCKER_CONTEXT", None)
    env.pop("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", None)

    # Through the leased wrapper (never a bare ./mvnw, AGENTS.md hot rule,
    # nexus-c00dw): argv[0] is "./mvnw" from the parsed workflow line, which
    # mvnw-leased.sh already supplies itself -- pass argv[1:] as its args.
    leased_argv = [str(MVNW_LEASED), *argv[1:]]
    proc = mvnw_runner(
        leased_argv,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    ok = proc.returncode == 0
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-60:])
    message = (
        f"{prereq_note}"
        f"{'PASSED' if ok else 'FAILED'}: {' '.join(argv)} "
        f"(DOCKER_HOST={DOCKER_HOST_NONEXISTENT}, via mvnw-leased.sh) rc={proc.returncode}\n"
        f"--- tail ---\n{tail}"
    )
    return ok, message


def check_checkout_shape_smoke(
    *,
    bin_path: str | None = None,
    runner=subprocess.run,
) -> tuple[bool, str]:
    """Run ``service/native-smoke.sh``'s real-client probes from THIS
    checkout (never from an installed wheel), asserting non-vacuously that
    the nexus-a2qhz guard's dev-checkout classification is True for the
    process doing the asserting -- the exact shape the release workflow's CI
    checkout is in, and the exact shape ``--shakeout``'s wheel-installed
    container is NOT in.

    *bin_path*: the executable ``native-smoke.sh`` should boot as ``$BIN``.
    Any HTTP-serving build of the engine works here -- this check is about
    the Python-level checkout/guard classification, which is orthogonal to
    native-image vs. JVM. Callers typically pass a thin shim that execs
    ``java -jar service/target/nexus-service-1.0-SNAPSHOT.jar`` (far cheaper
    than a native build) or the real native binary when one is already
    fresh. Defaults to ``$BIN``/``service/target/nexus-service`` (native-
    smoke.sh's own default) when not given.
    """
    if not NATIVE_SMOKE_SH.is_file():
        raise WorkflowShapeError(f"native-smoke.sh missing: {NATIVE_SMOKE_SH}")

    # Non-vacuity precondition, checked FIRST and independently of the smoke
    # run below: this process's own checkout classification must be True, or
    # everything that follows proves nothing about the CI-checkout shape.
    classify = runner(
        [sys.executable, "-c",
         "import sys; from nexus.db.service_endpoint import is_dev_checkout_process as f; "
         "sys.exit(0 if f() else 3)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if classify.returncode == 3:
        return False, (
            "NON-VACUITY FAILURE: is_dev_checkout_process() returned False for "
            f"{sys.executable} run from {REPO_ROOT} -- this process is not in "
            "the checkout shape the release workflow runs in, so a passing "
            "smoke run below would prove nothing about the class this check "
            "exists for. Run this script from a real git checkout of conexus."
        )
    if classify.returncode != 0:
        return False, f"dev-checkout classification probe errored: {classify.stdout}{classify.stderr}"

    env = dict(os.environ)
    if bin_path is not None:
        env["BIN"] = bin_path
    proc = runner(
        ["bash", str(NATIVE_SMOKE_SH)],
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    ok = proc.returncode == 0 and "NATIVE SMOKE PASS" in proc.stdout
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-80:])
    message = (
        f"{'PASSED' if ok else 'FAILED'}: native-smoke.sh from {REPO_ROOT} "
        f"(checkout-shape confirmed: is_dev_checkout_process()=True) rc={proc.returncode}\n"
        f"--- tail ---\n{tail}"
    )
    return ok, message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-checkout-smoke", action="store_true",
        help="skip phase (a) -- native-smoke.sh's client probes in checkout shape",
    )
    parser.add_argument(
        "--skip-dockerless-build", action="store_true",
        help="skip phase (b) -- the release's exact Maven invocation, Docker-less",
    )
    parser.add_argument(
        "--bin", dest="bin_path", default=None,
        help="executable for native-smoke.sh's $BIN (phase a); defaults to "
             "native-smoke.sh's own default (service/target/nexus-service)",
    )
    parser.add_argument(
        "--maven-goal", default="test-compile",
        help="Maven goal to run in place of the release's trailing 'package' "
             "(phase b); default test-compile is upstream of native-image "
             "linking and downstream of both incidents this check covers",
    )
    parser.add_argument(
        "--maven-maxheap", default="3g",
        help="literal value substituted for the workflow's "
             "${{ matrix.target.maxheap }} template (phase b)",
    )
    args = parser.parse_args(argv)

    overall_ok = True

    if not args.skip_checkout_smoke:
        print("[check_release_workflow_shape] phase (a): checkout-shape native-smoke.sh client probes")
        ok, message = check_checkout_shape_smoke(bin_path=args.bin_path)
        print(message)
        overall_ok = overall_ok and ok
    else:
        print("[check_release_workflow_shape] phase (a) SKIPPED (--skip-checkout-smoke)")

    if not args.skip_dockerless_build:
        print("[check_release_workflow_shape] phase (b): release Maven invocation, Docker-less")
        ok, message = check_dockerless_build(goal=args.maven_goal, maxheap=args.maven_maxheap)
        print(message)
        overall_ok = overall_ok and ok
    else:
        print("[check_release_workflow_shape] phase (b) SKIPPED (--skip-dockerless-build)")

    if overall_ok:
        print("RELEASE WORKFLOW SHAPE CHECK PASSED")
        return 0
    print("RELEASE WORKFLOW SHAPE CHECK FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
