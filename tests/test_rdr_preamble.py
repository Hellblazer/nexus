# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``nx rdr preamble <name>`` subcommands.

Covers: rdr-create, rdr-list, rdr-gate (including the re-gate block, round
number and fix check), rdr-accept, rdr-close, rdr-research, rdr-audit,
rdr-fix, rdr-verdict and phase-review-gate.

One test per behaviour: cases that differ only in their input are rows of a
parametrized table (the row name is the pytest id, and every assertion message
names the failing row); a test stands alone only where a sequence matters.

For the subcommands that read RDRs, both data paths are covered:
  - T2-read path   : T2Database seeded with known RDR fixtures
  - file-fallback  : empty T2, fixture .md files in tmp docs/rdr/

The ``$ARGUMENTS`` passthrough via ``--`` terminator is covered explicitly.

Invocation convention:
  CliRunner().invoke(rdr, ["preamble", "<name>", ...])
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.rdr import _gate_layer1_plan_grammar_conformant, rdr
from nexus.db.t2 import T2Database
from nexus.plans.audit_rounds import BLOCKS_PLANNING, DISCOVER_AT_IMPLEMENTATION

_PLUGIN_DIR = Path(__file__).parent.parent / "conexus"

#: nexus-yjf5l.10: the class-to-disposition clause (which disposition a
#: BLOCKS-PLANNING or unclassified residual needs) is stated identically in
#: rdr-accept/SKILL.md step 1b, conexus/commands/rdr-accept.md Step 2b, and
#: the printed ``preamble_rdr_accept`` brief. One regex extracts the sentence
#: from whichever surface carries it so the comparison is a single equality
#: across all three, not three independent substring checks that could each
#: drift on their own (the fix-check parenthetical is exactly what drifted:
#: the printed brief dropped it entirely).
_ACCEPT_DISPOSITION_CLAUSE_RE = re.compile(
    r"A residual classed `BLOCKS-PLANNING`, or an unclassified residual \(every line "
    r"written before the class field existed\), needs an explicit author disposition "
    r"— a sha \(with its fix check\) or a bead — and the choice is recorded, never "
    r"defaulted",
)


def _disposition_clause(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text)
    match = _ACCEPT_DISPOSITION_CLAUSE_RE.search(normalized)
    assert match, f"disposition clause not found in: {text[:200]!r}..."
    return match.group(0).strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _runner() -> CliRunner:
    """CliRunner."""
    return CliRunner()


@pytest.fixture(scope="session")
def _rdr_git_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A single real ``git init`` reused by every ``rdr_env`` instance.

    test-suite-compression P0 (nexus-test-cleanup, 2026-08-05): the fixture's
    behavioral requirement is a directory where ``git rev-parse
    --show-toplevel`` resolves cleanly — a truly empty ``.git/`` marker
    directory does NOT satisfy that (git requires a real repo structure), so
    the fix keeps ONE real ``git init`` and fans it out via
    ``shutil.copytree`` per test instead of re-spawning the subprocess ~74
    times (T1 scratch 47337851: this was the second-largest slow-test
    contributor after the storage-boundary-lint AST-scan cache).
    """
    template = tmp_path_factory.mktemp("rdr_git_template")
    subprocess.run(
        ["git", "init", str(template)],
        check=True, capture_output=True,
    )
    return template


@pytest.fixture()
def rdr_env(tmp_path: Path, monkeypatch, _rdr_git_template: Path):
    """Hermetic environment: tmp git repo, default T2 path, cwd set to repo root.

    Returns a namespace-like dict:
      rdr_dir     -- Path to tmp_path/docs/rdr (created)
      db_path     -- Path to tmp_path/t2.db
      db          -- live T2Database (open for seeding, auto-closed via yield)
      repo_root   -- tmp_path (the fake git root)
    """
    # Copy the session-template .git/ into this test's tmp_path instead of
    # spawning a fresh `git init` subprocess per test — cheap (shutil,
    # in-process) and behaviorally identical: `git rev-parse
    # --show-toplevel` resolves against the copied .git/ exactly as it would
    # against a freshly-initialized one.
    shutil.copytree(_rdr_git_template / ".git", tmp_path / ".git")
    monkeypatch.chdir(tmp_path)

    # Ensure docs/rdr exists (subcommands default to this path).
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True, exist_ok=True)

    # Redirect T2 to tmp SQLite so we don't touch the real database.
    db_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)

    db = T2Database(db_path)
    yield {
        "rdr_dir": rdr_dir,
        "db_path": db_path,
        "db": db,
        "repo_root": tmp_path,
    }
    db.close()


def _write_rdr(rdr_dir: Path, filename: str, frontmatter: dict, body: str = "") -> Path:
    """Write a minimal RDR markdown file with YAML frontmatter."""
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    if body:
        fm_lines.append(body)
    path = rdr_dir / filename
    path.write_text("\n".join(fm_lines), encoding="utf-8")
    return path


def _seed_rdr_t2(db: T2Database, repo_name: str, rdr_id: str, **fields) -> None:
    """Seed a single RDR entry in T2 under project ``<repo_name>_rdr``.

    ``rdr_id`` must be a numeric string (e.g. "1", "130") — the ported
    rdr-list code filters on ``re.match(r'^\\d+$', title)``.
    """
    content_lines = [f"{k}: {v}" for k, v in fields.items()]
    db.put(
        project=f"{repo_name}_rdr",
        title=rdr_id,
        content="\n".join(content_lines),
    )




class _FakeT2ResearchClient:
    """In-memory T2 double for ``rdr-research add`` — matches the
    ``get_all(project=...)`` / ``get(project=, title=)`` / ``put(project=,
    title=, content=, ...)`` contract ``T2Database`` exposes (RDR-201 P1.4
    convention, see ``_FakeT2CensusClient`` in test_rdr_audit_vocabulary.py).

    ``hidden_from_get_all`` lets a test simulate a stale scan: a title
    present in ``get()`` (so a collision check still finds it) but absent
    from ``get_all()`` (so the seq-scan doesn't see it) — the exact race
    window nexus-zu1q0 exploited.
    """

    def __init__(
        self,
        entries: dict[str, str] | None = None,
        hidden_from_get_all: frozenset[str] = frozenset(),
    ) -> None:
        self._store: dict[str, str] = dict(entries or {})
        self._hidden = hidden_from_get_all
        self.put_calls: list[tuple[str, str]] = []

    def __enter__(self) -> "_FakeT2ResearchClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def get_all(self, project: str | None = None) -> list[dict]:
        return [
            {"title": t, "content": c}
            for t, c in self._store.items()
            if t not in self._hidden
        ]

    def get(self, project: str | None = None, title: str | None = None, id: int | None = None):
        content = self._store.get(title)
        return None if content is None else {"title": title, "content": content}

    def put(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        self.put_calls.append((title, content))
        self._store[title] = content
        return len(self._store)


class _FakeT2UpsertClient(_FakeT2ResearchClient):
    """Like ``_FakeT2ResearchClient``, but with real T2 id semantics
    (nexus-yjf5l.13): a title gets a permanent id at its first ``put()``,
    and every later ``put()`` to the SAME title reuses that id —
    ``memory_put``'s "Upserts by (project, title)" contract. ``get()``
    returns that id.

    ``_FakeT2ResearchClient.get()`` never set an ``id`` key at all, so it
    could not model the defect this reproduces: a ``-gate-latest`` row is
    upserted under one fixed title every round, so a real T2 id for that
    title stays constant across every round while a critique record's id
    (a fresh, date-suffixed title each round, never overwritten) does not.
    """

    def __init__(
        self,
        entries: dict[str, str] | None = None,
        ids: dict[str, int] | None = None,
    ) -> None:
        super().__init__(entries)
        self._ids: dict[str, int] = dict(ids or {})
        self._next_id = max(self._ids.values(), default=0) + 1

    def get(self, project: str | None = None, title: str | None = None, id: int | None = None):
        content = self._store.get(title)
        if content is None:
            return None
        return {"title": title, "content": content, "id": self._ids.get(title)}

    def put(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        self.put_calls.append((title, content))
        self._store[title] = content
        if title not in self._ids:
            self._ids[title] = self._next_id
            self._next_id += 1
        return self._ids[title]


# ---------------------------------------------------------------------------
# Shared helpers for the table-driven tests below
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "rdr_gate_critiques"

_HELLO = {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"}
_BOARD = "| File | Title | Status | Type |"
_ITEM_TABLE = "| # | Label | Evidence needed |"


class _Boom:
    """A T2 client whose open raises, standing in for an unreachable engine."""

    def __enter__(self):
        raise ConnectionError("engine down")

    def __exit__(self, *a):
        return False


def _preamble(*argv: str):
    return _runner().invoke(rdr, ["preamble", *argv])


def _use_t2(monkeypatch, entries=None, *, hidden=frozenset(), client=None):
    """Point the preamble at an in-memory T2 double; returns the double."""
    import nexus.commands.rdr as rdr_mod

    fake = client if client is not None else _FakeT2ResearchClient(
        entries or {}, hidden_from_get_all=hidden,
    )
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    return fake


def _assert_output(case, out, *, has=(), lacks=(), has_ci=(), ordered=()):
    """Assert on a preamble's output; every message names the failing case."""
    for s in has:
        assert s in out, f"[{case}] expected {s!r} in output:\n{out}"
    for s in lacks:
        assert s not in out, f"[{case}] unexpected {s!r} in output:\n{out}"
    for s in has_ci:
        assert s.lower() in out.lower(), f"[{case}] expected {s!r} (any case) in output:\n{out}"
    if ordered:
        positions = []
        for s in ordered:
            assert s in out, f"[{case}] ordered marker {s!r} missing from output:\n{out}"
            positions.append(out.index(s))
        assert positions == sorted(positions), (
            f"[{case}] markers {ordered!r} out of order (positions {positions}):\n{out}"
        )


def _git_commit_rdr(
    rdr_env, body: str, msg: str, *, status: str = "draft", name: str = "rdr-204-example.md",
) -> str:
    """Write ``docs/rdr/<name>``, commit it, return the short sha."""
    path = _write_rdr(
        rdr_env["rdr_dir"], name,
        {"title": "Example", "status": status, "type": "Architecture", "priority": "medium"},
        body=body,
    )
    root = str(rdr_env["repo_root"])
    subprocess.run(["git", "-C", root, "add", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%h"], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _table(rows: list[dict]) -> list:
    """Parametrize rows keyed by ``name``; the name is the pytest id."""
    return [pytest.param(row, id=row["name"]) for row in rows]


def _write_files(rdr_env, files) -> None:
    for filename, fm, body in files:
        _write_rdr(rdr_env["rdr_dir"], filename, fm, body)


# ---------------------------------------------------------------------------
# Shared resolution: repo name, rdr_paths, companion files
# ---------------------------------------------------------------------------


def test_repo_name_is_the_primary_checkout_not_the_worktree_dir(tmp_path: Path, monkeypatch):
    """``git rev-parse --show-toplevel`` returns the WORKTREE's own root from a
    linked worktree, so naively taking its basename prints the worktree
    directory's name as the repo name (nexus-w5gma). repo_name must resolve via
    the git common dir, which every worktree shares with the primary."""
    primary = tmp_path / "nexus"
    primary.mkdir()
    subprocess.run(["git", "init", str(primary)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(primary), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    worktree = tmp_path / "some-agent-worktree-dir"
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", str(worktree)],
        check=True, capture_output=True,
    )

    monkeypatch.chdir(worktree)
    from nexus.commands.rdr import _preamble_resolve_repo
    repo_root, repo_name = _preamble_resolve_repo()

    assert repo_root == str(worktree)
    assert repo_name == "nexus"
    assert repo_name != worktree.name


def test_preamble_rdr_dir_accepts_a_scalar_rdr_paths(tmp_path):
    """nexus-u1jxt.10: ``rdr_paths: docs/decisions`` (a scalar, not a list)
    resolved to its first character."""
    from nexus.commands.rdr import _preamble_rdr_dir

    (tmp_path / ".nexus.yml").write_text("indexing:\n  rdr_paths: docs/decisions\n")
    assert _preamble_rdr_dir(str(tmp_path)) == "docs/decisions"


_REAL = {"title": "Real", "status": "accepted"}
_COMPANION = {"title": "Plan", "kind": "companion"}

#: (case, files[(filename, frontmatter)], lookups[(argument, expected filename)])
_COMPANION_RESOLVER_CASES = [
    ("companion_that_sorts_first_is_not_the_rdr",
     [("rdr-049-aaa-plan.md", {**_COMPANION, "status": "abandoned"}),
      ("rdr-049-real.md", _REAL)],
     [("49", "rdr-049-real.md"), ("RDR-049", "rdr-049-real.md")]),
    # The companion sorts AFTER the RDR, so first-match-by-name cannot return
    # it: only the named-file branch can.
    ("naming_the_companion_file_still_resolves_it",
     [("rdr-049-real.md", _REAL), ("rdr-049-zzz-plan.md", _COMPANION)],
     [("rdr-049-zzz-plan.md", "rdr-049-zzz-plan.md"), ("rdr-049-zzz-plan", "rdr-049-zzz-plan.md")]),
    # rdr-079-calibration.md and rdr-152-fts-parity-contract.md carry
    # ``id: companion-note`` and no ``kind``.
    ("older_companion_note_without_kind_is_not_the_rdr",
     [("rdr-079-aaa-calibration.md", {"title": "Cal", "id": "companion-note", "status": "closed"}),
      ("rdr-079-real.md", {"title": "Real", "status": "abandoned"})],
     [("79", "rdr-079-real.md")]),
    ("file_carrying_the_rdrs_own_id_wins",
     [("rdr-152-aaa-untagged-note.md", {"title": "Note", "status": "closed"}),
      ("rdr-152-real.md", {"title": "Real", "id": "RDR-152", "status": "closed"})],
     [("152", "rdr-152-real.md")]),
    ("lone_companion_is_still_found",
     [("rdr-077-notes.md", {"title": "Notes", "kind": "companion"})],
     [("77", "rdr-077-notes.md")]),
]


@pytest.mark.parametrize("case, files, lookups", _COMPANION_RESOLVER_CASES,
                         ids=[c[0] for c in _COMPANION_RESOLVER_CASES])
def test_resolver_skips_companions(rdr_env, case, files, lookups):
    """nexus-u1jxt.1: the resolver returned the alphabetically first file whose
    number matched, so a ``kind: companion`` file that sorts first was taken
    for the RDR and every preamble acted on the wrong file."""
    from nexus.commands.rdr import _preamble_find_rdr_file  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    d = rdr_env["rdr_dir"]
    for filename, fm in files:
        _write_rdr(d, filename, fm, "x\n")
    for arg, expected in lookups:
        got = _preamble_find_rdr_file(d, arg)
        assert got.name == expected, f"[{case}] lookup {arg!r} resolved to {got.name}, want {expected}"


#: (case, files[(filename, frontmatter)], number, filename to inspect, exit code or None, status text)
_COMPANION_SET_STATUS_CASES = [
    ("flips_the_rdr_and_not_the_companion",
     [("rdr-049-aaa-plan.md", {**_COMPANION, "status": "abandoned"}),
      ("rdr-049-real.md", {"title": "Real", "status": "draft"})],
     "49", "rdr-049-real.md", None, "status: abandoned"),
    ("refuses_a_bare_number_whose_only_match_is_a_companion",
     [("rdr-077-notes.md", {**_COMPANION, "status": "draft"})],
     "77", "rdr-077-notes.md", 1, "status: draft"),
]


@pytest.mark.parametrize("case, files, number, inspected, exit_code, status_text",
                         _COMPANION_SET_STATUS_CASES,
                         ids=[c[0] for c in _COMPANION_SET_STATUS_CASES])
def test_set_status_skips_companions(rdr_env, case, files, number, inspected, exit_code, status_text):
    d = rdr_env["rdr_dir"]
    for filename, fm in files:
        _write_rdr(d, filename, fm, "x\n")
    res = _runner().invoke(rdr, ["set-status", number, "abandoned", "--reason", "test"])
    if exit_code is not None:
        assert res.exit_code == exit_code, f"[{case}] {res.output}"
    assert status_text in (d / inspected).read_text(), f"[{case}] {res.output}"


def test_on_the_real_tree_a_shared_number_never_resolves_to_a_companion():
    """The fixtures above cannot see a marker nobody thought of: the first
    version of this fix passed them and still resolved 79 and 152 to
    companions. This walks docs/rdr itself."""
    from nexus.commands.rdr import (  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        _PREAMBLE_EXCLUDED, _preamble_find_rdr_file, _preamble_parse_frontmatter, _rdr_meta_is_companion,
    )
    tree = Path(__file__).resolve().parents[1] / "docs" / "rdr"
    by_number: dict[int, list[Path]] = {}
    for f in sorted(tree.glob("*.md")):
        nums = re.findall(r"\d+", f.stem)
        if nums and f.name.lower() not in _PREAMBLE_EXCLUDED:
            by_number.setdefault(int(nums[0]), []).append(f)
    shared = {n: fs for n, fs in by_number.items() if len(fs) > 1}
    assert len(shared) >= 5, f"non-vacuity: expected several shared numbers, found {sorted(shared)}"
    for n, files in shared.items():
        real = [f for f in files if not _rdr_meta_is_companion(_preamble_parse_frontmatter(f)[0])]
        if not real:
            continue
        got = _preamble_find_rdr_file(tree, str(n))
        assert got in real, f"{n} resolved to {got.name}, a companion; real: {[f.name for f in real]}"


# ---------------------------------------------------------------------------
# rdr-list / rdr-create
# ---------------------------------------------------------------------------

_LIST_CASES = [
    {"name": "t2_path", "seed_t2": True, "files": [], "drop_dir": False,
     "has": ["source: T2", "| ID | Title | Status | Type | Priority |",
             "Command Preambles via the nx CLI", "130"]},
    {"name": "file_fallback", "seed_t2": False,
     "files": [("rdr-001-hello-world.md", _HELLO, "")], "drop_dir": False,
     "has": ["source: files", "| ID | Title | Status | Type | Priority |", "Hello World"]},
    {"name": "no_rdr_dir_exits_clean", "seed_t2": False, "files": [], "drop_dir": True,
     "has": ["docs/rdr"]},
]


@pytest.mark.parametrize("case", _table(_LIST_CASES))
def test_rdr_list(rdr_env, case):
    """T2-seeded, file-fallback (empty T2) and missing-directory paths."""
    if case["seed_t2"]:
        _seed_rdr_t2(
            rdr_env["db"], rdr_env["repo_root"].name, "130",
            title="Command Preambles via the nx CLI", status="accepted",
            type="decision", priority="P0",
            file_path="docs/rdr/rdr-130-command-preambles-via-nx-cli.md",
        )
    _write_files(rdr_env, case["files"])
    if case["drop_dir"]:
        shutil.rmtree(rdr_env["rdr_dir"])
    result = _preamble("rdr-list")
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"])


_CREATE_CASES = [
    {"name": "with_existing_rdrs", "files": [("rdr-001-hello-world.md", _HELLO, "")], "drop_dir": False,
     "has": ["**Next ID:**", "RDR-002", "**ID style detected:**", "Existing RDRs", "Hello World"]},
    {"name": "no_rdr_dir_bootstraps", "files": [], "drop_dir": True,
     "has": ["bootstrap required", "RDR-001", "this will be the first RDR"]},
]


@pytest.mark.parametrize("case", _table(_CREATE_CASES))
def test_rdr_create(rdr_env, case):
    _write_files(rdr_env, case["files"])
    if case["drop_dir"]:
        shutil.rmtree(rdr_env["rdr_dir"])
    result = _preamble("rdr-create")
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"])


# ---------------------------------------------------------------------------
# No-argument usage across subcommands
# ---------------------------------------------------------------------------

_OPEN_ONE = {"title": "Open One", "status": "open", "type": "decision", "priority": "P1"}

_USAGE_CASES = [
    ("rdr-gate", ["Usage", "Available RDRs", _BOARD]),
    # The eligibility table includes `open` RDRs too (GH #1409).
    ("rdr-accept", ["Usage", "Draft RDRs (eligible for acceptance)", "Hello World", "Open One"]),
    ("rdr-close", ["Usage", "Open/Draft RDRs", "Hello World"]),
    ("rdr-research", ["Available RDRs", _BOARD, "Usage"]),
    ("phase-review-gate", ["Usage", "What this gate does", "Pass 1", "Pass 2"]),
]


@pytest.mark.parametrize("subcommand, has", _USAGE_CASES, ids=[c[0] for c in _USAGE_CASES])
def test_no_argument_prints_usage(rdr_env, subcommand, has):
    _write_rdr(rdr_env["rdr_dir"], "rdr-001-hello-world.md", _HELLO)
    _write_rdr(rdr_env["rdr_dir"], "rdr-002-open-one.md", _OPEN_ONE)
    result = _preamble(subcommand)
    assert result.exit_code == 0, f"[{subcommand}] {result.output}"
    _assert_output(subcommand, result.output, has=has)


# ---------------------------------------------------------------------------
# rdr-gate: structure, plan-grammar warning, helpers
# ---------------------------------------------------------------------------

_GATE_STRUCTURE_CASES = [
    {"name": "pre65_prints_section_structure", "file": "rdr-001-hello-world.md", "fm": _HELLO,
     "body": ("## Problem Statement\n\nProblem here.\n\n"
              "## Proposed Solution\n\nSolution here.\n\n"
              "## Tradeoffs\n\nTradeoffs here."),
     "id": "1", "has": ["Section Structure", "## Problem Statement"], "has_ci": []},
    {"name": "post65_without_gaps_is_blocked", "file": "rdr-070-taxonomy.md",
     "fm": {"title": "Taxonomy", "status": "draft", "type": "decision", "priority": "P0"},
     "body": "## Problem Statement\n\nNo gaps structured here.\n\n## Approach\n\nDo things.",
     "id": "70", "has": ["BLOCKED"], "has_ci": ["gap structure"]},
    # rdr-gate output uses the no-space form "Gap1" to match the original rdr_gate.py.
    {"name": "post65_with_gaps_lists_them", "file": "rdr-130-command-preambles.md",
     "fm": {"title": "Command Preambles", "status": "draft", "type": "decision", "priority": "P0"},
     "body": ("## Problem Statement\n\n"
              "#### Gap 1: Missing preamble commands\nThe nx CLI lacks preamble commands.\n\n"
              "#### Gap 2: Brittle bash injection\nBash heredocs break.\n\n"
              "## Proposed Solution\n\nPort to nx CLI."),
     "id": "130", "has": ["Gap1", "Gap2", "gap heading(s) present"], "has_ci": []},
]


@pytest.mark.parametrize("case", _table(_GATE_STRUCTURE_CASES))
def test_rdr_gate_section_and_gap_structure(rdr_env, case):
    _write_rdr(rdr_env["rdr_dir"], case["file"], case["fm"], body=case["body"])
    result = _preamble("rdr-gate", "--", case["id"])
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], has_ci=case["has_ci"])


# nexus-r9esy: Layer 1 plan-grammar WARNING, grandfathered at id <= 205.
_NONCONFORMANT_PLAN_BODY = (
    "## Problem Statement\n\n"
    "#### Gap 1: Something\nSomething is wrong.\n\n"
    "## Approach\n\nDo things.\n\n"
    "## Implementation Plan\n\n"
    "### Phase 1: Do the thing\n\n"
    "1. First step, no heading, a plain numbered list (RDR-204's shape).\n"
    "2. Second step.\n"
)
_CONFORMANT_PLAN_BODY = (
    "## Problem Statement\n\n"
    "#### Gap 1: Something\nSomething is wrong.\n\n"
    "## Approach\n\nDo things.\n\n"
    "## Implementation Plan\n\n"
    "### Phase 1: Do the thing\n\n"
    "#### Step 1: First step\n\nInstructions.\n\n"
    "#### Step 2: Second step\n\nMore instructions.\n"
)

#: (case, rdr id, body, warns, grandfathered note)
_PLAN_GRAMMAR_BOUNDARY_CASES = [
    # One past the boundary: a WARNING, never a block.
    ("206_nonconformant_warns", "206", _NONCONFORMANT_PLAN_BODY, True, False),
    # Exactly the boundary: grandfathered, a note rather than a warning.
    ("205_boundary_is_silent", "205", _NONCONFORMANT_PLAN_BODY, False, True),
    # The actual RDR-204 numbered-list shape named in the bead.
    ("204_below_boundary_is_silent", "204", _NONCONFORMANT_PLAN_BODY, False, False),
    ("207_conformant_never_warns", "207", _CONFORMANT_PLAN_BODY, False, False),
]


@pytest.mark.parametrize("case, rdr_id, body, warns, grandfathered", _PLAN_GRAMMAR_BOUNDARY_CASES,
                         ids=[c[0] for c in _PLAN_GRAMMAR_BOUNDARY_CASES])
def test_rdr_gate_plan_grammar_warning_boundary(rdr_env, case, rdr_id, body, warns, grandfathered):
    _write_rdr(
        rdr_env["rdr_dir"], f"rdr-{rdr_id}-example.md",
        {"title": "Example", "status": "draft", "type": "decision", "priority": "P1"}, body=body,
    )
    result = _preamble("rdr-gate", "--", rdr_id)
    out = result.output
    assert result.exit_code == 0, f"[{case}] {out}"
    if warns:
        _assert_output(case, out, has=["WARNING", "Phase N", "Step N", "Section Structure"],
                       has_ci=["plan grammar"])
    else:
        assert "WARNING" not in out, f"[{case}] unexpected WARNING in:\n{out}"
    if grandfathered:
        low = out.lower()
        assert "predates" in low, f"[{case}] {out}"
        assert "plan-grammar" in low or "plan grammar" in low, f"[{case}] {out}"


# A leading "\n" precedes every fixture: _prg_extract_implementation_plan_
# section's own heading regex requires a preceding newline (it scans for a
# heading mid-document, never document-initial) -- these are section bodies
# handed the way the real RDR file's full text would, never the very first
# bytes of a file.
_CONFORMANT_PLAN = (
    "\n## Implementation Plan\n\n"
    "### Phase 1: Do the thing\n\n"
    "#### Step 1: First step\n\nInstructions.\n\n"
    "#### Step 2: Second step\n\nMore instructions.\n\n"
    "### Phase 2: Do another thing\n\n"
    "#### Step 1: Only step\n\nInstructions.\n"
)

_PLAN_GRAMMAR_CONFORMANT_CASES = [
    ("conformant_plan", _CONFORMANT_PLAN, True),
    # RDR-204's shape: a Phase heading with no Step sub-headings.
    ("plain_numbered_list",
     "\n## Implementation Plan\n\n### Phase 1: Do the thing\n\n1. First step.\n2. Second step.\n", False),
    ("no_phase_heading_at_all", "\n## Implementation Plan\n\nJust prose, no phases.\n", False),
    ("no_implementation_plan_section", "\n## Approach\n\n1. Do a thing.\n2. Do another.\n", False),
    # EVERY phase must have at least one step sub-heading -- a mix is still non-conformant.
    ("one_phase_with_steps_and_one_without",
     ("\n## Implementation Plan\n\n"
      "### Phase 1: Has steps\n\n#### Step 1: Fine\n\nOK.\n\n"
      "### Phase 2: No steps\n\nJust prose here.\n"), False),
    # #### Phase N / ##### Step N (one level deeper than the template's own
    # ### / ####) is still internally consistent.
    ("deeper_heading_depth",
     ("\n## Implementation Plan\n\n"
      "#### Phase 1: Do the thing\n\n##### Step 1: First step\n\nInstructions.\n"), True),
]


@pytest.mark.parametrize("case, text, expected", _PLAN_GRAMMAR_CONFORMANT_CASES,
                         ids=[c[0] for c in _PLAN_GRAMMAR_CONFORMANT_CASES])
def test_gate_layer1_plan_grammar_conformant(case, text, expected):
    assert _gate_layer1_plan_grammar_conformant(text) is expected, case


def _find_gaps(problem_stmt: str) -> list[tuple[str, str, str]]:
    """Replica of the gap-extraction regex in the rdr-close preamble (nexus-2fnet)."""
    return re.findall(r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", problem_stmt, re.MULTILINE)


#: (case, section text, expected [(number, title)]) -- the CONTRACT of which
#: heading shapes the rdr-close preamble's gap regex matches.
_GAP_REGEX_CASES = [
    ("h4_gaps", "#### Gap 1: First gap\nContent.\n\n#### Gap 2: Second gap\nContent.",
     [("1", "First gap"), ("2", "Second gap")]),
    ("h3_gap", "### Gap 1: Three-hash gap\nContent.", [("1", "Three-hash gap")]),
    ("h5_gap", "##### Gap 1: Five-hash gap\nContent.", [("1", "Five-hash gap")]),
    ("h2_not_matched", "## Gap 1: Too few hashes\nContent.", []),
    ("h6_not_matched", "###### Gap 1: Too many hashes\nContent.", []),
    ("no_colon_not_matched", "#### Gap 1 Missing the colon\nContent.", []),
    ("parenthetical_gap", "#### Gap 4 (prerequisite for Gap 1): Complex title\nContent.",
     [("4", "Complex title")]),
    ("multi_digit_number", "#### Gap 12: Twelfth gap\nContent.", [("12", "Twelfth gap")]),
    ("no_gaps", "Some section with no gap headings.\n### Not a gap heading", []),
]


@pytest.mark.parametrize("case, section, expected", _GAP_REGEX_CASES, ids=[c[0] for c in _GAP_REGEX_CASES])
def test_gap_heading_regex_contract(case, section, expected):
    got = [(num, title) for num, _, title in _find_gaps(section)]
    assert got == expected, f"[{case}] {got!r} != {expected!r}"


# ---------------------------------------------------------------------------
# rdr-accept
# ---------------------------------------------------------------------------

_ACCEPT_FM = {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"}

_PHASED_APPROACH_BODY = (
    "## Problem Statement\n\nProblem.\n\n"
    "## Approach\n\n"
    "### Phase 1: Implement\nDo the work.\n\n"
    "### Phase 2: Validate\nCheck it works.\n\n"
    "## Tradeoffs\n\nSome tradeoffs."
)
_NUMBERED_APPROACH_BODY = (
    "## Problem Statement\n\nProblem.\n\n"
    "## Approach\n\n"
    "1. **First step.** Do thing one.\n"
    "2. **Second step.** Do thing two.\n"
    "3. **Third step.** Do thing three.\n"
    "   1. A nested sub-item that must NOT be counted.\n"
    "4. **Fourth step.** Do thing four.\n\n"
    "## Tradeoffs\n\nSome tradeoffs."
)
_OPEN_CONVENTION_BODY = (
    "## Problem Statement\n\nProblem.\n\n"
    "## Approach\n\n### Phase 1: Implement\nWork.\n\n"
    "## Tradeoffs\n\nSome."
)

_ACCEPT_STATUS_CASES = [
    {"name": "draft_prints_planning_handoff", "status": "draft", "body": _PHASED_APPROACH_BODY,
     "has": ["### RDR:", "Planning Handoff", "Step count detected:"], "lacks": []},
    # nexus convention: a numbered list under ## Approach (no ### subheadings)
    # -> step_count == the number of top-level items.
    {"name": "numbered_approach_items_are_counted", "status": "draft", "body": _NUMBERED_APPROACH_BODY,
     "has": ["Step count detected:** 4", "Has plan section:** yes"], "lacks": []},
    # GH #1409 (nexus-qsryj): `open` is a synonym for `draft` here; the
    # gate-PASSED check is the real guard, not the pre-accept status word.
    {"name": "open_status_is_a_draft_synonym", "status": "open", "body": _OPEN_CONVENTION_BODY,
     "has": ["Planning Handoff"], "lacks": ["BLOCKED"]},
    {"name": "closed_status_is_blocked", "status": "closed", "body": "",
     "has": ["BLOCKED", "closed"], "lacks": []},
]


@pytest.mark.parametrize("case", _table(_ACCEPT_STATUS_CASES))
def test_rdr_accept_by_status(rdr_env, case):
    _write_rdr(
        rdr_env["rdr_dir"], "rdr-001-example.md",
        {"title": "Hello World", "status": case["status"], "type": "decision", "priority": "P1"},
        body=case["body"],
    )
    result = _preamble("rdr-accept", "--", "1")
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"])


def test_rdr_accept_brief_names_the_disposition_rules(rdr_env):
    """The printed accept brief carries the residual rules: residuals recorded
    by a round-3+ gate are dispositioned at accept (nexus-g7zgw.2); a residual
    dispositioned by a change to the RDR file gets a fix check on that change
    and a bead id needs none; the class decides which disposition applies
    (nexus-yjf5l.8)."""
    from nexus.commands.rdr import FIX_CHECK_CONSENSUS_CLAUSE

    _write_rdr(rdr_env["rdr_dir"], "rdr-204-example.md", _ACCEPT_FM, body="## Problem\n\nText.\n")
    result = _preamble("rdr-accept", "--", "204")
    assert result.exit_code == 0, result.output
    out = result.output
    _assert_output(
        "accept_brief", out,
        has=[
            # residual disposition rule
            "`residuals:`", "blocks accept",
            # the fix check a sha disposition carries
            "204-fix-check-<sha>", "docs/rdr/rdr-204-example.md", "tip", "bead id", "needs none",
            FIX_CHECK_CONSENSUS_CLAUSE,
            # class-to-disposition rule
            DISCOVER_AT_IMPLEMENTATION, "Implementation Plan phase", BLOCKS_PLANNING,
            "unclassified", "never defaulted",
        ],
    )
    # nexus-yjf5l.10: the disposition clause is the SAME sentence in the skill
    # and this printed brief, as one equality rather than a substring check
    # that could drift (the printed brief once dropped the "(with its fix
    # check)" parenthetical the skill carried).
    skill = (_PLUGIN_DIR / "skills" / "rdr-accept-checklist" / "SKILL.md").read_text()
    assert _disposition_clause(skill) == _disposition_clause(out)


def test_rdr_accept_preamble_opens_no_t2_client(rdr_env, monkeypatch) -> None:
    """nexus-yjf5l.8 / .18: preamble_rdr_accept prints instructions only; it
    never opens a T2 client itself, directly OR through a helper it calls. The
    runtime form: monkeypatching the factory to raise catches both, at every
    calling frame, where a static source scan would miss a helper."""
    import nexus.commands.rdr as rdr_mod

    def _boom() -> None:
        raise AssertionError("preamble_rdr_accept must never open a T2 client")

    monkeypatch.setattr(rdr_mod, "_t2_client_factory", _boom)
    _write_rdr(rdr_env["rdr_dir"], "rdr-204-example.md", _ACCEPT_FM, body="## Problem\n\nText.\n")
    result = _preamble("rdr-accept", "--", "204")
    assert result.exit_code == 0, result.output
    assert "Planning Handoff" in result.output


# ---------------------------------------------------------------------------
# rdr-close
# ---------------------------------------------------------------------------

_ACCEPTED_FM = {"title": "Hello World", "status": "accepted", "type": "decision", "priority": "P1"}
_DEFERRED_FM = {"title": "X", "status": "deferred", "type": "feature"}
_DRAFT_FM = {"title": "Y", "status": "draft", "type": "feature"}

_CLOSE_STATUS_CASES = [
    {"name": "draft_is_blocked", "files": [("rdr-001-hello-world.md", _HELLO, "")],
     "argv": ["--", "1"], "has": ["BLOCKED"], "has_ci": ["draft", "accepted"], "lacks": [], "fake_t2": False},
    # Pre-65 with no gaps: warns and proceeds to the T2 Metadata section.
    {"name": "accepted_pre65_no_gaps_proceeds_to_t2",
     "files": [("rdr-001-hello-world.md", _ACCEPTED_FM,
                "## Problem Statement\n\nProblem without structured gaps.\n\n## Approach\n\nStuff.")],
     "argv": ["--", "1", "--reason", "implemented"], "has": ["T2 Metadata"], "has_ci": [], "lacks": [],
     "fake_t2": False},
    {"name": "force_overrides_the_draft_block", "files": [("rdr-001-hello-world.md", _HELLO, "")],
     "argv": ["--", "1", "--force"], "has": ["Override"], "has_ci": [], "lacks": ["BLOCKED"], "fake_t2": False},
    # S1 regression: --force-implemented with an empty reason must error.
    {"name": "force_implemented_with_empty_reason_errors",
     "files": [("rdr-001-hello-world.md", _ACCEPTED_FM, "")],
     "argv": ["--", "1", "--reason", "implemented", "--force-implemented", ""],
     "has": ["ERROR", "non-empty reason"], "has_ci": [], "lacks": ["T2 Metadata"], "fake_t2": False},
    # nexus-u1jxt.10: `--force` on a non-accepted RDR printed `set-status N
    # closed`, which the lifecycle table always refuses. A deferred RDR has no
    # close edge (resume first); a draft closes only with `--reason`.
    {"name": "force_on_a_deferred_rdr_prints_the_tables_own_command",
     "files": [("rdr-400-x.md", _DEFERRED_FM, "")], "argv": ["--", "400", "--force"],
     "has": ["set-status 400 draft", "no close edge"], "has_ci": [], "lacks": ["set-status 400 closed"],
     "fake_t2": True},
    {"name": "force_on_a_draft_rdr_prints_close_with_reason",
     "files": [("rdr-401-y.md", _DRAFT_FM, "")], "argv": ["--", "401", "--force"],
     "has": ["set-status 401 closed --reason"], "has_ci": [], "lacks": [], "fake_t2": True},
]


@pytest.mark.parametrize("case", _table(_CLOSE_STATUS_CASES))
def test_rdr_close_by_status(rdr_env, monkeypatch, case):
    _write_files(rdr_env, case["files"])
    if case["fake_t2"]:
        _use_t2(monkeypatch, {})
    result = _preamble("rdr-close", *case["argv"])
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"], has_ci=case["has_ci"])


@pytest.mark.parametrize("bd_output, warns", [
    ("nexus-abc: some open bead (open)", True),
    ("No issues found.", False),
], ids=["open_beads_warn", "no_open_beads_no_warning"])
def test_rdr_close_open_beads_warning(rdr_env, monkeypatch, bd_output, warns):
    """S3: the WARNING block is conditional on `bd list` returning open beads."""
    _write_rdr(rdr_env["rdr_dir"], "rdr-001-hello-world.md", _ACCEPTED_FM)

    def _fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return subprocess.run(cmd, **kwargs)
        r = subprocess.CompletedProcess(cmd, 0)
        r.stdout = bd_output
        r.stderr = ""
        return r

    monkeypatch.setattr("nexus.commands.rdr.run_bounded", _fake_run)
    result = _preamble("rdr-close", "--", "1")
    assert result.exit_code == 0, result.output
    if warns:
        _assert_output("open_beads_warn", result.output, has=["WARNING", "Open beads exist", "explicit"])
    else:
        _assert_output("no_open_beads_no_warning", result.output, lacks=["WARNING", "Open beads exist"])


def test_rdr_close_pass2_success_attempts_scratch_put(rdr_env, monkeypatch):
    """S2: after gap-pointer validation passes, the best-effort `nx scratch put`
    marker (rdr-close-active tag) is attempted."""
    impl_file = rdr_env["repo_root"] / "src" / "impl.py"
    impl_file.parent.mkdir(parents=True, exist_ok=True)
    impl_file.write_text("# implementation\n")
    _write_rdr(
        rdr_env["rdr_dir"], "rdr-130-cmd.md",
        {"title": "Command Preambles", "status": "accepted", "type": "decision", "priority": "P0"},
        body=("## Problem Statement\n\n#### Gap 1: Missing feature\nThe feature is missing.\n\n"
              "## Approach\n\nImplement it."),
    )
    scratch_calls = []

    def _capture_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return subprocess.run(cmd, **kwargs)
        scratch_calls.append(list(cmd))
        r = subprocess.CompletedProcess(cmd, 0)
        r.stdout = "No issues found."
        r.stderr = ""
        return r

    monkeypatch.setattr("nexus.commands.rdr.run_bounded", _capture_run)
    result = _preamble("rdr-close", "--", "130", "--reason", "implemented", "--pointers", "Gap1=src/impl.py:1")
    assert result.exit_code == 0, result.output
    assert "validation passed" in result.output
    scratch_cmds = [c for c in scratch_calls if "scratch" in c and "put" in c]
    assert scratch_cmds, f"expected an 'nx scratch put' call; got calls: {scratch_calls}"
    assert any("rdr-close-active" in str(c) for c in scratch_cmds), (
        f"expected the rdr-close-active tag in the scratch put call; got: {scratch_cmds}"
    )


_GAP_BODY = "## Problem Statement\n\n#### Gap 1: g\n\n## X\n"
_TWO_RDRS = [
    ("rdr-042-x.md", {"title": "X", "status": "accepted"}, _GAP_BODY),
    ("rdr-069-c.md", {"title": "C", "status": "accepted"}, _GAP_BODY),
]
_LONG_REASON = "critic false positive - gap addressed at src/foo.py:42"

#: nexus-my04w (intrastate [26115] #7 HIGH, #11): the preamble joined its argv
#: into one string and re-scanned it with regexes, so a multi-word
#: ``--force-implemented`` reason kept one word and the rest fell where the
#: first digits won the RDR lookup: the skill's own example reason
#: (``... src/foo.py:42``) closed rdr-042.
_CLOSE_REASON_CASES = [
    {"name": "multi_word_reason_is_kept_whole",
     "argv": ["--", "069", "--reason", "implemented", "--force-implemented", _LONG_REASON],
     "has": [f"**Force Implemented (audit):** {_LONG_REASON}", "rdr-069-c.md"], "lacks": [], "exit0": True},
    # Flag before the id: ``:42`` in the reason must not close rdr-042.
    {"name": "digits_inside_the_reason_never_select_the_rdr",
     "argv": ["--", "--force-implemented", _LONG_REASON, "--reason", "reverted", "069"],
     "has": ["rdr-069-c.md"], "lacks": ["rdr-042-x.md"], "exit0": True},
    # The skill may hand the whole line over as one argv element.
    {"name": "one_shell_string_is_split_with_its_quotes",
     "argv": ["--", "069 --reason implemented --force-implemented 'gap addressed at src/foo.py:42'"],
     "has": ["**Force Implemented (audit):** gap addressed at src/foo.py:42", "rdr-069-c.md"],
     "lacks": [], "exit0": True},
    {"name": "unquoted_reason_words_run_to_the_next_flag",
     "argv": ["--", "069", "--force-implemented", "critic", "false", "positive", "--reason", "implemented"],
     "has": ["**Force Implemented (audit):** critic false positive"], "lacks": [], "exit0": False},
    {"name": "an_id_swallowed_by_an_unquoted_reason_is_named",
     "argv": ["--", "--force-implemented", "critic", "false", "positive", "069"],
     "has": ["reason ends in `069`"], "lacks": ["rdr-069-c.md", "rdr-042-x.md"], "exit0": False},
    {"name": "a_flag_is_never_taken_as_another_flags_value",
     "argv": ["--", "069", "--reason"], "has": ["--reason needs a value"], "lacks": [], "exit0": False},
]


@pytest.mark.parametrize("case", _table(_CLOSE_REASON_CASES))
def test_rdr_close_reason_and_id_parsing(rdr_env, case):
    _write_files(rdr_env, _TWO_RDRS)
    res = _preamble("rdr-close", *case["argv"])
    if case["exit0"]:
        assert res.exit_code == 0, f"[{case['name']}] {res.output}"
    _assert_output(case["name"], res.output, has=case["has"], lacks=case["lacks"])


def test_rdr_close_parse_args_never_takes_a_flag_as_another_flags_value():
    from nexus.commands.rdr import _rdr_close_parse_args  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    parsed = _rdr_close_parse_args(("069", "--reason", "--pointers", "Gap1=src/foo.py:42"))
    assert parsed.reason is None and parsed.pointers == "Gap1=src/foo.py:42"
    assert parsed.missing_value == ("--reason",)
    parsed = _rdr_close_parse_args(("069", "--pointers", "--force"))
    assert parsed.pointers is None and parsed.force is True


#: (case, --pointers value, create src/foo.py first, validation passes, output names Gap1)
_CLOSE_POINTER_CASES = [
    ("empty_file_part_is_refused", "Gap1=:12", False, False, True),
    ("a_directory_is_refused", "Gap1=docs:1", False, False, True),
    ("absolute_path_outside_the_repo_is_refused", "Gap1=/etc/hosts:1", False, False, False),
    ("relative_path_escaping_the_repo_is_refused", "Gap1=../../../../../../etc/hosts:1", False, False, False),
    ("a_real_file_still_passes", "Gap1=src/foo.py:1", True, True, False),
]


@pytest.mark.parametrize("case, pointer, make_file, passes, names_gap", _CLOSE_POINTER_CASES,
                         ids=[c[0] for c in _CLOSE_POINTER_CASES])
def test_rdr_close_pointer_validation(rdr_env, case, pointer, make_file, passes, names_gap):
    _write_files(rdr_env, _TWO_RDRS)
    if make_file:
        (rdr_env["repo_root"] / "src").mkdir()
        (rdr_env["repo_root"] / "src" / "foo.py").write_text("x = 1\n")
    res = _preamble("rdr-close", "--", "069", "--reason", "implemented", "--pointers", pointer)
    assert ("validation passed" in res.output) is passes, f"[{case}] {res.output}"
    if names_gap:
        assert "Gap1" in res.output, f"[{case}] {res.output}"


def test_blank_status_key_is_an_empty_status_not_a_crash(rdr_env, monkeypatch):
    """nexus-u1jxt.10: ``status:`` with no value made rdr-accept (no id) and
    rdr-close die with AttributeError on ``None.lower()``."""
    _write_rdr(rdr_env["rdr_dir"], "rdr-300-x.md", {"title": "X", "status": "", "type": "feature"})
    _use_t2(monkeypatch, {})
    for argv in (["rdr-accept"], ["rdr-close", "--", "300"]):
        result = _preamble(*argv)
        assert result.exit_code == 0, (argv, result.output, result.exception)


# ---------------------------------------------------------------------------
# rdr-research
# ---------------------------------------------------------------------------


def _fake_memory_list(monkeypatch, rows: str) -> None:
    """Make `nx memory list` return ``rows`` and every other shell-out fail."""
    import nexus.commands.rdr as rdr_mod

    def _fake_run(cmd, *a, **k):
        if cmd[:3] == ["nx", "memory", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=rows, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unavailable")

    monkeypatch.setattr(rdr_mod, "run_bounded", _fake_run)


_RESEARCH_CONTEXT_CASES = [
    {"name": "with_id_prints_the_rdr_header", "files": [("rdr-001-hello-world.md", _HELLO,
                                                          "## Research Findings\n\n- Finding A\n- Finding B")],
     "rows": None, "argv": ["--", "1"], "has": ["### RDR 1:", "Hello World", "Research Findings"], "lacks": []},
    # `add <id>` with no finding text still prints RDR context rather than
    # attempting a T2 write.
    {"name": "add_word_with_only_an_id_falls_through_to_context",
     "files": [("rdr-001-hello-world.md", _HELLO, "")], "rows": None,
     "argv": ["--", "add", "1"], "has": ["RDR 1"], "lacks": []},
    # Regression (2026-07-22, caught on RDR-188): `nx memory list` rows are
    # "[id] <project>/<title>  (...)", and the old ^-anchored title regex
    # matched NOTHING, so every preamble reported "No research findings
    # recorded" while T2 held them.
    {"name": "t2_rows_match_despite_the_listing_prefix",
     "files": [("rdr-001-hello-world.md", _HELLO, "")],
     "rows": ("[21044] nexus_rdr/1-research-1: canned finding  (-, 2026-07-22T00:00:00Z)\n"
              "[21042] nexus_rdr/1  (-, 2026-07-22T00:00:00Z)\n"),
     "argv": ["--", "1"], "has": ["1-research-1: canned finding"], "lacks": ["No research findings recorded"]},
    # Intrastate [26115] #6 / probe P9 (nexus-nc08w.2): the canonical title is
    # ``%03d-research-N`` and the listing filtered on the unpadded key.
    {"name": "listing_finds_zero_padded_titles_below_100",
     "files": [("rdr-097-z.md", {"title": "Z", "status": "draft"}, "## Research Findings\n\nx\n")],
     "rows": ("[1] fakerepo_rdr/097-research-1  (rdr,research)\n"
              "[2] fakerepo_rdr/197-research-1  (rdr,research)\n"),
     "argv": ["--", "97"], "has": ["097-research-1"],
     "lacks": ["197-research-1", "No research findings recorded"]},
]


@pytest.mark.parametrize("case", _table(_RESEARCH_CONTEXT_CASES))
def test_rdr_research_context(rdr_env, monkeypatch, case):
    _write_files(rdr_env, case["files"])
    if case["rows"] is not None:
        _fake_memory_list(monkeypatch, case["rows"])
    result = _preamble("rdr-research", *case["argv"])
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"])


#: nexus-zu1q0: the next sequence number is derived from existing
#: ``<id>-research-*`` titles, and an add never silently upserts over one.
_RESEARCH_ADD_CASES = [
    {"name": "seq_1_when_no_prior_findings", "entries": {}, "hidden": frozenset(),
     "args": ["add", "201", "first", "finding"], "out_has": ["201-research-1"],
     "put_titles": ["201-research-1"], "stored_has": {"201-research-1": "first finding"}, "absent": []},
    # Live titles below 100 are zero-padded; an unpadded `97` must join that
    # sequence, never fork a bare `97-research-1` namespace beside it.
    {"name": "unpadded_id_joins_zero_padded_legacy_titles",
     "entries": {"097-research-9": "finding: ninth\n"}, "hidden": frozenset(),
     "args": ["add", "97", "tenth", "finding"], "out_has": ["097-research-10"], "put_titles": None,
     "stored_has": {"097-research-10": "tenth finding"}, "absent": ["97-research-1"]},
    {"name": "advances_past_an_existing_seq", "entries": {"201-research-1": "finding: first\n"},
     "hidden": frozenset(), "args": ["add", "201", "second", "finding"], "out_has": ["201-research-2"],
     "put_titles": None, "stored_has": {"201-research-1": "first", "201-research-2": "second finding"},
     "absent": []},
    # A stale seq-scan that misses a concurrently-created seq-2 must not upsert
    # over it: the command advances to seq 3.
    {"name": "never_overwrites_a_title_the_scan_missed",
     "entries": {"201-research-1": "finding: first\n", "201-research-2": "finding: concurrent\n"},
     "hidden": frozenset({"201-research-2"}), "args": ["add", "201", "third", "finding"],
     "out_has": ["201-research-3"], "put_titles": None,
     "stored_has": {"201-research-2": "concurrent", "201-research-3": "third finding"}, "absent": []},
    {"name": "accepts_an_rdr_prefixed_id_token", "entries": {}, "hidden": frozenset(),
     "args": ["add", "RDR-97", "some", "finding"], "out_has": [], "put_titles": ["097-research-1"],
     "stored_has": {}, "absent": []},
]


@pytest.mark.parametrize("case", _table(_RESEARCH_ADD_CASES))
def test_rdr_research_add(monkeypatch, case):
    name = case["name"]
    fake = _use_t2(monkeypatch, case["entries"], hidden=case["hidden"])
    result = _preamble("rdr-research", "--", *case["args"])
    assert result.exit_code == 0, f"[{name}] {result.output}"
    _assert_output(name, result.output, has=case["out_has"])
    if case["put_titles"] is not None:
        assert [t for t, _ in fake.put_calls] == case["put_titles"], f"[{name}] {result.output}"
    for title, text in case["stored_has"].items():
        assert text in fake._store[title], f"[{name}] {title} lacks {text!r}: {fake._store}"
    for title in case["absent"]:
        assert title not in fake._store, f"[{name}] unexpected {title} in {sorted(fake._store)}"


def test_two_consecutive_research_adds_never_collide(monkeypatch):
    """The exact repro shape from nexus-zu1q0: two back-to-back `add` calls
    against the same RDR each claim a distinct sequence number, sharing one
    client across both invocations (as two consecutive CLI calls would)."""
    fake = _use_t2(monkeypatch, {})
    r1 = _preamble("rdr-research", "--", "add", "201", "finding", "one")
    r2 = _preamble("rdr-research", "--", "add", "201", "finding", "two")
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output
    assert "201-research-1" in r1.output
    assert "201-research-2" in r2.output
    assert "finding one" in fake._store["201-research-1"]
    assert "finding two" in fake._store["201-research-2"]


def test_next_seq_sees_titles_with_a_summary_suffix():
    """Live T2 titles read "204-research-16: <summary>"; the scan must count
    them, or the next add overwrites nothing and restarts at 1."""
    from nexus.commands.rdr import _rdr_research_next_seq

    rows = [{"title": "204-research-16: the Key Discoveries bullet"}, {"title": "204-research-3"}]
    assert _rdr_research_next_seq(rows, "204") == 17


# ---------------------------------------------------------------------------
# rdr-audit
# ---------------------------------------------------------------------------

_AUDIT_MODE_CASES = [
    ("default_mode", ["rdr-audit"], ["**Mode:** audit dispatch", "Target project:"]),
    ("list_subcommand", ["rdr-audit", "--", "list"], ["management subcommand", "list", "read-only"]),
    ("explicit_project", ["rdr-audit", "--", "myproject"], ["myproject"]),
]


@pytest.mark.parametrize("case, argv, has", _AUDIT_MODE_CASES, ids=[c[0] for c in _AUDIT_MODE_CASES])
def test_rdr_audit_modes(rdr_env, case, argv, has):
    result = _preamble(*argv)
    assert result.exit_code == 0, f"[{case}] {result.output}"
    _assert_output(case, result.output, has=has)


@pytest.mark.parametrize("env_root_holds_a_decoy", [False, True],
                         ids=["scans_the_checkout_it_runs_in", "current_checkout_beats_a_valid_env_root"])
def test_rdr_audit_target_is_the_current_checkout(rdr_env, monkeypatch, env_root_holds_a_decoy):
    """nexus-u1jxt.6: from a repo outside the home candidate roots the audit
    scanned nothing and read clean; the checkout the command runs in is the
    target when the names agree, even over a NEXUS_PROJECT_ROOTS root holding a
    directory named like the target (the precedence flipped twice across two
    commits with nothing holding it)."""
    root, name = rdr_env["repo_root"], rdr_env["repo_root"].name
    if env_root_holds_a_decoy:
        elsewhere = root.parent / "elsewhere"
        (elsewhere / name / "docs" / "rdr").mkdir(parents=True)
        _write_rdr(elsewhere / name / "docs" / "rdr", "rdr-205-y.md",
                   {"title": "Y", "status": "bogus-elsewhere", "type": "feature"})
        _write_rdr(rdr_env["rdr_dir"], "rdr-204-x.md", {"title": "X", "status": "bogus-here", "type": "feature"})
        monkeypatch.setenv("NEXUS_PROJECT_ROOTS", str(elsewhere))
        out = _preamble("rdr-audit", "--", name).output
        _assert_output("current_checkout_beats_a_valid_env_root", out,
                       has=[f"**Worktree found:** `{root}` (via the current checkout)", "bogus-here"],
                       lacks=["bogus-elsewhere"])
    else:
        _write_rdr(rdr_env["rdr_dir"], "rdr-204-x.md", {"title": "X", "status": "bogus-status", "type": "feature"})
        monkeypatch.setenv("NEXUS_PROJECT_ROOTS", str(root / "nowhere"))
        out = _preamble("rdr-audit", "--", name).output
        _assert_output("scans_the_checkout_it_runs_in", out,
                       has=["**Worktree found:**", str(root), "bogus-status"])


_HEALTH_CASES = [
    # nexus-zbdm0 (G2): the bound ships with its counter-metric.
    {"name": "block_lists_gated_rdrs",
     "entries": {
         "204-gate-latest": (
             "outcome: \"PASSED\"\ncritical_count: 0\nsignificant_count: 1\n"
             "residuals:\n  - Finalization Gate count is stale; disposition at accept: pointer, no number\n"
             "  - a second residual\n"
             "prior: [9] (BLOCKED 3C), [8] (PASSED 0C 2S), [7] (BLOCKED 1C 2S), [6] (PASSED 0C 3S), [5] (1C)\n"
         ),
         "150-gate-latest": "outcome: \"PASSED\"\ncritical_count: 0\n",
         "150": "status: accepted\n",
     },
     "has": ["### Gate loop health", "RDR-204: 6 rounds", "Criticals per round: 1, 0, 1, 0, 3, 0",
             "residuals: 2", "cap did not end the loop", "RDR-150: 1 round"]},
    # The doctrine's signature: rounds per RDR fell AND findings per round fell
    # since the cap shipped.
    {"name": "bound_test_flags_both_falling",
     "entries": {
         "100-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-08-01\"\ncritical_count: 0\nprior: [1] (BLOCKED 3C), [2] (BLOCKED 2C)\n",
         "101-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-08-02\"\ncritical_count: 1\nprior: [3] (BLOCKED 2C)\n",
         "300-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-09-08\"\ncritical_count: 0\n",
     },
     "has": ["Bound test (RDRs gated before 2026-09-07: 2; since: 1)", "BOTH FELL"]},
    {"name": "bound_test_needs_both_sides",
     "entries": {"300-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-09-08\"\ncritical_count: 0\n"},
     "has": ["not yet measurable (0 RDRs gated before"]},
    {"name": "round_count_prefers_critique_records",
     "entries": {"150-gate-latest": "outcome: \"PASSED\"\ncritical_count: 0\n",
                 **{f"150-gate-critique-2026-09-0{i}": f"round {i}" for i in range(1, 5)}},
     "has": ["RDR-150: 4 rounds", "cap did not end the loop"]},
    {"name": "unreachable_t2_is_named_not_silent", "entries": None,
     "has": ["Gate loop health: T2 unreachable"]},
]


@pytest.mark.parametrize("case", _table(_HEALTH_CASES))
def test_rdr_audit_gate_loop_health(rdr_env, monkeypatch, case):
    if case["entries"] is None:
        _use_t2(monkeypatch, client=_Boom())
    else:
        _use_t2(monkeypatch, case["entries"])
    result = _preamble("rdr-audit")
    if case["name"] == "block_lists_gated_rdrs":
        assert result.exit_code == 0, result.output
    _assert_output(case["name"], result.output, has=case["has"])


@pytest.mark.parametrize("text, expected", [
    ("residuals:\n  - one; with a semicolon\ncommit: abc\n", 1),
    ("residuals: inline one\n", 1),
    ("outcome: PASSED\n", 0),
], ids=["bullet_with_a_semicolon", "inline_value", "no_residuals_field"])
def test_residual_count_is_by_bullet_not_punctuation(text, expected):
    from nexus.commands.rdr import _residual_count

    assert _residual_count(text) == expected


# ---------------------------------------------------------------------------
# rdr-gate re-gate block (nexus-7vdf9): after a prior gate, the preamble leads
# with the prior critique's findings and the survivor-sweep instruction; a
# first gate prints nothing extra; an unreachable T2 is a named note.
# ---------------------------------------------------------------------------

_REGATE_BODY = (
    "## Problem Statement\n\n#### Gap 1: a gap\nText.\n\n"
    "## Proposed Solution\n\nSix parse sites.\n"
)
_PRIOR_FINDINGS = "Prior findings (each must be closed EVERYWHERE"
_RECORDED = "Recorded residuals (dispositioned at accept; not survivors"


def _write_regate_rdr(rdr_env) -> None:
    _write_rdr(rdr_env["rdr_dir"], "rdr-204-example.md", _ACCEPT_FM, body=_REGATE_BODY)


def _prior_section(out: str) -> str:
    """The active-sweep list: from 'Prior findings' to Layer 0."""
    return out[out.index(_PRIOR_FINDINGS):out.index("Layer 0")]


_REGATE_STATE_CASES = [
    {"name": "blocked_prior_gate_prints_findings_and_layer_zero",
     "entries": {
         "204-gate-latest": (
             "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncritical_count: 2\n"
             "summary: \"two survivors\"\ncritique: nexus_rdr/204-gate-critique-2026-09-07c [24809]\n"
         ),
         "204-gate-critique-2026-09-07c": (
             "# Critique\n\n**Critical 1**: ghost sweep omits topic_assignments.\n"
             "- Significant: Phase 1 item 4 still says nx config set reminds the user.\n"
             "NEW CRITICAL\n\nIssue: the sweep is narrower than collectionIsEmpty.\n"
             "Observation: fine.\n"
         ),
     },
     "has": ["Re-gate: the previous gate was BLOCKED", "204-gate-critique-2026-09-07c",
             "ghost sweep omits topic_assignments", "nx config set reminds the user", "NEW CRITICAL",
             "narrower than collectionIsEmpty", "Prior findings", "Layer 0 (survivor sweep"],
     "lacks": ["Observation: fine"],  # observations are not survivors to sweep
     "ordered": ["Re-gate:", "Section Structure"]},
    {"name": "absent_prior_gate_prints_nothing_extra", "entries": {}, "has": [],
     "lacks": ["Re-gate", "Fix check"], "ordered": []},
    # nexus-g7zgw.4: Layer 0 fires after a PASSED gate too. Both RDR-204 rounds
    # that introduced new Criticals were fixes authored against a PASSED
    # gate's Significants, with the sweep structurally off.
    {"name": "passed_prior_gate_still_prints_the_block",
     "entries": {
         "204-gate-latest": (
             "outcome: \"PASSED\"\ndate: \"2026-09-07\"\nsignificant_count: 2\n"
             "critique: nexus_rdr/204-gate-critique-2026-09-07f\n"
         ),
         "204-gate-critique-2026-09-07f": "- Significant: the walk sentence overstates.\n",
     },
     "has": ["Re-gate: the previous gate was PASSED", "the walk sentence overstates",
             "Layer 0 (survivor sweep"], "lacks": [], "ordered": []},
    {"name": "missing_critique_pointer_says_so",
     "entries": {"204-gate-latest": "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\n"},
     "has": ["No `critique:` pointer", "Layer 0 (survivor sweep"], "lacks": [], "ordered": []},
    # A pointer to a record that does not exist must say so, never "loaded but
    # nothing recognised" (critique [24815] Significant 1).
    {"name": "missing_critique_record_is_named_not_mislabelled",
     "entries": {"204-gate-latest": (
         "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncritique: nexus_rdr/204-gate-critique-missing\n")},
     "has": ["no such T2 record was found"], "lacks": ["nothing recognised"], "ordered": []},
    {"name": "bad_gated_commit_is_reported_not_rendered_as_no_changes",
     "entries": {"204-gate-latest": "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncommit: deadbeef0\n"},
     "has": ["Changed since the gated commit `deadbeef0`: unknown"],
     "lacks": ["no changes to the RDR file"], "ordered": []},
    # Regression pin (round-1/round-2 path, nexus-yjf5l.3): a gate record with
    # no `residuals:` field prints the same Prior-findings and Layer 0 text as
    # before the residuals bead.
    {"name": "no_residuals_regate_output_is_unchanged",
     "entries": {
         "204-gate-latest": (
             "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\n"
             "critique: nexus_rdr/204-gate-critique-2026-09-07c\n"
         ),
         "204-gate-critique-2026-09-07c": (
             "## Critical Issues\n\n### Issue: ghost sweep omits topic_assignments\n- **Location**: L1\n"
         ),
     },
     "has": ["Prior findings (each must be closed EVERYWHERE in the file, not at the quoted line):",
             "**Layer 0 (survivor sweep, before Layer 3):** for every prior finding, sweep every "],
     "lacks": ["Recorded residuals", "no matching finding in the critique", "that is not a recorded residual"],
     "ordered": []},
    # The Sites: line is what Layer 0 sweeps.
    {"name": "blocked_gate_carries_the_critics_sites_lines",
     "entries": {
         "204-gate-latest": (
             "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncommit: {sha}\n"
             "critique: nexus_rdr/204-gate-critique-2026-09-07h\n"
         ),
         "204-gate-critique-2026-09-07h": (
             "## Critical Issues\n\n### Issue: model_version never parsed\n"
             "- **Location**: L471\n- **Sites**: L471, L254-257, L612\n"
             "- **Recommendation**: cite indexer.py:790\n"
         ),
     },
     "has": ["Sites: L471, L254-257, L612"], "lacks": [], "ordered": []},
]


@pytest.mark.parametrize("case", _table(_REGATE_STATE_CASES))
def test_regate_block_by_prior_gate_state(rdr_env, monkeypatch, case):
    sha = _git_commit_rdr(rdr_env, _REGATE_BODY, "gated")
    entries = {k: v.replace("{sha}", sha) for k, v in case["entries"].items()}
    _use_t2(monkeypatch, entries)
    result = _preamble("rdr-gate", "--", "204")
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"], ordered=case["ordered"])


def test_regate_with_an_unreachable_t2_is_a_named_note(rdr_env, monkeypatch):
    _write_regate_rdr(rdr_env)
    _use_t2(monkeypatch, client=_Boom())
    result = _preamble("rdr-gate", "--", "204")
    assert result.exit_code == 0, result.output
    assert "T2 unreachable" in result.output and "engine down" in result.output
    assert "Section Structure" in result.output, "the rest of the preamble still prints"


_UNUSED_CRITIQUE = (
    "## Critical Issues\n\n### Issue: query timeout doubles under load\n- **Location**: L100\n\n"
    "## Significant Issues\n\n### Issue: unused variable in the fallback branch\n- **Location**: L200\n"
)

#: nexus-yjf5l.3 / .7 / .15: a finding recorded on the prior round's
#: `residuals:` lines was dispositioned at accept, so it is not a survivor to
#: re-sweep. It prints under its own heading, is excluded from the "Prior
#: findings" list, and Layer 0 names the exemption. Plain, class-tagged and
#: free-form-critique shapes all take the same exemption.
_RECORDED_RESIDUAL_CASES = [
    {"name": "plain_residual_line",
     "residual": "unused variable in the fallback branch", "critique": _UNUSED_CRITIQUE,
     "blocker": "query timeout doubles under load"},
    {"name": "classed_residual_line",
     "residual": "[DISCOVER-AT-IMPLEMENTATION] unused variable in the fallback branch",
     "critique": _UNUSED_CRITIQUE, "blocker": "query timeout doubles under load"},
    {"name": "free_form_critique_shape",
     "residual": "unused variable in the fallback branch",
     "critique": ("CRITICAL — query timeout doubles under load\nShip-blocker: yes\n\n"
                  "SIGNIFICANT — unused variable in the fallback branch\nShip-blocker: no\n"),
     "blocker": "query timeout doubles under load"},
]


@pytest.mark.parametrize("case", _table(_RECORDED_RESIDUAL_CASES))
def test_recorded_residual_is_exempt_from_the_survivor_sweep(rdr_env, monkeypatch, case):
    name = case["name"]
    _write_regate_rdr(rdr_env)
    _use_t2(monkeypatch, {
        "204-gate-latest": (
            "outcome: \"PASSED\"\ndate: \"2026-09-09\"\n"
            "critique: nexus_rdr/204-gate-critique-2026-09-09z\n"
            f"residuals:\n  - {case['residual']}\n"
        ),
        "204-gate-critique-2026-09-09z": case["critique"],
    })
    result = _preamble("rdr-gate", "--", "204")
    assert result.exit_code == 0, f"[{name}] {result.output}"
    out = result.output
    title = "unused variable in the fallback branch"
    _assert_output(name, out, has=[_RECORDED, "that is not a recorded residual"],
                   ordered=[_RECORDED, title, _PRIOR_FINDINGS, case["blocker"]])
    assert title not in _prior_section(out), (
        f"[{name}] a recorded residual must not also appear in the survivor sweep list:\n{out}"
    )


_UNMATCHED_RESIDUAL_CASES = [
    # A recorded residual matching no finding is named under its own line,
    # never silently dropped.
    ("named_not_dropped", "a residual the critique no longer states",
     "## Significant Issues\n\n### Issue: unrelated finding\n- **Location**: L1\n",
     "Recorded residual, no matching finding in the critique: a residual the critique no longer states"),
    # nexus-yjf5l.14: even under the digit-stripped loose key, the survivor
    # sharing the most loose-key tokens is named as a hint for a human.
    ("names_the_nearest_finding_by_loose_key",
     "gate timeout retries 3 times before it eventually times out",
     "## Significant Issues\n\n### Issue: gate timeout retries 5 times before it always times out\n"
     "- **Location**: L1\n",
     "Recorded residual, no matching finding in the critique "
     "(nearest by title: Issue: gate timeout retries 5 times before it always times out): "
     "gate timeout retries 3 times before it eventually times out"),
]


@pytest.mark.parametrize("case, residual, critique, expected", _UNMATCHED_RESIDUAL_CASES,
                         ids=[c[0] for c in _UNMATCHED_RESIDUAL_CASES])
def test_unmatched_residual_is_named(rdr_env, monkeypatch, case, residual, critique, expected):
    _write_regate_rdr(rdr_env)
    _use_t2(monkeypatch, {
        "204-gate-latest": (
            "outcome: \"PASSED\"\ndate: \"2026-09-09\"\n"
            "critique: nexus_rdr/204-gate-critique-2026-09-09y\n"
            f"residuals:\n  - {residual}\n"
        ),
        "204-gate-critique-2026-09-09y": critique,
    })
    _assert_output(case, _preamble("rdr-gate", "--", "204").output, has=[expected])


_GHOST = "## Critical Issues\n\n### Issue: ghost sweep omits topic_assignments\n- **Location**: L1\n"
_STALE = "## Significant Issues\n\n### Issue: phase 1 item now stale\n- **Location**: L2\n"
_GATE_09 = ("outcome: \"BLOCKED\"\ndate: \"2026-09-09\"\n"
            "critique: nexus_rdr/204-gate-critique-2026-09-09\n")

#: nexus-yjf5l.11 / follow-on review F1: a finding absent from the last two
#: rounds' own critiques retires (printed under its own count-and-titles
#: line), unless a later critique re-raised it; retirement uses the same
#: strict-then-loose title rule the survivors/recorded split applies, and a
#: recorded residual is never retired.
_RETIREMENT_CASES = [
    {"name": "finding_absent_two_rounds_running_retires",
     "entries": {
         "204-gate-latest": _GATE_09,
         "204-gate-critique-2026-09-07": _GHOST,
         "204-gate-critique-2026-09-08": _STALE,
         "204-gate-critique-2026-09-09": ("## Significant Issues\n\n### Issue: new census gap\n"
                                          "- **Location**: L3\n"),
     },
     "has": ["Retired from the sweep (confirmed closed in the last two rounds): 1"], "lacks": [],
     "ordered": ["Retired from the sweep", "ghost sweep omits topic_assignments", _PRIOR_FINDINGS],
     "prior_has": ["phase 1 item now stale", "new census gap"],
     "prior_lacks": ["ghost sweep omits topic_assignments"]},
    {"name": "finding_re_raised_in_a_later_round_is_still_swept",
     "entries": {
         "204-gate-latest": _GATE_09,
         "204-gate-critique-2026-09-07": _GHOST,
         "204-gate-critique-2026-09-08": _STALE,
         "204-gate-critique-2026-09-09": _GHOST,
     },
     "has": [], "lacks": ["Retired from the sweep"], "ordered": [],
     "prior_has": ["ghost sweep omits topic_assignments", "phase 1 item now stale"], "prior_lacks": []},
    # A round-1 finding reworded by nothing but a digit when re-raised in round
    # 3 must never print as retired while the SAME issue also sweeps.
    {"name": "drifted_title_is_not_both_retired_and_swept",
     "entries": {
         "204-gate-latest": _GATE_09,
         "204-gate-critique-2026-09-07": ("## Critical Issues\n\n### Issue: fewer than 5 callers checked\n"
                                          "- **Location**: L1\n"),
         "204-gate-critique-2026-09-08": ("## Significant Issues\n\n### Issue: unrelated stale item\n"
                                          "- **Location**: L2\n"),
         "204-gate-critique-2026-09-09": ("## Critical Issues\n\n### Issue: fewer than 8 callers checked\n"
                                          "- **Location**: L1\n"),
     },
     "has": [], "lacks": ["Retired from the sweep"], "ordered": [],
     "prior_has": ["fewer than 8 callers checked"], "prior_lacks": []},
    # A recorded residual is exempt from the retired bucket regardless of age,
    # even when its stored title drifted (a digit changed) since it was raised.
    {"name": "drifted_recorded_residual_is_never_retired",
     "entries": {
         "204-gate-latest": _GATE_09 + "residuals:\n  - [DISCOVER-AT-IMPLEMENTATION] fewer than 8 callers checked\n",
         "204-gate-critique-2026-09-07": ("## Critical Issues\n\n### Issue: fewer than 5 callers checked\n"
                                          "- **Location**: L1\n"),
         "204-gate-critique-2026-09-08": ("## Significant Issues\n\n### Issue: unrelated stale item\n"
                                          "- **Location**: L2\n"),
         "204-gate-critique-2026-09-09": ("## Significant Issues\n\n### Issue: new census gap\n"
                                          "- **Location**: L3\n"),
     },
     "has": [], "lacks": ["Retired from the sweep"], "ordered": [], "prior_has": [], "prior_lacks": []},
]


@pytest.mark.parametrize("case", _table(_RETIREMENT_CASES))
def test_regate_retirement_from_the_sweep(rdr_env, monkeypatch, case):
    name = case["name"]
    _write_regate_rdr(rdr_env)
    _use_t2(monkeypatch, case["entries"])
    result = _preamble("rdr-gate", "--", "204")
    assert result.exit_code == 0, f"[{name}] {result.output}"
    out = result.output
    _assert_output(name, out, has=case["has"], lacks=case["lacks"], ordered=case["ordered"])
    if case["prior_has"] or case["prior_lacks"]:
        _assert_output(f"{name} (Prior findings section)", _prior_section(out),
                       has=case["prior_has"], lacks=case["prior_lacks"])


_FINDING_A = "## Critical Issues\n\n### Issue: finding A\n- **Location**: L1\n"
_FINDING_B = "## Significant Issues\n\n### Issue: finding B\n- **Location**: L2\n"
_FINDING_C = "## Significant Issues\n\n### Issue: finding C\n- **Location**: L3\n"

#: nexus-yjf5l.11 / nexus-u1jxt.3: when the gate record's critique history
#: implies an older critique that cannot be placed among the enumerated ones
#: (the nexus-zu1q0 race: the record's own critique is fetchable but hidden
#: from the get_all enumeration), a visible note says so; an ordinary round-2
#: gate, where the latest critique is the only one, prints no such note.
_SECOND_CRITIQUE_CASES = [
    {"name": "hidden_latest_critique_is_a_visible_note",
     "entries": {
         "204-gate-latest": ("outcome: \"BLOCKED\"\ndate: \"2026-09-09\"\n"
                             "critique: nexus_rdr/204-gate-critique-2026-09-09c\n"),
         "204-gate-critique-2026-09-07": _FINDING_A,
         "204-gate-critique-2026-09-08": _FINDING_B,
         "204-gate-critique-2026-09-09c": _FINDING_C,
     },
     "hidden": frozenset({"204-gate-critique-2026-09-09c"}),
     "has": ["**Second critique missing:**", "could not be loaded", "finding C"], "lacks": []},
    {"name": "ordinary_first_regate_prints_no_note",
     "entries": {
         "204-gate-latest": ("outcome: \"BLOCKED\"\ndate: \"2026-09-09\"\n"
                             "critique: nexus_rdr/204-gate-critique-2026-09-09\n"),
         "204-gate-critique-2026-09-09": _FINDING_A,
     },
     "hidden": frozenset(), "has": ["finding A"], "lacks": ["Second critique missing"]},
    # The round-2 boundary of the hidden-current-title path: `critique_count`
    # excludes the hidden title, so at round 2 it is 1, and the old `>= 2`
    # threshold silently narrowed the sweep to the latest round with no note.
    {"name": "hidden_latest_critique_at_round_2_is_a_visible_note",
     "entries": {
         "204-gate-latest": ("outcome: \"BLOCKED\"\ndate: \"2026-09-08\"\n"
                             "critique: nexus_rdr/204-gate-critique-2026-09-08b\n"),
         "204-gate-critique-2026-09-07": _FINDING_A,
         "204-gate-critique-2026-09-08b": _FINDING_B,
     },
     "hidden": frozenset({"204-gate-critique-2026-09-08b"}),
     "has": ["**Second critique missing:**", "could not be loaded", "finding B"], "lacks": []},
]


@pytest.mark.parametrize("case", _table(_SECOND_CRITIQUE_CASES))
def test_regate_second_critique_note(rdr_env, monkeypatch, case):
    _write_regate_rdr(rdr_env)
    _use_t2(monkeypatch, case["entries"], hidden=case["hidden"])
    result = _preamble("rdr-gate", "--", "204")
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"])


# ---------------------------------------------------------------------------
# Gate round number and the fix check (nexus-g7zgw.1 / .2)
# ---------------------------------------------------------------------------

_ROUND_BODY = "## Problem Statement\n\n#### Gap 1: a gap\nText.\n\n## Proposed Solution\n\nSix sites.\n"

#: (case, record text, substrings in the first round line, expected round number)
_ROUND_HELPER_CASES = [
    # Entries without an outcome word and chains wrapped over lines must still
    # count (deep critique [24873] Critical 1).
    ("bare_entries_count_as_unlabelled",
     'outcome: "BLOCKED"\nprior: [1], [2], [3], [4], [5], [6], [7], [8], [9]\n',
     ["Gate round 11", "9 unlabelled"], 11),
    ("wrapped_chain_counts",
     ('outcome: "PASSED"\nprior: [1] (BLOCKED 1C), [2] (PASSED 0C),\n'
      "  [3] (BLOCKED 2C), [4] (PASSED)\ncommit: abc1234\n"),
     ["Gate round 6", "2 BLOCKED, 3 PASSED, 0 unlabelled"], 6),
    # nexus-yjf5l.13: `_gate_round_number` counts ``[<digits>]`` entries and is
    # indifferent to whether the ids repeat (the real `205-gate-latest`
    # record's four entries all read `[25098]`, the upserted row's own id) or
    # are distinct (each round's own critique record id).
    ("repeated_prior_ids",
     ('outcome: "PASSED"\nprior: [25098] (BLOCKED 2C 7S), [25098] (BLOCKED 2C 7S), '
      "[25098] (BLOCKED 3C 11S), [25098] (BLOCKED 6C 11S)\n"), [], 6),
    ("distinct_prior_ids",
     ('outcome: "PASSED"\nprior: [25125] (BLOCKED 2C 7S), [25122] (BLOCKED 2C 7S), '
      "[25114] (BLOCKED 3C 11S), [25096] (BLOCKED 6C 11S)\n"), [], 6),
]


@pytest.mark.parametrize("case, text, line_has, number", _ROUND_HELPER_CASES,
                         ids=[c[0] for c in _ROUND_HELPER_CASES])
def test_gate_round_helpers(case, text, line_has, number):
    from nexus.commands.rdr import _gate_round_lines, _gate_round_number

    line = _gate_round_lines(text)[0]
    for s in line_has:
        assert s in line, f"[{case}] {s!r} not in {line!r}"
    assert _gate_round_number(text, 0) == number, case


def _gate_record(sha: str, *, outcome: str = "BLOCKED", prior: str | None = None, date: bool = True) -> str:
    out = f'outcome: "{outcome}"\n' + ('date: "2026-09-07"\n' if date else "") + f"commit: {sha}\n"
    return out + (f"prior: {prior}\n" if prior is not None else "")


_ROUND_PREAMBLE_CASES = [
    {"name": "round_counts_the_prior_chain", "argv": "rdr-gate",
     "record": lambda sha: _gate_record(
         sha, prior="[24847] (PASSED 0C 2S 2O), [24844] (BLOCKED 1C 2S 2O), [24841] (PASSED 0C 3S 4O)"),
     "extra": {}, "has": ["Gate round 5", "prior rounds: 4 (2 BLOCKED, 2 PASSED, 0 unlabelled"], "lacks": []},
    {"name": "round_without_a_prior_field_is_two", "argv": "rdr-gate",
     "record": lambda sha: _gate_record(sha), "extra": {},
     "has": ["Gate round 2"], "lacks": ["only a ship-blocker blocks"]},
    # A hand-retyped chain that lost entries cannot reset the cap: the critique
    # records T2 holds are the count nobody retypes.
    {"name": "round_prefers_the_critique_record_count", "argv": "rdr-gate",
     "record": lambda sha: _gate_record(sha),
     "extra": {f"204-gate-critique-2026-09-0{i}": f"round {i}" for i in range(1, 6)},
     "has": ["Gate round 6", "from the critique records"], "lacks": []},
    {"name": "round_three_gate_names_the_residual_rule", "argv": "rdr-gate",
     "record": lambda sha: _gate_record(sha, prior="[1] (BLOCKED 1C)"), "extra": {},
     "has": ["Gate round 3", "only a ship-blocker blocks"], "lacks": []},
    # nexus-yjf5l.2: the fix preamble reads the same gate record through the
    # same _gate_round_lines call, so its round-3+ rule is the identical text.
    {"name": "round_three_fix_preamble_names_the_residual_rule", "argv": "rdr-fix",
     "record": lambda sha: _gate_record(sha, prior="[1] (BLOCKED 1C)", date=False), "extra": {},
     "has": ["Gate round 3", "only a ship-blocker blocks"], "lacks": []},
]


@pytest.mark.parametrize("case", _table(_ROUND_PREAMBLE_CASES))
def test_gate_round_number_in_the_preamble(rdr_env, monkeypatch, case):
    sha = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _use_t2(monkeypatch, {"204-gate-latest": case["record"](sha), **case["extra"]})
    result = _preamble(case["argv"], "--", "204")
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"])


_FIX_CHECK_FLAG_CASES = [
    # Omitting `fix_check:` on a re-gate is a skipped check, never a clean one
    # (deep critique [24873] Critical 3).
    {"name": "regate_without_fix_check_field_is_flagged",
     "gate": 'outcome: "PASSED"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\n', "extra": {},
     "marker": "Fix check missing", "flagged": True, "has": []},
    {"name": "first_gate_without_fix_check_field_is_not_flagged",
     "gate": 'outcome: "PASSED"\ncommit: {sha}\n', "extra": {},
     "marker": "Fix check missing", "flagged": False, "has": []},
    {"name": "regate_with_a_none_fix_check_is_not_flagged",
     "gate": ('outcome: "PASSED"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\n'
              "fix_check: none (no change since {sha})\n"), "extra": {},
     "marker": "Fix check missing", "flagged": False, "has": []},
    {"name": "pointer_to_an_absent_record_is_flagged",
     "gate": ('outcome: "PASSED"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\n'
              "fix_check: nexus_rdr/204-fix-check-{sha}\n"), "extra": {},
     "marker": "Fix check record missing", "flagged": True, "has": []},
    {"name": "pointer_to_a_present_record_is_not_flagged",
     "gate": ('outcome: "PASSED"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\n'
              "fix_check: nexus_rdr/204-fix-check-{sha}\n"),
     "extra": {"204-fix-check-{sha}": "verdict: CLEAN\n"},
     "marker": "Fix check record missing", "flagged": False, "has": ["Fix check"]},
    # critique [24865] Critical 1: `fix_check:` must name `commit:`'s sha.
    {"name": "pointer_naming_another_sha_is_flagged",
     "gate": 'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: {sha}\nfix_check: nexus_rdr/204-fix-check-df91f4072 (CLEAN)\n',
     "extra": {}, "marker": "Fix check pointer mismatch", "flagged": True, "has": []},
    {"name": "pointer_naming_the_commit_sha_is_not_flagged",
     "gate": 'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: {sha}\nfix_check: nexus_rdr/204-fix-check-{sha}\n',
     "extra": {}, "marker": "Fix check pointer mismatch", "flagged": False, "has": []},
    {"name": "bare_sha_pointer_is_not_flagged",
     "gate": 'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: {sha}\nfix_check: {sha}\n',
     "extra": {}, "marker": "Fix check pointer mismatch", "flagged": False, "has": []},
    {"name": "unresolvable_gated_commit_is_reported",
     "gate": 'outcome: "PASSED"\ndate: "2026-09-07"\ncommit: deadbeef0\n', "extra": {},
     "marker": "Fix check: the gated commit `deadbeef0` does not resolve", "flagged": True, "has": []},
]


@pytest.mark.parametrize("case", _table(_FIX_CHECK_FLAG_CASES))
def test_fix_check_flags(rdr_env, monkeypatch, case):
    name = case["name"]
    sha = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    entries = {"204-gate-latest": case["gate"].replace("{sha}", sha)}
    entries.update({k.replace("{sha}", sha): v for k, v in case["extra"].items()})
    _use_t2(monkeypatch, entries)
    out = _preamble("rdr-gate", "--", "204").output
    assert (case["marker"] in out) is case["flagged"], f"[{name}] marker {case['marker']!r}:\n{out}"
    _assert_output(name, out, has=case["has"])


_DIFF_RANGE = "git diff {gated}..HEAD -- docs/rdr/rdr-204-example.md"

_FIX_CHECK_SCENARIOS = [
    {"name": "file_changed_names_the_diff_range", "after": "fix",
     "has": ["### Fix check (required before Layer 1)", _DIFF_RANGE, "fix", "{fixed}",
             "204-fix-check-{fixed}",  # the T2 title carries the tip sha
             "enumeration", "universal", "Do not enter Layer 1 or Layer 3",
             # nexus-yjf5l.5 (R2): every changed identifier's other
             # occurrences in the file, enumerated.
             "owning phase", "every other occurrence", "under any other name"],
     "lacks": []},
    {"name": "nothing_changed_needs_no_fix_check", "after": None,
     "has": ["Fix check: not required", "no change to the RDR file since `{gated}`"],
     "lacks": ["### Fix check (required"]},
    # Past the gate there is no re-gate to gate, but a residual dispositioned by
    # a change to the RDR file still carries a fix check on that change.
    {"name": "past_the_gate_names_the_dispositions_own_fix_check", "after": "accept",
     "has": ["past the gate", "`accepted`", "204-fix-check-<sha>", _DIFF_RANGE, "bead id", "needs none"],
     "lacks": ["### Fix check (required"]},
]


@pytest.mark.parametrize("case", _table(_FIX_CHECK_SCENARIOS))
def test_fix_check_section_by_git_state(rdr_env, monkeypatch, case):
    name = case["name"]
    gated = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    fixed = gated
    if case["after"] == "fix":
        fixed = _git_commit_rdr(rdr_env, _ROUND_BODY + "\nFive registration sites, not one.\n", "fix")
    elif case["after"] == "accept":
        _git_commit_rdr(rdr_env, _ROUND_BODY, "accept", status="accepted")
    _use_t2(monkeypatch, {"204-gate-latest": _gate_record(gated, outcome="PASSED")})
    out = _preamble("rdr-gate", "--", "204").output
    fmt = lambda xs: [x.format(gated=gated, fixed=fixed) for x in xs]  # noqa: E731
    _assert_output(name, out, has=fmt(case["has"]), lacks=fmt(case["lacks"]))


def test_fix_check_git_log_failure_never_yields_a_fake_sha(rdr_env):
    """code review [24866] Important 3: a failed `git log` must not print `HEAD`
    as the tip sha the T2 title is keyed on."""
    from nexus.commands.rdr import _fix_check_lines

    lines = _fix_check_lines(
        repo_root=str(rdr_env["repo_root"]), t2_key="204", rel="docs/rdr/nope.md",
        gated_commit="0000000", changed=True,
    )
    joined = "\n".join(lines)
    assert "fix-check-HEAD" not in joined
    assert "could not be read" in joined


# ---------------------------------------------------------------------------
# rdr-fix (nexus-zbdm0): the fix step's own surface
# ---------------------------------------------------------------------------

_FIX_CRITIQUE = (
    "## Critical Issues\n\n### Issue: model_version never parsed\n"
    "- **Location**: L471\n- **Sites**: L471, L254-257, L612\n"
    "- **Recommendation**: cite indexer.py:790\n\n## Observations\n- fine\n"
)

_FIX_STATE_CASES = [
    {"name": "no_gate_record_says_nothing_to_fix", "scenario": "draft", "entries": {}, "boom": False,
     "has": ["rdr-research"], "has_ci": ["no gate record"], "lacks": []},
    {"name": "prints_findings_sites_diff_research_title_and_rules", "scenario": "gated_fixed", "boom": False,
     "entries": {
         "204-gate-latest": ('outcome: "BLOCKED"\ndate: "2026-09-07"\ncommit: {gated}\n'
                             "critique: nexus_rdr/204-gate-critique-2026-09-07h\nprior: [1] (PASSED 0C 2S)\n"),
         "204-gate-critique-2026-09-07h": _FIX_CRITIQUE,
         "204-research-3": "finding: earlier\n",
     },
     "has": ["### Fix RDR-204", "Gate round 3", "model_version never parsed",
             "Sites: L471, L254-257, L612", "git diff {gated}..HEAD -- docs/rdr/rdr-204-example.md",
             "{fixed}", "fix one",
             "204-research-4",  # the next research seq is the pre-edit entry's title
             "nx rdr preamble rdr-research -- add 204", "nothing else", "inferred, not read", "census",
             "204-fix-check-{fixed}",
             # nexus-yjf5l.5 (R2): the serial precondition (fix, check, then
             # Layer 1 and Layer 3, never a parallel dispatch) is printed here.
             "dispatched against the same commit in parallel"],
     "has_ci": ["no fix-check record yet"], "lacks": ["- fine"]},
    {"name": "existing_fix_check_record_is_reported", "scenario": "gated_fixed", "boom": False,
     "entries": {"204-gate-latest": 'outcome: "BLOCKED"\ncommit: {gated}\n',
                 "204-fix-check-{fixed}": "verdict: CLEAN\n"},
     "has": ["Fix-check record `204-fix-check-{fixed}` exists"], "has_ci": [], "lacks": []},
    {"name": "past_the_gate_is_named", "scenario": "gated_accepted", "boom": False,
     "entries": {"204-gate-latest": 'outcome: "PASSED"\ncommit: {gated}\n'},
     "has": ["past the gate", "accepted", "204-fix-check-<sha>", "bead id", "needs none"], "has_ci": [],
     "lacks": ["#### Before the edit"]},
    {"name": "unreachable_t2_is_named", "scenario": "draft", "entries": {}, "boom": True,
     "has": ["T2 unreachable", "engine down"], "has_ci": [], "lacks": []},
]


@pytest.mark.parametrize("case", _table(_FIX_STATE_CASES))
def test_rdr_fix_by_gate_state(rdr_env, monkeypatch, case):
    name = case["name"]
    scenario = case["scenario"]
    if scenario == "draft":
        gated = fixed = _git_commit_rdr(rdr_env, _ROUND_BODY, "draft")
    else:
        gated = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
        fixed = gated
        if scenario == "gated_fixed":
            fixed = _git_commit_rdr(rdr_env, _ROUND_BODY + "\nFive sites.\n", "fix one")
        else:
            _git_commit_rdr(rdr_env, _ROUND_BODY, "accept", status="accepted")
    fmt = lambda s: s.format(gated=gated, fixed=fixed) if "{" in s else s  # noqa: E731
    entries = {fmt(k): fmt(v) for k, v in case["entries"].items()}
    _use_t2(monkeypatch, entries, client=_Boom() if case["boom"] else None)
    result = _preamble("rdr-fix", "--", "204")
    assert result.exit_code == 0, f"[{name}] {result.output}"
    _assert_output(name, result.output, has=[fmt(s) for s in case["has"]],
                   has_ci=case["has_ci"], lacks=case["lacks"])


_SHIP_BLOCKER = "query timeout doubles under load"
_RESIDUAL = "unused variable in the fallback branch"
_FIX_SHIP = "Ship-blockers (fix these)"
_FIX_RESID = "Residuals (record; do not fix in this change)"
_ROUND_FOUR_GATE = (
    'outcome: "BLOCKED"\ndate: "2026-09-09"\ncommit: {gated}\n'
    "critique: nexus_rdr/204-gate-critique-2026-09-09z\n"
    "prior: [1] (BLOCKED 1C), [2] (BLOCKED 1C)\n"
)

_FIX_SPLIT_CASES = [
    # nexus-yjf5l.2: from round 3 the fix preamble separates the findings that
    # block (`Ship-blocker: yes`) from the residuals the round recorded.
    {"name": "round_three_splits_ship_blockers_from_residuals",
     "gate": _ROUND_FOUR_GATE + f"residuals:\n  - {_RESIDUAL}\n",
     "critique": ("## Critical Issues\n\n### Issue: " + _SHIP_BLOCKER + "\n"
                  "- **Location**: L100\n- **Ship-blocker**: yes\n\n"
                  "## Significant Issues\n\n### Issue: " + _RESIDUAL + "\n"
                  "- **Location**: L200\n- **Ship-blocker**: no\n"),
     "ordered": [_FIX_SHIP, _SHIP_BLOCKER, _FIX_RESID, _RESIDUAL], "none_between": False},
    # nexus-yjf5l.15: the free-form 'CRITICAL — <title>' shape drives the same
    # split; before the fix _critique_findings returned [] for it.
    {"name": "free_form_critique_takes_the_same_split",
     "gate": _ROUND_FOUR_GATE + f"residuals:\n  - {_RESIDUAL}\n",
     "critique": (f"CRITICAL — {_SHIP_BLOCKER}\nShip-blocker: yes\n\n"
                  f"SIGNIFICANT — {_RESIDUAL}\nShip-blocker: no\n"),
     "ordered": [_FIX_SHIP, _SHIP_BLOCKER, _FIX_RESID, _RESIDUAL], "none_between": False},
    # nexus-u1jxt.9: rdr-fix matched residuals strict-key only while rdr-gate
    # matched strict-then-loose, so a residual whose title drifted by a digit
    # was a ship-blocker to fix here and a recorded residual there.
    {"name": "residual_that_drifted_by_a_digit_is_still_a_residual",
     "gate": _ROUND_FOUR_GATE
     + "residuals:\n  - the round 5 counter is off by one in the fallback branch\n",
     "critique": ("## Significant Issues\n\n### Issue: the round 6 counter is off by one in the fallback branch\n"
                  "- **Location**: L200\n- **Ship-blocker**: no\n"),
     "ordered": [_FIX_RESID, "the round 6 counter is off by one"], "none_between": True},
]


@pytest.mark.parametrize("case", _table(_FIX_SPLIT_CASES))
def test_rdr_fix_round_three_splits_ship_blockers_from_residuals(rdr_env, monkeypatch, case):
    name = case["name"]
    gated = _git_commit_rdr(rdr_env, _ROUND_BODY, "gated")
    _use_t2(monkeypatch, {
        "204-gate-latest": case["gate"].format(gated=gated),
        "204-gate-critique-2026-09-09z": case["critique"],
    })
    out = _preamble("rdr-fix", "--", "204").output
    _assert_output(name, out, has=["Gate round 4", _FIX_SHIP, _FIX_RESID], ordered=case["ordered"])
    if case["none_between"]:
        assert "(none" in out[out.index(_FIX_SHIP):out.index(_FIX_RESID)], (
            f"[{name}] the ship-blocker list must read (none ...):\n{out}"
        )


# ---------------------------------------------------------------------------
# phase-review-gate
# ---------------------------------------------------------------------------

_ACCEPTED_P0 = {"title": "Command Preambles", "status": "accepted", "type": "decision", "priority": "P0"}
_ACCEPTED_ARCH = {"title": "Storage Substrate Split", "status": "accepted", "type": "architecture", "priority": "P1"}


def _approach_body(*items: str, heading: str = "### Approach") -> str:
    return ("## Problem Statement\n\nProblem.\n\n" + f"{heading}\n\n" + "".join(items)
            + "\n## Tradeoffs\n\nSome tradeoffs.")


_TWO_ITEMS = ("1. **T2 read**: Read from T2 database.\n"
              "2. **File fallback**: Fall back to .md files.\n")
_THREE_ITEMS = _TWO_ITEMS + "3. **CLI output**: Print markdown table.\n"
_PHASE_BLOCK_BODY = (  # nexus-4u6mt: RDR-120-style phase blocks with sub-bullets
    "## Problem Statement\n\nProblem.\n\n"
    "### Approach\n\n"
    "**Phase 0: Lint + cutover flag scaffolding**\n\n"
    "- Implement nx doctor --check-storage-boundary\n"
    "- Add NX_STORAGE_MODE env-var\n\n"
    "**Phase 1: T3 daemon**\n\n"
    "- Stand up the T3 daemon process\n"
    "- Route T3 reads through T3Client\n"
    "- Add storage_boundary_lint T3 enforcement\n\n"
    "## Tradeoffs\n\nSome tradeoffs."
)
_IMPLEMENTATION_PLAN_BODY = (  # nexus-2pw1x: conexus RDR-001's layout
    "## Problem Statement\n\nProblem.\n\n"
    "## Implementation Plan\n\n"
    "1. **Schema slice**: Add the retention column.\n"
    "2. **ETL passthrough**: Relax the null-doc skip.\n\n"
    "## Tradeoffs\n\nSome tradeoffs."
)
_GATE_1 = ["phase-review-gate", "--"]

_PRG_CASES = [
    {"name": "no_approach_section_errors", "file": "rdr-001-hello-world.md",
     "fm": {**_HELLO, "status": "accepted"},
     "body": "## Problem Statement\n\nProblem.\n\n## Proposed Solution\n\nSolution.",
     "argv": ["1", "--phase", "1"], "has": ["ERROR", "Approach"], "lacks": []},
    {"name": "pass1_enumerates_items", "file": "rdr-130-command-preambles.md", "fm": _ACCEPTED_P0,
     "body": _approach_body(_THREE_ITEMS), "argv": ["130", "--phase", "1"],
     "has": ["§Approach Cross-Walk", _ITEM_TABLE, "Item1", "T2 read", "Item2", "File fallback", "Item3"],
     "lacks": []},
    # GH #1443: a `1a.` item used to be absorbed into item 1 and the gate
    # enumerated 2 of 3 items; now it refuses with the offending line.
    {"name": "refuses_a_subset_when_an_item_start_fails_to_parse", "file": "rdr-130-command-preambles.md",
     "fm": _ACCEPTED_P0,
     "body": _approach_body("1. **T2 read**: Read from T2 database.\n",
                            "1a. **T2 lease**: added after drafting.\n",
                            "2. **File fallback**: Fall back to .md files.\n"),
     "argv": ["130", "--phase", "1", "--evidence", "Item1=nexus-aaaa,Item2=nexus-bbbb"],
     "has": ["ERROR", "GH #1443", "1a. **T2 lease**"], "lacks": ["CROSS-WALK PASSED", _ITEM_TABLE]},
    {"name": "pass2_all_covered_passes", "file": "rdr-130-command-preambles.md", "fm": _ACCEPTED_P0,
     "body": _approach_body(_TWO_ITEMS),
     "argv": ["130", "--phase", "1", "--evidence", "Item1=nexus-abc1,Item2=nexus-xyz2"],
     "has": ["APPROACH CROSS-WALK PASSED", "nexus-abc1", "nexus-xyz2"], "lacks": []},
    {"name": "pass2_missing_evidence_is_blocked", "file": "rdr-130-command-preambles.md", "fm": _ACCEPTED_P0,
     "body": _approach_body(_TWO_ITEMS),
     "argv": ["130", "--phase", "1", "--evidence", "Item1=nexus-abc1"],
     "has": ["BLOCKED", "Item2"], "lacks": []},
    # nexus-2fnet: an empty evidence value (`Item2=`) blocks too.
    {"name": "pass2_empty_evidence_value_is_blocked", "file": "rdr-130-command-preambles.md",
     "fm": _ACCEPTED_P0, "body": _approach_body(_TWO_ITEMS),
     "argv": ["130", "--phase", "1", "--evidence", "Item1=nexus-abc1,Item2="],
     "has": ["BLOCKED"], "lacks": []},
    {"name": "phase_block_enumerates_the_requested_phase_bullets",
     "file": "rdr-120-storage-substrate-split.md", "fm": _ACCEPTED_ARCH, "body": _PHASE_BLOCK_BODY,
     "argv": ["120", "--phase", "1"],
     "has": ["§Approach Cross-Walk", "Item1", "Item2", "Item3", "Phase 1: Stand up the T3 daemon process"],
     "lacks": ["Item4", "check-storage-boundary"]},  # Phase 0 bullets must not leak in
    {"name": "phase_block_phase0_enumerates_phase0_bullets",
     "file": "rdr-120-storage-substrate-split.md", "fm": _ACCEPTED_ARCH, "body": _PHASE_BLOCK_BODY,
     "argv": ["120", "--phase", "0"],
     "has": ["Item1", "Item2", "check-storage-boundary"], "lacks": ["Item3"]},
    # RDR-121/125-style numbered items keep enumerating phase-agnostically.
    {"name": "numbered_items_still_work_unchanged", "file": "rdr-125-routing-hook-plugin-ownership.md",
     "fm": {"title": "Routing Hook Ownership", "status": "accepted", "type": "architecture", "priority": "P1"},
     "body": ("## Problem Statement\n\nProblem.\n\n### Approach\n\n"
              "1. **Vendor the hook**: Copy _lib.py into sn.\n"
              "2. **Byte-equality CI guard**: Assert identical bytes.\n\n## Tradeoffs\n\nT."),
     "argv": ["125", "--phase", "1"],
     "has": ["Item1", "Vendor the hook", "Item2", "Byte-equality CI guard"], "lacks": []},
    # nexus-2pw1x: RDRs that structure phased work under '## Implementation Plan'.
    {"name": "implementation_plan_heading_pass1_enumerates", "file": "rdr-001-multitenant-cloud.md",
     "fm": {"title": "Multitenant Cloud", "status": "accepted", "type": "architecture", "priority": "P1"},
     "body": _IMPLEMENTATION_PLAN_BODY, "argv": ["1", "--phase", "1"],
     "has": ["§Approach Cross-Walk", "Item1", "Schema slice", "Item2", "ETL passthrough"], "lacks": ["ERROR"]},
    {"name": "implementation_plan_heading_pass2_validates_evidence", "file": "rdr-001-multitenant-cloud.md",
     "fm": {"title": "Multitenant Cloud", "status": "accepted", "type": "architecture", "priority": "P1"},
     "body": _IMPLEMENTATION_PLAN_BODY,
     "argv": ["1", "--phase", "1", "--evidence", "Item1=nexus-abc1,Item2=nexus-xyz2"],
     "has": ["APPROACH CROSS-WALK PASSED", "nexus-abc1", "nexus-xyz2"], "lacks": []},
    # nexus-moht0 non-vacuity: prose in both sections still refuses loudly
    # rather than silently reporting zero items as a pass.
    {"name": "neither_layout_reports_no_items_parsed", "file": "rdr-140-neither-layout.md",
     "fm": {"title": "Neither Layout", "status": "accepted", "type": "architecture", "priority": "P2"},
     "body": ("## Proposed Solution\n\n### Approach\n\n"
              "Just prose, no numbered items, no bold phase blocks.\n\n"
              "## Implementation Plan\n\nAlso just prose here. No Phase headings at all.\n"),
     "argv": ["140", "--phase", "1"], "has": ["no items parsed", "Implementation Plan"], "lacks": []},
    # The unparsed-item guard used to run only when numbered items parsed, so a
    # phase-block §Approach with a stray column-0 numbered line fell through
    # unguarded.
    {"name": "phase_block_structure_is_guarded_too", "file": "rdr-120-storage-substrate-split.md",
     "fm": {"title": "Storage substrate split", "status": "accepted", "type": "decision", "priority": "P1"},
     "body": ("## Problem Statement\n\nProblem.\n\n### Approach\n\n**Phase 1: Core**\n\n"
              "- **Daemon**: stand it up\n2. a numbered line the fallback would drop\n\n"
              "## Tradeoffs\n\nSome tradeoffs."),
     "argv": ["120", "--phase", "1"], "has": ["GH #1443"], "lacks": [_ITEM_TABLE]},
]


@pytest.mark.parametrize("case", _table(_PRG_CASES))
def test_phase_review_gate_passes(rdr_env, case):
    _write_rdr(rdr_env["rdr_dir"], case["file"], case["fm"], body=case["body"])
    result = _preamble(*_GATE_1, *case["argv"])
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"])


_REAL_RDR_DIR = Path(__file__).parent.parent / "docs" / "rdr"
_RDR_205 = "rdr-205-linda-tuple-space-over-postgres.md"
_ITEMS_1_TO_6 = [f"Item{i}" for i in range(1, 7)]

#: nexus-w5gma: the gate against REAL RDR files, not synthetic fixtures, so the
#: fix is proven against the exact documents that surfaced the bug.
_PRG_REAL_RDR_CASES = [
    # RDR-205: §Approach is prose, phases live under §Implementation Plan as
    # `### Phase N` / `#### Step N` headings; Phase 1 has six Step headings,
    # matching Sam's by-hand cross-walk (T2 nexus/phase1-close-nexus-em75s-2026-09-10).
    {"name": "rdr_205_phase_headings_pass1", "src": _RDR_205, "evidence": None,
     "has": ["§Approach Cross-Walk", *_ITEMS_1_TO_6, "Settle the pooler fact", "Changesets", "Registry",
             "Repository and handler", "Sweep", "Local spike"], "lacks": ["ERROR"]},
    {"name": "rdr_205_phase_headings_pass2", "src": _RDR_205,
     "evidence": ",".join(f"Item{i}=nexus-em75s.{i}" for i in range(1, 7)),
     "has": ["APPROACH CROSS-WALK PASSED", "nexus-em75s.1", "nexus-em75s.6"], "lacks": []},
    # RDR-204: §Approach is prose and Phase 1 is a plain numbered list directly
    # under `### Phase 1`: the other real-world shape this bug covered.
    {"name": "rdr_204_plain_numbered_phase", "src": "rdr-204-embedding-profile-and-collection-authority.md",
     "evidence": None, "has": ["§Approach Cross-Walk", *_ITEMS_1_TO_6], "lacks": ["ERROR"]},
]


@pytest.mark.parametrize("case", _table(_PRG_REAL_RDR_CASES))
def test_phase_review_gate_on_real_rdr_files(rdr_env, case):
    src = _REAL_RDR_DIR / case["src"]
    shutil.copy(src, rdr_env["rdr_dir"] / src.name)
    number = re.search(r"rdr-(\d+)-", src.name).group(1)
    argv = [number, "--phase", "1"] + (["--evidence", case["evidence"]] if case["evidence"] else [])
    result = _preamble(*_GATE_1, *argv)
    assert result.exit_code == 0, f"[{case['name']}] {result.output}"
    _assert_output(case["name"], result.output, has=case["has"], lacks=case["lacks"])


def _approach_text(heading: str) -> str:
    return f"## Intro\n\nx.\n\n{heading}\n\nbody line.\n\n## Next\n\ny."


#: _prg_extract_approach_section synonym recognition (nexus-2pw1x). (case, text, expected body)
_APPROACH_EXTRACTOR_CASES = [
    ("implementation_plan_heading", _approach_text("## Implementation Plan"), "body line."),
    ("phases_heading", _approach_text("### Phases"), "body line."),
    ("plain_plan_heading_is_case_insensitive", _approach_text("## plan"), "body line."),
    ("approach_heading", _approach_text("### Approach"), "body line."),
    ("no_recognised_heading_returns_empty", "## Intro\n\nx.\n\n## Tradeoffs\n\ny.", ""),
    # Bare 'Plan' must not match 'Planned'/'Planning'/'Planner' (prefix).
    ("planned_work_does_not_match", _approach_text("## Planned Work").replace("body line.", "body."), ""),
    ("planning_notes_does_not_match", _approach_text("## Planning Notes").replace("body line.", "body."), ""),
    ("planner_design_does_not_match", _approach_text("### Planner Design").replace("body line.", "body."), ""),
    # '## Plan Optimization' is a differently-scoped section: bare 'Plan' matches
    # only as the whole heading name.
    ("plan_with_extra_words_does_not_match", _approach_text("## Plan Optimization").replace("body line.", "body."), ""),
    # Suffix tolerance preserved for Approach/Implementation Plan/Phases.
    ("approach_with_trailing_text_still_matches", _approach_text("### Approach (two tracks)"), "body line."),
    # '## Proposed Approach' is the most common phrasing (RDR-176); the
    # 'Proposed' prefix previously defeated the matcher.
    ("proposed_approach_heading", _approach_text("## Proposed Approach (pillars)"), "body line."),
    ("proposed_plan_heading", _approach_text("### Proposed Plan"), "body line."),
    # 'Proposed' only licenses Approach/Plan synonyms, never 'Proposed Solution'.
    ("proposed_solution_does_not_match", _approach_text("## Proposed Solution").replace("body line.", "body."), ""),
]


@pytest.mark.parametrize("case, text, expected", _APPROACH_EXTRACTOR_CASES,
                         ids=[c[0] for c in _APPROACH_EXTRACTOR_CASES])
def test_prg_extract_approach_section(case, text, expected):
    from nexus.commands.rdr import _prg_extract_approach_section

    assert _prg_extract_approach_section(text).strip() == expected, case


#: GH #1443: the §Approach shapes the item regex misses must be reported,
#: never absorbed into the previous item. (case, text, expected unparsed lines,
#: expected parsed item numbers or None)
_UNPARSED_ITEM_CASES = [
    ("clean_numbered_list_has_none",
     ("1. **T2 read**: read from T2.\n   continuation prose of item one\n"
      "2. **File fallback**: fall back to files.\n- a sub bullet\n"), [], None),
    ("non_integer_item_number",
     ("5. **Daemon**: stand it up.\n5a. **Daemon lease**: added after drafting.\n"
      "6. **Routes**: wire reads.\n"),
     ["5a. **Daemon lease**: added after drafting."], [5, 6]),
    ("wrapped_bold_label",
     ("1. **Short**: fine.\n2. **A label long enough that the author wrapped it\n"
      "   onto the next line**: description.\n"),
     ["2. **A label long enough that the author wrapped it"], None),
    ("label_on_the_following_line",
     "1. **First**: fine.\n2.\n**Second**: label below its number.\n", ["2."], None),
    # `5.1.` and `2)` reproduce the GH #1443 symptom and the first detector
    # missed both (critique of ad158133b).
    ("decimal_and_paren_numbering_and_a_plain_item",
     ("5. **Daemon**: stand it up.\n5.1. **Lease**: a decimal sub-item.\n"
      "6) **Routes**: paren numbering.\n7. plain numbered item with no bold label\n"),
     ["5.1. **Lease**: a decimal sub-item.", "6) **Routes**: paren numbering.",
      "7. plain numbered item with no bold label"], None),
    # The extracted section can run through several `###` subsections
    # (rdr-195): only the item list's own block is scanned.
    ("numbered_aside_under_a_later_subheading_is_out_of_scope",
     ("1. **Engine**: cap the batch.\n2. **Client**: size the byte budget.\n\n"
      "Two consequences follow.\n1. Skewed users can still hit the ceiling.\n"
      "2. The budget must be sized with headroom.\n\n"
      "### Technical Design\n1. an aside under a later heading\n"), [], None),
    # rdr-089's wrapped label is item 1 and the first PARSED item is 2: the
    # scan begins at the block's heading, not at the first parse.
    ("dropped_item_before_the_first_parsed_one",
     ("### Approach\n1. **A label that wraps onto\n   the next line**: description.\n"
      "2. **Second**: fine.\n"), ["1. **A label that wraps onto"], None),
    ("plain_line_inside_the_item_block",
     ("1. **Engine**: cap the batch.\n2. plain step between two items\n"
      "3. **Client**: size the byte budget.\n\n### Technical Design\n1. an aside that is out of scope\n"),
     ["2. plain step between two items"], None),
    ("phase_block_headers_are_not_item_starts",
     "**Phase 0: Scaffolding**\n- bullet\n**Phase 1: Core**\n- bullet\n", [], None),
    # rdr-037 (numbered shell recipe in a code fence) and rdr-063 (nested
    # checklists): column 0 is the item grammar; nothing indented or fenced is.
    ("indented_numbered_lines_and_fenced_code_are_not_item_starts",
     ("1. **Consolidate**: one database.\n   1. nested step one\n   2. nested step two\n"
      "```bash\n1. not an item, a recipe line\n2a. also not an item\n```\n2. **Cut over**: flip.\n"),
     [], None),
    # rdr-146: bold emphasis that wraps across two lines inside an item's prose.
    ("wrapped_bold_prose_is_not_an_item_start",
     ("1. **Daemon**: stand it up.\n   **This matters because the store is behind the\n"
      "   daemon** and nothing else reaches it.\n2. **Routes**: wire reads.\n"), [], None),
]


@pytest.mark.parametrize("case, text, unparsed, parsed", _UNPARSED_ITEM_CASES,
                         ids=[c[0] for c in _UNPARSED_ITEM_CASES])
def test_prg_find_unparsed_item_starts(case, text, unparsed, parsed):
    from nexus.commands.rdr import _prg_find_unparsed_item_starts, _prg_parse_approach_items  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    assert _prg_find_unparsed_item_starts(text) == unparsed, case
    if parsed is not None:
        assert [n for n, _, _ in _prg_parse_approach_items(text)] == parsed, case


_PHASE_APPROACH = (
    "**Phase 0: Scaffolding**\n\n- bullet zero a\n- bullet zero b\n\n"
    "**Phase 1: Core**\n\n- **Daemon**: stand it up\n- route reads\n"
)
_P1_ITEMS = [(1, "Phase 1: Daemon", "stand it up"), (2, "Phase 1: route reads", "route reads")]
_P0_ITEMS = [(1, "Phase 0: bullet zero a", "bullet zero a"), (2, "Phase 0: bullet zero b", "bullet zero b")]

#: _prg_parse_phase_block_items (nexus-4u6mt). (case, text, phase, expected items)
_PHASE_BLOCK_CASES = [
    ("selects_only_the_requested_phase", _PHASE_APPROACH, "1", _P1_ITEMS),
    ("phase0_selects_phase0", _PHASE_APPROACH, "0", _P0_ITEMS),
    # GH #1443 critique residual: a non-bulleted line after a bullet was
    # dropped; it is that bullet's continuation.
    ("a_bullet_continuation_line_is_kept",
     ("**Phase 1: Core**\n- **Daemon**: stand it up\n  and keep it up across restarts\n- route reads\n"), "1",
     [(1, "Phase 1: Daemon", "stand it up and keep it up across restarts"),
      (2, "Phase 1: route reads", "route reads")]),
    ("no_phase_enumerates_all_blocks", _PHASE_APPROACH, None,
     [(1, *_P0_ITEMS[0][1:]), (2, *_P0_ITEMS[1][1:]), (3, *_P1_ITEMS[0][1:]), (4, *_P1_ITEMS[1][1:])]),
    ("numbered_approach_has_no_phase_header", "1. **Foo**: bar\n2. **Baz**: qux\n", "1", []),
    ("phase_arg_accepts_phase_n_prose", _PHASE_APPROACH, "Phase 1", _P1_ITEMS),
    # P5a: `--phase 1.5` enumerates Phase 1.5, not Phase 1.
    ("decimal_phase_selects_its_own_block",
     "**Phase 1: alpha**\n\n- do A\n- do B\n\n**Phase 1.5: beta**\n\n- do C\n", "1.5",
     [(1, "Phase 1.5: do C", "do C")]),
    ("integer_phase_is_not_the_decimal_one",
     "**Phase 1: alpha**\n\n- do A\n- do B\n\n**Phase 1.5: beta**\n\n- do C\n", "1",
     [(1, "Phase 1: do A", "do A"), (2, "Phase 1: do B", "do B")]),
]


@pytest.mark.parametrize("case, text, phase, expected", _PHASE_BLOCK_CASES,
                         ids=[c[0] for c in _PHASE_BLOCK_CASES])
def test_prg_parse_phase_block_items(case, text, phase, expected):
    from nexus.commands.rdr import _prg_parse_phase_block_items

    assert _prg_parse_phase_block_items(text, phase=phase) == expected, case


# nexus-w5gma: the RDR template's own §Implementation Plan placement --
# ``### Phase N: title`` headings, which neither the numbered-bold-item parser
# nor the ``**Phase N:**`` bold-block parser recognises.
_HEADING_STEPS = (  # RDR-205 shape: a step is its own heading, one level under Phase
    "### Phase 1: Engine\n\n#### Step 1: Settle the pooler fact\n\nSome prose about the pooler.\n\n"
    "#### Step 2: Changesets\n\nSome prose about changesets.\n\n"
    "### Phase 2: Client\n\n#### Step 1: `HttpTupleStore`\n\nClient prose.\n"
)
_PLAIN_NUMBERED = (  # RDR-204 shape: a plain numbered list directly under the Phase heading
    "### Phase 1: Schema and backfill\n\n"
    "1. One Liquibase changeset on the hygiene shape.\n2. Engine boot writes the profile.\n"
    "3. Delete the seven stub inserts.\n\n"
    "### Phase 2: Engine reads the row\n\n1. CollectionRegistry caches the row.\n"
)
_PROSE_ONLY = (  # a phase with no internal structure at all
    "### Phase 4: Consumer one, the ledger\n\n"
    "Prose describing the whole phase with no sub-list and no\nsub-headings whatsoever.\n\n"
    "### Phase 5: Consumer two\n\nMore prose.\n"
)
_STEP_1 = (1, "Phase 1: Step 1: Settle the pooler fact", "Some prose about the pooler.")
_STEP_2 = (2, "Phase 1: Step 2: Changesets", "Some prose about changesets.")
_STEP_3 = (3, "Phase 2: Step 1: `HttpTupleStore`", "Client prose.")
_PROSE_4 = (1, "Phase 4: Consumer one, the ledger",
            "Prose describing the whole phase with no sub-list and no sub-headings whatsoever.")

_PLAN_PHASE_CASES = [
    ("heading_steps_enumerate_the_requested_phase", _HEADING_STEPS, "1", [_STEP_1, _STEP_2]),
    ("heading_steps_second_phase_restarts_numbering", _HEADING_STEPS, "2",
     [(1, "Phase 2: Step 1: `HttpTupleStore`", "Client prose.")]),
    ("heading_steps_no_phase_enumerates_all_sequentially", _HEADING_STEPS, None, [_STEP_1, _STEP_2, _STEP_3]),
    ("plain_numbered_list_under_a_phase_heading", _PLAIN_NUMBERED, "1",
     [(1, "Phase 1: One Liquibase changeset on the hygiene shape", "One Liquibase changeset on the hygiene shape."),
      (2, "Phase 1: Engine boot writes the profile", "Engine boot writes the profile."),
      (3, "Phase 1: Delete the seven stub inserts", "Delete the seven stub inserts.")]),
    ("plain_numbered_list_second_phase", _PLAIN_NUMBERED, "2",
     [(1, "Phase 2: CollectionRegistry caches the row", "CollectionRegistry caches the row.")]),
    # nexus-moht0 non-vacuity: real content but no internal structure is one
    # cross-walkable item, never silently zero.
    ("prose_only_phase_is_one_item_not_zero", _PROSE_ONLY, "4", [_PROSE_4]),
    ("prose_only_no_phase_is_one_item_per_phase", _PROSE_ONLY, None,
     [_PROSE_4, (2, "Phase 5: Consumer two", "More prose.")]),
    ("no_phase_heading_present", "Just prose, no phases.\n", "1", []),
    ("requested_phase_does_not_exist", _HEADING_STEPS, "99", []),
]


@pytest.mark.parametrize("case, text, phase, expected", _PLAN_PHASE_CASES,
                         ids=[c[0] for c in _PLAN_PHASE_CASES])
def test_prg_parse_plan_phase_items(case, text, phase, expected):
    from nexus.commands.rdr import _prg_parse_plan_phase_items

    assert _prg_parse_plan_phase_items(text, phase=phase) == expected, case


def _fenced_impl_plan(text: str):
    from nexus.commands.rdr import _prg_extract_implementation_plan_section, _prg_parse_plan_phase_items

    sec = _prg_extract_implementation_plan_section(text)
    return ("Step 3: third" in sec, "## Consequences" not in sec,
            [lbl for _, lbl, _ in _prg_parse_plan_phase_items(sec)])


def _fenced_approach(text: str):
    from nexus.commands.rdr import (
        _prg_extract_approach_section, _prg_find_unparsed_item_starts, _prg_parse_approach_items,
    )

    sec = _prg_extract_approach_section(text)
    return [n for n, _, _ in _prg_parse_approach_items(sec)], _prg_find_unparsed_item_starts(sec)


def _renumbered(text: str):
    from nexus.commands.rdr import _prg_parse_approach_items

    return [(n, lbl) for n, lbl, _ in _prg_parse_approach_items(text)]


def _impl_plan_section_shape(text: str):
    from nexus.commands.rdr import _prg_extract_implementation_plan_section

    section = _prg_extract_implementation_plan_section(text)
    return ("### Phase 1: Foo" in section, "body line." in section, "Test Plan" not in section,
            # Independent of the earliest-match Approach extraction: the
            # Approach prose itself must not leak into this section.
            "no items" not in section)


def _impl_plan_section(text: str):
    from nexus.commands.rdr import _prg_extract_implementation_plan_section

    return _prg_extract_implementation_plan_section(text)


#: The gate must never cross-walk a subset of the section it was handed
#: (intrastate review T2 intrastate/[26115] #1, #8, #9; plan N1). Probes P5a-c
#: from nexus-redo-probes-2026-09-17/review-nexus-rdr/probe_rdr.py.
_SUBSET_PARSE_CASES = [
    # P5b: a column-0 ``# comment`` inside a code fence is not a heading.
    ("fenced_comment_does_not_end_the_implementation_plan_section", _fenced_impl_plan,
     ("\n## Implementation Plan\n\n### Phase 1: one\n\n#### Step 1: first\ntext\n\n"
      "```bash\n# install\nmake\n```\n\n#### Step 2: second\ntext\n\n"
      "### Phase 2: two\n\n#### Step 3: third\ntext\n\n## Consequences\n"),
     (True, True, ["Phase 1: Step 1: first", "Phase 1: Step 2: second", "Phase 2: Step 3: third"])),
    # P5b, §Approach shape: items after a fenced ``# comment`` are kept.
    ("fenced_comment_does_not_end_the_approach_section", _fenced_approach,
     ("\n## Approach\n\n1. **A**: first\n\n```bash\n# build\nmake\n```\n\n"
      "2. **B**: second\n3. **C**: third\n\n## Consequences\n"),
     ([1, 2, 3], [])),
    # Review of 55b38cd25: only colliding lists are renumbered, above every
    # number in use; a third list with unique numbers keeps its keys.
    ("only_colliding_lists_are_renumbered", _renumbered,
     ("#### Track A\n\n1. **A1**: a\n2. **A2**: b\n\n#### Track B\n\n1. **B1**: c\n2. **B2**: d\n\n"
      "#### Track C\n\n3. **C1**: e\n4. **C2**: f\n"),
     [(5, "Track A: A1"), (6, "Track A: A2"), (7, "Track B: B1"), (8, "Track B: B2"), (3, "C1"), (4, "C2")]),
    # nexus-w5gma: the Implementation Plan extractor is independent of the Approach.
    ("implementation_plan_section_after_a_separate_approach", _impl_plan_section_shape,
     ("## Proposed Solution\n\n### Approach\n\nProse, no items.\n\n"
      "### Technical Design\n\nMore prose.\n\n"
      "## Implementation Plan\n\n### Phase 1: Foo\n\nbody line.\n\n## Test Plan\n\nirrelevant.\n"),
     (True, True, True, True)),
    ("no_implementation_plan_heading_is_empty", _impl_plan_section,
     "## Proposed Solution\n\n### Approach\n\nProse only.\n", ""),
]


@pytest.mark.parametrize("case, run, text, expected", _SUBSET_PARSE_CASES,
                         ids=[c[0] for c in _SUBSET_PARSE_CASES])
def test_phase_review_gate_never_crosswalks_a_subset(case, run, text, expected):
    assert run(text) == expected, case


def test_two_lists_restarting_at_one_get_distinct_evidence_keys(rdr_env):
    """P5c: two tracks numbered 1..2 each are four items needing four pointers;
    ``Item1=..,Item2=..`` must not cover all four."""
    _write_rdr(
        rdr_env["rdr_dir"], "rdr-050-tracks.md", {"title": "Tracks", "status": "accepted"},
        "# Tracks\n\n### Approach (two tracks)\n\n#### Track A\n\n"
        "1. **A1**: a one\n2. **A2**: a two\n\n#### Track B\n\n"
        "1. **B1**: b one\n2. **B2**: b two\n\n## Consequences\n",
    )
    res = _preamble(*_GATE_1, "50", "--phase", "1")
    assert res.exit_code == 0, res.output
    keys = re.findall(r"^\| (Item\d+) \|", res.output, re.MULTILINE)
    assert len(keys) == 4 and len(set(keys)) == 4, res.output
    res = _preamble(*_GATE_1, "50", "--phase", "1", "--evidence", "Item1=nexus-a,Item2=nexus-b")
    # All four collide, so all four take fresh keys (Item3..Item6); the RDR's
    # own Item1/Item2 name nothing and cover nothing.
    assert "BLOCKED" in res.output, res.output
    assert "4 of 4" in res.output, res.output


@pytest.mark.parametrize("passed", [True, False], ids=["passed_writes_a_sentinel", "blocked_writes_none"])
def test_phase_review_gate_sentinel(rdr_env, monkeypatch, tmp_path, passed):
    """RDR-121 P2 co-requirement: the PASSED path writes a sentinel JSON under
    $TMPDIR/nx-phase-gate-sentinel/; BLOCKED must not."""
    import json

    _write_rdr(rdr_env["rdr_dir"], "rdr-130-test.md",
               {"title": "Test RDR", "status": "accepted", "type": "decision", "priority": "P0"},
               body=_approach_body(_TWO_ITEMS))
    sentinel_base = tmp_path / "sentinels"
    sentinel_base.mkdir()
    monkeypatch.setenv("TMPDIR", str(sentinel_base))
    evidence = "Item1=nexus-abc1,Item2=nexus-def2" if passed else "Item1=nexus-abc1"  # Item2 missing
    result = _preamble(*_GATE_1, "130", "--phase", "1", "--evidence", evidence)
    assert result.exit_code == 0, result.output
    sentinel_dir = sentinel_base / "nx-phase-gate-sentinel"
    files = list(sentinel_dir.glob("*-130-1.json")) if sentinel_dir.exists() else []
    if passed:
        assert "APPROACH CROSS-WALK PASSED" in result.output
        assert len(files) == 1, f"expected one sentinel for RDR-130 phase 1, got {files}"
        payload = json.loads(files[0].read_text())
        assert (payload["outcome"], payload["rdr_id"], payload["phase"]) == ("PASSED", "130", "1")
    else:
        assert "BLOCKED" in result.output
        assert files == [], f"BLOCKED outcome must not write a sentinel; found {files}"


_ONE_ITEM_BODY = "### Approach\n\n1. **A**: one\n\n## Consequences\n"
_OTHER = ("rdr-002-other.md", {"title": "Other", "status": "accepted"})

#: Sweep for the nexus-my04w class: every preamble joined argv to one string and
#: took the RDR id from the first digits anywhere in it.
_ARGV_SIBLING_CASES = [
    # One properly quoted argv element ``Item1=a, Item2=b`` was stripped by a
    # no-spaces pattern, leaving ``Item2=b`` behind, and ``2`` won the lookup.
    {"name": "evidence_with_a_space_does_not_pick_the_rdr",
     "files": [(*_OTHER, "### Approach\n\n1. **A**: one\n2. **B**: two\n\n## Consequences\n"),
               ("rdr-112-real.md", {"title": "Real", "status": "accepted"},
                "### Approach\n\n1. **A**: one\n2. **B**: two\n\n## Consequences\n")],
     "argv": ["--phase", "1", "--evidence", "Item1=nexus-a, Item2=nexus-b", "112"],
     "has": ["rdr-112-real.md", "APPROACH CROSS-WALK PASSED"], "lacks": [], "exit0": True},
    # nexus-u1jxt.5: a one-pair evidence value equals its own token, so
    # filtering "evidence tokens" also removed the flag's VALUE and the phase
    # number became the RDR id.
    {"name": "single_evidence_pair_with_the_id_last_does_not_pick_the_phase",
     "files": [(*_OTHER, _ONE_ITEM_BODY), ("rdr-205-real.md", {"title": "Real", "status": "accepted"}, _ONE_ITEM_BODY)],
     "argv": ["--evidence", "Item1=nexus-a", "--phase", "2", "205"],
     "has": ["rdr-205-real.md"], "lacks": ["rdr-002-other.md"], "exit0": False},
    {"name": "phase_number_never_selects_the_rdr",
     "files": [("rdr-003-other.md", {"title": "Other", "status": "accepted"}, _ONE_ITEM_BODY),
               ("rdr-112-real.md", {"title": "Real", "status": "accepted"}, _ONE_ITEM_BODY)],
     "argv": ["--phase", "3", "112"], "has": ["rdr-112-real.md"], "lacks": [], "exit0": False},
]


@pytest.mark.parametrize("case", _table(_ARGV_SIBLING_CASES))
def test_preamble_argv_siblings(rdr_env, case):
    _write_files(rdr_env, case["files"])
    res = _preamble(*_GATE_1, *case["argv"])
    if case["exit0"]:
        assert res.exit_code == 0, f"[{case['name']}] {res.output}"
    _assert_output(case["name"], res.output, has=case["has"], lacks=case["lacks"])


_ID_TOKEN_CASES = [
    ("rdr_prefixed_after_a_flag", (("--skip-gaps", "RDR-097"), {}), "097"),
    ("bare_number_after_a_word", (("status", "97"), {}), "97"),
    ("one_shell_string", (("069 --reason implemented",), {}), "069"),
    ("phase_value_flag_is_skipped", (("--phase", "3", "112"), {"value_flags": ("--phase",)}), "112"),
    ("no_id_shaped_token_falls_back_to_digits", (("rdr-097-foo.md",), {}), "097"),
    ("empty_argv", ((), {}), None),
]


@pytest.mark.parametrize("case, call, expected", _ID_TOKEN_CASES, ids=[c[0] for c in _ID_TOKEN_CASES])
def test_preamble_id_token_prefers_an_id_shaped_positional(case, call, expected):
    from nexus.commands.rdr import _preamble_id_token  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    argv, kwargs = call
    assert _preamble_id_token(argv, **kwargs) == expected, case


# ---------------------------------------------------------------------------
# rdr-verdict (nexus-yxo2l): the gate outcome is computed in code from the
# critique and the round, never in the gating model's head (audit_rounds design
# decision 1). Real RDR-204 critiques are the fixtures.
# ---------------------------------------------------------------------------

_VERDICT_BODY = _ROUND_BODY


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _verdict(monkeypatch, store: dict[str, str], critique: str):
    _use_t2(monkeypatch, store)
    return _preamble("rdr-verdict", "--", "204", critique)


_ALPHA_BLOCKER = ("## Critical Issues\n\n### Issue: alpha\n- **Location**: L1\n"
                  "- **Class**: BLOCKS-PLANNING\n- **Ship-blocker**: yes\n\n")
_ROUND_FOUR_PRIOR = {"204-gate-latest": 'outcome: "PASSED"\nprior: [1] (1C), [2] (1C)\n'}
_ONE_ONE_VERDICT = ("## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n"
                    "- **significant_count**: 1\n- **ship_blockers**: 1\n")
_BLOCKED = 'outcome: "BLOCKED"'

_VERDICT_CASES = [
    # -07h: 3 Criticals, ship_blockers 0; a first gate blocks.
    {"name": "round_one_blocks_on_any_critical", "key": "204-gate-critique-2026-09-07h",
     "crit": ("fixture", "204-gate-critique-2026-09-07h.md"), "extra": {},
     "has": ["Gate round 1", "critical_count: 3", "ship_blockers: 0", _BLOCKED, "rule: any-critical"],
     "lacks": [], "counts": {}},
    # The same critique at round 10 (204's real position): PASSED with three
    # residuals, which is what would have ended the loop at pass 5.
    {"name": "round_three_passes_with_residuals_when_no_ship_blocker", "key": "204-gate-critique-2026-09-07h",
     "crit": ("fixture", "204-gate-critique-2026-09-07h.md"),
     "extra": {"204-gate-latest": (
         'outcome: "PASSED"\ndate: "2026-09-07"\ncritical_count: 0\nsignificant_count: 2\ncommit: {sha}\n'
         "prior: [24844] (BLOCKED 1C 2S 2O), [24841] (PASSED 0C 3S 4O), [24812] (1C), [24809] (2C), "
         "[24806] (2C 2S), [24800] (1C 2S), [24789] (2C)\n")},
     "has": ["Gate round 9", "rule: ship-blocker", 'outcome: "PASSED"', "residuals:", "model_version", "prior: [",
             # the chain is pre-filled from the current record
             "[24844] (BLOCKED 1C 2S 2O)", "commit: {sha}"],
     "lacks": [], "counts": {}},
    # -07f: free-form layout, `Ship-blocker: yes` on the Critical and a
    # `VERDICT: ... ship_blockers=1` line; round 7 in the real loop.
    {"name": "free_form_critique_with_a_ship_blocker_blocks_at_any_round",
     "key": "204-gate-critique-2026-09-07f", "crit": ("fixture", "204-gate-critique-2026-09-07f.md"),
     "extra": {"204-gate-latest": 'outcome: "PASSED"\nprior: [1] (1C), [2] (2C), [3] (2C), [4] (1C), [5] (0C)\n'},
     "has": ["Gate round 7", "critical_count: 1", "ship_blockers: 1", _BLOCKED], "lacks": [], "counts": {}},
    # Two issues marked Ship-blocker: yes but a Verdict saying 0: the counted
    # value wins and the discrepancy is named.
    {"name": "self_report_below_the_count_is_overridden", "key": "c",
     "crit": ("## Critical Issues\n\n### Issue: one\n- **Location**: L1\n- **Ship-blocker**: yes\n\n"
              "### Issue: two\n- **Location**: L2\n- **Ship-blocker**: yes\n\n## Significant Issues\nNone.\n\n"
              "## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n"
              "- **significant_count**: 0\n- **ship_blockers**: 0\n"),
     "extra": {"204-gate-latest": 'outcome: "PASSED"\nprior: [1] (1C), [2] (1C)\n'},
     "has": ["critical_count: 2", "ship_blockers: 2", "self-reported", "counted", _BLOCKED],
     "lacks": [], "counts": {}},
    {"name": "missing_ship_blockers_line_reads_as_critical_count", "key": "c",
     "crit": ("## Critical Issues\n\n### Issue: one\n- **Location**: L1\n\n## Verdict\n\n"
              "- **outcome**: not-justified\n- **critical_count**: 1\n"),
     "extra": {"204-gate-latest": 'outcome: "PASSED"\nprior: [1] (1C), [2] (1C)\n'},
     "has": ["ship_blockers: 1", _BLOCKED, "no ship_blockers line"], "lacks": [], "counts": {}},
    # Critique [24898] Critical 1: zero counts from an unparsed critique must
    # never print PASSED.
    {"name": "unrecognised_shape_refuses_instead_of_passing", "key": "c",
     "crit": "Some free prose about the RDR with no sections, no markers, no verdict.\n", "extra": {},
     "has": ["neither recognised shape"], "lacks": ["Outcome:"], "counts": {}},
    {"name": "missing_critique_is_named", "key": "204-gate-critique-nope", "crit": None, "extra": {},
     "has": ["no such T2 record"], "lacks": [], "counts": {}},
    # nexus-u1jxt.2: a Critical section whose blocks are headed ``### C1:``
    # rather than ``### Issue:`` counted zero, and the critic's explicit
    # `outcome: BLOCKED` was captured and never read, so the round PASSED.
    {"name": "critics_blocked_outcome_beats_zero_counted_blocks", "key": "204-gate-critique-2026-09-07",
     "crit": ("## Critical Issues\n\n### C1: The design contradicts itself\n- **Location**: L1\n"
              "- **Ship-blocker**: yes\n\n## Significant Issues\n\nNone.\n\n## Verdict\n\n"
              "- **outcome**: BLOCKED\n"), "extra": {},
     "has": ["**Outcome: BLOCKED**", "stricter reading wins"], "lacks": [], "counts": {}},
    # Decision 2: `Ship-blocker: yes` plus `Class: DISCOVER-AT-IMPLEMENTATION`
    # on the SAME finding disagree (a ship-blocker is BLOCKS-PLANNING by
    # definition), and the verdict refuses to compute an outcome at all.
    {"name": "ship_blocker_and_discover_at_implementation_contradiction_refuses", "key": "c",
     "crit": ("## Critical Issues\n\n### Issue: gamma\n- **Location**: L1\n"
              "- **Class**: DISCOVER-AT-IMPLEMENTATION\n- **Ship-blocker**: yes\n\n"
              "## Significant Issues\nNone.\n\n"
              "## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n"
              "- **significant_count**: 0\n- **ship_blockers**: 1\n"), "extra": {},
     "has": ["Contradiction", "gamma", "Ship-blocker: yes", "DISCOVER-AT-IMPLEMENTATION"],
     "lacks": [_BLOCKED, 'outcome: "PASSED"', "**Outcome:"], "counts": {}},
    # Class lines but no per-finding Ship-blocker line at all is no
    # contradiction: the ordinary ship_blockers computation runs untouched.
    {"name": "class_without_a_ship_blocker_line_computes_normally", "key": "c",
     "crit": ("## Critical Issues\n\n### Issue: delta\n- **Location**: L1\n- **Class**: BLOCKS-PLANNING\n\n"
              "## Significant Issues\nNone.\n\n"
              "## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n"
              "- **significant_count**: 0\n- **ship_blockers**: 0\n"), "extra": {},
     "has": [_BLOCKED],  # critical_count > 0 blocks at round 1 regardless of Class
     "lacks": ["Contradiction"], "counts": {}},
    # nexus-yjf5l.7 (R3): at round 4, alpha's `Ship-blocker: yes` makes it the
    # ship-blocker (not a residual); beta is a residual and its
    # `Class: DISCOVER-AT-IMPLEMENTATION` line survives onto the printed
    # `residuals:` line exactly as `  - [<class>] <title>`.
    {"name": "residual_line_carries_its_class", "key": "c",
     "crit": (_ALPHA_BLOCKER + "## Significant Issues\n\n### Issue: beta\n- **Location**: L2\n"
              "- **Class**: DISCOVER-AT-IMPLEMENTATION\n- **Ship-blocker**: no\n\n" + _ONE_ONE_VERDICT),
     "extra": _ROUND_FOUR_PRIOR,
     "has": ["Gate round 4", _BLOCKED, "residuals:\n  - [DISCOVER-AT-IMPLEMENTATION] beta"],
     "lacks": ["[DISCOVER-AT-IMPLEMENTATION] alpha"], "counts": {}},
    # gamma carries no `Class:` line: it prints as a residual under the
    # conservative BLOCKS-PLANNING default but never changes what blocks.
    {"name": "unclassified_residual_does_not_change_what_blocks", "key": "c",
     "crit": (_ALPHA_BLOCKER + "## Significant Issues\n\n### Issue: gamma\n- **Location**: L2\n"
              "- **Ship-blocker**: no\n\n" + _ONE_ONE_VERDICT),
     "extra": _ROUND_FOUR_PRIOR,
     "has": ["Gate round 4", _BLOCKED, "residuals:\n  - [BLOCKS-PLANNING] gamma"], "lacks": [], "counts": {}},
    # Boundary (critique nexus-yjf5l.9, item (a)): `Ship-blocker: yes` and NO
    # `Class:` line. The contradiction check only fires on an explicit
    # DISCOVER-AT-IMPLEMENTATION, and a ship-blocker never reaches `residuals:`.
    {"name": "ship_blocker_with_no_class_line_is_unclassified_and_not_a_residual", "key": "c",
     "crit": ("## Critical Issues\n\n### Issue: alpha\n- **Location**: L1\n- **Ship-blocker**: yes\n\n"
              "## Significant Issues\n\n### Issue: beta\n- **Location**: L2\n- **Ship-blocker**: no\n\n"
              + _ONE_ONE_VERDICT),
     "extra": _ROUND_FOUR_PRIOR,
     "has": ["Gate round 4", _BLOCKED, "ship_blockers: 1", "residuals:\n  - [BLOCKS-PLANNING] beta"],
     "lacks": ["Contradiction", "[BLOCKS-PLANNING] alpha"], "counts": {}},
    # nexus-yjf5l.14 F1: with Layer 0 telling critics not to re-raise a recorded
    # residual, round 3's gamma is absent from round 4's critique and must
    # still appear in `residuals:`, carrying its class and the round it came from.
    {"name": "residual_absent_from_the_critique_is_carried_forward", "key": "c",
     "crit": ("## Critical Issues\n\n### Issue: alpha\n- **Location**: L1\n- **Ship-blocker**: yes\n\n"
              "## Significant Issues\nNone.\n\n"
              "## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n"
              "- **significant_count**: 0\n- **ship_blockers**: 1\n"),
     "extra": {"204-gate-latest": (
         'outcome: "BLOCKED"\ndate: "2026-09-07"\nround: 3\nprior: [1] (1C), [2] (1C)\n'
         "residuals:\n  - [DISCOVER-AT-IMPLEMENTATION] gamma\n")},
     "has": ["Gate round 4", _BLOCKED,
             "residuals:\n  - [DISCOVER-AT-IMPLEMENTATION] gamma (carried from round 3)"],
     "lacks": [], "counts": {}},
    # nexus-yjf5l.14 F1/critique 1: a residual's title carries a self-referential
    # count that drifts between rounds; the looser key must still recognise it
    # as the same finding and never carry a duplicate. ``epsilon`` is untouched
    # by this round's critique and anchors that carry-forward actually ran.
    {"name": "drifted_residual_title_matches_and_is_not_duplicated", "key": "c",
     "crit": ("## Critical Issues\nNone.\n\n"
              "## Significant Issues\n\n### Issue: Finalization Gate critique count is stale by 3\n"
              "- **Location**: L1\n- **Ship-blocker**: no\n\n"
              "## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 0\n"
              "- **significant_count**: 1\n- **ship_blockers**: 0\n"),
     "extra": {"204-gate-latest": (
         'outcome: "PASSED"\ndate: "2026-09-08"\nround: 5\n'
         "prior: [1] (1C), [2] (1C), [3] (1C), [4] (1C)\n"
         "residuals:\n  - [BLOCKS-PLANNING] Finalization Gate critique count is stale by 2\n"
         "  - [BLOCKS-PLANNING] epsilon untouched\n")},
     "has": ["Gate round 6", "epsilon untouched (carried from round 5)", "stale by 3"],
     "lacks": ["stale by 2"],  # the drifted-count duplicate is not carried forward
     "counts": {"stale by": 2}},  # once in the "Residuals (N)" line, once in `residuals:`
]


@pytest.mark.parametrize("case", _table(_VERDICT_CASES))
def test_rdr_verdict_outcome(rdr_env, monkeypatch, case):
    name = case["name"]
    sha = _git_commit_rdr(rdr_env, _VERDICT_BODY, "gated")
    store = {k: v.replace("{sha}", sha) for k, v in case["extra"].items()}
    crit = case["crit"]
    if crit is not None:
        store[case["key"]] = _fixture(crit[1]) if isinstance(crit, tuple) else crit
    out = _verdict(monkeypatch, store, case["key"]).output
    _assert_output(name, out, has=[s.replace("{sha}", sha) for s in case["has"]], lacks=case["lacks"])
    for needle, n in case["counts"].items():
        assert out.count(needle) == n, f"[{name}] {needle!r} appears {out.count(needle)} times, want {n}:\n{out}"


def test_already_gated_critique_is_named_as_a_recomputation(rdr_env, monkeypatch):
    """Critique [24898] Critical 2: the round is the next gate's; say so when
    the critique is already the recorded one."""
    sha = _git_commit_rdr(rdr_env, _VERDICT_BODY, "gated")
    store = {
        "204-gate-critique-2026-09-07i": _fixture("204-gate-critique-2026-09-07i.md"),
        "204-gate-latest": (f'outcome: "PASSED"\ncritique: nexus_rdr/204-gate-critique-2026-09-07i\n'
                            f"commit: {sha}\nprior: [1] (1C)\n"),
    }
    out = _verdict(monkeypatch, store, "204-gate-critique-2026-09-07i").output
    assert "already the one `204-gate-latest` records" in out
    store["204-gate-critique-2026-09-07h"] = _fixture("204-gate-critique-2026-09-07h.md")
    out = _verdict(monkeypatch, store, "204-gate-critique-2026-09-07h").output
    assert "assumes this critique is the new" in out


def test_prior_chain_names_each_rounds_own_critique_never_the_upserted_latest_id(rdr_env, monkeypatch):
    """nexus-yjf5l.13: the real ``205-gate-latest`` record's ``prior:`` chain had
    four entries all reading ``[25098]``, the record's OWN T2 id, because
    ``205-gate-latest`` is upserted under one fixed title every round so its id
    never changes. The previous round must be named by its own CRITIQUE
    record's id (a fresh, never-overwritten title each round).
    ``_FakeT2UpsertClient`` models real upsert-by-title id semantics: same
    title, same id, forever."""
    _git_commit_rdr(rdr_env, _VERDICT_BODY, "gated")
    crit = lambda title, line: (  # noqa: E731
        f"## Critical Issues\n\n### Issue: {title}\n- **Location**: L{line}\n- **Ship-blocker**: yes\n")
    fake = _FakeT2UpsertClient({"204-gate-critique-2026-09-09": crit("one", 1)},
                               ids={"204-gate-critique-2026-09-09": 501})
    _use_t2(monkeypatch, client=fake)

    # Round 1: first gate, no prior chain yet.
    out1 = _preamble("rdr-verdict", "--", "204", "204-gate-critique-2026-09-09").output
    assert "Gate round 1" in out1, out1
    block1 = re.search(r"```\n(.*?)```", out1, re.DOTALL).group(1)
    # The upserted "-gate-latest" row's OWN id, assigned once and reused
    # verbatim on every later put to that title.
    latest_row_id = fake.put(project="nexus_rdr", title="204-gate-latest", content=block1)
    crit2_id = fake.put(project="nexus_rdr", title="204-gate-critique-2026-09-09b", content=crit("two", 2))

    # Round 2: the prior chain's one entry names round 1's own critique id (501).
    out2 = _preamble("rdr-verdict", "--", "204", "204-gate-critique-2026-09-09b").output
    assert "Gate round 2" in out2, out2
    prior_line2 = re.search(r"^prior: (.+)$", out2, re.MULTILINE).group(1)
    assert f"[{latest_row_id}]" not in prior_line2, (
        f"the upserted gate-latest row's own (constant) id must never appear in the chain: {prior_line2!r}"
    )
    assert "[501]" in prior_line2, f"round 1's own critique id must name round 1: {prior_line2!r}"

    block2 = re.search(r"```\n(.*?)```", out2, re.DOTALL).group(1)
    assert fake.put(project="nexus_rdr", title="204-gate-latest", content=block2) == latest_row_id
    fake.put(project="nexus_rdr", title="204-gate-critique-2026-09-09c", content=crit("three", 3))

    # Round 3: not a one-round coincidence; the old code would print the
    # constant upserted id again here and at every future round.
    out3 = _preamble("rdr-verdict", "--", "204", "204-gate-critique-2026-09-09c").output
    assert "Gate round 3" in out3, out3
    prior_line3 = re.search(r"^prior: (.+)$", out3, re.MULTILINE).group(1)
    assert f"[{latest_row_id}]" not in prior_line3, (
        f"the constant upserted id must never appear, at any round: {prior_line3!r}"
    )
    assert prior_line3.count("[501]") == 1, prior_line3
    assert prior_line3.count(f"[{crit2_id}]") == 1, prior_line3


def test_verdict_prints_the_one_line_revision_history_form(rdr_env, monkeypatch):
    """nexus-yjf5l.12: a gate round appends ONE Revision History line (date,
    round, outcome, critical and significant counts, ship-blockers, the gated
    commit and the critique's T2 record title) and nothing else. The
    gate-latest record's own title is NOT named: it is upserted under one fixed
    title, so a pointer to it in an older round's line would resolve to a later
    round's content. The finding titles live only in the T2 records."""
    sha = _git_commit_rdr(rdr_env, _VERDICT_BODY, "gated")
    project = f"{rdr_env['repo_root'].name}_rdr"
    crit = (
        "## Critical Issues\n\n### Issue: alpha\n- **Location**: L1\n- **Ship-blocker**: yes\n\n"
        "## Significant Issues\n\n### Issue: beta\n- **Location**: L2\n- **Ship-blocker**: no\n\n"
        + _ONE_ONE_VERDICT
    )
    out = _verdict(monkeypatch, {"c": crit, **_ROUND_FOUR_PRIOR}, "c").output
    assert "Gate round 4" in out, out
    assert "Revision History line to append" in out, out
    today = datetime.now(timezone.utc).date().isoformat()
    expected = (
        f"- {today}: Gate round 4 — BLOCKED (1 Critical, 1 Significant, "
        f"1 ship-blocker(s)); commit `{sha}`; critique `{project}/c`."
    )
    assert expected in out, out
    after = out.split("Revision History line to append", 1)[1]
    assert "alpha" not in after, after
    assert "beta" not in after, after
    assert "gate record" not in after, "the gate-latest record's upserted title must not appear in the line"


_CANONICAL_POISON = (
    "Example of the block:\n```\n## Verdict\n- **critical_count**: 9\n- **ship_blockers**: 9\n```\n"
    "## Critical Issues\n\n### Issue: one\n- **Ship-blocker**: yes\n- **Ship-blocker**: yes\n\n"
    "## Observations\n- CRITICAL — historically this recurred, no action\n\n"
    "## Verdict\n\n- **critical_count**: 1\n- **significant_count**: 0\n- **ship_blockers**: 1\n"
)
_FREE_FORM_POISON = (
    "CRITICAL — a real one.\nShip-blocker: yes\n\nOBSERVATIONS\n\n"
    "CRITICAL — historically this recurred, no action.\nShip-blocker: yes\n\n"
    "VERDICT: not-justified. critical_count=1, ship_blockers=1.\n"
)
_MINOR_HEADING = (
    "## Critical Issues\n\nNone.\n\n## Minor Issues\n\n### Issue: a typo in the title\n"
    "- **Location**: L1\n\n## Verdict\n\n- **outcome**: PASSED\n- **critical_count**: 0\n"
    "- **significant_count**: 0\n- **ship_blockers**: 0\n"
)

_TALLY_CASES = [
    # Code review [24900] 1-3: a fenced example verdict, a duplicate
    # Ship-blocker line, and a CRITICAL aside inside OBSERVATIONS.
    ("canonical_not_poisoned_by_earlier_verdict_shaped_text", _CANONICAL_POISON,
     {"reported_critical": 1, "reported_ship_blockers": 1, "criticals": ["one"], "ship_blocker_titles": ["one"]}),
    ("free_form_not_poisoned_by_an_observations_aside", _FREE_FORM_POISON,
     {"criticals": ["a real one."], "ship_blocker_titles": ["a real one."], "reported_critical": 1}),
    # nexus-u1jxt.8: the tally reset its section only on five named headings, so
    # ``### Issue:`` under ``## Minor Issues`` counted as a Critical while the
    # findings parser (which resets on any heading) saw nothing.
    ("issue_under_a_later_non_finding_heading_is_not_a_critical", _MINOR_HEADING,
     {"criticals": [], "significants": [], "reported_outcome": "passed", "findings": []}),
]


@pytest.mark.parametrize("case, text, expected", _TALLY_CASES, ids=[c[0] for c in _TALLY_CASES])
def test_critique_tally(case, text, expected):
    from nexus.commands.rdr import _critique_findings, _critique_tally

    tally = _critique_tally(text)
    got = {k: (_critique_findings(text) if k == "findings" else getattr(tally, k)) for k in expected}
    assert got == expected, case


_CANONICAL_CRITIQUE = (
    "## Critique Summary\nFine overall.\n\n"
    "## Critical Issues\n\n"
    "### Issue: Ghost sweep narrower than collectionIsEmpty\n"
    "- **Location**: Technical Design step 3\n"
    "- **Problem**: audit-only tables are not FK-constrained\n"
    "- **Recommendation**: reuse COLLECTION_SCOPED_TABLES\n"
    "- **Ship-blocker**: no\n\n"
    "## Significant Issues\n\n"
    "### Issue: Phase 1 item 4 stale sentence\n"
    "- **Location**: Implementation Plan\n\n"
    "## Observations\n\n### Issue: bare bead ids\n- **Location**: everywhere\n\n"
    "## Verification Performed\ngrepped.\n"
)

#: nexus-7vdf9 (critique [24815] Critical 2): the extractor reads the
#: substantive-critic's canonical format, a free-form critique, and the shape
#: the real RDR-204 fourth-gate critique used, dropping Observations.
#: ``elements`` are exact finding lines; ``has`` / ``lacks`` are substrings of
#: any finding.
_FINDINGS_CASES = [
    {"name": "canonical_format_yields_titles_locations_and_recommendations", "text": _CANONICAL_CRITIQUE,
     "elements": ["Issue: Ghost sweep narrower than collectionIsEmpty", "  Location: Technical Design step 3",
                  "  Recommendation: reuse COLLECTION_SCOPED_TABLES", "Issue: Phase 1 item 4 stale sentence"],
     "has": [], "lacks": ["bare bead ids", "Ship-blocker"]},  # observations are not findings
    {"name": "none_sections_yield_nothing",
     "text": "## Critical Issues\nNone.\n\n## Significant Issues\nNone.\n", "empty": True},
    {"name": "free_form_critique",
     "text": ("Prior gate ...\n\nNEW CRITICAL\n\nIssue: the sweep is narrower.\n"
              "Location: step 3\n\n**Critical 1**: ghost sweep omits topic_assignments.\n"
              "- Significant: Phase 1 item 4 stale.\nObservation: fine.\n"),
     "has": ["NEW CRITICAL", "the sweep is narrower", "omits topic_assignments", "Phase 1 item 4 stale"],
     "lacks": ["Observation: fine"]},
    {"name": "empty_text_yields_nothing", "text": "", "empty": True},
    # nexus-yjf5l.7: unlike Ship-blocker (handled solely by _critique_tally),
    # Class must reach the author through the re-gate and fix preambles.
    {"name": "class_line_survives_the_allowlist",
     "text": ("## Critical Issues\n\n### Issue: title\n- **Location**: L1\n"
              "- **Class**: BLOCKS-PLANNING\n- **Ship-blocker**: yes\n\n## Significant Issues\nNone.\n"),
     "has": ["Class: BLOCKS-PLANNING"], "lacks": ["Ship-blocker"]},
    # Backward compatibility: a historical critique that predates the Class
    # field parses as before, and no Class detail is invented for it.
    {"name": "absent_class_line_is_not_an_error", "fixture": "204-gate-critique-2026-09-07i.md",
     "nonempty": True, "lacks": ["Class:"]},
    # nexus-yjf5l.15: the free-form 'CRITICAL — <title>' shape _critique_tally
    # already counted must yield findings too, or the round-3+ split and the
    # Layer 0 exemption are silently inert for it.
    {"name": "free_form_em_dash_shape_matches_the_tally",
     "text": ("CRITICAL — a false claim introduced by this fix commit.\nShip-blocker: yes\n\n"
              "SIGNIFICANT — redundant clause left stale at a second site.\nShip-blocker: no\n"),
     "nonempty": True, "lacks": ["Ship-blocker"], "tally_titles": True},
    {"name": "real_free_form_fixture_matches_the_tally", "fixture": "204-gate-critique-2026-09-07f.md",
     "tally_counts": (1, 2), "n_findings": 3, "tally_titles": True},
]


@pytest.mark.parametrize("case", _table(_FINDINGS_CASES))
def test_critique_findings(case):
    from nexus.commands.rdr import _critique_findings, _critique_tally

    name = case["name"]
    text = _fixture(case["fixture"]) if "fixture" in case else case["text"]
    findings = _critique_findings(text)
    if case.get("empty"):
        assert findings == [], f"[{name}] {findings}"
    if case.get("nonempty"):
        assert findings, f"[{name}] the findings must not be empty"
    for e in case.get("elements", []):
        assert e in findings, f"[{name}] {e!r} not in {findings}"
    for s in case.get("has", []):
        assert any(s in f for f in findings), f"[{name}] no finding contains {s!r}: {findings}"
    for s in case.get("lacks", []):
        assert not any(s in f for f in findings), f"[{name}] a finding contains {s!r}: {findings}"
    tally = _critique_tally(text)
    if "tally_counts" in case:
        assert (len(tally.criticals), len(tally.significants)) == case["tally_counts"], name
    if "n_findings" in case:
        assert len(findings) == case["n_findings"], f"[{name}] {findings}"
    if case.get("tally_titles"):
        for title in tally.criticals + tally.significants:
            assert any(title in f for f in findings), f"[{name}] {title!r} missing from {findings}"


def _strict(title: str) -> str:
    from nexus.commands.rdr import _finding_title_key

    return _finding_title_key(title)


def _loose(title: str) -> str:
    from nexus.commands.rdr import _finding_title_key_loose

    return _finding_title_key_loose(title)


#: (case, key function, title a, title b, keys equal)
_TITLE_KEY_CASES = [
    # nexus-yjf5l.7: a `residuals:` line carries a class tag that a
    # `_critique_findings` title ("Issue: <title>") never has; one
    # normalisation must still equate them.
    ("discover_tag_equals_the_issue_prefix", _strict,
     "[DISCOVER-AT-IMPLEMENTATION] Some Title", "Issue: Some Title", True),
    ("blocks_planning_tag_equals_the_bare_title", _strict,
     "[BLOCKS-PLANNING] Some Title", "Some Title", True),
    # nexus-yjf5l.18 (F1): the class-strip is built from the two real classes,
    # not a generic ``[A-Z][A-Z-]*`` shape; a title genuinely beginning ``[SQL]``
    # keeps its bracket.
    ("non_class_bracket_is_not_stripped", _strict,
     "[SQL] query builder allows injection", "query builder allows injection", False),
    ("non_class_bracket_equals_itself", _strict,
     "[SQL] query builder allows injection", "[SQL] query builder allows injection", True),
    ("loose_key_keeps_the_file_path", _loose,
     "Off-by-one in the sweep at foo.py:120", "Off-by-one in the sweep at bar.py:340", False),
    ("loose_key_drops_only_the_line", _loose,
     "Off-by-one in the sweep at foo.py:120", "Off-by-one in the sweep at foo.py:121", True),
]


@pytest.mark.parametrize("case, key, a, b, equal", _TITLE_KEY_CASES, ids=[c[0] for c in _TITLE_KEY_CASES])
def test_finding_title_keys(case, key, a, b, equal):
    assert (key(a) == key(b)) is equal, f"[{case}] {key(a)!r} vs {key(b)!r}"


@pytest.mark.parametrize("case, line, expected", [
    ("a_non_class_bracket_defaults_to_blocks_planning", "[SQL] query builder allows injection",
     (BLOCKS_PLANNING, "[SQL] query builder allows injection")),
    ("a_real_class_tag_is_split_off", f"[{DISCOVER_AT_IMPLEMENTATION}] a title",
     (DISCOVER_AT_IMPLEMENTATION, "a title")),
], ids=["non_class_bracket", "real_class_tag"])
def test_residual_class_tag_is_scoped_to_the_two_classes(case, line, expected):
    from nexus.commands.rdr import _residual_class_and_title

    assert _residual_class_and_title(line) == expected, case


def test_loose_match_is_refused_when_ambiguous():
    from nexus.commands.rdr import _loose_unique_index

    idx = _loose_unique_index([
        "the round 3 residuals count is off by 1",
        "the round 5 residuals count is off by 1",
        "an unrelated title",
    ])
    assert "an unrelated title" in idx.values()
    assert not any("off by" in v for v in idx.values()), (
        "two titles sharing every non-digit word must not resolve to either"
    )
