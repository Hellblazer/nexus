# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5j8fp on the real engine: the indexer registers no document for a text file it will
not chunk because it holds only whitespace (the nexus-rqsh1 invariant, extended from zero-byte).

The measured population (T2 nexus/5j8fp-zero-chunk-population-2026-10-26): ghidra's 1-2 byte
``Module.manifest`` files fall through the classifier to PROSE and fail every run with "all
chunks empty after whitespace filtering"; serena's 1-byte ``__init__.py`` files are CODE and
``chunk_file`` returns ``[]``. Both passed the zero-byte guard (size > 0), registered, and never
completed their fence. They now leave discovery before registration, so the walk never offers
them to ``indexed_for_catalog``. A row an earlier run registered is tombstoned in the same run
through the nexus-d6qmz path (full and ``--since-head``), with housekeeping's miss-count sweep
as the fallback. ``.drawio`` (draw.io XML) is a SKIP
extension and takes the same path.

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
Real git, real catalog, real staleness decisions; the embedder is the shared local fake.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import documents_by_file_path
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 — fixtures and helpers shared with the fence tests
    _commit,
    _index,
    _init_repo,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)

pytestmark = [pytest.mark.integration]

_BLANK_FILES = {
    "ws/__init__.py": b"\n",                  # serena shape: CODE, 1 byte
    "mod/Module.manifest": b" \n",            # ghidra shape: unknown extension -> PROSE, 2 bytes
    "docs/blank.md": b"\n\n\t \n",            # PROSE
}
_REAL_FILES = {
    "src/real.py": b"def greet(name: str) -> str:\n    return f'hello {name}'\n",
    "docs/real.md": b"# Real\n\nGenuine prose content for indexing.\n",
}


def _seed(repo: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_whitespace_only_files_are_never_registered_and_real_files_are(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo, {**_BLANK_FILES, **_REAL_FILES})
    _commit(repo)

    _index(repo, local_t3, monkeypatch)

    for rel in _REAL_FILES:
        assert documents_by_file_path(rel), f"{rel} is real content and must register"
    for rel in _BLANK_FILES:
        assert documents_by_file_path(rel) == [], (
            f"{rel} holds only whitespace, yields no chunk, and must not register a document"
        )


def test_the_guard_is_not_called_for_a_file_over_the_cap(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk already holds each file's size; a file over WHITESPACE_ONLY_MAX_BYTES cannot be
    guard-skipped, so it costs no second stat or open."""
    from nexus import classifier

    repo = tmp_path / "repo"
    _init_repo(repo)
    big = b"".join(b"VALUE_%04d = %d\n" % (i, i) for i in range(400))
    assert len(big) > classifier.WHITESPACE_ONLY_MAX_BYTES
    _seed(repo, {**_BLANK_FILES, **_REAL_FILES, "src/big.py": big})
    _commit(repo)
    seen: list[str] = []
    real = classifier.is_whitespace_only_text

    def recording(path: Path, *a, **k) -> bool:
        seen.append(Path(path).name)
        return real(path, *a, **k)

    monkeypatch.setattr(classifier, "is_whitespace_only_text", recording)

    _index(repo, local_t3, monkeypatch)

    assert "big.py" not in seen, f"the guard was called for a file over the cap: {seen}"
    assert {"__init__.py", "Module.manifest", "blank.md"} <= set(seen), (
        f"the guard must still run for the small files, or this proves nothing: {seen}"
    )
    assert documents_by_file_path("src/big.py"), "the large real file must still register"


def test_a_whitespace_only_rdr_file_is_never_registered(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RDR walk is a separate discovery pass feeding the same registration."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo, {
        "docs/rdr/rdr-blank.md": b"\n",
        "docs/rdr/rdr-real.md": b"---\nstatus: accepted\n---\n\n# RDR: Real\n\nGenuine content.\n",
    })
    _commit(repo)

    _index(repo, local_t3, monkeypatch)

    assert documents_by_file_path("docs/rdr/rdr-real.md"), "a real RDR must still register"
    assert documents_by_file_path("docs/rdr/rdr-blank.md") == []


def test_a_drawio_file_is_never_registered(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo, {
        "maps/framework.drawio": (
            b'<mxfile><diagram name="p"><mxGraphModel><root><mxCell id="0"/></root>'
            b"</mxGraphModel></diagram></mxfile>\n"
        ),
        **_REAL_FILES,
    })
    _commit(repo)

    _index(repo, local_t3, monkeypatch)

    assert documents_by_file_path("maps/framework.drawio") == []
    assert documents_by_file_path("docs/real.md")


def _register_blank_files_unguarded(
    repo: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 1 with the guard off reproduces the pre-fix tree: the blank files register."""
    with monkeypatch.context() as m:
        m.setattr("nexus.classifier.is_whitespace_only_text", lambda *a, **k: False, raising=False)
        _index(repo, local_t3, monkeypatch)
    for rel in _BLANK_FILES:
        assert documents_by_file_path(rel), (
            f"{rel} must be registered by the unguarded run, or this test proves nothing"
        )


def test_a_previously_registered_whitespace_file_is_retired_in_the_same_full_run(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file still on disk and positively whitespace-only has no rename ambiguity, so its
    existing document is tombstoned in the run that skips it, through the same-run path
    nexus-d6qmz built for a present-but-excluded file, not left for housekeeping's two-run
    miss count."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo, {**_BLANK_FILES, **_REAL_FILES})
    _commit(repo)
    _register_blank_files_unguarded(repo, local_t3, monkeypatch)

    _index(repo, local_t3, monkeypatch)

    for rel in _BLANK_FILES:
        assert documents_by_file_path(rel) == [], f"{rel} must be retired by the first guarded run"
        assert (repo / rel).is_file()
    for rel in _REAL_FILES:
        assert documents_by_file_path(rel), f"{rel} is real content and must survive"


def test_a_previously_registered_whitespace_file_is_retired_in_a_since_head_run(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-commit hook runs ``--since-head``, which skips housekeeping entirely: a
    hook-only repo would otherwise never retire the row. A blanked file is in the delta, the
    guard skips it, and the same-run tombstone retires its document."""
    from tests.integration.test_a3rl7_since_head_skips_unchanged_delta import _head, _index_delta

    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo, {**_BLANK_FILES, **_REAL_FILES})
    _commit(repo)
    _register_blank_files_unguarded(repo, local_t3, monkeypatch)
    base = _head(repo)
    # The commit under the hook: each blank file is rewritten (still whitespace-only) and so
    # appears in the delta.
    for rel in _BLANK_FILES:
        (repo / rel).write_bytes(b"\n\n\n")
    _commit(repo)

    phases = _index_delta(repo, local_t3, monkeypatch, base=base)

    assert any(p.startswith("--since-head: 3 changed") for p in phases), (
        f"the delta must name the three blank files, or this test proves nothing: {phases}")
    for rel in _BLANK_FILES:
        assert documents_by_file_path(rel) == [], f"{rel} must be retired by the delta run"
    for rel in _REAL_FILES:
        assert documents_by_file_path(rel), f"{rel} is real content and must survive"


def test_housekeeping_still_retires_a_whitespace_file_when_the_same_run_path_finds_nothing(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback: with the same-run lookup returning nothing (a catalog read that failed
    is a safe empty set), the file's absence from discovery is counted twice and
    housekeeping's miss-count sweep deletes the document on the second consecutive miss."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo, {**_BLANK_FILES, **_REAL_FILES})
    _commit(repo)
    _register_blank_files_unguarded(repo, local_t3, monkeypatch)

    monkeypatch.setattr("nexus.indexer._stored_paths_among", lambda repo, paths: set())

    _index(repo, local_t3, monkeypatch)
    for rel in _BLANK_FILES:
        (entry,) = documents_by_file_path(rel)
        assert int((entry.meta or {}).get("miss_count", 0)) == 1, (
            f"{rel}: the first guarded run counts one miss and keeps the row (the grace window)"
        )
    _index(repo, local_t3, monkeypatch)

    for rel in _BLANK_FILES:
        assert documents_by_file_path(rel) == [], f"{rel} must be retired after two guarded runs"
        assert (repo / rel).is_file()
    for rel in _REAL_FILES:
        assert documents_by_file_path(rel), f"{rel} is real content and must survive the sweep"
