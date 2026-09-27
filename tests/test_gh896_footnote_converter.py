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
        assert "tumbler 1.1.194[^tumbler-attention-is-all-you-need]" in result.text
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
        assert "tumbler[^tumbler-original-title]" in first.text

        e1_renamed = _FakeEntry(tumbler=_FakeTumbler((2, 2, 2)), title="Renamed Title")
        reader2 = _fake_reader({"2.2.2": e1_renamed})
        second = convert_links_to_footnotes(first.text, lambda: reader2)

        assert second.changed is True
        assert "tumbler[^tumbler-original-title]" in second.text  # marker UNCHANGED
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
        marker = "tumbler[^tumbler-doomed]"
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

        assert "a[^tumbler-alpha-paper]" in result.text
        assert "b[^tumbler-beta-paper]" in result.text
        assert result.text.count("## Footnotes") == 1

    def test_slug_collision_gets_numeric_suffix(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Same Title")
        e2 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 2)), title="Same Title")
        reader = _fake_reader({"1.1.1": e1, "1.1.2": e2})
        text = "[a](nx://catalog/1.1.1)\n[b](nx://catalog/1.1.2)\n"

        result = convert_links_to_footnotes(text, lambda: reader)

        assert "a[^tumbler-same-title]" in result.text
        assert "b[^tumbler-same-title-2]" in result.text

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
        assert "tumbler[^tumbler-real-doc]" in result.text
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

    def test_working_relative_link_with_base_dir_and_repo_root(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        (repo_root / "docs").mkdir(parents=True)
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 1)), title="Doc", file_path="docs/x.md")
        reader = _fake_reader({"1.1.1": e1})
        reader.get_owner_by_prefix.return_value = {"repo_root": str(repo_root)}
        text = "[x](nx://catalog/1.1.1)\n"

        result = convert_links_to_footnotes(text, lambda: reader, base_dir=tmp_path)

        assert "[repo/docs/x.md](repo/docs/x.md)." in result.text


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
            "tumbler 1.1.194[^tumbler-x]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-x]: nx catalog tumbler `1.1.194`. Title: "X". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert result.text == "[tumbler 1.1.194](nx://catalog/1.1.194)\n"
        assert result.dangling == []

    def test_unknown_slug_left_as_marker_and_reported(self) -> None:
        text = (
            "orphan[^tumbler-ghost]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-real]: nx catalog tumbler `1.1.1`. Title: "Real". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert "orphan[^tumbler-ghost]" in result.text  # left untouched
        assert len(result.dangling) == 1
        assert result.dangling[0].tumbler == "tumbler-ghost"

    def test_fenced_code_left_untouched(self) -> None:
        text = (
            "```\nexample[^tumbler-x]\n```\n"
            "real[^tumbler-x]\n\n"
            "## Footnotes\n\n"
            '[^tumbler-x]: nx catalog tumbler `1.1.1`. Title: "X". Content type: paper.\n'
        )
        result = convert_footnotes_to_links(text)
        assert "example[^tumbler-x]" in result.text  # inside fence, untouched
        assert "[real](nx://catalog/1.1.1)" in result.text


class TestRoundTrip:
    """Acceptance criterion: links -> footnotes -> --to-links reproduces
    the original. EXACT whenever each citation is the first thing on its
    own line (see the module docstring on the label-recovery boundary)."""

    def test_single_tumbler_round_trip(self) -> None:
        e1 = _FakeEntry(tumbler=_FakeTumbler((1, 1, 194)), title="Attention Is All You Need")
        reader = _fake_reader({"1.1.194": e1})
        original = "[tumbler 1.1.194](nx://catalog/1.1.194) for the source.\n"

        footnoted = convert_links_to_footnotes(original, lambda: reader)
        back = convert_footnotes_to_links(footnoted.text)

        assert back.text == original
        assert back.dangling == []

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

    def test_dangling_tumbler_left_as_link_reported_and_exits_nonzero(self, tmp_path: Path) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.commands.catalog import catalog

        tumbler = _register_real_doc(title="Doomed Doc", owner_name="footnotes-cli-dangling")
        with HttpCatalogClient() as cat:
            cat.delete_document(tumbler)

        doc = tmp_path / "src.md"
        doc.write_text(f"line one\nline two [x](nx://catalog/{tumbler}) here\n")

        result = CliRunner().invoke(catalog, ["footnotes", str(doc)])
        assert result.exit_code == 1, result.output
        assert f"{doc}:2: unresolved tumbler {tumbler}" in result.output
        # left as a link, not silently dropped
        assert f"[x](nx://catalog/{tumbler})" in doc.read_text()

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

    def test_check_and_dry_run_are_mutually_exclusive(self, tmp_path: Path) -> None:
        from nexus.commands.catalog import catalog

        doc = tmp_path / "src.md"
        doc.write_text("plain prose\n")
        result = CliRunner().invoke(catalog, ["footnotes", str(doc), "--check", "--dry-run"])
        assert result.exit_code != 0

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
