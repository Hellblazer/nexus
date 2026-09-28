# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #896 (the in-place converter half): ``nx catalog footnotes``.

nexus-sevlu (``tests/test_gh896_catalog_link_tokens.py``) landed only
``nx doc render``/``nx doc validate``'s ``.rendered.md`` sidecar
resolution. This is the in-place converter the issue's acceptance
criteria actually describe: ``nx catalog footnotes <file.md>`` rewrites
the SOURCE file itself, idempotently, with ``--check``/``--dry-run``/
``--to-links``.

Test tiers, same shape as ``test_gh896_catalog_link_tokens.py``: pure
module-level tests against a fake reader (round trip, idempotence,
drift, dangling, fenced code, conflicting footnotes section — no
engine), and CLI-level tests against the real engine substrate for
resolution (mirrors that file's fixture approach exactly).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from nexus.doc.catalog_links import CatalogLinkResolutionError
from nexus.doc.footnote_converter import (
    ConversionResult,
    FootnoteSectionConflict,
    convert_footnotes_to_links,
    convert_links_to_footnotes,
)


# ── Fakes (mirrors test_gh896_catalog_link_tokens.py's own fakes) ──────────


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
    indexed_at: str = ""


def _fake_reader(entries: dict[str, _FakeEntry], *, links_from: dict[str, list] | None = None) -> MagicMock:
    reader = MagicMock()
    reader.resolve_many.side_effect = lambda tumblers: {
        t: entries[t] for t in tumblers if t in entries
    }
    reader.get_owner_by_prefix.return_value = None
    reader.links_from.side_effect = lambda t: (links_from or {}).get(str(t), [])
    return reader


# ── Forward conversion: round trip, idempotence, drift, dangling ──────────


class TestConvertLinksToFootnotes:
    def test_no_links_no_reader_call(self) -> None:
        get_reader = MagicMock(side_effect=AssertionError("must not be called"))
        result = convert_links_to_footnotes("plain prose, no links\n", get_reader)
        assert result.changed is False
        assert result.text == "plain prose, no links\n"
        assert result.dangling == []
        get_reader.assert_not_called()

    def test_single_link_converts_to_marker_and_footnote(self) -> None:
        e1 = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 194)), title="Attention Is All You Need",
            content_type="paper", indexed_at="2026-05-13",
        )
        reader = _fake_reader({"1.1.194": e1})
        text = "[tumbler 1.1.194](nx://catalog/1.1.194) for the source.\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert result.changed is True
        assert result.dangling == []
        assert "[tumbler 1.1.194][^tumbler-attention-is-all-you-need]" in result.text
        assert "## Footnotes" in result.text
        assert "[^tumbler-attention-is-all-you-need]: nx catalog tumbler `1.1.194`." in result.text
        assert 'Title: "Attention Is All You Need".' in result.text
        assert "Content type: paper." in result.text
        assert "Indexed 2026-05-13." in result.text

    def test_idempotent_on_unchanged_catalog_state(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X", content_type="paper")
        reader = _fake_reader({"1.1.1": e1})
        text = "[x](nx://catalog/1.1.1)\n"

        once = convert_links_to_footnotes(text, lambda: reader)
        twice = convert_links_to_footnotes(once.text, lambda: reader)

        assert twice.changed is False
        assert twice.text == once.text
        assert twice.dangling == []

    def test_title_drift_refreshes_body_but_marker_stays(self) -> None:
        """The acceptance-criteria case: catalog state has drifted (a
        title edit) -> only the footnote DEFINITION is rewritten, the
        marker already assigned in the body is untouched."""
        e1 = _FakeEntry(tumbler=_FakeTumbler((2, 2, 2)), title="Original Title")
        reader1 = _fake_reader({"2.2.2": e1})
        text = "[tumbler](nx://catalog/2.2.2)\n"
        first = convert_links_to_footnotes(text, lambda: reader1)
        assert "[tumbler][^tumbler-original-title]" in first.text

        e1_renamed = _FakeEntry(tumbler=_FakeTumbler((2, 2, 2)), title="Renamed Title")
        reader2 = _fake_reader({"2.2.2": e1_renamed})
        second = convert_links_to_footnotes(first.text, lambda: reader2)

        assert second.changed is True
        assert "[tumbler][^tumbler-original-title]" in second.text  # marker UNCHANGED
        assert "Renamed Title" in second.text
        assert "Original Title" not in second.text

    def test_merge_note_appears_in_footnote_body(self) -> None:
        """A merged duplicate (``alias_of`` set on the resolved entry)
        redirects to its canonical entry with a merge note — the same
        ``resolve_catalog_links`` behaviour ``format_footnote`` relies
        on (exercised against a fake two-call reader here, and against
        the real engine in ``TestResolveCatalogLinksUnit`` /
        ``TestRenderResolvesCatalogLinks`` in the sibling test file)."""
        duplicate = _FakeEntry(tumbler=_FakeTumbler((5, 5, 1)), title="Stale Duplicate", alias_of="5.5.2")
        canonical = _FakeEntry(tumbler=_FakeTumbler((5, 5, 2)), title="Canonical")
        reader = MagicMock()
        reader.resolve_many.side_effect = [
            {"5.5.1": duplicate},
            {"5.5.2": canonical},
        ]
        reader.get_owner_by_prefix.return_value = None
        reader.links_from.return_value = []
        text = "[x](nx://catalog/5.5.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert "Canonical" in result.text
        assert "Stale Duplicate" not in result.text
        assert "Merged into `nx://catalog/5.5.2`." in result.text

    def test_dangling_new_link_stays_a_link_and_is_reported(self) -> None:
        reader = _fake_reader({})  # nothing resolves
        text = "line one\n[bad](nx://catalog/9.9.9)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert result.text == text  # left exactly as a link
        assert result.changed is False
        assert len(result.dangling) == 1
        assert result.dangling[0].lineno == 2
        assert result.dangling[0].tumbler == "9.9.9"

    def test_existing_marker_gone_dangling_since_last_run(self) -> None:
        """A tumbler already carrying a marker resolves fine at first,
        then the entry is deleted/unreachable on a later run — the
        marker in the body must NOT move (nothing safe to rewrite it
        to), but the footnote body must say so, and it must be
        reported."""
        e1 = _FakeEntry(tumbler=_FakeTumbler((4, 4, 4)), title="Doomed")
        reader1 = _fake_reader({"4.4.4": e1})
        text = "[tumbler](nx://catalog/4.4.4)\n"
        first = convert_links_to_footnotes(text, lambda: reader1)
        marker = "[tumbler][^tumbler-doomed]"
        assert marker in first.text

        reader2 = _fake_reader({})  # now dangling
        second = convert_links_to_footnotes(first.text, lambda: reader2)

        assert marker in second.text  # marker itself is UNTOUCHED
        assert "unresolved (no longer in the catalog)" in second.text
        assert len(second.dangling) == 1
        assert second.dangling[0].tumbler == "4.4.4"

    def test_two_distinct_tumblers_get_distinct_slugs(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Alpha Paper")
        e2 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 2)), title="Beta Paper")
        reader = _fake_reader({"1.1.1": e1, "1.1.2": e2})
        text = "[a](nx://catalog/1.1.1)\n[b](nx://catalog/1.1.2)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert "[a][^tumbler-alpha-paper]" in result.text
        assert "[b][^tumbler-beta-paper]" in result.text
        assert result.text.count("## Footnotes") == 1

    def test_slug_collision_gets_numeric_suffix(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Same Title")
        e2 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 2)), title="Same Title")
        reader = _fake_reader({"1.1.1": e1, "1.1.2": e2})
        text = "[a](nx://catalog/1.1.1)\n[b](nx://catalog/1.1.2)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert "[a][^tumbler-same-title]" in result.text
        assert "[b][^tumbler-same-title-2]" in result.text

    def test_repeated_citation_of_the_same_tumbler_reuses_one_slug(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Doc")
        reader = _fake_reader({"1.1.1": e1})
        text = "[x](nx://catalog/1.1.1)\n[y](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert result.text.count("[^tumbler-doc]") == 3  # 2 markers + 1 def
        assert result.text.count("## Footnotes") == 1

    def test_fenced_code_block_left_untouched(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((2, 2, 2)), title="Real Doc")
        reader = _fake_reader({"2.2.2": e1})
        text = (
            "Docs:\n"
            "```\n"
            "[example](nx://catalog/1.1.1)\n"
            "```\n"
            "Real: [tumbler](nx://catalog/2.2.2)\n"
        )

        result = convert_links_to_footnotes(text, lambda: reader)

        assert "[example](nx://catalog/1.1.1)" in result.text  # untouched
        assert "[tumbler][^tumbler-real-doc]" in result.text
        # The fenced tumbler was never scanned, so it never entered
        # resolution and is not reported as dangling.
        assert result.dangling == []

    def test_outbound_links_appear_in_footnote_body(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Doc")

        @dataclass
        class _FakeLink:
            to_tumbler: str
            link_type: str

        reader = _fake_reader(
            {"1.1.1": e1},
            links_from={"1.1.1": [_FakeLink(to_tumbler="1.1.2", link_type="cites")]},
        )
        text = "[x](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert "Outbound links: `cites -> 1.1.2`." in result.text

    def test_catalog_service_outage_raises_typed_error(self) -> None:
        def _boom() -> None:
            raise ConnectionError("boom")

        with pytest.raises(CatalogLinkResolutionError):
            convert_links_to_footnotes("[x](nx://catalog/1.1.1)\n", _boom)

    def test_foreign_footnotes_section_refused(self) -> None:
        text = (
            "body\n\n## Footnotes\n\n"
            "[^1]: a hand-written footnote unrelated to this converter\n"
        )
        with pytest.raises(FootnoteSectionConflict):
            convert_links_to_footnotes(text, lambda: MagicMock())

    def test_working_relative_link_with_base_dir_inside_repo_root(self, tmp_path: Path) -> None:
        """base_dir must be INSIDE repo_root for a working link to be
        emitted (code-review round, cross-repo safety) -- here base_dir
        is repo_root itself, the simplest such case."""
        repo_root = tmp_path / "repo"
        (repo_root / "docs").mkdir(parents=True)
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Doc", file_path="docs/x.md")
        reader = _fake_reader({"1.1.1": e1})
        reader.get_owner_by_prefix.return_value = {"repo_root": str(repo_root)}
        text = "[x](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader, base_dir=repo_root)

        assert "[docs/x.md](docs/x.md)." in result.text

    def test_plain_text_fallback_when_base_dir_outside_repo_root(self, tmp_path: Path) -> None:
        """base_dir OUTSIDE repo_root entirely -- falls back to the
        plain (repo-relative) label, never a cross-repo link."""
        repo_root = tmp_path / "repo"
        (repo_root / "docs").mkdir(parents=True)
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Doc", file_path="docs/x.md")
        reader = _fake_reader({"1.1.1": e1})
        reader.get_owner_by_prefix.return_value = {"repo_root": str(repo_root)}
        text = "[x](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader, base_dir=other_dir)

        assert "(repo-relative) `docs/x.md`." in result.text
        assert str(repo_root) not in result.text


# ── Reverse conversion ──────────────────────────────────────────────────────


class TestConvertFootnotesToLinks:
    def test_no_footnotes_section_is_a_no_op(self) -> None:
        text = "plain markdown, no footnotes at all\n"
        result = convert_footnotes_to_links(text)
        assert result.changed is False
        assert result.text == text
        assert result.dangling == []

    def test_expands_marker_back_into_link(self) -> None:
        text = (
            "[tumbler 1.1.194][^tumbler-x]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-x]: nx catalog tumbler `1.1.194`. Title: "X". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert result.text == "[tumbler 1.1.194](nx://catalog/1.1.194)\n"
        assert result.dangling == []

    def test_unknown_slug_left_as_marker_and_reported(self) -> None:
        text = (
            "[orphan][^tumbler-ghost]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-real]: nx catalog tumbler `1.1.1`. Title: "Real". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert "[orphan][^tumbler-ghost]" in result.text  # left untouched
        assert len(result.dangling) == 1
        assert result.dangling[0].tumbler == "tumbler-ghost"

    def test_fenced_code_left_untouched(self) -> None:
        text = (
            "```\n[example][^tumbler-x]\n```\n"
            "[real][^tumbler-x]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-x]: nx catalog tumbler `1.1.1`. Title: "X". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert "[example][^tumbler-x]" in result.text  # inside fence, untouched
        assert "[real](nx://catalog/1.1.1)" in result.text

    def test_inline_code_span_example_left_untouched(self) -> None:
        """Code-review round: a docs page quoting the marker syntax as
        a literal example (inline backticks) must not be reverse-
        converted -- it was never a real citation."""
        text = (
            "Example syntax: `[label][^tumbler-real]` is what it looks like.\n"
            "Real one: [it works][^tumbler-real]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-real]: nx catalog tumbler `1.1.1`. Title: "Real". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert "`[label][^tumbler-real]`" in result.text  # inline example untouched
        assert "[it works](nx://catalog/1.1.1)" in result.text


class TestRoundTrip:
    """Acceptance criterion: links -> footnotes -> --to-links reproduces
    the original. Code-review round: EXACT regardless of what precedes
    or follows a citation on its line, or how many citations share a
    line -- see the module docstring's "Marker form and why" section
    (the label stays bracketed, so the reverse regex needs no boundary
    guess)."""

    def test_single_tumbler_round_trip(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 194)), title="Attention Is All You Need")
        reader = _fake_reader({"1.1.194": e1})
        original = "[tumbler 1.1.194](nx://catalog/1.1.194) for the source.\n"

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        back = convert_footnotes_to_links(footnoted.text)

        assert back.text == original
        assert back.dangling == []

    def test_mid_line_citation_round_trip(self) -> None:
        """Code-review round finding 1: the common #896 case -- a
        citation embedded mid-sentence, not alone at the start of its
        line."""
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = _fake_reader({"1.1.1": e1})
        original = "As noted in the paper [x](nx://catalog/1.1.1), the technique works.\n"

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        back = convert_footnotes_to_links(footnoted.text)

        assert back.text == original
        assert back.dangling == []

    def test_multiple_citations_on_one_line_round_trip(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Alpha")
        e2 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 2)), title="Beta")
        reader = _fake_reader({"1.1.1": e1, "1.1.2": e2})
        original = "Compare [a](nx://catalog/1.1.1) with [b](nx://catalog/1.1.2) directly.\n"

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        back = convert_footnotes_to_links(footnoted.text)

        assert back.text == original
        assert back.dangling == []

    def test_label_containing_brackets_is_left_untouched_and_round_trips_trivially(self) -> None:
        """A label with an embedded, unescaped ``]`` is a PRE-EXISTING
        limitation of ``nexus.doc.catalog_links``'s own link-scanning
        regex (``[^\\]\\n]+`` cannot match across a literal ``]``,
        nested or not) -- unrelated to this module, and out of scope to
        fix here. Documented, not silently broken: such a citation is
        simply never recognized as a catalog link in the first place,
        so it passes through both directions completely unchanged --
        round-tripping trivially, by virtue of never being touched."""
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = _fake_reader({"1.1.1": e1})
        original = "[term [x]](nx://catalog/1.1.1) more text\n"

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        assert footnoted.text == original  # never recognized as a link at all
        assert footnoted.changed is False

        back = convert_footnotes_to_links(footnoted.text)
        assert back.text == original

    def test_no_trailing_newline_round_trip(self) -> None:
        """Code-review round: a source file with NO trailing newline at
        all must come back with none after a full footnote-then-link
        round trip."""
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = _fake_reader({"1.1.1": e1})
        original = "[x](nx://catalog/1.1.1)"  # no trailing \n

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        assert not footnoted.text.endswith("\n\n")  # sanity: well-formed

        back = convert_footnotes_to_links(footnoted.text)
        assert back.text == original
        assert not back.text.endswith("\n")

    def test_crlf_round_trip(self) -> None:
        """Code-review round: a CRLF file keeps CRLF line endings
        through the whole round trip, and no masking artifact
        (``\\x00``) ever reaches the output."""
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="X")
        reader = _fake_reader({"1.1.1": e1})
        original = "[x](nx://catalog/1.1.1)\r\nsecond line\r\n"

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        assert "\r\n" in footnoted.text
        assert "\x00" not in footnoted.text

        back = convert_footnotes_to_links(footnoted.text)
        assert back.text == original

    def test_multi_tumbler_multi_line_round_trip(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 194)), title="Attention Is All You Need")
        e2 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 2)), title="Second Doc")
        reader = _fake_reader({"1.1.194": e1, "1.1.2": e2})
        original = (
            "[tumbler 1.1.194](nx://catalog/1.1.194) for the source.\n"
            "Some unrelated prose line, no citation here.\n"
            "[tumbler 1.1.194](nx://catalog/1.1.194) again, same tumbler, trailing prose.\n"
            "[second doc](nx://catalog/1.1.2): a description.\n"
        )

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        idempotent = convert_links_to_footnotes(footnoted.text, lambda: reader)
        assert idempotent.text == footnoted.text

        back = convert_footnotes_to_links(footnoted.text)
        assert back.text == original
        assert back.dangling == []


class TestRefreshOnly:
    """GH #896 ask 1's ``--refresh``: only refresh EXISTING footnote
    bodies against current catalog state; a brand-new raw link is left
    completely untouched -- not converted, not resolved, not reported."""

    def test_new_link_is_untouched_and_not_reported(self) -> None:
        reader = _fake_reader({})  # would be dangling if attempted
        text = "[brand new](nx://catalog/9.9.9)\n"

        result = convert_links_to_footnotes(text, lambda: reader, refresh_only=True)

        assert result.text == text
        assert result.changed is False
        assert result.dangling == []  # never attempted, so never reported

    def test_existing_marker_body_still_refreshes(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Original")
        reader1 = _fake_reader({"1.1.1": e1})
        text = "[x](nx://catalog/1.1.1)\n"
        first = convert_links_to_footnotes(text, lambda: reader1)

        e1_renamed = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Refreshed")
        reader2 = _fake_reader({"1.1.1": e1_renamed})
        second = convert_links_to_footnotes(
            first.text, lambda: reader2, refresh_only=True,
        )

        assert "[x][^tumbler-original]" in second.text  # marker unchanged
        assert "Refreshed" in second.text
        assert "Original" not in second.text.split("## Footnotes")[1]

    def test_new_link_alongside_existing_marker_only_new_one_is_untouched(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Known")
        reader1 = _fake_reader({"1.1.1": e1})
        first = convert_links_to_footnotes(
            "[x](nx://catalog/1.1.1)\n", lambda: reader1,
        )
        body, footnotes_section = first.text.split("## Footnotes")
        text_with_new_link = (
            body + "[brand new](nx://catalog/9.9.9)\n\n"
            "## Footnotes" + footnotes_section
        )

        reader2 = _fake_reader({"1.1.1": e1})  # unchanged
        result = convert_links_to_footnotes(
            text_with_new_link, lambda: reader2, refresh_only=True,
        )

        assert "[x][^tumbler-known]" in result.text  # existing marker refreshed/kept
        assert "[brand new](nx://catalog/9.9.9)" in result.text  # new link untouched
        assert result.dangling == []


class TestStyleOption:
    """GH #896 ask 1's ``--style``: 'long' (default) vs 'short' footnote
    body verbosity."""

    def test_short_style_is_title_and_tumbler_only(self) -> None:
        e1 = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), title="A Paper", content_type="paper",
            indexed_at="2026-01-01",
        )
        reader = _fake_reader({"1.1.1": e1})
        text = "[x](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader, style="short")

        assert '[^tumbler-a-paper]: nx catalog tumbler `1.1.1`. Title: "A Paper".' in result.text
        assert "Content type" not in result.text
        assert "Indexed" not in result.text

    def test_long_style_is_the_default_and_includes_more_detail(self) -> None:
        e1 = _FakeEntry(
            tumbler=_FakeTumbler((1, 1, 1)), title="A Paper", content_type="paper",
            indexed_at="2026-01-01",
        )
        reader = _fake_reader({"1.1.1": e1})
        text = "[x](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader)  # default style

        assert "Content type: paper." in result.text
        assert "Indexed 2026-01-01." in result.text


# ── CLI-level tests against the real engine substrate ───────────────────────


def _register_real_doc(
    *, title: str, owner_name: str, content_type: str = "paper",
) -> str:
    """Register a real catalog document, mirroring
    ``test_gh896_catalog_link_tokens._register_catalog_doc``."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    with HttpCatalogClient() as cat:
        owner = cat.register_owner(
            owner_name, "repo", repo_hash=f"footnotes-test-{owner_name}",
        )
        tumbler = cat.register(
            owner, title, content_type=content_type,
            physical_collection="knowledge__footnotes-test",
        )
    return str(tumbler)


class TestFootnotesCliConversion:
    def test_converts_file_in_place(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="CLI Test Doc", owner_name="footnotes-cli-convert")
        doc = tmp_path / "src.md"
        doc.write_text(f"See [x](nx://catalog/{tumbler}) for details.\n")

        result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 0, result.output

        body = doc.read_text()
        assert "[^tumbler-cli-test-doc]" in body
        assert "## Footnotes" in body
        assert "CLI Test Doc" in body

    def test_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Idempotent Doc", owner_name="footnotes-cli-idempotent")
        doc = tmp_path / "src.md"
        doc.write_text(f"[x](nx://catalog/{tumbler})\n")

        runner = CliRunner()
        r1 = runner.invoke(catalog, ["footnotes", str(doc)])
        assert r1.exit_code == 0, r1.output
        first_body = doc.read_text()

        r2 = runner.invoke(catalog, ["footnotes", str(doc)])
        assert r2.exit_code == 0, r2.output
        assert doc.read_text() == first_body
        assert "already up to date" in r2.output

    def test_check_mode_fails_on_unconverted_file_and_writes_nothing(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Check Doc", owner_name="footnotes-cli-check")
        doc = tmp_path / "src.md"
        original = f"[x](nx://catalog/{tumbler})\n"
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--check"])
        assert result.exit_code == 1, result.output
        assert doc.read_text() == original  # nothing written

    def test_check_mode_passes_on_converted_file(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Check Doc OK", owner_name="footnotes-cli-check-ok")
        doc = tmp_path / "src.md"
        doc.write_text(f"[x](nx://catalog/{tumbler})\n")

        runner = CliRunner()
        assert runner.invoke(catalog, ["footnotes", str(doc)]).exit_code == 0
        converted = doc.read_text()

        result = runner.invoke(catalog, ["footnotes", str(doc), "--check"])
        assert result.exit_code == 0, result.output
        assert doc.read_text() == converted

    def test_dry_run_writes_nothing_and_prints_a_diff(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Dry Run Doc", owner_name="footnotes-cli-dry-run")
        doc = tmp_path / "src.md"
        original = f"[x](nx://catalog/{tumbler})\n"
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert doc.read_text() == original  # nothing written
        assert "+" in result.output  # a unified diff was printed
        assert "Dry Run Doc" in result.output

    def test_to_links_reverses_a_converted_file(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Reverse Doc", owner_name="footnotes-cli-reverse")
        doc = tmp_path / "src.md"
        original = f"[reverse doc](nx://catalog/{tumbler}) trailing prose.\n"
        doc.write_text(original)

        runner = CliRunner()
        assert runner.invoke(catalog, ["footnotes", str(doc)]).exit_code == 0
        assert "## Footnotes" in doc.read_text()

        result = runner.invoke(catalog, ["footnotes", str(doc), "--to-links"])
        assert result.exit_code == 0, result.output
        assert doc.read_text() == original
        assert "## Footnotes" not in doc.read_text()

    def test_dangling_tumbler_writes_nothing_reports_and_exits_1(self, tmp_path: Path) -> None:
        """GH #896's own acceptance criterion, held literally
        (code-review round): an unresolvable tumbler means NOTHING is
        written for the file -- not even the surrounding text that
        would have otherwise been left alone. Every failure is still
        reported, and the file is byte-identical to before the run."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Doomed Doc", owner_name="footnotes-cli-dangling")
        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "src.md"
        original = f"line one\nline two [x](nx://catalog/{tumbler}) here\n"
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 1, result.output
        assert f"{doc}:2: unresolved tumbler {tumbler}" in result.output
        assert "not converted" in result.output
        assert doc.read_text() == original  # NOTHING written

    def test_mixed_resolvable_and_dangling_writes_nothing_at_all(self, tmp_path: Path) -> None:
        """The literal 'no PARTIAL write' case: one tumbler resolves
        fine, a second is dangling -- the whole file stays untouched,
        including the citation that would have converted cleanly on
        its own."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.commands.catalog import catalog

        good_tumbler = _register_real_doc(title="Good Doc", owner_name="footnotes-cli-mixed-good")
        bad_tumbler = _register_real_doc(title="Bad Doc", owner_name="footnotes-cli-mixed-bad")
        with HttpCatalogClient() as cat:
            cat.delete_document(bad_tumbler)

        doc = tmp_path / "src.md"
        original = (
            f"[good](nx://catalog/{good_tumbler})\n"
            f"[bad](nx://catalog/{bad_tumbler})\n"
        )
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 1, result.output
        assert f"unresolved tumbler {bad_tumbler}" in result.output
        assert doc.read_text() == original  # not even the "good" one converted

    def test_check_treats_dangling_tumbler_as_failure(self, tmp_path: Path) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Doomed Check Doc", owner_name="footnotes-cli-dangling-check")
        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "src.md"
        original = f"[x](nx://catalog/{tumbler})\n"
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--check"])
        assert result.exit_code == 1, result.output
        assert doc.read_text() == original

    def test_dry_run_shows_dangling_reference_without_writing(self, tmp_path: Path) -> None:
        """--dry-run always shows the FULL picture, dangling references
        included, even though the real write would be refused."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Dry Run Dangling", owner_name="footnotes-cli-dry-dangling")
        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "src.md"
        original = f"[x](nx://catalog/{tumbler})\n"
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--dry-run"])
        assert f"unresolved tumbler {tumbler}" in result.output
        assert doc.read_text() == original

    def test_check_and_dry_run_are_mutually_exclusive(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        doc = tmp_path / "src.md"
        doc.write_text("plain prose\n")
        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--check", "--dry-run"])
        assert result.exit_code == 2, result.output  # code-review round: flag misuse -> 2

    def test_check_and_to_links_are_mutually_exclusive(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        doc = tmp_path / "src.md"
        doc.write_text("plain prose\n")
        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--check", "--to-links"])
        assert result.exit_code == 2, result.output

    def test_dry_run_and_to_links_together_is_allowed(self, tmp_path: Path) -> None:
        """--dry-run + --to-links previews the REVERSE conversion --
        an entirely legitimate, explicitly supported combination."""
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Dry Reverse Doc", owner_name="footnotes-cli-dry-reverse")
        doc = tmp_path / "src.md"
        original_link = f"[x](nx://catalog/{tumbler})\n"
        doc.write_text(original_link)

        runner = CliRunner()
        assert runner.invoke(catalog, ["footnotes", str(doc)]).exit_code == 0
        converted = doc.read_text()
        assert "## Footnotes" in converted

        result = runner.invoke(catalog, ["footnotes", str(doc), "--to-links", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert doc.read_text() == converted  # nothing written
        assert "+" in result.output or "-" in result.output  # a diff was printed

    def test_refresh_and_to_links_are_mutually_exclusive(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        doc = tmp_path / "src.md"
        doc.write_text("plain prose\n")
        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--refresh", "--to-links"])
        assert result.exit_code == 2, result.output

    def test_refresh_flag_leaves_new_link_untouched(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Refresh New Doc", owner_name="footnotes-cli-refresh-new")
        doc = tmp_path / "src.md"
        original = f"[x](nx://catalog/{tumbler})\n"
        doc.write_text(original)

        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--refresh"])
        assert result.exit_code == 0, result.output
        assert doc.read_text() == original  # brand-new link untouched
        assert "already up to date" in result.output

    def test_style_short_option_produces_shorter_footnote_body(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Short Style Doc", owner_name="footnotes-cli-style-short")
        doc = tmp_path / "src.md"
        doc.write_text(f"[x](nx://catalog/{tumbler})\n")

        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--style", "short"])
        assert result.exit_code == 0, result.output
        body = doc.read_text()
        assert "Short Style Doc" in body
        assert "Content type" not in body

    def test_service_outage_exits_2(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from nexus.commands.catalog import catalog

        doc = tmp_path / "src.md"
        doc.write_text("[x](nx://catalog/1.1.1)\n")

        with patch(
            "nexus.commands.doc._open_catalog_link_reader",
            side_effect=RuntimeError("catalog unreachable"),
        ):
            result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 2, result.output
        assert "catalog" in result.output.lower()


class TestStrictUtf8AndAtomicWrite:
    """nexus-3ioz2 (CRITICAL, code-review round): the source file is
    read as strict UTF-8, never ``errors="replace"`` (which silently
    corrupts non-UTF-8 bytes and writes the corruption back over the
    original), and written atomically."""

    def test_invalid_utf8_bytes_refused_file_untouched(self, tmp_path: Path) -> None:
        doc = tmp_path / "src.md"
        # 0xFF is not valid UTF-8 anywhere; a real link is present too,
        # so a lossy read-and-rewrite would have silently corrupted it.
        invalid_bytes = b"[x](nx://catalog/1.1.1) \xff\xfe binary garbage\n"
        doc.write_bytes(invalid_bytes)

        from nexus.commands.catalog import catalog

        result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 2, result.output
        assert "not valid UTF-8" in result.output
        # byte-identical afterward -- not even a partial/lossy rewrite.
        assert doc.read_bytes() == invalid_bytes

    def test_write_uses_a_sibling_tmp_file_then_replaces(self, tmp_path: Path) -> None:
        """Mirrors nexus.commands.t3._save_backfill_state's tmp+rename
        pattern -- proven here by asserting no stray .tmp file survives
        a successful run (Path.replace() consumed it) and the final
        content is exactly what conversion produced."""
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Atomic Write Doc", owner_name="footnotes-cli-atomic")
        doc = tmp_path / "src.md"
        doc.write_text(f"[x](nx://catalog/{tumbler})\n")

        result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 0, result.output
        assert not doc.with_suffix(".tmp").exists()
        assert "Atomic Write Doc" in doc.read_text()

    def test_write_preserves_the_original_file_mode(self, tmp_path: Path) -> None:
        """Re-review round: the tmp file is created fresh (umask
        defaults, typically 0644) -- ``Path.replace()`` renames that
        inode into place, so without an explicit mode copy a 0600
        source file would silently become 0644 after conversion. Calls
        ``_atomic_write_text`` directly (a pure filesystem operation,
        no catalog/engine needed) against a real 0600 file."""
        import stat

        from nexus.commands.catalog_cmds.footnotes import _atomic_write_text

        doc = tmp_path / "secret.md"
        doc.write_text("original content\n")
        doc.chmod(0o600)
        assert stat.S_IMODE(doc.stat().st_mode) == 0o600

        _atomic_write_text(doc, "new content\n")

        assert doc.read_text() == "new content\n"
        assert stat.S_IMODE(doc.stat().st_mode) == 0o600, (
            "file mode was not preserved across the atomic replace"
        )

    def test_failed_write_cleans_up_tmp_and_leaves_target_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failure between creating the tmp file and the replace
        succeeding must never leave a stray ``.tmp`` file, and must
        never touch the real target at all."""
        from nexus.commands.catalog_cmds.footnotes import _atomic_write_text

        doc = tmp_path / "src.md"
        original = "original content, untouched\n"
        doc.write_text(original)

        def _boom(self: Path, target) -> None:  # noqa: ANN001 — matches Path.replace's signature
            raise OSError("simulated replace failure")

        monkeypatch.setattr(Path, "replace", _boom)

        with pytest.raises(OSError, match="simulated replace failure"):
            _atomic_write_text(doc, "would-be new content\n")

        assert not doc.with_suffix(".tmp").exists()
        assert doc.read_text() == original  # byte-identical, untouched
