# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #896 (render/validate half; the in-place converter is nexus-sxiay):
``nx://catalog/<tumbler>`` markdown-link resolution.

Docs cite catalog entries as markdown links, e.g.
``[tumbler 1.1.194](nx://catalog/1.1.194)`` — nothing resolved that
scheme. This adds a tumbler-link resolver alongside the existing
RDR-082/083/086 doc-token machinery in ``src/nexus/commands/doc.py``:

  * ``nx doc render`` resolves every such link against the catalog and
    appends a ``## Catalog References`` footnote block (title, content
    type, owner, and a working link — never a ``file://`` URI or an
    absolute path, see ``TestSafeLinkTarget``), mirroring
    ``_append_chash_footnotes``.
  * ``nx doc validate`` fails (nonzero exit, naming file/line/tumbler,
    once PER CITING LINE) on a tumbler that no longer resolves.
  * A merged duplicate (``alias_of`` set) redirects to its canonical
    entry, noting the merge (nexus-w715w).

Test tiers: pure scanner/formatter unit tests (no engine), resolver
unit tests against a fake reader (batch-call shape, the
service-vs-not-found distinction, alias-of redirect — all without a
real engine), and CLI-level tests against the real engine substrate —
the same pattern ``tests/test_phase4_doc_commands.py`` uses for the
chash footnote feature this mirrors.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

_TRAILING_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)\s*$", re.MULTILINE)


def _extract_link_target(rendered_body: str) -> str:
    """Pull the trailing ``[x](x)`` link off the last footnote line in
    *rendered_body* (the ``format_footnote``/``_format_def`` convention:
    link text == link target, always the line's final segment)."""
    matches = list(_TRAILING_LINK_RE.finditer(rendered_body))
    assert matches, rendered_body
    m = matches[-1]
    assert m.group(1) == m.group(2), rendered_body
    return m.group(1)


# ── Scanner ──────────────────────────────────────────────────────────────────


class TestScanCatalogLinks:
    def test_finds_one_link(self) -> None:
        from nexus.doc.catalog_links import scan_catalog_links

        links = scan_catalog_links(
            "See [tumbler 1.1.194](nx://catalog/1.1.194) for details.\n"
        )
        assert len(links) == 1
        link = links[0]
        assert link.tumbler == "1.1.194"
        assert link.display == "tumbler 1.1.194"
        assert link.lineno == 1

    def test_finds_multiple_links_across_lines(self) -> None:
        from nexus.doc.catalog_links import scan_catalog_links

        md = (
            "line one\n"
            "[a](nx://catalog/1.1.1) and [b](nx://catalog/1.2.3)\n"
            "line three [c](nx://catalog/2.1.1)\n"
        )
        links = scan_catalog_links(md)
        assert [link.tumbler for link in links] == ["1.1.1", "1.2.3", "2.1.1"]
        assert links[0].lineno == 2
        assert links[2].lineno == 3

    def test_returns_every_occurrence_including_repeats(self) -> None:
        """nexus-sevlu review item 3: the scanner itself must NOT dedup —
        validate's error reporting depends on seeing every citing line
        for the same tumbler."""
        from nexus.doc.catalog_links import scan_catalog_links

        md = (
            "first [x](nx://catalog/9.9.9) citation\n"
            "second [y](nx://catalog/9.9.9) citation\n"
        )
        links = scan_catalog_links(md)
        assert [link.tumbler for link in links] == ["9.9.9", "9.9.9"]
        assert [link.lineno for link in links] == [1, 2]

    def test_ignores_non_catalog_scheme(self) -> None:
        from nexus.doc.catalog_links import scan_catalog_links

        links = scan_catalog_links(
            "See [chash link](chash:" + "a" * 64 + ") and "
            "[http link](https://example.com) but not a catalog link.\n"
        )
        assert links == []

    def test_skips_link_inside_fenced_code_block(self) -> None:
        from nexus.doc.catalog_links import scan_catalog_links

        md = (
            "Docs:\n"
            "```\n"
            "[example](nx://catalog/1.1.1)\n"
            "```\n"
            "Real: [tumbler](nx://catalog/2.2.2)\n"
        )
        links = scan_catalog_links(md)
        assert len(links) == 1
        assert links[0].tumbler == "2.2.2"

    def test_rejects_malformed_tumbler(self) -> None:
        """A non-digit / non-dotted tumbler shape must not match — the
        grammar mirrors ``Tumbler.parse``'s own (dotted non-negative
        integers)."""
        from nexus.doc.catalog_links import scan_catalog_links

        links = scan_catalog_links(
            "[bad](nx://catalog/not-a-tumbler) and [also bad](nx://catalog/1)\n"
        )
        assert links == []


# ── Formatter ────────────────────────────────────────────────────────────────


@dataclass
class _FakeTumbler:
    segments: tuple[int, ...]

    def owner_address(self) -> "_FakeTumbler":
        return _FakeTumbler(self.segments[:2])

    def __str__(self) -> str:
        return ".".join(str(s) for s in self.segments)


@dataclass
class _FakeEntry:
    tumbler: _FakeTumbler
    title: str = ""
    content_type: str = ""
    source_uri: str = ""
    file_path: str = ""
    alias_of: str = ""


class TestSafeLinkTarget:
    """nexus-w715w / GH #896 review item 2 (+ round 2 hardening): a
    ``file://`` source_uri or an absolute/UNC/``~``/escaping file_path
    must never reach rendered output — all leak or misrepresent the
    indexing machine's own path layout."""

    def test_prefers_an_allowlisted_source_uri(self) -> None:
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        link = CatalogLink(display="d", tumbler="1.1.194", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 194)),
            title="Attention Is All You Need",
            content_type="paper",
            source_uri="https://arxiv.org/abs/1706.03762",
            file_path="papers/attention.pdf",
        )
        line = format_footnote(link, entry, {"1.1": "grossberg-papers"})
        assert line == (
            "- `nx://catalog/1.1.194` — **Attention Is All You Need** "
            "(paper, owner: grossberg-papers) — "
            "[https://arxiv.org/abs/1706.03762](https://arxiv.org/abs/1706.03762)"
        )

    def test_file_scheme_source_uri_is_never_emitted(self) -> None:
        """A ``file://`` source_uri (what ``nx index repo`` derives for
        every registration) must be REJECTED — the relative file_path
        is used instead (as plain text, with no repo_root/base_dir
        supplied here), never the machine-local URI."""
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        link = CatalogLink(display="d", tumbler="1.1.7", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 7)),
            title="Local Paper",
            content_type="paper",
            source_uri="file:///Users/hal.hildebrand/git/nexus/papers/attention.pdf",
            file_path="papers/attention.pdf",
        )
        line = format_footnote(link, entry, {})
        assert "file://" not in line
        assert "/Users/hal.hildebrand" not in line
        assert "(repo-relative) `papers/attention.pdf`" in line

    @pytest.mark.parametrize("bad_uri", [
        "FILE:///Users/bob/x.md",
        "File:///Users/bob/x.md",
        "file:/Users/bob/x.md",
        "FiLe://localhost/Users/bob/x.md",
    ])
    def test_file_scheme_rejected_regardless_of_case_or_slash_form(self, bad_uri: str) -> None:
        """nexus-w715w round 2, code-review-expert finding 3:
        ``source_uri.startswith("file://")`` was case- and slash-
        sensitive; ``FILE:///...``/``File:///...``/``file:/...`` all
        slipped past it. Parsed via ``urlsplit`` + lowercase compare
        instead, so every casing/slash variant is caught."""
        from nexus.doc.catalog_links import safe_link_target

        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), file_path="docs/x.md", source_uri=bad_uri,
        )
        result = safe_link_target(entry)
        assert result is not None
        assert result.is_link is False
        assert result.text == "docs/x.md"

    @pytest.mark.parametrize("safe_uri", [
        "https://arxiv.org/abs/1706.03762",
        "x-devonthink-item://ABCD-1234",
    ])
    def test_allowlisted_schemes_pass(self, safe_uri: str) -> None:
        from nexus.doc.catalog_links import safe_link_target

        entry = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), source_uri=safe_uri)
        result = safe_link_target(entry)
        assert result is not None
        assert result.is_link is True
        assert result.text == safe_uri

    def test_nx_scratch_scheme_is_rejected(self) -> None:
        """Code-review round: T1 scratch is SESSION-scoped, so a
        ``nx-scratch://`` URI is unresolvable by any later reader —
        removed from the link-safe allowlist. A file_path fallback (or
        no link at all) is used instead, same as any other unsafe
        source_uri."""
        from nexus.doc.catalog_links import safe_link_target

        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)),
            source_uri="nx-scratch://session/note",
            file_path="docs/x.md",
        )
        result = safe_link_target(entry)
        assert result is not None
        assert result.is_link is False
        assert result.text == "docs/x.md"

        entry_no_fallback = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), source_uri="nx-scratch://session/note",
        )
        assert safe_link_target(entry_no_fallback) is None

    @pytest.mark.parametrize("unsafe_path", [
        "/abs/path.md",
        "C:\\Users\\bob\\x.md",
        "C:/Users/bob/x.md",
        "\\\\server\\share\\x.md",
        "//server/share/x.md",
        "~/x.md",
        "../../escape.md",
        "../ok/../../escape2.md",
        # Code-review round: percent-encoded traversal must be decoded
        # before classification, not compared literally.
        "%2e%2e%2f%2e%2e%2fescape.md",
        "%2e%2e/%2e%2e/escape.md",
        "%2Fabs%2Fpath.md",
        "%7E/x.md",
    ])
    def test_unsafe_relative_path_shapes_are_rejected(self, unsafe_path: str) -> None:
        """nexus-w715w round 2, code-review-expert finding 4:
        ``Path(file_path).is_absolute()`` is host-OS-native — a Windows
        drive-letter path, a UNC path, ``~``, or an upward-escaping
        ``..`` sequence all read as "relative" on this (POSIX) host.
        Classification must be content-based, not host-OS-native."""
        from nexus.doc.catalog_links import safe_link_target

        entry = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), file_path=unsafe_path)
        assert safe_link_target(entry) is None

    @pytest.mark.parametrize("good_path", ["docs/x.md", "a/../b/x.md", "x.md"])
    def test_safe_relative_path_shapes_pass_as_plain_text_without_repo_root(
        self, good_path: str,
    ) -> None:
        from nexus.doc.catalog_links import safe_link_target

        entry = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), file_path=good_path)
        result = safe_link_target(entry)
        assert result is not None
        assert result.is_link is False
        assert result.text == good_path

    def test_absolute_file_path_with_no_safe_source_uri_omits_link(self) -> None:
        """Neither candidate is safe (file:// source_uri, absolute
        file_path) — render title/type/owner only, no link segment at
        all, rather than leak the absolute path."""
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        link = CatalogLink(display="d", tumbler="1.1.8", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 8)),
            title="No Safe Link",
            content_type="paper",
            source_uri="file:///Users/hal.hildebrand/leak.pdf",
            file_path="/Users/hal.hildebrand/leak.pdf",
        )
        line = format_footnote(link, entry, {})
        assert line == (
            "- `nx://catalog/1.1.8` — **No Safe Link** (paper, owner: 1.1)"
        )
        assert "/Users/hal.hildebrand" not in line
        assert "file://" not in line
        assert "](" not in line

    def test_falls_back_to_relative_file_path_as_plain_text_when_no_source_uri(self) -> None:
        """No repo_root/base_dir supplied — a safe repo-relative
        file_path is shown as plain, non-clickable text, not a link
        that has never been confirmed to resolve."""
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        link = CatalogLink(display="d", tumbler="1.1.5", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 5)),
            title="A Paper",
            content_type="paper",
            source_uri="",
            file_path="docs/a-paper.md",
        )
        line = format_footnote(link, entry, {})
        assert line == (
            "- `nx://catalog/1.1.5` — **A Paper** (paper, owner: 1.1) — "
            "(repo-relative) `docs/a-paper.md`"
        )

    def test_relative_file_path_becomes_a_working_link_when_base_dir_is_inside_repo_root(
        self, tmp_path: Path,
    ) -> None:
        """nexus-w715w round 2, substantive-critic finding 2: a
        markdown renderer resolves a relative link against the CITING
        file's own directory, not the repo root — so the emitted link
        must be re-expressed relative to *base_dir*, and must actually
        reach the real file. *base_dir* here is a DIFFERENT directory
        inside the SAME repo as repo_root (not repo_root itself, and
        not outside it) — the case this working-link path exists for."""
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        repo_root = tmp_path / "repo"
        (repo_root / "docs").mkdir(parents=True)
        (repo_root / "docs" / "a-paper.md").write_text("x")
        citing_dir = repo_root / "notes"  # inside repo_root, different subdir
        citing_dir.mkdir()

        link = CatalogLink(display="d", tumbler="1.1.5", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 5)), title="A Paper", content_type="paper",
            file_path="docs/a-paper.md",
        )
        line = format_footnote(
            link, entry, {}, base_dir=citing_dir, repo_root=str(repo_root),
        )
        # The link text is a WORKING relative path from citing_dir.
        import re

        m = re.search(r"\[([^\]]+)\]\(([^)]+)\)$", line)
        assert m is not None, line
        assert m.group(1) == m.group(2)
        resolved = (citing_dir / m.group(1)).resolve()
        assert resolved == (repo_root / "docs" / "a-paper.md").resolve()
        assert "file://" not in line
        assert str(repo_root) not in line

    def test_cross_repo_base_dir_falls_back_to_plain_text(self, tmp_path: Path) -> None:
        """Code-review round, substantive-critic finding 3: when *base_dir*
        is NOT inside *repo_root* at all (a citing file in a completely
        different repository/tree), a working relative link would be a
        ``../../..`` traversal into an unrelated repo's filesystem layout
        — refused; falls back to the plain, non-clickable
        ``(repo-relative)`` label instead, same as when repo_root/base_dir
        are simply unknown."""
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        repo_root = tmp_path / "repo"
        (repo_root / "docs").mkdir(parents=True)
        (repo_root / "docs" / "a-paper.md").write_text("x")
        other_repo_dir = tmp_path / "a-completely-different-repo"
        other_repo_dir.mkdir()

        link = CatalogLink(display="d", tumbler="1.1.6", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 6)), title="Cross Repo Paper", content_type="paper",
            file_path="docs/a-paper.md",
        )
        line = format_footnote(
            link, entry, {}, base_dir=other_repo_dir, repo_root=str(repo_root),
        )
        assert line == (
            "- `nx://catalog/1.1.6` — **Cross Repo Paper** (paper, owner: 1.1) — "
            "(repo-relative) `docs/a-paper.md`"
        )
        assert "file://" not in line
        assert str(repo_root) not in line
        assert ".." not in line

    def test_no_link_when_neither_target_present(self) -> None:
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        link = CatalogLink(display="d", tumbler="1.1.9", lineno=1, col=1)
        entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 9)), title="No Location", content_type="knowledge",
        )
        line = format_footnote(link, entry, {})
        assert line == (
            "- `nx://catalog/1.1.9` — **No Location** (knowledge, owner: 1.1)"
        )


class TestFormatFootnote:
    def test_merged_into_note_appears_before_link(self) -> None:
        from nexus.doc.catalog_links import CatalogLink, format_footnote

        link = CatalogLink(display="d", tumbler="1.1.1", lineno=1, col=1)
        canonical_entry = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 2)),
            title="Canonical Paper",
            content_type="paper",
            file_path="docs/canonical.md",
        )
        line = format_footnote(
            link, canonical_entry, {}, merged_into="1.1.2",
        )
        assert line == (
            "- `nx://catalog/1.1.1` — **Canonical Paper** (paper, owner: 1.1) "
            "— merged into `nx://catalog/1.1.2` — "
            "(repo-relative) `docs/canonical.md`"
        )

    def test_unresolved_footnote_names_tumbler(self) -> None:
        from nexus.doc.catalog_links import CatalogLink, format_unresolved_footnote

        link = CatalogLink(display="d", tumbler="9.9.9", lineno=4, col=1)
        line = format_unresolved_footnote(link)
        assert "nx://catalog/9.9.9" in line
        assert "unresolved tumbler: 9.9.9" in line


# ── resolve_catalog_links (fake reader — no engine needed) ──────────────────


class TestResolveCatalogLinksUnit:
    def test_batches_into_one_resolve_many_call(self) -> None:
        from nexus.doc.catalog_links import resolve_catalog_links

        entry = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = MagicMock()
        reader.resolve_many.return_value = {"1.1.1": entry}
        reader.get_owner_by_prefix.return_value = {"name": "owner-x"}

        entries, owner_names, merged_into = resolve_catalog_links(
            ["1.1.1", "1.1.1", "1.1.2"], reader,
        )

        # Deduplicated + sorted before the single batch call.
        reader.resolve_many.assert_called_once_with(["1.1.1", "1.1.2"])
        assert entries == {"1.1.1": entry}
        assert owner_names == {"1.1": "owner-x"}
        assert merged_into == {}

    def test_missing_tumbler_is_a_plain_omission_not_an_exception(self) -> None:
        from nexus.doc.catalog_links import resolve_catalog_links

        reader = MagicMock()
        reader.resolve_many.return_value = {}  # nothing resolved
        entries, owner_names, merged_into = resolve_catalog_links(["9.9.9"], reader)
        assert entries == {}
        assert owner_names == {}
        assert merged_into == {}

    def test_service_failure_raises_typed_error(self) -> None:
        from nexus.doc.catalog_links import (
            CatalogLinkResolutionError,
            resolve_catalog_links,
        )

        reader = MagicMock()
        reader.resolve_many.side_effect = ConnectionError("boom")
        with pytest.raises(CatalogLinkResolutionError):
            resolve_catalog_links(["1.1.1"], reader)

    def test_owner_lookup_failure_is_best_effort_not_fatal(self) -> None:
        from nexus.doc.catalog_links import resolve_catalog_links

        entry = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = MagicMock()
        reader.resolve_many.return_value = {"1.1.1": entry}
        reader.get_owner_by_prefix.side_effect = RuntimeError("owner lookup broke")

        entries, owner_names, merged_into = resolve_catalog_links(["1.1.1"], reader)
        assert entries == {"1.1.1": entry}
        assert owner_names == {}  # best-effort miss, not a raised error
        assert merged_into == {}

    def test_empty_input_short_circuits(self) -> None:
        from nexus.doc.catalog_links import resolve_catalog_links

        reader = MagicMock()
        entries, owner_names, merged_into = resolve_catalog_links([], reader)
        assert entries == {} and owner_names == {} and merged_into == {}
        reader.resolve_many.assert_not_called()

    # ── alias_of redirect (nexus-w715w / GH #896 review item 4) ─────────────

    def test_alias_of_redirects_to_canonical_entry(self) -> None:
        from nexus.doc.catalog_links import resolve_catalog_links

        duplicate = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), title="Stale Duplicate",
            alias_of="1.1.2",
        )
        canonical = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 2)), title="Canonical",
        )
        reader = MagicMock()
        reader.resolve_many.side_effect = [
            {"1.1.1": duplicate},   # first batch: the cited tumbler
            {"1.1.2": canonical},  # second batch: the alias target
        ]
        reader.get_owner_by_prefix.return_value = None

        entries, _owner_names, merged_into = resolve_catalog_links(
            ["1.1.1"], reader,
        )
        assert entries == {"1.1.1": canonical}
        assert merged_into == {"1.1.1": "1.1.2"}
        assert reader.resolve_many.call_count == 2
        reader.resolve_many.assert_any_call(["1.1.2"])

    def test_alias_target_unresolvable_falls_back_to_duplicate_entry(self) -> None:
        """The canonical side itself doesn't resolve (should not happen
        absent a race) — keep the duplicate's own, still-real, data
        rather than manufacture a miss."""
        from nexus.doc.catalog_links import resolve_catalog_links

        duplicate = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), title="Stale Duplicate",
            alias_of="1.1.2",
        )
        reader = MagicMock()
        reader.resolve_many.side_effect = [
            {"1.1.1": duplicate},
            {},  # canonical side: nothing
        ]
        reader.get_owner_by_prefix.return_value = None

        entries, _owner_names, merged_into = resolve_catalog_links(
            ["1.1.1"], reader,
        )
        assert entries == {"1.1.1": duplicate}
        assert merged_into == {}

    def test_alias_target_resolution_failure_raises_typed_error(self) -> None:
        from nexus.doc.catalog_links import (
            CatalogLinkResolutionError,
            resolve_catalog_links,
        )

        duplicate = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), title="Stale Duplicate",
            alias_of="1.1.2",
        )
        reader = MagicMock()
        reader.resolve_many.side_effect = [
            {"1.1.1": duplicate},
            ConnectionError("boom"),
        ]
        with pytest.raises(CatalogLinkResolutionError):
            resolve_catalog_links(["1.1.1"], reader)


# ── scan_and_resolve_catalog_links (shared helper, review item 5) ───────────


class TestScanAndResolveCatalogLinks:
    def test_no_links_never_opens_a_reader(self) -> None:
        from nexus.doc.catalog_links import scan_and_resolve_catalog_links

        get_reader = MagicMock(side_effect=AssertionError("must not be called"))
        resolved = scan_and_resolve_catalog_links("plain prose, no links\n", get_reader)
        assert resolved.links == []
        assert resolved.entries == {}
        get_reader.assert_not_called()

    def test_links_present_resolves_via_get_reader(self) -> None:
        from nexus.doc.catalog_links import scan_and_resolve_catalog_links

        entry = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = MagicMock()
        reader.resolve_many.return_value = {"1.1.1": entry}
        reader.get_owner_by_prefix.return_value = None

        resolved = scan_and_resolve_catalog_links(
            "[x](nx://catalog/1.1.1)\n", lambda: reader,
        )
        assert len(resolved.links) == 1
        assert resolved.entries == {"1.1.1": entry}

    def test_get_reader_failure_is_normalized(self) -> None:
        from nexus.doc.catalog_links import (
            CatalogLinkResolutionError,
            scan_and_resolve_catalog_links,
        )

        def _boom() -> None:
            raise RuntimeError("catalog reader construction failed")

        with pytest.raises(CatalogLinkResolutionError):
            scan_and_resolve_catalog_links("[x](nx://catalog/1.1.1)\n", _boom)


# ── CLI-level tests against the real engine substrate ───────────────────────


def _register_catalog_doc(
    *,
    title: str,
    content_type: str = "paper",
    file_path: str = "",
    source_uri: str = "",
    owner_name: str = "gh896-test-owner",
    repo_root: str = "",
) -> str:
    """Register a real catalog document via the same client the CLI
    resolves through, and return its tumbler string. Mirrors
    ``tests/_catalog_fixture_ops.py::register_real_doc_id`` with control
    over content_type/file_path/source_uri/repo_root, which that helper
    doesn't expose. Passing *repo_root* lets the engine derive a
    ``file://`` source_uri server-side from a relative *file_path* —
    the exact shape ``nx index repo`` produces
    (``CatalogRepository.deriveSourceUri``)."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    with HttpCatalogClient() as cat:
        owner = cat.register_owner(
            owner_name, "repo", repo_hash="gh896-test-hash",
            repo_root=repo_root or None,
        )
        tumbler = cat.register(
            owner, title,
            content_type=content_type,
            file_path=file_path,
            source_uri=source_uri,
            physical_collection="knowledge__gh896",
        )
    return str(tumbler)


class TestRenderResolvesCatalogLinks:
    def test_resolving_tumbler_renders_footnote(self, tmp_path: Path) -> None:
        from nexus.commands.doc import render_cmd

        tumbler = _register_catalog_doc(
            title="Attention Is All You Need",
            content_type="paper",
            file_path="papers/attention.pdf",
            owner_name="gh896-owner-render",
        )

        doc = tmp_path / "src.md"
        doc.write_text(
            f"See [tumbler {tumbler}](nx://catalog/{tumbler}) for the source.\n"
        )

        runner = CliRunner()
        result = runner.invoke(render_cmd, [str(doc), "--allow-unresolved"])
        assert result.exit_code == 0, result.output

        rendered = tmp_path / "src.rendered.md"
        assert rendered.exists(), result.output
        body = rendered.read_text()
        assert "## Catalog References" in body
        assert f"nx://catalog/{tumbler}" in body
        assert "Attention Is All You Need" in body
        assert "paper" in body
        assert "papers/attention.pdf" in body

    def test_source_uri_with_file_scheme_is_never_leaked(
        self, tmp_path: Path,
    ) -> None:
        """nexus-w715w / GH #896 review item 2, real-substrate proof:
        register through an owner WITH repo_root set, so the engine's
        own ``deriveSourceUri`` produces a ``file://<abspath>``
        source_uri exactly as ``nx index repo`` does — and assert
        neither the URI nor any absolute path reaches rendered output,
        AND (round 2, substantive-critic finding 2) that the emitted
        relative link, resolved against the RENDERED file's own
        directory, reaches the real registered file. The citing doc
        lives INSIDE repo_root (a different subdirectory) — code-review
        round: a working link is only emitted when base_dir is inside
        repo_root at all, so this is the case that must produce one.
        """
        repo_root = tmp_path / "repo"
        rel_path = "docs/paper.md"
        (repo_root / "docs").mkdir(parents=True)
        (repo_root / rel_path).write_text("the actual paper content\n")

        tumbler = _register_catalog_doc(
            title="Leaky Paper",
            content_type="paper",
            file_path=rel_path,
            owner_name="gh896-owner-w715w",
            repo_root=str(repo_root),
        )

        from nexus.commands.doc import render_cmd

        src_dir = repo_root / "notes"
        src_dir.mkdir()
        doc = src_dir / "src.md"
        doc.write_text(f"See [x](nx://catalog/{tumbler}).\n")

        runner = CliRunner()
        result = runner.invoke(render_cmd, [str(doc), "--allow-unresolved"])
        assert result.exit_code == 0, result.output

        rendered = src_dir / "src.rendered.md"
        body = rendered.read_text()
        assert "Leaky Paper" in body
        assert "file://" not in body
        assert str(repo_root) not in body

        emitted = _extract_link_target(body)
        resolved = (rendered.parent / emitted).resolve()
        assert resolved == (repo_root / rel_path).resolve()
        assert resolved.read_text() == "the actual paper content\n"

    def test_source_uri_with_file_scheme_out_of_repo_root_and_out_dir(
        self, tmp_path: Path,
    ) -> None:
        """Code-review round, substantive-critic finding 3: the citing
        doc lives OUTSIDE repo_root entirely (a different tree,
        ``--out-dir`` elsewhere again) — a working relative link would
        be a ``../../..`` traversal into an unrelated repo's layout, so
        this must fall back to the plain, non-clickable
        ``(repo-relative)`` label instead — never leak repo_root, never
        emit a cross-repo traversal link."""
        repo_root = tmp_path / "repo"
        rel_path = "notes/deep/paper.md"
        (repo_root / "notes" / "deep").mkdir(parents=True)
        (repo_root / rel_path).write_text("deep paper content\n")

        tumbler = _register_catalog_doc(
            title="Deep Paper",
            content_type="paper",
            file_path=rel_path,
            owner_name="gh896-owner-w715w-outdir",
            repo_root=str(repo_root),
        )

        from nexus.commands.doc import render_cmd

        src_dir = tmp_path / "citing"
        src_dir.mkdir()
        doc = src_dir / "src.md"
        doc.write_text(f"See [x](nx://catalog/{tumbler}).\n")

        out_dir = tmp_path / "rendered-out"

        runner = CliRunner()
        result = runner.invoke(
            render_cmd, [str(doc), "--allow-unresolved", "--out-dir", str(out_dir)],
        )
        assert result.exit_code == 0, result.output

        rendered = out_dir / "src.rendered.md"
        assert rendered.exists(), result.output
        body = rendered.read_text()
        assert "Deep Paper" in body
        assert "file://" not in body
        assert str(repo_root) not in body
        assert ".." not in body  # no cross-repo traversal link emitted
        assert f"(repo-relative) `{rel_path}`" in body

    def test_merged_duplicate_redirects_to_canonical(
        self, tmp_path: Path,
    ) -> None:
        """nexus-w715w / GH #896 review item 4, real-substrate proof: a
        tumbler that has been merged into another (``alias_of`` set,
        row NOT tombstoned) renders the CANONICAL entry's data, with a
        merge note — never the stale duplicate's own title."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.commands.doc import render_cmd

        canonical_tumbler = _register_catalog_doc(
            title="Canonical Paper", owner_name="gh896-owner-merge",
        )
        duplicate_tumbler = _register_catalog_doc(
            title="Duplicate Paper (stale)", owner_name="gh896-owner-merge",
        )
        with HttpCatalogClient() as cat:
            cat.merge_documents(duplicate=duplicate_tumbler, canonical=canonical_tumbler)

        doc = tmp_path / "src.md"
        doc.write_text(f"See [x](nx://catalog/{duplicate_tumbler}).\n")

        runner = CliRunner()
        result = runner.invoke(render_cmd, [str(doc), "--allow-unresolved"])
        assert result.exit_code == 0, result.output

        rendered = tmp_path / "src.rendered.md"
        body = rendered.read_text()
        assert "Canonical Paper" in body
        assert "Duplicate Paper (stale)" not in body
        assert f"merged into `nx://catalog/{canonical_tumbler}`" in body

    def test_dangling_tumbler_renders_unresolved_marker(
        self, tmp_path: Path,
    ) -> None:
        from nexus.commands.doc import render_cmd

        # Register then delete so the tumbler is a REAL, catalog-shaped
        # tumbler that genuinely no longer resolves — not a made-up one
        # that never existed.
        tumbler = _register_catalog_doc(
            title="Deleted Doc", owner_name="gh896-owner-dangling",
        )
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "dangling.md"
        doc.write_text(f"See [x](nx://catalog/{tumbler}).\n")

        runner = CliRunner()
        result = runner.invoke(render_cmd, [str(doc), "--allow-unresolved"])
        assert result.exit_code == 0, result.output

        rendered = tmp_path / "dangling.rendered.md"
        body = rendered.read_text()
        assert f"unresolved tumbler: {tumbler}" in body

    def test_document_with_no_catalog_links_is_unchanged(
        self, tmp_path: Path,
    ) -> None:
        """A doc with no nx://catalog links never opens a catalog reader
        and gets no footnote block appended — the render is byte-for-byte
        what the token engine alone would have produced."""
        from nexus.commands.doc import render_cmd

        doc = tmp_path / "plain.md"
        doc.write_text("Nothing but plain prose here.\n")

        runner = CliRunner()
        result = runner.invoke(render_cmd, [str(doc)])
        assert result.exit_code == 0, result.output

        rendered = tmp_path / "plain.rendered.md"
        body = rendered.read_text()
        assert body == "Nothing but plain prose here.\n"
        assert "Catalog References" not in body


class TestValidateFailsOnDanglingTumbler:
    def test_resolving_tumbler_validates_clean(self, tmp_path: Path) -> None:
        from nexus.commands.doc import validate_cmd

        tumbler = _register_catalog_doc(
            title="Good Doc", owner_name="gh896-owner-validate-ok",
        )
        doc = tmp_path / "ok.md"
        doc.write_text(f"See [x](nx://catalog/{tumbler}).\n")

        runner = CliRunner()
        result = runner.invoke(validate_cmd, [str(doc)])
        assert result.exit_code == 0, result.output

    def test_dangling_tumbler_fails_with_file_line_and_tumbler(
        self, tmp_path: Path,
    ) -> None:
        from nexus.commands.doc import validate_cmd

        tumbler = _register_catalog_doc(
            title="Doomed Doc", owner_name="gh896-owner-validate-fail",
        )
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "bad.md"
        doc.write_text(
            "line one\n"
            f"line two [x](nx://catalog/{tumbler}) here\n"
        )

        runner = CliRunner()
        result = runner.invoke(validate_cmd, [str(doc)])
        assert result.exit_code == 1, result.output
        assert str(doc) in result.output
        assert ":2:" in result.output
        assert tumbler in result.output

    def test_dangling_tumbler_reported_on_every_citing_line(
        self, tmp_path: Path,
    ) -> None:
        """nexus-sevlu review item 3: the SAME dangling tumbler cited
        twice must produce TWO error lines, not one — dedup is correct
        for a footnote block, wrong for an error report."""
        from nexus.commands.doc import validate_cmd

        tumbler = _register_catalog_doc(
            title="Doomed Doc Twice", owner_name="gh896-owner-validate-twice",
        )
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "bad_twice.md"
        doc.write_text(
            "line one\n"
            f"line two [x](nx://catalog/{tumbler}) here\n"
            "line three, unrelated\n"
            f"line four [y](nx://catalog/{tumbler}) again\n"
        )

        runner = CliRunner()
        result = runner.invoke(validate_cmd, [str(doc)])
        assert result.exit_code == 1, result.output
        assert result.output.count(f"unresolved tumbler {tumbler}") == 2, result.output
        assert ":2:" in result.output
        assert ":4:" in result.output

    def test_document_with_no_catalog_links_validates_clean(
        self, tmp_path: Path,
    ) -> None:
        from nexus.commands.doc import validate_cmd

        doc = tmp_path / "plain.md"
        doc.write_text("Nothing but plain prose here.\n")

        runner = CliRunner()
        result = runner.invoke(validate_cmd, [str(doc)])
        assert result.exit_code == 0, result.output


class TestCatalogServiceOutageThroughCli:
    """nexus-sevlu review item 6: the outage path exercised through the
    actual CLI commands, not just the lower-level helper — monkeypatches
    the reader factory so no real engine call is even attempted."""

    def test_render_aborts_nonzero_with_message(self, tmp_path: Path) -> None:
        from nexus.commands.doc import render_cmd

        doc = tmp_path / "src.md"
        doc.write_text("[x](nx://catalog/1.1.1)\n")

        with patch(
            "nexus.commands.doc._open_catalog_link_reader",
            side_effect=RuntimeError("catalog unreachable"),
        ):
            runner = CliRunner()
            result = runner.invoke(render_cmd, [str(doc), "--allow-unresolved"])

        assert result.exit_code != 0, result.output
        assert "catalog" in result.output.lower()

    def test_validate_exits_2_naming_the_catalog(self, tmp_path: Path) -> None:
        from nexus.commands.doc import validate_cmd

        doc = tmp_path / "src.md"
        doc.write_text("[x](nx://catalog/1.1.1)\n")

        with patch(
            "nexus.commands.doc._open_catalog_link_reader",
            side_effect=RuntimeError("catalog unreachable"),
        ):
            runner = CliRunner()
            result = runner.invoke(validate_cmd, [str(doc)])

        assert result.exit_code == 2, result.output
        assert "catalog" in result.output.lower()
