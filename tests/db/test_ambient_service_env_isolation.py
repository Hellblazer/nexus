# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ambient-service-env scrub actually scrubs (nexus-dvom6).

``_isolate_service_endpoint_env`` (tests/conftest.py) removes the four
service-endpoint env vars from every unit test. Asserting their absence from
inside a test would be vacuous -- they are absent on any machine that never
sourced the credentials, which is every CI runner. So the proof runs a
previously-polluted test in a SUBPROCESS with the env deliberately set.

Delete the fixture and this fails. That is the whole point.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Exactly what a developer shell looks like after
#: ``source ~/.config/nexus/activate.sh``, plus the two env-first tier vars.
_POLLUTED = {
    "NX_SERVICE_URL": "https://ambient.example.invalid",
    "NX_SERVICE_TOKEN": "ambient-token-that-must-not-leak",
    "NX_SERVICE_HOST": "ambient.example.invalid",
    "NX_SERVICE_PORT": "9999",
}

#: Representatives of the two failure shapes the leak produced: an assertion
#: on the "nothing resolvable" error text, and a lease-tier test that env-first
#: resolution short-circuits before it can run.
_CANARIES = [
    "tests/db/test_http_telemetry_store.py::TestConfigErrors::test_missing_port_raises",
    "tests/db/test_om64x_stale_port_recovery.py::TestRecoverEndpointFromLease",
    "tests/db/test_om64x_stale_port_recovery.py::TestTokenStoreRecovery",
    "tests/db/test_om64x_stale_port_recovery.py::TestScratchStoreRecovery",
]

#: Child processes run on the SQLITE substrate, always (nexus-aqbrk).
#:
#: The subject here is ``_isolate_service_endpoint_env``, which is
#: substrate-INDEPENDENT — it scrubs four env vars. But a child that inherits
#: NX_TEST_T2_SUBSTRATE=engine boots its OWN PG + JVM session engine, and in a
#: FULL RUN the parent already has one. Measured: every subprocess errored at
#: exactly 60.3s — ``_engine_substrate.py``'s ``_wait_tcp(timeout=60.0)`` —
#: and the parent then reported "the scrub is not covering it", which is not
#: what happened at all. Five failures, one resource collision, and a
#: misleading message on top.
#:
#: Standalone this file passed 6/6 because no parent engine was competing, so
#: the per-file verification could not see it. Pinning the child makes the test
#: hermetic AND fast: it stops spawning a database and a JVM to check that four
#: environment variables were unset.
_CHILD_ENV = {"NX_TEST_T2_SUBSTRATE": "none"}
#: "none", not "sqlite" (nexus-lom9g / i711w): the requirement is "provision
#: NO T2 substrate", which =sqlite only ever expressed by accident. RDR-158 P4
#: deletes the SQLite backend, and on a tree without it =sqlite silently stops
#: meaning anything — every child boots a PG + JVM and errors at
#: _engine_substrate._wait_tcp's 60s timeout, exactly the collision the block
#: above measured. Verified by simulating the post-i711w world.

#: The DIRECT proof, run in the same polluted subprocess (nexus-aqbrk).
#:
#: The canaries above are INDIRECT: they assert "a test that would break under
#: pollution does not break". That was a valid proxy only while those tests
#: depended on the scrub for their unpolluted precondition. The substrate port
#: made them self-sufficient — om64x grew ``_unpin_service_url`` and
#: ``test_missing_port_raises`` now strips all four vars itself, both correctly,
#: because a test must state the precondition its subject requires rather than
#: inherit it from a fixture two files away.
#:
#: MEASURED CONSEQUENCE, not a worry: with every ``delenv`` in
#: ``_isolate_service_endpoint_env`` neutered, this module still passed 5/5.
#: The file's headline claim — "Delete the fixture and this fails. That is the
#: whole point." — had become false, and nothing would have reported it. The
#: canaries are kept (they still prove those tests survive pollution, which is
#: worth knowing) but they can no longer be the proof.
_SCRUB_PROBE = (
    "tests/db/test_ambient_service_env_isolation.py"
    "::test_scrub_is_visible_to_the_running_test"
)


def test_scrub_actually_scrubs_under_real_pollution() -> None:
    """The direct proof: run the in-process assertion WITH pollution set.

    ``test_scrub_is_visible_to_the_running_test`` is vacuous when run on a
    clean machine — it says so itself. This supplies the pollution the
    parent process does not have, so the assertion becomes load-bearing:
    the four ambient values are exported into the child, and the child's
    conftest scrub is the only thing that can remove them.

    Neuter the scrub and THIS fails, which is the property the module
    docstring always claimed and (until nexus-aqbrk) no longer had.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _SCRUB_PROBE, "-q", "--no-header"],
        env={**os.environ, **_POLLUTED, **_CHILD_ENV},
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "the ambient-env scrub did not remove the developer-shell values from "
        "a polluted child process — _isolate_service_endpoint_env is missing, "
        "neutered, or no longer covers all four vars.\n"
        f"{proc.stdout[-3000:]}"
    )


@pytest.mark.parametrize("target", _CANARIES)
def test_polluted_env_does_not_break_endpoint_tests(target: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header"],
        env={**os.environ, **_POLLUTED, **_CHILD_ENV},
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"{target} failed with ambient service env set — the "
        "_isolate_service_endpoint_env scrub is not covering it.\n"
        f"{proc.stdout[-3000:]}"
    )


def test_scrub_is_visible_to_the_running_test() -> None:
    """Companion to the subprocess proof: inside a test, no var still carries
    the AMBIENT value, regardless of what the developer's shell exported.

    Vacuous on a clean machine by itself — it earns its keep only next to the
    parametrized subprocess test above, which supplies the pollution. Kept
    because it names the invariant at the point of use.

    nexus-aqbrk: this asserted ``is None``, which was right only while nothing
    legitimately set these vars. Under the engine substrate ``t2_service_env``
    sets NX_SERVICE_URL and NX_SERVICE_TOKEN on purpose, AFTER the conftest
    scrub, so the T2 Http*Stores can reach the test engine — and ``is None``
    then reports a working substrate as a leak.

    The invariant was never "unset". It is "does not carry the ambient
    value": the scrub's job is removing what the developer's shell exported,
    not preventing the suite from configuring its own endpoint. Comparing
    against ``_POLLUTED`` states that directly and holds on BOTH arms — unset
    on the sqlite arm, the test engine's own loopback URL on the engine arm,
    never ``https://ambient.example.invalid``.

    Strictly stronger than the old form, too: ``is None`` would have passed if
    the scrub deleted the vars and something re-exported the ambient values
    under a different code path, whereas this names the value that must not
    survive.
    """
    for var, ambient in _POLLUTED.items():
        actual = os.environ.get(var)
        assert actual != ambient, (
            f"{var} still carries the AMBIENT value {ambient!r} — the "
            f"_isolate_service_endpoint_env scrub did not run, or something "
            f"re-exported the developer-shell value after it."
        )
