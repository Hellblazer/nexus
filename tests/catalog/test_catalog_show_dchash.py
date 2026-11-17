# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 2: ``nx catalog show`` renders dchash spans and never fails on a bad one."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.dchash import dchash_status, text_hash
from nexus.cli import main
from tests._catalog_fixture_ops import ActiveCatalog, active_reader

PASSAGE = "a sentence the link quotes"
BODY = f"First line.\n{PASSAGE}, then the rest.\n"


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """As tests/test_catalog_cli.py: point NEXUS_CATALOG_PATH at tmp_path."""
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def initialized_catalog(catalog_env: Path) -> ActiveCatalog:
    """As tests/test_catalog_cli.py: the live catalog with one owner, 1.1."""
    cat = ActiveCatalog()
    cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
    return cat


@pytest.mark.t2_tenant
def test_show_and_the_live_curator_and_repo_legs(
    initialized_catalog: ActiveCatalog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tenant for both checks (tests/storage_touch_ceiling.toml).

    ``nx catalog show`` previews a good dchash link once and lists a stale one;
    then the real resolve_path: None for a curator (falls through to file://),
    repo_root for a repo, never the cwd.
    """
    src = tmp_path / "quoted.md"
    src.write_text(BODY)
    runner = CliRunner()
    assert (
        runner.invoke(
            main, ["catalog", "register", "--title", "A", "--owner", "1.1"]
        ).exit_code
        == 0
    )
    r = runner.invoke(
        main,
        [
            "catalog",
            "register",
            "--title",
            "B",
            "--owner",
            "1.1",
            "--file-path",
            str(src),
        ],
    )
    assert r.exit_code == 0, r.output
    start = BODY.index(PASSAGE)
    good = f"dchash:{text_hash(BODY)}:{start}-{start + len(PASSAGE)}"
    stale = f"dchash:{'0' * 64}:{start}-{start + len(PASSAGE)}"
    for link_type, span in (("quotes", good), ("cites", stale)):
        r = runner.invoke(
            main,
            [
                "catalog",
                "link",
                "1.1.1",
                "1.1.2",
                "--type",
                link_type,
                "--to-span",
                span,
            ],
        )
        assert r.exit_code == 0, r.output

    r = runner.invoke(main, ["catalog", "show", "1.1.1"])
    assert r.exit_code == 0, r.output
    assert f"(quotes) [{good}]" in r.output
    assert f"(cites) [{stale}]" in r.output
    assert f'"{PASSAGE}"' in r.output
    assert r.output.count(f'"{PASSAGE}"') == 1

    cat = initialized_catalog
    single = tmp_path / "single"
    single.mkdir()
    loose = single / "loose.md"
    loose.write_text(BODY)
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text(BODY)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "docs").mkdir(parents=True)
    (elsewhere / "docs" / "a.md").write_text("decoy\n")

    curator = cat.register_owner("dchash-curator", "curator")
    repo_owner = cat.register_owner(
        "dchash-repo", "repo", repo_hash="feedbeef", repo_root=str(repo)
    )
    t_cur = cat.register(
        curator, "loose", file_path=str(loose), source_uri=f"file://{loose}"
    )
    t_repo = cat.register(repo_owner, "a", file_path="docs/a.md")

    monkeypatch.chdir(elsewhere)
    span = f"dchash:{text_hash(BODY)}:{start}-{start + len(PASSAGE)}"
    assert cat.resolve_path(t_cur) is None
    for t in (t_cur, t_repo):
        assert cat.resolve_span_text(t, span) == PASSAGE
        assert dchash_status(cat.resolve(t), span, active_reader()) == "ok"
