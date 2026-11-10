# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 7 (nexus-u5m5g.8): the index-path link generators, counted at the HTTP layer.

The bar (the bead's residual 5, restating "at most one scoped read and one write per
generator"):

- each file-path linker makes ZERO list reads and one ``links_upsert_many`` write per 1000 edges;
- the dependency linker makes TWO scoped list reads (the current owner's rdr and prose rows, and
  the rows under its ``docs/rdr/`` source prefix) and one write;
- the PDF linker makes ONE ``links_same_content`` call;
- ``resolve_path`` and ``resolve`` probes are counted and reported, not capped.

Plus: no ``GET /list?content_type=`` without an owner on the index path (residual 4 of the
design: the tenant-wide type listings are gone), and the generators take their inputs (a new
tumbler's file path and content type) from the hook's ``file_to_doc_id`` and staged rows with no
catalog read (residual 6).

Requests are counted at the transport: a real ``HttpCatalogClient`` over the fake engine in
``tests/catalog/_link_engine_fake.py``, never a patched generator or client method.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.factory import _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.link_generator import (
    HookLinkInputs,
    generate_pdf_corpus_links,
    generate_prose_filepath_links,
    generate_rdr_dependency_links,
    generate_rdr_filepath_links,
    hook_link_inputs,
    relink_changed_pdfs,
)
from nexus.catalog.rdr_canonical import rdr_source_prefix
from nexus.catalog.tumbler import Tumbler
from tests.catalog._link_engine_fake import State, start_server

OWNER = "1.1"


@pytest.fixture
def fake_server():
    State.reset()
    server, url = start_server()
    yield url
    server.shutdown()


@pytest.fixture
def reader(fake_server: str):
    with HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok") as c:
        yield c


@pytest.fixture
def writer(fake_server: str):
    w = _ServiceCatalogWriter(HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok"))
    yield w
    w.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / "docs" / "rdr").mkdir(parents=True)
    State.set_owner(OWNER, repo_root=str(root))
    return root


def _code(tumbler: str, repo: Path, rel: str) -> None:
    State.add_document(
        tumbler, title=rel, content_type="code", file_path=rel, source_uri=f"file://{repo / rel}",
    )


def _source(tumbler: str, repo: Path, rel: str, content_type: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    State.add_document(
        tumbler, title=rel, content_type=content_type, file_path=rel, source_uri=f"file://{f}",
    )


def _inputs(*docs: tuple[str, str, str]) -> HookLinkInputs:
    return HookLinkInputs(owner=OWNER, docs={t: (fp, ct) for t, fp, ct in docs})


def _probes() -> int:
    """resolve_path's own round trips: the document read and the (cached) owner read."""
    return State.count("GET", "/show") + State.count("GET", "/owners/show")


class TestFilepathLinkersMakeNoListReads:
    def test_rdr_linker_zero_lists_one_upsert_many(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _code("1.1.2", repo, "src/nexus/b.py")
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", "See `src/nexus/a.py` and `src/nexus/b.py`.\n")
        _source("1.1.4", repo, "docs/rdr/rdr-002-y.md", "rdr", "See `src/nexus/a.py`.\n")
        State.requests.clear()

        count = generate_rdr_filepath_links(
            reader, writer=writer,
            new_tumblers=[Tumbler.parse("1.1.3"), Tumbler.parse("1.1.4")],
            new_content_types={"rdr", "code"},
            inputs=_inputs(
                ("1.1.3", "docs/rdr/rdr-001-x.md", "rdr"), ("1.1.4", "docs/rdr/rdr-002-y.md", "rdr"),
                ("1.1.1", "src/nexus/a.py", "code"), ("1.1.2", "src/nexus/b.py", "code"),
            ),
        )

        assert count == 3
        assert State.lists() == [], "a file-path linker on the index path lists nothing"
        assert len(State.posts("/links/upsert_many")) == 1
        # two sources read once each; the owner is read once and cached (reported, not capped)
        assert _probes() == 3
        assert {(lnk["from_tumbler"], lnk["to_tumbler"]) for lnk in State.links} == {
            ("1.1.3", "1.1.1"), ("1.1.3", "1.1.2"), ("1.1.4", "1.1.1"),
        }

    def test_edges_name_the_run_owner_and_the_path_not_a_looked_up_tumbler(
        self, reader, writer, repo,
    ) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", "See `src/nexus/a.py`.\n")
        State.requests.clear()
        generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")],
            inputs=_inputs(("1.1.3", "docs/rdr/rdr-001-x.md", "rdr")),
        )
        (body,) = State.posts("/links/upsert_many")
        assert body["edges"] == [{
            "from": {"tumbler": "1.1.3"},
            "to": {"owner": OWNER, "file_path": "src/nexus/a.py"},
            "link_type": "implements",
            "created_by": "filepath_extractor",
        }]

    def test_a_source_under_another_owner_names_the_hook_run_owner_not_its_own(
        self, reader, writer, repo,
    ) -> None:
        """On the hook path the path edge names the run's owner (``inputs.owner``), not the owner of
        the source's tumbler: a source registered under a stale owner (here 1.2) still links into the
        repository the run indexes (1.1). Deriving the owner from the source tumbler goes red."""
        State.set_owner("1.2", repo_root=str(repo))
        _code("1.1.1", repo, "src/nexus/a.py")
        f = repo / "docs/rdr/rdr-001-x.md"
        f.write_text("See `src/nexus/a.py`.\n")
        State.add_document(
            "1.2.3", title="rdr", content_type="rdr", file_path="docs/rdr/rdr-001-x.md",
            source_uri=f"file://{f}",
        )
        State.requests.clear()
        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.2.3")],
            inputs=_inputs(("1.2.3", "docs/rdr/rdr-001-x.md", "rdr")),
        )
        (body,) = State.posts("/links/upsert_many")
        assert body["edges"][0]["from"] == {"tumbler": "1.2.3"}
        assert body["edges"][0]["to"] == {"owner": OWNER, "file_path": "src/nexus/a.py"}
        assert count == 1

    def test_a_seed_the_hook_holds_no_row_for_takes_the_run_owner_too(
        self, reader, writer, repo, tmp_path: Path,
    ) -> None:
        """``_seed_docs``' fallback for a tumbler the hook has no row for (one ``resolve_many``) is
        a document of THIS run: it names ``inputs.owner``, not the repository owner a ``source_uri``
        lookup would find (here the stale owner 1.2, whose root holds the file)."""
        stale_root = (tmp_path / "stale").resolve()
        (stale_root / "docs/rdr").mkdir(parents=True)
        State.set_owner("1.2", repo_root=str(stale_root))
        _code("1.1.1", repo, "src/nexus/a.py")
        f = stale_root / "docs/rdr/rdr-001-x.md"
        f.write_text("See `src/nexus/a.py`.\n")
        State.add_document(
            "1.2.3", title="rdr", content_type="rdr", file_path="docs/rdr/rdr-001-x.md",
            source_uri=f"file://{f}",
        )
        State.requests.clear()
        generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.2.3")], inputs=_inputs(),
        )
        assert State.posts("/resolve_many"), "the seed had no hook row: one resolve_many probe"
        (body,) = State.posts("/links/upsert_many")
        assert body["edges"][0]["to"]["owner"] == OWNER

    def test_the_rdr_linker_skips_when_no_new_document_is_an_rdr_even_without_a_row(
        self, reader, writer, repo,
    ) -> None:
        """The seed predicate is the only thing that keeps the linker from probing a seed the hook
        holds no row for. Removing ``_no_qualifying_seed`` from the RDR linker makes this one
        ``resolve_many`` read."""
        _code("1.1.1", repo, "src/nexus/a.py")
        State.requests.clear()
        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1")], new_content_types={"code"},
            inputs=_inputs(),
        )
        assert count == 0
        assert State.requests == []

    def test_a_path_the_engine_cannot_resolve_is_sent_and_not_counted(self, reader, writer, repo) -> None:
        """The client no longer knows which paths exist (that read is gone); the engine says."""
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", "See `src/nexus/missing.py`.\n")
        State.requests.clear()
        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")],
            inputs=_inputs(("1.1.3", "docs/rdr/rdr-001-x.md", "rdr")),
        )
        assert count == 0
        assert State.lists() == []
        assert len(State.posts("/links/upsert_many")) == 1
        assert State.links == []

    def test_one_write_per_thousand_edges(self, reader, writer, repo) -> None:
        text = "".join(f"`src/nexus/m{i}.py`\n" for i in range(1001))
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", text)
        State.requests.clear()
        generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")],
            inputs=_inputs(("1.1.3", "docs/rdr/rdr-001-x.md", "rdr")),
        )
        sizes = [len(b["edges"]) for b in State.posts("/links/upsert_many")]
        assert sizes == [1000, 1]
        assert State.lists() == []

    def test_a_new_code_document_is_not_a_source_and_costs_nothing(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        State.requests.clear()
        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1")], new_content_types={"code"},
            inputs=_inputs(("1.1.1", "src/nexus/a.py", "code")),
        )
        assert count == 0
        assert State.requests == []

    def test_prose_linker_zero_lists_one_upsert_many(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.5", repo, "docs/guide.md", "prose", "Run ``src/nexus/a.py``.\n")
        State.requests.clear()
        count = generate_prose_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.5")], new_content_types={"prose"},
            inputs=_inputs(("1.1.5", "docs/guide.md", "prose")),
        )
        assert count == 1
        assert State.lists() == []
        assert len(State.posts("/links/upsert_many")) == 1
        assert _probes() == 2

    def test_prose_linker_accepts_each_prose_source_type(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        for t, ct in (("1.1.5", "prose"), ("1.1.6", "markdown"), ("1.1.7", "docs")):
            _source(t, repo, f"docs/{ct}.md", ct, "Run ``src/nexus/a.py``.\n")
        State.requests.clear()
        count = generate_prose_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse(t) for t in ("1.1.5", "1.1.6", "1.1.7")],
            inputs=_inputs(
                ("1.1.5", "docs/prose.md", "prose"), ("1.1.6", "docs/markdown.md", "markdown"),
                ("1.1.7", "docs/docs.md", "docs"),
            ),
        )
        assert count == 3
        assert len(State.posts("/links/upsert_many")) == 1
        assert State.lists() == []


class TestDependencyLinkerReads:
    def _rdr(self, tumbler: str, repo: Path, n: int, front: str = "", *, owner_root: bool = True) -> None:
        text = f"---\nid: RDR-{n:03d}\n{front}---\n# RDR-{n:03d}\n"
        _source(tumbler, repo, f"docs/rdr/rdr-{n:03d}-t.md", "rdr", text)

    def test_two_scoped_reads_and_one_write(self, reader, writer, repo) -> None:
        self._rdr("1.1.3", repo, 1, "related_rdrs: [RDR-002]\n")
        self._rdr("1.1.4", repo, 2)
        State.requests.clear()
        count = generate_rdr_dependency_links(
            reader, writer=writer, current_owner=Tumbler.parse(OWNER),
            repo_source_prefix=rdr_source_prefix(repo),
            new_tumblers=[Tumbler.parse("1.1.3")], new_content_types={"rdr"},
            inputs=_inputs(("1.1.3", "docs/rdr/rdr-001-t.md", "rdr")),
        )
        assert count == 1
        lists = State.lists()
        assert len(lists) == 2, lists
        by_owner, by_prefix = lists
        assert by_owner["owner"] == [OWNER]
        assert sorted(by_owner["content_type"]) == ["prose", "rdr"]
        assert "source_uri_prefix" not in by_owner
        assert by_prefix["source_uri_prefix"] == [rdr_source_prefix(repo)]
        assert len(State.posts("/links/upsert_many")) == 1
        (body,) = State.posts("/links/upsert_many")
        assert body["edges"] == [{
            "from": {"tumbler": "1.1.3"}, "to": {"tumbler": "1.1.4"},
            "link_type": "relates", "created_by": "rdr_dependency_extractor",
        }]
        assert not [p for p in lists if "content_type" in p and "owner" not in p and "source_uri_prefix" not in p]

    def test_a_target_registered_under_a_stale_owner_is_found_by_the_prefix_read(
        self, reader, writer, repo,
    ) -> None:
        State.set_owner("1.2", repo_root=str(repo))
        self._rdr("1.1.3", repo, 1, "supersedes: RDR-002\n")
        self._rdr("1.2.9", repo, 2)  # stale owner 1.2, but its source_uri is under this repo's docs/rdr/
        State.requests.clear()
        count = generate_rdr_dependency_links(
            reader, writer=writer, current_owner=Tumbler.parse(OWNER),
            repo_source_prefix=rdr_source_prefix(repo),
            new_tumblers=[Tumbler.parse("1.1.3")], new_content_types={"rdr"},
            inputs=_inputs(("1.1.3", "docs/rdr/rdr-001-t.md", "rdr")),
        )
        assert count == 1
        assert len(State.lists()) == 2
        assert [(lnk["from_tumbler"], lnk["to_tumbler"], lnk["link_type"]) for lnk in State.links] == [
            ("1.1.3", "1.2.9", "supersedes"),
        ]

    def test_a_batch_with_no_rdr_keyed_file_reads_nothing(self, reader, writer, repo) -> None:
        """The seed check runs on the hook's file paths: no probe, no list, no write."""
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.5", repo, "docs/guide.md", "prose", "# guide\n")
        State.requests.clear()
        count = generate_rdr_dependency_links(
            reader, writer=writer, current_owner=Tumbler.parse(OWNER),
            repo_source_prefix=rdr_source_prefix(repo),
            new_tumblers=[Tumbler.parse("1.1.5")], new_content_types={"prose"},
            inputs=_inputs(("1.1.5", "docs/guide.md", "prose")),
        )
        assert count == 0
        assert State.requests == []


class TestPdfLinker:
    def _pdfs(self) -> None:
        State.add_document("1.1.1", content_type="pdf", index_content_hash="h", physical_collection="docs__a")
        State.add_document("1.1.2", content_type="pdf", index_content_hash="h", physical_collection="knowledge__b")
        State.add_document("1.1.7", content_type="code")

    def test_one_same_content_call_no_lists_no_probes(self, reader, writer) -> None:
        self._pdfs()
        State.requests.clear()
        count = generate_pdf_corpus_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.1"), Tumbler.parse("1.1.2"), Tumbler.parse("1.1.7")],
            new_content_types={"pdf", "code"},
            inputs=_inputs(("1.1.1", "a.pdf", "pdf"), ("1.1.2", "b.pdf", "pdf"), ("1.1.7", "src/x.py", "code")),
        )
        assert count == 1
        assert State.requests == [
            ("POST", "/links/same_content", {"tumblers": ["1.1.1", "1.1.2"], "prune": False}),
        ], "one call; the code document is not sent"

    def test_relink_changed_pdfs_is_one_prune_call(self, reader, writer) -> None:
        self._pdfs()
        State.links.append({
            "from_tumbler": "1.1.2", "to_tumbler": "1.1.1", "link_type": "same-as",
            "created_by": "content_hash_dedup", "from_span": "", "to_span": "",
        })
        State.documents["1.1.2"]["index_content_hash"] = "new-bytes"
        State.requests.clear()
        assert relink_changed_pdfs(reader, writer=writer, changed={"1.1.2"}) == (1, 0)
        assert State.requests == [
            ("POST", "/links/same_content", {"tumblers": ["1.1.2"], "prune": True}),
        ]

    def test_relink_changed_pdfs_with_nothing_changed_is_free(self, reader, writer) -> None:
        State.requests.clear()
        assert relink_changed_pdfs(reader, writer=writer, changed=set()) == (0, 0)
        assert State.requests == []

    def test_relink_pages_the_changed_set_at_a_thousand(self, reader, writer) -> None:
        State.requests.clear()
        relink_changed_pdfs(reader, writer=writer, changed={f"1.1.{i}" for i in range(1, 2002)})
        sizes = [len(b["tumblers"]) for b in State.posts("/links/same_content")]
        assert sizes == [1000, 1000, 1]


class TestTheIndexPathNeverListsTheTenant:
    def test_no_content_type_list_without_an_owner_or_prefix_across_every_generator(
        self, reader, writer, repo,
    ) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", "---\nid: RDR-001\nrelated_rdrs: [RDR-002]\n---\n`src/nexus/a.py`\n")
        _source("1.1.4", repo, "docs/rdr/rdr-002-y.md", "rdr", "---\nid: RDR-002\n---\n# two\n")
        _source("1.1.5", repo, "docs/guide.md", "prose", "``src/nexus/a.py``\n")
        State.add_document("1.1.8", content_type="pdf", index_content_hash="h")
        State.add_document("1.1.9", content_type="pdf", index_content_hash="h")
        docs = (
            ("1.1.1", "src/nexus/a.py", "code"), ("1.1.3", "docs/rdr/rdr-001-x.md", "rdr"),
            ("1.1.4", "docs/rdr/rdr-002-y.md", "rdr"), ("1.1.5", "docs/guide.md", "prose"),
            ("1.1.8", "a.pdf", "pdf"), ("1.1.9", "b.pdf", "pdf"),
        )
        seeds = [Tumbler.parse(t) for t, _, _ in docs]
        kinds = {ct for _, _, ct in docs}
        inputs = _inputs(*docs)
        State.requests.clear()

        total = 0
        for gen, extra in (
            (generate_rdr_filepath_links, {}),
            (generate_prose_filepath_links, {}),
            (generate_pdf_corpus_links, {}),
            (generate_rdr_dependency_links, {
                "current_owner": Tumbler.parse(OWNER), "repo_source_prefix": rdr_source_prefix(repo),
            }),
        ):
            total += gen(reader, writer=writer, new_tumblers=seeds, new_content_types=kinds, inputs=inputs, **extra)
        total += sum(relink_changed_pdfs(reader, writer=writer, changed={"1.1.8"}))

        assert total >= 4, "the run must have linked something, or the zero below proves nothing"
        for params in State.lists():
            assert "owner" in params or "source_uri_prefix" in params, (
                f"an unscoped list read on the index path: {params}"
            )
        assert len(State.lists()) == 2, "only the dependency linker's two scoped reads"


class TestGeneratorInputsComeFromTheHook:
    def test_hook_link_inputs_is_built_from_the_runs_own_rows_with_no_catalog(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        a, b, c = repo / "docs/rdr/rdr-001-x.md", repo / "src/a.py", repo / "docs/rdr/rdr-009-z.md"
        file_to_doc_id = {a: "1.1.3", b: "1.1.1", c: "1.1.8"}
        rel_to_abs = {"docs/rdr/rdr-001-x.md": a, "src/a.py": b, "docs/rdr/rdr-009-z.md": c}
        staged = {
            "docs/rdr/rdr-001-x.md": {"content_type": "rdr", "file_path": "docs/rdr/rdr-001-x.md"},
            "src/a.py": {"content_type": "code", "file_path": "src/a.py"},
            "docs/rdr/rdr-009-z.md": {"content_type": "rdr", "file_path": "docs/rdr/rdr-009-z.md"},
        }
        got = hook_link_inputs(
            "1.1", file_to_doc_id=file_to_doc_id, rel_to_abs=rel_to_abs, staged=staged,
            seeds=[Tumbler.parse("1.1.3"), Tumbler.parse("1.1.1")],
        )
        assert got == HookLinkInputs(owner="1.1", docs={
            "1.1.3": ("docs/rdr/rdr-001-x.md", "rdr"), "1.1.1": ("src/a.py", "code"),
        })

    def test_a_seed_with_no_staged_row_is_left_out_not_guessed(self) -> None:
        got = hook_link_inputs(
            "1.1", file_to_doc_id={}, rel_to_abs={}, staged={}, seeds=[Tumbler.parse("1.1.3")],
        )
        assert got.docs == {}

    def test_generators_make_no_catalog_read_for_their_own_inputs(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", "`src/nexus/a.py`\n")
        State.requests.clear()
        generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")], new_content_types={"rdr"},
            inputs=_inputs(("1.1.3", "docs/rdr/rdr-001-x.md", "rdr")),
        )
        assert State.count("POST", "/resolve_many") == 0
        assert State.lists() == []


class TestFullPassKeepsItsListingsAndDropsPathToCode:
    """``nx catalog generate-links`` is a bulk job: it lists its source documents, by type. It no
    longer lists the code documents (the tenant-wide ``path_to_code`` dict is deleted everywhere)."""

    def test_rdr_full_pass_lists_rdrs_only_and_sends_path_edges(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.3", repo, "docs/rdr/rdr-001-x.md", "rdr", "`src/nexus/a.py`\n")
        State.requests.clear()
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 1
        types = [p.get("content_type") for p in State.lists()]
        assert types == [["rdr"]], f"the source listing only, never the code listing: {types}"
        (body,) = State.posts("/links/upsert_many")
        assert body["edges"][0]["to"] == {"owner": OWNER, "file_path": "src/nexus/a.py"}

    def test_prose_full_pass_never_lists_code(self, reader, writer, repo) -> None:
        _code("1.1.1", repo, "src/nexus/a.py")
        _source("1.1.5", repo, "docs/guide.md", "prose", "``src/nexus/a.py``\n")
        State.requests.clear()
        assert generate_prose_filepath_links(reader, writer=writer) == 1
        assert ["code"] not in [p.get("content_type") for p in State.lists()]
