# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ratchets timeout-less ``java.net.http`` use in the engine test tree.

nexus-9meyc. A ``java.net.http`` request with no timeout sits on a socket read
forever. Three engine-suite runs wedged that way on 2026-09-07 and 2026-09-08:
28 minutes in ``TaxonomyCentroidHandlerTest``, about an hour and forty in
``VectorsRepointFunctionsIntegrationTest``, and thirty-plus minutes in a
full-suite pass. Each sat at 0% CPU **holding the shared build lease**, so one
hang blocked every other engine build on the box until an operator jstack'd and
killed it, and each reran clean in seconds or minutes. The whole cost was the
hang.

A timeout does not fix the cause. It converts an unbounded wedge into a test
failure with a stack, which is the difference between a defect someone can
debug and a box someone has to rescue.

**Why a ratchet rather than a ban.** At filing, 44 files built their own client
and there were 123 timeout-less request sites (43 and 120 after migrating
``TaxonomyCentroidHandlerTest``, the file that actually hung, which is what
proves the helper works rather than merely compiles). Rewriting them all in one
change would be a
large mechanical diff across the whole engine test tree, with real regression
risk and no way to review it meaningfully — and it would have to land before
any protection existed at all. The ratchet gives the protection immediately: the
count can fall and never grow, so every new test gets ``TestHttp`` while the
existing ones are migrated as their files are touched for other reasons. That is
the same shape as this repo's raw-SQL gate ceiling.

**This lint is one of three layers and the weakest.** ``TestHttp`` supplies the
timeouts at the source; ``forkedProcessTimeoutInSeconds`` in ``service/pom.xml``
is the backstop that needs no cooperation and is the only one that covers the
third occurrence, where every class had reported and the fork simply never
exited, so no per-request timeout could have caught it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
_TEST_TREE = _REPO / "service" / "src" / "test" / "java"

#: Census taken 2026-09-12, the day the helper and the backstop landed. These
#: are CEILINGS: lower them whenever a file is migrated to TestHttp, never raise
#: them. A raise means a new test shipped able to wedge the box for half an hour
#: — use ``TestHttp.client()`` / ``TestHttp.request(uri)`` instead.
#: Lowered 2026-09-12 when TupleHandlerWiringTest migrated to TestHttp
#: (nexus-h61dl.4): its two request helpers and its client now come from there, so
#: every test in that file carries a timeout instead of three sites relying on
#: nobody forgetting. 43 -> 42 and 120 -> 118.
MAX_BARE_CLIENTS = 42
MAX_TIMEOUTLESS_REQUESTS = 118

_BARE_CLIENT = re.compile(r"HttpClient\.newHttpClient\(\)")
_REQUEST_BUILDER = re.compile(r"HttpRequest\.newBuilder")
_TIMEOUT = re.compile(r"\.timeout\(")


def _java_files() -> list[Path]:
    return sorted(_TEST_TREE.rglob("*.java"))


def _counts() -> tuple[int, int, dict[str, int]]:
    """(bare clients, timeout-less request sites, per-file bare-client counts)."""
    bare = 0
    builders = 0
    timeouts = 0
    per_file: dict[str, int] = {}
    for path in _java_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        n_bare = len(_BARE_CLIENT.findall(src))
        if n_bare:
            per_file[str(path.relative_to(_REPO))] = n_bare
        bare += n_bare
        builders += len(_REQUEST_BUILDER.findall(src))
        timeouts += len(_TIMEOUT.findall(src))
    return bare, max(0, builders - timeouts), per_file


def test_bare_http_clients_do_not_grow() -> None:
    bare, _, per_file = _counts()
    assert bare <= MAX_BARE_CLIENTS, (
        f"{bare} HttpClient.newHttpClient() call sites in the engine test tree, ceiling "
        f"{MAX_BARE_CLIENTS} (nexus-9meyc). A client with no connectTimeout can park "
        f"forever on a socket read, holding the shared build lease and blocking every "
        f"other engine build on this box — three runs wedged that way for 28 min, ~1h40 "
        f"and 30+ min. Use TestHttp.client(). Top files: "
        f"{sorted(per_file.items(), key=lambda kv: -kv[1])[:5]}"
    )


def test_timeoutless_requests_do_not_grow() -> None:
    _, timeoutless, _ = _counts()
    assert timeoutless <= MAX_TIMEOUTLESS_REQUESTS, (
        f"{timeoutless} HttpRequest builder sites with no .timeout(), ceiling "
        f"{MAX_TIMEOUTLESS_REQUESTS} (nexus-9meyc). Use TestHttp.request(uri), which "
        f"applies the timeout so a caller cannot forget it. A timeout-less request is "
        f"what wedged TaxonomyCentroidHandlerTest for 28 minutes."
    )


def test_the_ceilings_are_not_slack() -> None:
    """A ceiling far above the real count silently permits regressions.

    This is the guard the raw-SQL ratchet earned the hard way: a ceiling nobody
    lowers stops being a ratchet and becomes decoration. If a migration drops the
    count well below the pin, this fails and asks for the pin to follow it down.
    """
    bare, timeoutless, _ = _counts()
    assert bare >= MAX_BARE_CLIENTS - 5, (
        f"bare clients fell to {bare} against a ceiling of {MAX_BARE_CLIENTS} — lower "
        f"MAX_BARE_CLIENTS to {bare} so the ratchet keeps its grip."
    )
    assert timeoutless >= MAX_TIMEOUTLESS_REQUESTS - 10, (
        f"timeout-less requests fell to {timeoutless} against a ceiling of "
        f"{MAX_TIMEOUTLESS_REQUESTS} — lower MAX_TIMEOUTLESS_REQUESTS to {timeoutless}."
    )


def test_the_helper_exists_and_sets_both_timeouts() -> None:
    """The lint is only half a remedy without somewhere to send people."""
    helper = _TEST_TREE / "dev" / "nexus" / "service" / "TestHttp.java"
    assert helper.is_file(), "TestHttp.java is missing — the lint points nowhere"
    src = helper.read_text(encoding="utf-8")
    assert "connectTimeout(" in src, "TestHttp.client() must set a connect timeout"
    assert ".timeout(REQUEST_TIMEOUT)" in src, (
        "TestHttp.request() must apply the per-request timeout — that is the whole "
        "point of routing callers through it"
    )


def test_the_surefire_backstop_is_wired() -> None:
    """The layer that needs no cooperation, and the only one covering occurrence 3.

    The third hang was NOT a request read: every class had reported and the fork
    never exited. No per-request timeout could have caught it, so the fork
    timeout is not redundant with the other two layers.
    """
    pom = (_REPO / "service" / "pom.xml").read_text(encoding="utf-8")
    assert "forkedProcessTimeoutInSeconds" in pom, (
        "service/pom.xml has no forkedProcessTimeoutInSeconds — a wedged fork can "
        "again hold the shared build lease indefinitely (nexus-9meyc)"
    )
