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
scaffolding proving the detector actually detects, and kill-controls.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

import check_wire_contract_pairing as wctp

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_LEDGER = wctp.DEFAULT_LEDGER_PATH

#: The three known both-halves commits from the RDR-191 GATE-2 incident
#: (T2 [22490] Q3b census). Fixed, permanent identifiers.
_KNOWN_MEMBERS = {
    "498c92953ea3ad60a75389aea53a9f501d8b126a",
    "b361a8106953c0bb586ab3aac969f904d3dff9df",
    "8c75a61a3fd1d65f61695263ea1b0961377c358d",
}


def test_the_live_ledger_is_well_formed() -> None:
    """The file exists, keeps its seeded history, and every bullet parses: a malformed line is
    silently DROPPED by parse_ledger (the nexus-o8dil.33 line once vanished with zero signal).
    The [additive] token must LEAD the note and carry direction-safety prose naming both
    directions (nexus-1emxn, T2 [23828]), and every in-scope Shipped entry names its engine tag
    once as `engine half <tag>` and leads a ` -- ` segment with the token (a mention is not a
    statement)."""
    assert _LEDGER.is_file(), f"{_LEDGER} is missing"
    ledger = wctp.parse_ledger(_LEDGER)
    assert set(ledger.shipped) >= _KNOWN_MEMBERS, "do not delete the historical record"
    for sha in _KNOWN_MEMBERS:
        assert ledger.shipped[sha].shipped_in == "v7.7.0"

    section, bad = None, []
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Unshipped"):
            section = wctp._UNSHIPPED_RE
        elif line.startswith("## Shipped"):
            section = wctp._SHIPPED_RE
        elif line.startswith("## "):
            section = None
        elif section is not None and line.startswith("- `") and not section.match(line):
            bad.append(line)
    assert not bad, f"bullets that parse_ledger would silently drop: {bad}"

    problems: list[str] = []
    for e in ledger.unshipped.values():
        for token in ("[additive]", "[not-additive]"):
            if token in e.note and not e.note.startswith(token):
                problems.append(f"{e.sha[:9]} ({e.bead}): {token} appears mid-note; lead the note with it or drop it")
        if e.additive is True and not ("old client" in e.note.lower() and "new engine" in e.note.lower()):
            problems.append(f"{e.sha[:9]} ({e.bead}): [additive] with no 'old client' + 'new engine' reasoning")
    assert not problems, "\n".join(problems)

    in_scope = [(sha, e) for sha, e in ledger.shipped.items() if wctp.shipped_is_in_convention_scope(e.note)]
    assert len(in_scope) >= 20, f"only {len(in_scope)} in-scope shipped entries; the scan is broken"
    floor = wctp.SHIPPED_CONVENTION_FLOOR
    assert not [s for s, e in in_scope if len(wctp._SHIPPED_ENGINE_TAG_RE.findall(e.note)) != 1], (
        f"shipped entries at or above {floor} must name their engine tag exactly once as `engine half <tag>`"
    )
    assert not [s for s, e in in_scope if wctp._shipped_additive_token(e.note) is None], (
        f"shipped entries at or above {floor} must carry [additive] or [not-additive] LEADING a ` -- ` segment"
    )


def test_live_repo_ledger_is_clean() -> None:
    """The actual gate: a both-halves commit landing without a ledger entry, or an entry going
    stale without being cleared, fails HERE (nexus-1vogq)."""
    assert wctp.check(repo_root=_REPO_ROOT) == 0, (
        "the wire-contract ledger and the live repo state disagree; run "
        f"`uv run python scripts/check_wire_contract_pairing.py` and update {_LEDGER}"
    )


def test_the_detector_finds_the_known_historical_members() -> None:
    """Non-vacuity on a fixed range (these tags are immutable): the real detector finds all
    three RDR-191 GATE-2 members in v7.6.1..HEAD; 8c75a61a3's only engine touch is a test file,
    so the engine side must be the FULL service/ tree; and a raw _post("/import/...") test
    envelope counts as a client-side touch (the 2026-08-14 blind spot)."""
    found = {c.sha for c in wctp.flagged_commits("v7.6.1..HEAD", repo_root=_REPO_ROOT)}
    assert not (_KNOWN_MEMBERS - found), f"detector missed {_KNOWN_MEMBERS - found}"
    paths = wctp._touched_paths("8c75a61a3fd1d65f61695263ea1b0961377c358d", repo_root=_REPO_ROOT)
    assert [p for p in paths if wctp._is_engine_path(p)] == [
        "service/src/test/java/dev/nexus/service/RdrO8dil7GlobalManifestAntiJoinTest.java"
    ]
    assert wctp._is_client_test_envelope(
        "498c92953ea3ad60a75389aea53a9f501d8b126a",
        "tests/db/test_http_catalog_integration.py",
        repo_root=_REPO_ROOT,
    )


@pytest.mark.parametrize(
    ("classifier", "path", "expected"),
    [
        ("client", "src/nexus/catalog/http_catalog_client.py", True),
        ("client", "src/nexus/catalog/store_hook.py", True),
        ("client", "src/nexus/mcp_infra.py", True),
        ("client", "src/nexus/indexer.py", True),
        ("client", "src/nexus/doc_indexer.py", True),
        ("client", "src/nexus/db/http_vector_client.py", True),
        # covered only by the structural http_ naming rule, not _CLIENT_FILE_SUBSTRINGS (T2 [22513])
        ("client", "src/nexus/db/t2/http_aspect_queue.py", True),
        ("client", "src/nexus/db/t2/http_taxonomy_store.py", True),
        ("client", "src/nexus/cli.py", False),
        ("client", "docs/architecture.md", False),
        ("client", "tests/test_indexer.py", False),  # tests/ is content-based
        ("engine", "service/src/main/java/dev/nexus/service/http/CatalogHandler.java", True),
        ("engine", "service/src/main/resources/db/changelog/catalog-025-collection-not-null.xml", True),
        ("engine", "service/src/test/java/dev/nexus/service/CatalogRepositoryTest.java", True),
        ("engine", "src/nexus/catalog/http_catalog_client.py", False),
        ("engine", "docs/architecture.md", False),
    ],
)
def test_path_classification(classifier: str, path: str, expected: bool) -> None:
    fn = wctp._is_client_module_path if classifier == "client" else wctp._is_engine_path
    assert fn(path) is expected


def test_the_client_coverage_drift_guard_detects_a_real_gap(tmp_path: pathlib.Path) -> None:
    """Every live module issuing a raw manifest/import _post is covered by
    _is_client_module_path; a synthetic uncovered module IS caught, and one matching the http_
    rule is NOT (kill controls: the guard is not vacuous)."""
    assert wctp.live_client_modules_missing_coverage(_REPO_ROOT) == []
    src = tmp_path / "src" / "nexus"
    src.mkdir(parents=True)
    call = 'class C:\n    def write(self):\n        return self._post("/manifest/write", {"collection": "x"})\n'
    (src / "fake_wire_caller.py").write_text(call)
    (src / "http_fake_store.py").write_text(call)
    assert wctp.live_client_modules_missing_coverage(tmp_path) == ["src/nexus/fake_wire_caller.py"]


# ---------------------------------------------------------------------------
# Merge commits: a real two-parent fixture repo, never a mock of git's output.
# ---------------------------------------------------------------------------


def _git_in(repo: pathlib.Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ["PATH"],
    }
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    )
    return proc.stdout.strip()


def _edit_line(repo: pathlib.Path, rel: str, line_no: int, text: str, message: str) -> str:
    path = repo / rel
    lines = path.read_text().splitlines()
    lines[line_no - 1] = text
    path.write_text("\n".join(lines) + "\n")
    _git_in(repo, "add", rel)
    _git_in(repo, "commit", "-q", "-m", message)
    return _git_in(repo, "rev-parse", "HEAD")


_ENGINE_FILE = "service/Engine.java"
_CLIENT_FILE = "src/nexus/db/http_fake_client.py"


def _both_halves_merge_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str, list[str]]:
    """A repo whose ONLY engine+client pairing is a merge: one line of history edits the engine file
    then the client file in two commits, the other does the same in different hunks, and the second
    merges into the first with no conflict. Returns (repo, merge sha, the four constituent shas)."""
    repo = tmp_path / "repo"
    (repo / "service").mkdir(parents=True)
    (repo / "src" / "nexus" / "db").mkdir(parents=True)
    body = "".join(f"line {i}\n" for i in range(1, 41))
    (repo / _ENGINE_FILE).write_text(body)
    (repo / _CLIENT_FILE).write_text(body)
    _git_in(repo, "init", "-q", "-b", "main")
    # One commit per half: a single base commit adding both would itself be a both-halves commit.
    _git_in(repo, "add", _ENGINE_FILE)
    _git_in(repo, "commit", "-q", "-m", "base: engine")
    _git_in(repo, "add", _CLIENT_FILE)
    _git_in(repo, "commit", "-q", "-m", "base: client")
    _git_in(repo, "checkout", "-q", "-b", "side")
    side = [
        _edit_line(repo, _ENGINE_FILE, 40, "side engine", "side: engine only"),
        _edit_line(repo, _CLIENT_FILE, 40, "side client", "side: client only"),
    ]
    _git_in(repo, "checkout", "-q", "main")
    main = [
        _edit_line(repo, _ENGINE_FILE, 1, "main engine", "main: engine only"),
        _edit_line(repo, _CLIENT_FILE, 1, "main client", "main: client only"),
    ]
    _git_in(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    return repo, _git_in(repo, "rev-parse", "HEAD"), main + side


def _is_both_halves(paths: list[str]) -> bool:
    return any(wctp._is_engine_path(p) for p in paths) and any(
        wctp._is_client_module_path(p) for p in paths
    )


def test_merge_commits_are_not_both_halves_unless_a_commit_in_them_is(tmp_path: pathlib.Path) -> None:
    """`git show --name-only` of a merge is the COMBINED diff, so a merge of two lines that each
    touched one half used to be flagged. It is not; and skipping merges must not hide a genuine
    pairing, which is flagged by its own sha."""
    repo, merge, constituents = _both_halves_merge_repo(tmp_path)
    # Non-vacuity: a two-parent commit whose combined-diff name list holds both halves.
    assert len(_git_in(repo, "rev-list", "--parents", "-n1", merge).split()) == 3
    assert _is_both_halves(wctp._touched_paths(merge, repo_root=repo))
    for sha in constituents:
        assert not _is_both_halves(wctp._touched_paths(sha, repo_root=repo)), f"{sha} must change only one half"
    assert wctp.flagged_commits("main", repo_root=repo) == []

    _git_in(repo, "checkout", "-q", "-b", "paired")
    for rel in (_ENGINE_FILE, _CLIENT_FILE):
        path = repo / rel
        lines = path.read_text().splitlines()
        lines[19] = "paired edit"
        path.write_text("\n".join(lines) + "\n")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "paired: both halves")
    paired = _git_in(repo, "rev-parse", "HEAD")
    _git_in(repo, "checkout", "-q", "main")
    _git_in(repo, "merge", "-q", "--no-ff", "-m", "merge paired", "paired")
    assert [f.sha for f in wctp.flagged_commits("main", repo_root=repo)] == [paired]


# ---------------------------------------------------------------------------
# Verdicts and token parsing.
# ---------------------------------------------------------------------------

_SYNTHETIC = wctp.FlaggedCommit(
    sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    subject="synthetic both-halves commit",
    engine_paths=("service/src/main/java/dev/nexus/service/http/FakeHandler.java",),
    client_paths=("src/nexus/catalog/http_catalog_client.py",),
)


def test_evaluate_verdicts() -> None:
    """Undeclared fails; declared passes whether Unshipped or already moved to Shipped (the
    release-PR window moves an entry before the tag exists: nexus-55r6o); an Unshipped entry
    whose commit is an ancestor of the newest published tag is STALE (a real, permanently
    shipped commit, so is_ancestor has real git state to answer against)."""
    undeclared = wctp.evaluate([_SYNTHETIC], wctp.Ledger(), newest_tag=None)
    assert undeclared is not wctp.GIT_UNAVAILABLE and not undeclared.ok
    assert _SYNTHETIC in undeclared.undeclared and not undeclared.stale

    unshipped = wctp.Ledger(unshipped={"deadbeef": wctp.LedgerEntry(
        sha="deadbeef", bead="nexus-fake", note="fixture", engine_tag="engine-service-v9.9.9")})
    assert wctp.evaluate([_SYNTHETIC], unshipped, newest_tag=None).ok

    shipped = wctp.Ledger(shipped={"deadbeef": wctp.LedgerEntry(
        sha="deadbeef", bead="nexus-fake", note="fixture", shipped_in="v9.9.9")})
    assert wctp.evaluate([_SYNTHETIC], shipped, newest_tag=None).ok

    sha = "498c92953ea3ad60a75389aea53a9f501d8b126a"
    stale_ledger = wctp.Ledger(unshipped={sha: wctp.LedgerEntry(
        sha=sha, bead="nexus-sh9v2", note="already shipped in v7.7.0", engine_tag="engine-service-v0.1.73")})
    stale = wctp.evaluate([], stale_ledger, newest_tag="v7.7.0", repo_root=_REPO_ROOT)
    assert stale is not wctp.GIT_UNAVAILABLE and not stale.ok
    assert [e.sha for e in stale.stale] == [sha]


def test_direction_safety_token_is_a_statement_only_where_it_leads(tmp_path: pathlib.Path) -> None:
    """Prose that MENTIONS [additive] mid-note authorizes nothing (T2 [23829]); a Shipped token
    must lead a ` -- ` segment, [not-additive] anywhere wins (fail-safe), entries below
    SHIPPED_CONVENTION_FLOOR are out of scope by declaration rather than read as answers, and
    the engine tag is the one `engine half` names, not the first one mentioned."""
    ledger_file = tmp_path / "ledger.md"
    ledger_file.write_text(
        "## Unshipped\n\n"
        "- `abcdefabcdefabcdefabcdefabcdefabcdefabcd` -- bead nexus-prose -- "
        "engine tag `engine-service-v9.9.9` -- unlike the sibling marked "
        "[additive], this one changes the wire\n"
        "## Shipped\n"
    )
    assert next(iter(wctp.parse_ledger(ledger_file).unshipped.values())).additive is None

    for note, in_scope, token, tag in (
        ("engine half engine-service-v0.1.116 (deployed BEFORE the client tag). [additive] one NEW route.",
         True, None, "engine-service-v0.1.116"),
        ("engine half engine-service-v0.1.112 (tagged on 9f0a5397c) -- [additive] no shape change.",
         True, True, "engine-service-v0.1.112"),
        ("engine half engine-service-v0.1.109 -- [additive] but actually [not-additive]",
         True, False, "engine-service-v0.1.109"),
        ("engine half engine-service-v0.1.88 (deployed 2026-08-27), client half in the same commit",
         False, None, "engine-service-v0.1.88"),
        ("engine half engine-service-v0.1.112 (supersedes the engine-service-v0.1.109 behaviour) -- [additive] no shape change.",
         True, True, "engine-service-v0.1.112"),
    ):
        assert wctp.shipped_is_in_convention_scope(note) is in_scope, note
        assert wctp._shipped_additive_token(note) is token, note
        assert wctp.shipped_engine_tag(note) == tag, note
