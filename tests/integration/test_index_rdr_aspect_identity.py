# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""shakedown 7.89.0 umbrella class e: ``nx index rdr <dir>`` outside a git repo.

A document outside any repo registers in the catalog by its ABSOLUTE path (a curator owner,
nexus-njn1a), but the index-time post-store hook was handed the path RELATIVE to the directory
(``docs/rdr/rdr-001-alpha.md``). The aspect queue row therefore keyed on a path the catalog does not
hold: ``aspect_source_path_uncanonical`` at index time, ``aspect_uri_for_relative_without_root`` in the
worker, and a second aspect row beside the one ``nx enrich aspects`` writes under the absolute path.

Real engine substrate; the aspect worker stays off (``NX_ASPECT_WORKER_AUTOSTART=0``), so the queue
row the hook wrote is what is read back.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database
from tests._catalog_fixture_ops import documents_by_file_path

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


def _project(root: Path) -> list[Path]:
    rdr = root / "docs" / "rdr"
    rdr.mkdir(parents=True)
    paths = []
    for n, word in ((1, "alpha"), (2, "beta")):
        p = rdr / f"rdr-00{n}-{word}.md"
        p.write_text(
            f"---\ntitle: RDR {n} {word}\nstatus: draft\n---\n\n# RDR {n} {word}\n\n"
            f"Consistent hashing places keys on a ring of {word} nodes.\n"
        )
        paths.append(p)
    return paths


def test_index_rdr_outside_a_git_repo_queues_the_catalog_identity(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    proj = tmp_path / "proj"
    files = _project(proj)

    res = CliRunner().invoke(main, ["index", "rdr", str(proj)])
    assert res.exit_code == 0, res.output

    identities = set()
    for f in files:
        docs = documents_by_file_path(str(f.resolve()))
        assert len(docs) == 1, f"catalog identity for {f}: {docs}"
        identities.add(docs[0].file_path)
    assert all(Path(i).is_absolute() for i in identities), identities

    with T2Database(tmp_path / "unused.db") as db:
        queued = [r.source_path for r in db.aspect_queue.list_pending()]
    assert sorted(queued) == sorted(identities), (
        "one queue row per document, keyed on the path the catalog holds"
    )


def test_index_rdr_inside_a_git_repo_still_queues_the_repo_relative_identity(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the contract: a repo document registers relative to its repo root, and
    the queue row keeps that spelling (the fix must not push every document to an absolute key)."""
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    proj = tmp_path / "repo"
    files = _project(proj)
    for args in (["init", "-q"], ["add", "docs"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)

    res = CliRunner().invoke(main, ["index", "rdr", str(proj)])
    assert res.exit_code == 0, res.output

    with T2Database(tmp_path / "unused.db") as db:
        queued = sorted(r.source_path for r in db.aspect_queue.list_pending())
    assert queued == sorted(f"docs/rdr/{f.name}" for f in files)
