# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rehearsal harness must never ship a STALE native binary as "the candidate".

nexus-ndve9 (2026-08-03). ``tests/e2e/migration-rehearsal/run.sh`` guarded its
GraalVM native build with a pure EXISTENCE test::

    if [ ! -x service/target/nexus-service ]; then   # build
    else                                             # "already built — reusing"

``mvn package`` leaves ``service/target/nexus-service`` on disk and no step ever
removes it, so once ANY native build had happened, every later
``run.sh --shakeout`` silently reused that artifact forever. The
engine-service-v0.1.63 pre-tag shakeout consequently validated a binary dated
2026-07-22 while claiming to validate ``abbcf1bd`` (2026-08-03) — 34 newer
``service/src/main`` files, and four count-emitting endpoints
(``store-get``, ``manifest/get_many``, ``manifest/chashes``,
``manifest/docs_for_chashes``) absent because the artifact predated the
commits that added them. Every JVM suite was green on the tree under test,
because the Java source was never wrong.

This is a static-source gate (same family as ``test_release_artifact_verb_rot``
/ ``test_engine_release_skill_parity``): it asserts the harness's build
DECISION is freshness-aware, which is the layer that failed. It deliberately
does not try to run the harness — the defect is in which artifact the harness
CHOOSES, and that choice is legible in its source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RUN_SH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "e2e"
    / "migration-rehearsal"
    / "run.sh"
)

NATIVE_ARTIFACT = "service/target/nexus-service"


@pytest.fixture(scope="module")
def run_sh_source() -> str:
    assert RUN_SH.is_file(), f"rehearsal harness missing at {RUN_SH}"
    return RUN_SH.read_text(encoding="utf-8")


def _native_build_guard(source: str) -> str:
    """The ``if`` condition guarding the GraalVM native build.

    Returned verbatim (possibly multi-line, backslash continuations joined)
    so the assertions below read the REAL decision, not a proxy.
    """
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("if ") and NATIVE_ARTIFACT in line:
            guard = [line]
            # follow backslash continuations
            while guard[-1].rstrip().endswith("\\") and i + len(guard) < len(lines):
                guard.append(lines[i + len(guard)])
            return "\n".join(guard)
    raise AssertionError(
        f"no `if` guard referencing {NATIVE_ARTIFACT} found in {RUN_SH} — "
        "the native-build decision moved; this gate must be re-pointed, "
        "never deleted"
    )


def test_native_build_guard_is_not_existence_only(run_sh_source: str) -> None:
    """An existence-only guard is the nexus-ndve9 defect, verbatim."""
    guard = _native_build_guard(run_sh_source)

    # Non-vacuity: we really did find the guard, and it really does test the
    # artifact's existence (that half is legitimate — it just isn't sufficient).
    assert "-x" in guard, f"guard no longer tests executability:\n{guard}"

    assert "-newer" in guard, (
        "the native-build guard is EXISTENCE-ONLY — it rebuilds only when "
        f"{NATIVE_ARTIFACT} is absent, so a stale artifact from any earlier "
        "build is silently re-shipped as 'the candidate' (nexus-ndve9: a "
        "12-day-stale binary passed as the v0.1.63 candidate). It must also "
        "compare the artifact against the service sources.\n"
        f"guard:\n{guard}"
    )


def test_native_build_guard_compares_against_service_sources(
    run_sh_source: str,
) -> None:
    """Freshness must be measured against the sources that produce the binary."""
    guard = _native_build_guard(run_sh_source)

    newer_clause = re.search(r"-newer\s+\S*nexus-service", guard)
    assert newer_clause, (
        "the freshness comparison must be `-newer <the native artifact>` so "
        "any source newer than the built binary forces a rebuild.\n"
        f"guard:\n{guard}"
    )

    assert "service/src" in guard, (
        "freshness must be measured against `service/src` — the Java sources "
        "and Liquibase changelogs compiled into the native image.\n"
        f"guard:\n{guard}"
    )
    assert "service/pom.xml" in guard, (
        "freshness must also be measured against `service/pom.xml` — a "
        "dependency or plugin bump changes the artifact with no `service/src` "
        "edit at all.\n"
        f"guard:\n{guard}"
    )
