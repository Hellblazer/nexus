# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 2: source lookup, span-to-text resolution and span status (gate S-A, S-D)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from nexus.catalog import catalog_spans
from nexus.catalog.dchash import (
    canonical_text,
    dchash_source,
    dchash_status,
    resolve_dchash_text,
    span_status,
    text_hash,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

PASSAGE = "the passage we quote"
BODY = f"Intro line.\n{PASSAGE} and more.\nEnd.\n"


def _entry(
    *, file_path: str = "", source_uri: str = "", tumbler: str = "1.2.3"
) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler),
        title="t",
        author="",
        year=0,
        content_type="prose",
        file_path=file_path,
        corpus="",
        physical_collection="docs__1-2__voyage-context-3__v1",
        chunk_count=1,
        head_hash="",
        indexed_at="",
        source_uri=source_uri,
    )


class FakeOwner:
    def __init__(self, owner_type: str, repo_root: str = "") -> None:
        self.owner_type = owner_type
        self.repo_root = repo_root


class FakeCatalog:
    """resolve_path with HttpCatalogClient's rules: repo_root / file_path, None for a curator."""

    def __init__(
        self,
        owner: FakeOwner,
        entry: CatalogEntry,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self.owner = owner
        self.entry = entry
        self.raises = raises
        self.calls = 0

    def owner_for(self, _tumbler: Any) -> FakeOwner:
        return self.owner

    def resolve_path(self, tumbler: Any) -> Path | None:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if self.owner.owner_type == "curator" or not self.entry.file_path:
            return None
        if Path(self.entry.file_path).is_absolute():
            return Path(self.entry.file_path)
        return Path(self.owner.repo_root) / self.entry.file_path


def _span(text: str, passage: str = PASSAGE) -> str:
    start = text.index(passage)
    return f"dchash:{text_hash(text)}:{start}-{start + len(passage)}"


@pytest.fixture(autouse=True)
def _no_t3(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dchash branch makes no vector-store call (gate observation 3)."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("dchash path touched T3")

    monkeypatch.setattr("nexus.db.make_t3", boom, raising=False)


def _not_in_git(d: Path) -> None:
    r = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "--show-toplevel"], capture_output=True
    )
    assert r.returncode != 0, f"{d} is inside a git checkout"


def test_curator_single_file_resolves_through_source_uri(tmp_path: Path) -> None:
    d = tmp_path / "single"
    d.mkdir()
    _not_in_git(d)
    f = d / "loose.md"
    f.write_text(BODY)
    entry = _entry(file_path="loose.md", source_uri=f"file://{f}")
    cat = FakeCatalog(FakeOwner("curator"), entry)
    assert cat.owner_for(entry.tumbler).owner_type == "curator"
    assert cat.resolve_path(entry.tumbler) is None
    assert dchash_source(entry, cat) == f
    span = _span(BODY)
    assert resolve_dchash_text(entry, span, cat) == PASSAGE
    assert dchash_status(entry, span, cat) == "ok"


def test_repo_relative_path_ignores_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text(BODY)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "docs").mkdir()
    (elsewhere / "docs" / "a.md").write_text("decoy\n")
    monkeypatch.chdir(elsewhere)
    entry = _entry(file_path="docs/a.md")
    cat = FakeCatalog(FakeOwner("repo", str(repo)), entry)
    assert resolve_dchash_text(entry, _span(BODY), cat) == PASSAGE


def test_relative_file_path_without_catalog_is_not_anchored_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.md").write_text(BODY)
    monkeypatch.chdir(tmp_path)
    entry = _entry(file_path="a.md")
    assert dchash_source(entry, None) is None
    assert dchash_status(entry, _span(BODY), None) == "unreadable"


def test_file_uri_without_catalog(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(source_uri=f"file://{f}")
    assert resolve_dchash_text(entry, _span(BODY), None) == PASSAGE


def test_absolute_file_path_fallback(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(file_path=str(f), source_uri="nx-note://x")
    assert resolve_dchash_text(entry, _span(BODY), None) == PASSAGE


def test_crlf_bom_rewritten_as_lf_still_ok(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_bytes(b"\xef\xbb\xbf" + BODY.replace("\n", "\r\n").encode())
    entry = _entry(source_uri=f"file://{f}")
    span = _span(canonical_text(f.read_bytes()))
    first = resolve_dchash_text(entry, span, None)
    f.write_bytes(BODY.encode())
    assert dchash_status(entry, span, None) == "ok"
    assert resolve_dchash_text(entry, span, None) == first == PASSAGE


def test_rechunk_survival_reads_no_chunks(tmp_path: Path) -> None:
    """The span resolves the same whatever the manifest says: the resolver never reads chunks."""
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(source_uri=f"file://{f}")
    span = _span(BODY)
    before = catalog_spans.resolve_span_text_for_entry(entry, span, catalog=None)
    entry.chunk_count = 7
    entry.physical_collection = "docs__1-2__voyage-context-3__v2"
    assert (
        catalog_spans.resolve_span_text_for_entry(entry, span, catalog=None)
        == before
        == PASSAGE
    )


def test_status_changed(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(source_uri=f"file://{f}")
    span = _span(BODY)
    f.write_text("X" + BODY)
    assert dchash_status(entry, span, None) == "changed"
    assert resolve_dchash_text(entry, span, None) is None


def test_status_missing() -> None:
    assert dchash_status(None, f"dchash:{'a' * 64}:0-1", None) == "missing"


def test_status_unreadable_deleted(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(source_uri=f"file://{f}")
    span = _span(BODY)
    f.unlink()
    assert dchash_status(entry, span, None) == "unreadable"
    assert resolve_dchash_text(entry, span, None) is None


def test_status_unreadable_not_utf8(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_bytes(b"\xff\xfe bad")
    entry = _entry(source_uri=f"file://{f}")
    assert dchash_status(entry, f"dchash:{'a' * 64}:0-1", None) == "unreadable"


def test_status_unreadable_directory(tmp_path: Path) -> None:
    entry = _entry(source_uri=f"file://{tmp_path}")
    assert dchash_status(entry, f"dchash:{'a' * 64}:0-1", None) == "unreadable"


def test_status_unreadable_no_path() -> None:
    entry = _entry(source_uri="nx-note://x")
    assert dchash_status(entry, f"dchash:{'a' * 64}:0-1", None) == "unreadable"


def test_status_unreadable_nul_in_path() -> None:
    entry = _entry(source_uri="file:///tmp/a\x00b")
    assert dchash_status(entry, f"dchash:{'a' * 64}:0-1", None) == "unreadable"


def test_offsets_are_code_points_past_the_bmp(tmp_path: Path) -> None:
    body = "\U0001f600\U0001f600 x " + PASSAGE + "\n"
    f = tmp_path / "a.md"
    f.write_text(body, encoding="utf-8")
    entry = _entry(source_uri=f"file://{f}")
    start = body.index(PASSAGE)
    assert start == 5
    span = f"dchash:{text_hash(body)}:{start}-{start + len(PASSAGE)}"
    assert resolve_dchash_text(entry, span, None) == PASSAGE


def test_unresolved_logs_the_tumbler_and_error_class(tmp_path: Path) -> None:
    entry = _entry(file_path="a.md", tumbler="1.2.9")
    cat = FakeCatalog(
        FakeOwner("repo", str(tmp_path)), entry, raises=RuntimeError("down")
    )
    with capture_logs() as logs:
        assert resolve_dchash_text(entry, f"dchash:{'a' * 64}:0-1", cat) is None
    hit = [e for e in logs if e["event"] == "dchash_span_unresolved"]
    assert hit and hit[0]["tumbler"] == "1.2.9" and hit[0]["error"] == "RuntimeError"


def test_status_out_of_range(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(source_uri=f"file://{f}")
    span = f"dchash:{text_hash(BODY)}:0-{len(BODY) + 1}"
    assert dchash_status(entry, span, None) == "out_of_range"
    assert resolve_dchash_text(entry, span, None) is None
    assert dchash_status(entry, f"dchash:{text_hash(BODY)}:0-{len(BODY)}", None) == "ok"


def test_status_malformed(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(source_uri=f"file://{f}")
    bad = f"dchash:{text_hash(BODY)}:5-5"
    assert dchash_status(entry, bad, None) == "malformed"
    assert resolve_dchash_text(entry, bad, None) is None
    assert catalog_spans.resolve_span_text_for_entry(entry, bad, catalog=None) is None


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("down"), RuntimeError("engine 503")]
)
def test_engine_error_read_path_none_audit_raises(
    tmp_path: Path, exc: BaseException
) -> None:
    f = tmp_path / "a.md"
    f.write_text(BODY)
    entry = _entry(file_path="a.md", source_uri=f"file://{f}")
    cat = FakeCatalog(FakeOwner("repo", str(tmp_path)), entry, raises=exc)
    span = _span(BODY)
    assert resolve_dchash_text(entry, span, cat) is None
    assert catalog_spans.resolve_span_text_for_entry(entry, span, catalog=cat) is None
    with pytest.raises(type(exc)):
        dchash_status(entry, span, cat)


@pytest.mark.parametrize(
    ("span", "doc", "found", "want"),
    [
        ("dchash:bad", ("a" * 64, 9), True, "malformed"),
        ("dchash:bad", None, False, "malformed"),
        (f"dchash:{'a' * 64}:0-1", None, False, "missing"),
        (f"dchash:{'a' * 64}:0-1", None, True, "unreadable"),
        (f"dchash:{'a' * 64}:0-1", ("b" * 64, 9), True, "changed"),
        (f"dchash:{'a' * 64}:0-10", ("a" * 64, 9), True, "out_of_range"),
        (f"dchash:{'a' * 64}:0-9", ("a" * 64, 9), True, "ok"),
    ],
)
def test_span_status_is_the_one_mapping(
    span: str, doc: tuple[str, int] | None, found: bool, want: str
) -> None:
    assert span_status(span, doc, entry_found=found) == want


def test_malformed_span_reads_no_file(tmp_path: Path) -> None:
    entry = _entry(file_path="a.md")
    cat = FakeCatalog(FakeOwner("repo", str(tmp_path)), entry)
    assert dchash_status(entry, "dchash:bad", cat) == "malformed"
    assert cat.calls == 0
