# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Both-halves wire-contract tripwire (nexus-1vogq).

THE BUG CLASS this guards. ``498c92953`` changed the engine's manifest-write
request validation AND the client's wire callers in the same commit, but a
hand-built raw ``_post("/import/chunk", ...)`` test envelope two functions
away carried no client-method signature for that change to reconcile against
-- structurally invisible to a signature-diff review. The engine tag deployed
before any client release carried the fix; every released client 400'd on
manifest writes for 34+ hours (T2
``nexus/rdr-191-manifest-400-caller-trace-2026-08-14`` [22490]).

THIS LINT is the production gate: it runs
:func:`scripts.check_wire_contract_pairing.check` against the LIVE repo state
and fails if a both-halves commit lands undeclared, or a declared entry goes
stale without being cleared. The remaining tests are the non-vacuity
scaffolding proving the detector actually detects (rather than vacuously
passing because nothing was ever exercised) and a kill-control proving an
undeclared commit is caught.
"""
from __future__ import annotations

import pathlib

import pytest

import check_wire_contract_pairing as wctp

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_LEDGER = wctp.DEFAULT_LEDGER_PATH

#: The three known both-halves commits from the RDR-191 GATE-2 incident
#: (T2 [22490] Q3b census). Fixed, permanent identifiers -- these commits do
#: not move once merged to develop.
_KNOWN_MEMBERS = {
    "498c92953ea3ad60a75389aea53a9f501d8b126a",
    "b361a8106953c0bb586ab3aac969f904d3dff9df",
    "8c75a61a3fd1d65f61695263ea1b0961377c358d",
}


def test_ledger_file_exists() -> None:
    assert _LEDGER.is_file(), (
        f"{_LEDGER} is missing -- the both-halves wire-contract ledger must "
        "exist for the tripwire to have anywhere to declare a pairing."
    )


def test_ledger_parses_seeded_history() -> None:
    ledger = wctp.parse_ledger(_LEDGER)
    assert set(ledger.shipped) >= _KNOWN_MEMBERS, (
        "seeded ## Shipped history is missing one or more of the three known "
        "RDR-191 GATE-2 members -- do not delete the historical record."
    )
    for sha in _KNOWN_MEMBERS:
        assert ledger.shipped[sha].shipped_in == "v7.7.0"


# ---------------------------------------------------------------------------
# Non-vacuity: the detector must actually detect, on a fixed historical range
# that will never change (these tags are immutable once published).
# ---------------------------------------------------------------------------


def test_detector_finds_known_members_in_v761_range() -> None:
    """The RDR-191 GATE-2 census, mechanized: run the real detector against
    ``v7.6.1..HEAD`` (all three known members are permanent ancestors of
    every commit on develop from here forward) and assert it finds them.
    A detector that always returns an empty list would pass every OTHER
    test in this module vacuously; this is the one that proves it doesn't.
    """
    flagged = wctp.flagged_commits("v7.6.1..HEAD", repo_root=_REPO_ROOT)
    found = {c.sha for c in flagged}
    missing = _KNOWN_MEMBERS - found
    assert not missing, (
        f"detector did not find known both-halves members {missing} in "
        "v7.6.1..HEAD -- see the module docstring's 'WHAT COUNTS AS BOTH "
        "HALVES' for why the engine-side surface must be the full service/ "
        "tree (8c75a61a3's engine half is a service/src/test/java/** file "
        "only)."
    )


def test_detector_engine_side_requires_full_service_tree() -> None:
    """8c75a61a3's only engine-side touch is a test file with no 'http' or
    'changelog' path segment -- pins the deliberate over-flagging design
    choice explained in the module docstring against a narrower reading
    silently creeping back in."""
    paths = wctp._touched_paths(
        "8c75a61a3fd1d65f61695263ea1b0961377c358d", repo_root=_REPO_ROOT
    )
    engine_paths = [p for p in paths if wctp._is_engine_path(p)]
    assert engine_paths == [
        "service/src/test/java/dev/nexus/service/RdrO8dil7GlobalManifestAntiJoinTest.java"
    ]
    assert not any("/http/" in p for p in engine_paths)
    assert not any("changelog" in p for p in engine_paths)


def test_detector_finds_test_envelope_client_touch() -> None:
    """The 2026-08-14 bead-comment class: a commit whose ONLY client-side
    touch is a raw ``_post("/import/...")`` test envelope, not a client
    module. Uses the real commit that introduced the /import/document raw
    envelopes into tests/db/test_http_catalog_integration.py."""
    touched = wctp._is_client_test_envelope(
        "498c92953ea3ad60a75389aea53a9f501d8b126a",
        "tests/db/test_http_catalog_integration.py",
        repo_root=_REPO_ROOT,
    )
    assert touched, (
        "raw _post('/import/...') envelope in "
        "tests/db/test_http_catalog_integration.py was not detected as a "
        "client-side wire touch -- the hand-built-test-envelope blind spot "
        "this tripwire exists to close would be silently unguarded."
    )


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/nexus/catalog/http_catalog_client.py", True),
        ("src/nexus/catalog/store_hook.py", True),
        ("src/nexus/mcp_infra.py", True),
        ("src/nexus/indexer.py", True),
        ("src/nexus/doc_indexer.py", True),
        ("src/nexus/db/http_vector_client.py", True),
        # Structural http_ coverage (2026-08-14 consolidated review, T2
        # [22513] recursive-gap fix): these are NOT in _CLIENT_FILE_SUBSTRINGS
        # -- covered only because _is_client_module_path recognizes the
        # http_ naming convention structurally.
        ("src/nexus/db/t2/http_aspect_queue.py", True),
        ("src/nexus/db/t2/http_taxonomy_store.py", True),
        ("src/nexus/db/t2/http_document_aspects_store.py", True),
        ("src/nexus/db/t2/http_document_highlights_store.py", True),
        ("src/nexus/db/t2/http_centroid_store.py", True),
        ("src/nexus/cli.py", False),
        ("docs/architecture.md", False),
        ("tests/test_indexer.py", False),  # tests/ handled separately (content-based)
    ],
)
def test_client_module_path_classification(path: str, expected: bool) -> None:
    assert wctp._is_client_module_path(path) is expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("service/src/main/java/dev/nexus/service/http/CatalogHandler.java", True),
        (
            "service/src/main/resources/db/changelog/catalog-025-collection-not-null.xml",
            True,
        ),
        ("service/src/test/java/dev/nexus/service/CatalogRepositoryTest.java", True),
        ("src/nexus/catalog/http_catalog_client.py", False),
        ("docs/architecture.md", False),
    ],
)
def test_engine_path_classification(path: str, expected: bool) -> None:
    assert wctp._is_engine_path(path) is expected


# ---------------------------------------------------------------------------
# Recursive-gap drift guard (2026-08-14 consolidated review, T2 [22513]):
# _CLIENT_FILE_SUBSTRINGS / _is_client_module_path must not silently miss a
# NEW client module that starts issuing manifest/import wire calls -- that
# is exactly the shape of blind spot nexus-1vogq exists to close, one level
# up. This is the PRODUCTION gate for that; the fixture test below is its
# non-vacuity companion / kill-control.
# ---------------------------------------------------------------------------


def test_live_client_modules_are_covered_by_substrings() -> None:
    """Scans the LIVE src/nexus/**/*.py tree for the raw manifest/import
    _post envelope idiom and asserts every match is covered by
    _is_client_module_path -- currently true only because of the structural
    http_ prefix rule (http_aspect_queue.py, http_taxonomy_store.py,
    http_document_aspects_store.py, http_document_highlights_store.py all
    match the envelope regex and are NOT in _CLIENT_FILE_SUBSTRINGS)."""
    offenders = wctp.live_client_modules_missing_coverage(_REPO_ROOT)
    assert offenders == [], (
        f"{offenders} issue raw manifest/import wire calls but are not "
        "covered by _is_client_module_path in "
        "scripts/check_wire_contract_pairing.py -- the both-halves detector "
        "would silently miss commits that touch this file on the client "
        "side. Prefer widening the structural rule (the http_ prefix) over "
        "appending to _CLIENT_FILE_SUBSTRINGS."
    )


def test_kill_control_new_client_module_without_coverage_fails(tmp_path: pathlib.Path) -> None:
    """A synthetic module that issues a manifest wire call but is named
    neither http_* nor any _CLIENT_FILE_SUBSTRINGS entry must be caught --
    proves the drift guard actually detects a real gap, not just the
    already-fixed http_* family."""
    fake_src = tmp_path / "src" / "nexus"
    fake_src.mkdir(parents=True)
    offender = fake_src / "fake_wire_caller.py"
    offender.write_text(
        'class FakeWireCaller:\n'
        '    def write(self):\n'
        '        return self._post("/manifest/write", {"collection": "x"})\n'
    )
    offenders = wctp.live_client_modules_missing_coverage(tmp_path)
    assert offenders == ["src/nexus/fake_wire_caller.py"], (
        "kill-control failed to detect a synthetic uncovered client module -- "
        "the drift guard would vacuously pass even with a real gap present"
    )


def test_kill_control_covered_module_does_not_false_positive(tmp_path: pathlib.Path) -> None:
    """Symmetry check: a module matching the http_ prefix rule must NOT be
    flagged, even though it issues the same wire call."""
    fake_src = tmp_path / "src" / "nexus"
    fake_src.mkdir(parents=True)
    (fake_src / "http_fake_store.py").write_text(
        'class HttpFakeStore:\n'
        '    def write(self):\n'
        '        return self._post("/manifest/write", {"collection": "x"})\n'
    )
    assert wctp.live_client_modules_missing_coverage(tmp_path) == []


# ---------------------------------------------------------------------------
# Kill control: a synthetic both-halves commit absent from the ledger must
# fail. Purely in-memory -- evaluate() only needs (sha, subject) tuples for
# the undeclared check, no real git commit required.
# ---------------------------------------------------------------------------


def test_kill_control_undeclared_synthetic_commit_fails() -> None:
    synthetic = wctp.FlaggedCommit(
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        subject="synthetic both-halves commit for kill-control",
        engine_paths=("service/src/main/java/dev/nexus/service/http/FakeHandler.java",),
        client_paths=("src/nexus/catalog/http_catalog_client.py",),
    )
    empty_ledger = wctp.Ledger()
    result = wctp.evaluate([synthetic], empty_ledger, newest_tag=None)
    assert result is not wctp.GIT_UNAVAILABLE
    assert not result.ok
    assert synthetic in result.undeclared
    assert not result.stale


def test_kill_control_declared_commit_passes() -> None:
    synthetic = wctp.FlaggedCommit(
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        subject="synthetic both-halves commit, declared",
        engine_paths=("service/src/main/java/dev/nexus/service/http/FakeHandler.java",),
        client_paths=("src/nexus/catalog/http_catalog_client.py",),
    )
    ledger = wctp.Ledger(
        unshipped={
            "deadbeef": wctp.LedgerEntry(
                sha="deadbeef",
                bead="nexus-fake",
                note="kill-control fixture",
                engine_tag="engine-service-v9.9.9",
            )
        }
    )
    result = wctp.evaluate([synthetic], ledger, newest_tag=None)
    assert result.ok


def test_every_ledger_bullet_parses() -> None:
    """Every `- \\`sha\\`` bullet under ## Unshipped / ## Shipped must match
    its section's regex — a malformed line is silently DROPPED by
    parse_ledger (no error), which can reopen the exact declared-vs-gate
    contradiction the Shipped-counts-as-declared fix closed (found live:
    the nexus-o8dil.33 line used `-- engine tag` phrasing in ## Shipped and
    vanished from ledger.shipped with zero signal)."""
    section = None
    bad: list[str] = []
    for line in wctp.DEFAULT_LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Unshipped"):
            section = wctp._UNSHIPPED_RE
        elif line.startswith("## Shipped"):
            section = wctp._SHIPPED_RE
        elif line.startswith("## "):
            section = None
        elif section is not None and line.startswith("- `") and not section.match(line):
            bad.append(line)
    assert not bad, (
        "ledger bullet(s) do not match their section's regex and would be "
        f"silently dropped by parse_ledger: {bad}"
    )


def test_shipped_entry_counts_as_declared() -> None:
    """A flagged commit declared under ## Shipped is NOT undeclared
    (nexus-55r6o follow-up): the release-PR window moves an entry
    Unshipped -> Shipped before the tag exists — the exact remedy the
    release-ledger-gate prescribes — while the commit is still inside the
    scanned range. Counting only Unshipped made the PR gate and this lint
    contradictory for every release PR."""
    synthetic = wctp.FlaggedCommit(
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        subject="synthetic both-halves commit, declared Shipped pre-tag",
        engine_paths=("service/src/main/java/dev/nexus/service/http/FakeHandler.java",),
        client_paths=("src/nexus/catalog/http_catalog_client.py",),
    )
    ledger = wctp.Ledger(
        shipped={
            "deadbeef": wctp.LedgerEntry(
                sha="deadbeef",
                bead="nexus-fake",
                note="release-PR-window fixture",
                shipped_in="v9.9.9",
            )
        }
    )
    result = wctp.evaluate([synthetic], ledger, newest_tag=None)
    assert result is not wctp.GIT_UNAVAILABLE
    assert result.ok


def test_stale_ledger_entry_detected() -> None:
    """An Unshipped entry whose commit is already an ancestor of the newest
    published tag must be flagged stale -- reuses a real, permanently-shipped
    commit so is_ancestor() has real git state to answer against."""
    ledger = wctp.Ledger(
        unshipped={
            "498c92953ea3ad60a75389aea53a9f501d8b126a": wctp.LedgerEntry(
                sha="498c92953ea3ad60a75389aea53a9f501d8b126a",
                bead="nexus-sh9v2",
                note="already shipped in v7.7.0 -- should be STALE here",
                engine_tag="engine-service-v0.1.73",
            )
        }
    )
    result = wctp.evaluate([], ledger, newest_tag="v7.7.0", repo_root=_REPO_ROOT)
    assert result is not wctp.GIT_UNAVAILABLE
    assert not result.ok
    assert len(result.stale) == 1
    assert result.stale[0].sha == "498c92953ea3ad60a75389aea53a9f501d8b126a"


# ---------------------------------------------------------------------------
# Production gate: run against the LIVE repo + seeded ledger.
# ---------------------------------------------------------------------------


def test_live_repo_ledger_is_clean() -> None:
    """The actual gate. A both-halves commit landing without a ledger entry,
    or a ledger entry going stale without being cleared, fails HERE -- this
    is what CI enforces on every push (nexus-1vogq)."""
    rc = wctp.check(repo_root=_REPO_ROOT)
    assert rc == 0, (
        "the wire-contract ledger and the live repo state disagree -- run "
        "`uv run python scripts/check_wire_contract_pairing.py` for the "
        f"full report, and update {_LEDGER}."
    )
