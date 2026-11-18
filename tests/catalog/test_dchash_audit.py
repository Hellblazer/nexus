# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 5: the stale dchash report (dchash_audit and link_audit's two keys)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from nexus.catalog.dchash import dchash_audit, dchash_status, text_hash
from nexus.catalog.dchash import source as dchash_source_mod
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, CatalogLink

BODY = "Intro line.\nthe passage we quote and more.\nEnd.\n"
GOOD = f"dchash:{text_hash(BODY)}:0-5"
OTHER_HASH = "b" * 64


def _entry(tumbler: str, file_path: str = "") -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler),
        title="t",
        author="",
        year=0,
        content_type="prose",
        file_path=file_path,
        corpus="",
        physical_collection="docs__1-2__bge-base-en-v15-768__v1",
        chunk_count=1,
        head_hash="",
        indexed_at="",
        source_uri="",
    )


def _link(
    frm: str, to: str, *, from_span: str = "", to_span: str = "", link_type: str = "cites"
) -> CatalogLink:
    return CatalogLink(
        from_tumbler=Tumbler.parse(frm),
        to_tumbler=Tumbler.parse(to),
        link_type=link_type,
        from_span=from_span,
        to_span=to_span,
        created_by="t",
        created_at="",
    )


class FakeCatalog:
    """resolve and resolve_path over a dict of entries; counts both."""

    def __init__(
        self, entries: dict[str, CatalogEntry], *, raises: BaseException | None = None,
        resolve_raises: BaseException | None = None,
    ) -> None:
        self.entries = entries
        self.raises = raises
        self.resolve_raises = resolve_raises
        self.resolve_calls: list[str] = []
        self.path_calls: list[str] = []

    def resolve(self, tumbler: Any) -> CatalogEntry | None:
        self.resolve_calls.append(str(tumbler))
        if self.resolve_raises is not None:
            raise self.resolve_raises
        return self.entries.get(str(tumbler))

    def resolve_path(self, tumbler: Any) -> Path | None:
        self.path_calls.append(str(tumbler))
        if self.raises is not None:
            raise self.raises
        entry = self.entries[str(tumbler)]
        return Path(entry.file_path) if entry.file_path else None


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every read_canonical call, in order."""
    seen: list[Path] = []
    real = dchash_source_mod.read_canonical

    def counting(path: Path) -> str:
        seen.append(path)
        return real(path)

    monkeypatch.setattr(dchash_source_mod, "read_canonical", counting)
    return seen


def _doc(tmp_path: Path, name: str, body: str = BODY) -> str:
    f = tmp_path / name
    f.write_text(body)
    return str(f)


def test_an_ok_span_produces_no_row(tmp_path: Path) -> None:
    cat = FakeCatalog({"1.1.1": _entry("1.1.1", _doc(tmp_path, "a.md"))})
    assert dchash_audit([_link("1.1.1", "1.1.1", from_span=GOOD, to_span=GOOD)], cat) == []


def test_links_without_a_dchash_span_cost_nothing(tmp_path: Path, reads: list[Path]) -> None:
    cat = FakeCatalog({})
    links = [
        _link("1.1.1", "1.1.2"),
        _link("1.1.1", "1.1.2", from_span="chash:" + "a" * 64, to_span="3-9"),
    ]
    assert dchash_audit(links, cat) == []
    assert cat.resolve_calls == [] and reads == []


def test_each_non_ok_status_reaches_the_row_with_its_side(tmp_path: Path) -> None:
    good_file = _doc(tmp_path, "good.md")
    bin_file = tmp_path / "bin.md"
    bin_file.write_bytes(b"\xff\xfe\x00bad")
    cat = FakeCatalog(
        {
            "1.1.1": _entry("1.1.1", good_file),  # reads fine
            "1.1.2": _entry("1.1.2", str(tmp_path / "gone.md")),  # file missing
            "1.1.3": _entry("1.1.3", str(bin_file)),  # not UTF-8
            "1.1.4": _entry("1.1.4"),  # no source path
        }
    )
    changed = f"dchash:{OTHER_HASH}:0-5"
    beyond = f"dchash:{text_hash(BODY)}:0-{len(BODY) + 1}"
    links = [
        _link("1.1.1", "1.1.9", from_span=changed, to_span=GOOD, link_type="a"),  # changed (from), 1.1.9 missing (to)
        _link("1.1.9", "1.1.1", from_span=GOOD, to_span=beyond, link_type="b"),  # missing (from), out_of_range (to)
        _link("1.1.2", "1.1.3", from_span=GOOD, to_span=GOOD, link_type="c"),  # unreadable both
        _link("1.1.4", "1.1.1", from_span=GOOD, to_span="dchash:oops", link_type="d"),  # unreadable (from), malformed (to)
    ]
    rows = dchash_audit(links, cat)
    got = {(r["link_type"], r["side"]): r["status"] for r in rows}
    assert got == {
        ("a", "from"): "changed",
        ("a", "to"): "missing",
        ("b", "from"): "missing",
        ("b", "to"): "out_of_range",
        ("c", "from"): "unreadable",
        ("c", "to"): "unreadable",
        ("d", "from"): "unreadable",
        ("d", "to"): "malformed",
    }
    assert len(rows) == 8
    row = next(r for r in rows if (r["link_type"], r["side"]) == ("a", "to"))
    assert row == {
        "from_tumbler": "1.1.1",
        "to_tumbler": "1.1.9",
        "link_type": "a",
        "side": "to",
        "span": GOOD,
        "status": "missing",
    }
    assert all(isinstance(v, str) for r in rows for v in r.values())


def test_the_audit_and_dchash_status_agree(tmp_path: Path) -> None:
    f = _doc(tmp_path, "a.md")
    entry = _entry("1.1.1", f)
    cat = FakeCatalog({"1.1.1": entry})
    spans = [
        GOOD,
        f"dchash:{OTHER_HASH}:0-5",
        f"dchash:{text_hash(BODY)}:0-9999",
        "dchash:nope",
    ]
    rows = dchash_audit([_link("1.1.1", "1.1.1", from_span=s) for s in spans], cat)
    by_span = {r["span"]: r["status"] for r in rows}
    for s in spans:
        want = dchash_status(entry, s, cat)
        assert by_span.get(s, "ok") == want


def test_a_document_with_many_spans_over_five_links_is_read_once(
    tmp_path: Path, reads: list[Path]
) -> None:
    cat = FakeCatalog(
        {
            "1.1.1": _entry("1.1.1", _doc(tmp_path, "a.md")),
            "1.1.2": _entry("1.1.2", _doc(tmp_path, "b.md")),
        }
    )
    changed = f"dchash:{OTHER_HASH}:0-5"
    links = [_link("1.1.1", "1.1.1", from_span=GOOD, to_span=changed) for _ in range(5)]
    links.append(_link("1.1.2", "1.1.1", from_span=GOOD, to_span=GOOD))
    rows = dchash_audit(links, cat)
    assert len(rows) == 5  # the five changed to-spans on 1.1.1
    assert [Path(p).name for p in reads].count("a.md") == 1
    assert [Path(p).name for p in reads].count("b.md") == 1
    assert cat.resolve_calls.count("1.1.1") == 1
    assert cat.path_calls.count("1.1.1") == 1


def test_a_missing_entry_is_never_read(tmp_path: Path, reads: list[Path]) -> None:
    cat = FakeCatalog({})
    rows = dchash_audit([_link("1.1.5", "1.1.6", from_span=GOOD, to_span=GOOD)], cat)
    assert {r["status"] for r in rows} == {"missing"}
    assert reads == [] and cat.path_calls == []


@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), RuntimeError("engine 503")])
@pytest.mark.parametrize("where", ["resolve", "resolve_path"])
def test_engine_errors_propagate_out_of_the_audit(
    tmp_path: Path, exc: BaseException, where: str
) -> None:
    entries = {"1.1.1": _entry("1.1.1", _doc(tmp_path, "a.md"))}
    cat = (
        FakeCatalog(entries, resolve_raises=exc)
        if where == "resolve"
        else FakeCatalog(entries, raises=exc)
    )
    with pytest.raises(type(exc)):
        dchash_audit([_link("1.1.1", "1.1.1", from_span=GOOD)], cat)


# --- link_audit ------------------------------------------------------------


def _client(
    links: list[CatalogLink], cat: FakeCatalog
) -> tuple[HttpCatalogClient, Any]:
    client = HttpCatalogClient.__new__(HttpCatalogClient)

    def link_query(*, limit: int, offset: int, **_k: Any) -> list[CatalogLink]:
        return links[offset : offset + limit]

    patches = (
        patch.object(HttpCatalogClient, "link_query", side_effect=link_query),
        patch.object(HttpCatalogClient, "_get", return_value={"links": []}),
        patch.object(HttpCatalogClient, "resolve", side_effect=cat.resolve),
        patch.object(HttpCatalogClient, "resolve_path", side_effect=cat.resolve_path),
    )
    return client, patches


def _run_audit(links: list[CatalogLink], cat: FakeCatalog) -> dict:
    client, patches = _client(links, cat)
    for p in patches:
        p.start()
    try:
        return client.link_audit()
    finally:
        for p in patches:
            p.stop()


def test_link_audit_without_dchash_spans_carries_empty_keys() -> None:
    cat = FakeCatalog({})
    result = _run_audit([_link("1.1.1", "1.1.2", from_span="3-9")], cat)
    assert result["stale_dchash"] == [] and result["stale_dchash_count"] == 0
    assert result["total"] == 1 and result["by_type"] == {"cites": 1}
    assert result["duplicate_count"] == 0 and result["orphaned_count"] == 0
    assert cat.resolve_calls == []


def test_link_audit_reports_stale_dchash_rows_and_counts_them(tmp_path: Path) -> None:
    cat = FakeCatalog({"1.1.1": _entry("1.1.1", _doc(tmp_path, "a.md"))})
    stale = f"dchash:{OTHER_HASH}:0-5"
    links = [
        _link("1.1.1", "1.1.1", from_span=GOOD, to_span=stale),
        _link("1.1.1", "1.1.1", from_span=GOOD, to_span=GOOD, link_type="ok"),
    ]
    result = _run_audit(links, cat)
    assert result["stale_dchash_count"] == len(result["stale_dchash"]) == 1
    assert result["stale_dchash"][0]["status"] == "changed"
    assert result["stale_dchash"][0]["side"] == "to"
    assert result["total"] == 2


def test_link_audit_pages_through_more_than_one_page_of_dchash_links(tmp_path: Path) -> None:
    cat = FakeCatalog({"1.1.1": _entry("1.1.1", _doc(tmp_path, "a.md"))})
    stale = f"dchash:{OTHER_HASH}:0-5"
    links = [_link("1.1.1", "1.1.2", from_span=stale, link_type=f"t{i}") for i in range(450)]
    result = _run_audit(links, cat)
    assert result["total"] == 450
    assert result["stale_dchash_count"] == 450
    assert cat.resolve_calls.count("1.1.1") == 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), RuntimeError("engine 503")])
def test_link_audit_raises_on_an_engine_error(tmp_path: Path, exc: BaseException) -> None:
    cat = FakeCatalog({"1.1.1": _entry("1.1.1", _doc(tmp_path, "a.md"))}, raises=exc)
    with pytest.raises(type(exc)):
        _run_audit([_link("1.1.1", "1.1.1", from_span=GOOD)], cat)
