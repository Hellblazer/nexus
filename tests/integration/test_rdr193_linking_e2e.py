# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 7 (nexus-u5m5g.8): the linking client end to end against the real engine.

The Test Plan scenarios of Part A4:

* an RDR names a path that exists in this repository and in another one: the ``implements`` link targets THIS
  repository's document. The old client resolved the path through a tenant-wide ``path_to_code`` dict in which the last
  listed document won, so the link went to the other repository's file whenever that one registered later. The engine
  resolves ``{owner, file_path}`` by the file's identity, inside the named repository;
* two PDFs with the same content in different collections get ONE ``same-as`` link, to the text-least tumbler: the
  anchor rule is ``min(str(tumbler))``, so ``1.N.10`` anchors ``1.N.9`` (a numeric order would re-anchor existing groups);
* the client's same-as prune (``prune_stale_same_as_links``, still behind ``nx catalog generate-links``) and the engine's
  (``links_same_content(prune=True)``, the index path) retract the same links on one fixture;
* the real index hook hands the generators its own rows (``HookLinkInputs``) and, in the linking window, lists no type
  tenant-wide and writes the path edges through ONE ``links_upsert_many`` call.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import nexus.catalog.link_generator as link_generator
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.link_generator import generate_pdf_corpus_links, prune_stale_same_as_links
from nexus.catalog.tumbler import Tumbler
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.db.t3 import T3Database
from nexus.repo_identity import _repo_identity
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 - fixtures and helpers shared with the fence tests
    _commit,
    _index,
    _init_repo,
    _prime_owner,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


def _owner_of(repo: Path) -> Tumbler:
    owner = make_catalog_reader().owner_for_repo(_repo_identity(repo)[1])
    assert owner is not None, f"{repo} has no catalog owner"
    return owner


def _doc(repo: Path, file_path: str):
    cat = make_catalog_reader()
    matches = [e for e in cat.by_owner(_owner_of(repo)) if e.file_path == file_path]
    assert len(matches) == 1, f"expected one live document at {file_path} in {repo}, got {matches}"
    return matches[0]


def _two_repos_sharing_a_path(
    tmp_path: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Repository A is indexed with ``src/shared.py``, then repository B (registered LATER, so its document is the
    last one listed for that path) with the same path. Returns ``(a, b)``."""
    repos = []
    for name in ("repo_a", "repo_b"):
        repo = tmp_path / name
        _init_repo(repo)
        _prime_owner(repo, t3, monkeypatch)
        (repo / "src").mkdir()
        (repo / "src" / "shared.py").write_text(f"def shared():\n    return '{name}'\n", encoding="utf-8")
        _commit(repo)
        _index(repo, t3, monkeypatch)
        repos.append(repo)
    return repos[0], repos[1]


def _add_rdr_naming_the_shared_path(repo: Path) -> None:
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "rdr-001-shared.md").write_text(
        "---\nid: RDR-001\ntitle: Shared\nstatus: draft\n---\n\n# Shared\n\nThe change lands in `src/shared.py`.\n",
        encoding="utf-8",
    )
    _commit(repo)


def test_an_rdr_naming_a_path_held_by_two_repositories_links_this_repositorys_document(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a, repo_b = _two_repos_sharing_a_path(tmp_path, local_t3, monkeypatch)
    mine, theirs = _doc(repo_a, "src/shared.py"), _doc(repo_b, "src/shared.py")
    assert mine.tumbler != theirs.tumbler and str(theirs.tumbler) > str(mine.tumbler), (
        "non-vacuity: two documents at one path, the other repository's the last one registered"
    )

    _add_rdr_naming_the_shared_path(repo_a)
    _index(repo_a, local_t3, monkeypatch)

    rdr = _doc(repo_a, "docs/rdr/rdr-001-shared.md")
    assert rdr.content_type == "rdr"
    targets = {
        str(lnk.to_tumbler)
        for lnk in make_catalog_reader().links_from(rdr.tumbler, link_type="implements")
        if lnk.created_by == "filepath_extractor"
    }
    assert targets == {str(mine.tumbler)}, (
        f"the link must target this repository's src/shared.py ({mine.tumbler}), never the other repository's "
        f"({theirs.tumbler}): {targets}"
    )


def test_the_hook_hands_the_generators_its_rows_lists_nothing_tenant_wide_and_writes_one_upsert_many(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a, _repo_b = _two_repos_sharing_a_path(tmp_path, local_t3, monkeypatch)
    _add_rdr_naming_the_shared_path(repo_a)

    window = {"open": 0}
    seen: list[tuple[str, str, dict]] = []  # (method, path, params) while a generator runs
    handed: dict[str, link_generator.HookLinkInputs | None] = {}
    real_send = RefreshableHttpStoreMixin._send

    def spy(self, method, path, **kw):  # noqa: ANN001
        if window["open"]:
            seen.append((method, path, dict(kw.get("params") or {})))
        return real_send(self, method, path, **kw)

    monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", spy)

    def wrap(name: str):
        real = getattr(link_generator, name)

        def inside(cat, **kw):  # noqa: ANN001
            handed[name] = kw.get("inputs")
            window["open"] += 1
            try:
                return real(cat, **kw)
            finally:
                window["open"] -= 1

        monkeypatch.setattr(link_generator, name, inside)

    for name in ("generate_rdr_filepath_links", "generate_prose_filepath_links",
                 "generate_pdf_corpus_links", "generate_rdr_dependency_links"):
        wrap(name)

    _index(repo_a, local_t3, monkeypatch)

    rdr = _doc(repo_a, "docs/rdr/rdr-001-shared.md")
    inputs = handed["generate_rdr_filepath_links"]
    assert inputs is not None, "the hook passed the generator no inputs"
    assert inputs.owner == str(_owner_of(repo_a))
    assert inputs.docs[str(rdr.tumbler)] == ("docs/rdr/rdr-001-shared.md", "rdr"), inputs.docs

    assert seen, "non-vacuity: the generators made requests, or the zero below proves nothing"
    lists = [p for m, path, p in seen if m == "GET" and path == "/v1/catalog/list"]
    for params in lists:
        assert params.get("owner") or params.get("source_uri_prefix"), (
            f"an unscoped list read on the index path: {params}"
        )
    assert [1 for m, path, _ in seen if m == "POST" and path == "/v1/catalog/links/upsert_many"], (
        "the path edges went through links_upsert_many"
    )
    assert not [1 for m, path, _ in seen if path in ("/v1/catalog/link", "/v1/catalog/link_query")], (
        "no per-link existence probe or write on the index path"
    )


def test_two_pdfs_with_the_same_content_in_different_collections_get_one_same_as_link_to_the_text_least_tumbler(
    tmp_path: Path, catalog_env: Path,
) -> None:
    cat = make_catalog_reader()
    writer = make_catalog_writer()
    try:
        owner = writer.register_owner("e2e_papers", "curator")
        # eight fillers with no content key, then the two PDFs as documents 9 and 10 of the owner: by text
        # "N.10" < "N.9", by number 9 < 10, so the rule decides which one anchors
        for i in range(8):
            writer.register(owner, f"filler-{i}", content_type="paper", physical_collection="knowledge__fill")
        nine = writer.register(
            owner, "one.pdf", content_type="pdf", meta={"content_hash": "same-bytes"}, physical_collection="knowledge__alpha",
        )
        ten = writer.register(
            owner, "two.pdf", content_type="pdf", meta={"content_hash": "same-bytes"}, physical_collection="docs__beta",
        )
        assert (str(nine).rsplit(".", 1)[1], str(ten).rsplit(".", 1)[1]) == ("9", "10"), (nine, ten)

        created = generate_pdf_corpus_links(
            cat, writer=writer, new_tumblers=[nine, ten], new_content_types={"pdf"},
        )

        assert created == 1
        same_as = [
            (str(lnk.from_tumbler), str(lnk.to_tumbler))
            for t in (nine, ten)
            for lnk in cat.links_from(t, link_type="same-as")
        ]
        assert same_as == [(str(nine), str(ten))], "one link, from the member to the text-least tumbler (the anchor)"
        assert cat.links_to(nine, link_type="same-as") == [], "the numerically-least tumbler is not the anchor"

        again = generate_pdf_corpus_links(cat, writer=writer, new_tumblers=[nine, ten], new_content_types={"pdf"})
        assert again == 0, "idempotent: the existing link is left as it is"
    finally:
        writer.close()


#: (label, content_type, content_hash) of the documents of one parity fixture. "" is a document with no key.
_PRUNE_DOCS = (
    ("m1", "pdf", "h1"), ("m2", "pdf", "h1"),    # same key: kept
    ("s1", "pdf", "h2"), ("s2", "pdf", "h3"),    # keys differ: retracted
    ("c1", "pdf", "h4"), ("c2", "pdf", "h5"),    # keys differ but co-asserted by another creator: kept
    ("n1", "pdf", ""), ("n2", "pdf", "h6"),      # one endpoint has no key: kept (unknown is not changed)
    ("r1", "prose", ""), ("r2", "pdf", "h7"),     # an endpoint is no longer a pdf: retracted
    ("x1", "pdf", "h8"), ("x2", "pdf", "h9"),    # keys differ, but another creator's link: never touched
)
#: (from, to, created_by, metadata) of the same-as links on the fixture.
_PRUNE_LINKS = (
    ("m2", "m1", "content_hash_dedup", {}),
    ("s2", "s1", "content_hash_dedup", {}),
    ("c2", "c1", "content_hash_dedup", {"co_discovered_by": "someone"}),
    ("n2", "n1", "content_hash_dedup", {}),
    ("r2", "r1", "content_hash_dedup", {}),
    ("x2", "x1", "someone_else", {}),
)


def _prune_fixture(cat, writer, name: str) -> tuple[dict[str, Tumbler], set[tuple[str, str, str]]]:
    """Register the fixture under its own owner. The engine groups documents tenant-wide, so each copy's
    content hashes carry its *name*: a hash shared across the two copies would link them to each other.
    Returns ``(tumbler by label, the same-as links as (from, to,
    created_by) label triples)``."""
    owner = writer.register_owner(f"e2e_prune_{name}", "curator")
    tumblers = {
        label: writer.register(
            owner, f"{label}.pdf", content_type=ct, physical_collection=f"knowledge__prune_{name}",
            meta={"content_hash": f"{h}-{name}"} if h else None,
        )
        for label, ct, h in _PRUNE_DOCS
    }
    for src, dst, creator, meta in _PRUNE_LINKS:
        writer.link(tumblers[src], tumblers[dst], "same-as", creator, **meta)
    return tumblers, _same_as(cat, tumblers)


def _same_as(cat, tumblers: dict[str, Tumbler]) -> set[tuple[str, str, str]]:
    label = {str(t): lbl for lbl, t in tumblers.items()}
    return {
        (label.get(str(lnk.from_tumbler), str(lnk.from_tumbler)), label.get(str(lnk.to_tumbler), str(lnk.to_tumbler)), lnk.created_by)
        for t in tumblers.values()
        for lnk in cat.links_from(t, link_type="same-as")
    }


def test_the_clients_same_as_prune_and_the_engines_retract_the_same_links_on_one_fixture(
    tmp_path: Path, catalog_env: Path,
) -> None:
    """Parity for the duplicated rule (RDR-193 Step 7, review of ceccd73cf): ``prune_stale_same_as_links`` is the client's
    copy of the engine's prune, kept for the maintenance verb. Two identical fixtures, one pruned by the client and one by
    ``links_same_content(prune=True)``: the retracted links must be the same ones, so the copies cannot drift apart
    unnoticed."""
    cat = make_catalog_reader()
    writer = make_catalog_writer()
    try:
        by_client, before_client = _prune_fixture(cat, writer, "client")
        by_engine, before_engine = _prune_fixture(cat, writer, "engine")
        assert before_client == before_engine and len(before_client) == len(_PRUNE_LINKS), (
            "non-vacuity: both fixtures start with all six links"
        )

        client_retracted = prune_stale_same_as_links(
            cat, writer=writer, scope={str(t) for t in by_client.values()},
        )
        reply = writer.links_same_content([str(t) for t in by_engine.values()], prune=True)

        after_client, after_engine = _same_as(cat, by_client), _same_as(cat, by_engine)
        removed_client, removed_engine = before_client - after_client, before_engine - after_engine
        assert removed_client == {
            ("s2", "s1", "content_hash_dedup"), ("r2", "r1", "content_hash_dedup"),
        }, f"the fixture must exercise the rule: {removed_client}"
        assert removed_client == removed_engine, (
            f"the client's prune and the engine's retracted different links: client {removed_client}, "
            f"engine {removed_engine}"
        )
        assert len(client_retracted) == int(reply["retracted"]) == len(removed_client)
        assert after_client == after_engine, "the two fixtures end with the same links (the engine created none)"
        assert int(reply.get("created", 0)) == 0
    finally:
        writer.close()
