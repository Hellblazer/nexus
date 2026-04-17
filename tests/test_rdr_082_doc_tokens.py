# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for RDR-082 Doc-Build Token Resolution.

Four things under test:

  1. Token grammar (``{{NAMESPACE:KEY[.FIELD][|FILTER=VALUE]*}}``) —
     positive grammar, malformed rejection, fenced-code-block skip.
  2. Bead / RDR resolvers — correct field routing, unknown-key → raise.
  3. Render engine — parse → resolve → substitute; unresolved tokens
     fail loud unless ``--allow-unresolved`` is set.
  4. Resolver registry — third parties can register additional
     namespaces without modifying the parser / engine / CLI. This is
     the extension point RDR-083 will consume for ``nx-anchor`` etc.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Token grammar ────────────────────────────────────────────────────────────


class TestTokenParse:

    def test_bare_namespace_and_key(self) -> None:
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("status: {{bd:nexus-123}}")
        assert len(toks) == 1
        t = toks[0]
        assert t.namespace == "bd"
        assert t.key == "nexus-123"
        assert t.field is None
        assert t.filters == {}

    def test_dotted_field(self) -> None:
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("RDR-072 {{rdr:072.status}}")
        assert len(toks) == 1
        assert toks[0].namespace == "rdr"
        assert toks[0].key == "072"
        assert toks[0].field == "status"

    def test_filter_key_value(self) -> None:
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("{{rdr:072.title|raw=true}}")
        assert toks[0].filters == {"raw": "true"}

    def test_multiple_filters(self) -> None:
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("{{nx-anchor:collection|top=5|sort=desc}}")
        assert toks[0].filters == {"top": "5", "sort": "desc"}

    def test_multiple_tokens_in_one_line(self) -> None:
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("{{bd:a.status}} and {{rdr:072.title}}")
        assert len(toks) == 2
        assert toks[0].namespace == "bd" and toks[1].namespace == "rdr"

    def test_malformed_empty_namespace_rejected(self) -> None:
        """Malformed tokens must not accidentally match — a caller edit
        like ``{{:foo}}`` should parse to zero tokens, not a broken one."""
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("oops {{:foo}} oops")
        assert toks == []

    def test_malformed_no_colon_rejected(self) -> None:
        from nexus.doc.tokens import parse_tokens

        toks = parse_tokens("{{no-colon-here}}")
        assert toks == []

    def test_token_inside_fenced_code_skipped(self) -> None:
        """Tutorial snippets showing the token syntax must not be
        resolved. A fenced code block is an intentional no-op zone."""
        from nexus.doc.tokens import parse_tokens

        md = (
            "Docs:\n"
            "```\n"
            "Write {{bd:nexus-123}} in your markdown.\n"
            "```\n"
            "Real token: {{rdr:082.status}}\n"
        )
        toks = parse_tokens(md)
        assert len(toks) == 1
        assert toks[0].namespace == "rdr" and toks[0].key == "082"

    def test_token_inside_tilde_fenced_code_skipped(self) -> None:
        from nexus.doc.tokens import parse_tokens

        md = (
            "~~~\n"
            "{{bd:x.status}}\n"
            "~~~\n"
            "{{bd:real.status}}\n"
        )
        toks = parse_tokens(md)
        assert len(toks) == 1 and toks[0].key == "real"

    def test_span_reports_line_and_col(self) -> None:
        from nexus.doc.tokens import parse_tokens

        md = "line1\nnothing here\nprefix {{bd:a.status}} suffix\n"
        toks = parse_tokens(md)
        assert len(toks) == 1
        assert toks[0].lineno == 3
        assert toks[0].col == md.splitlines()[2].index("{{") + 1


# ── Resolvers ────────────────────────────────────────────────────────────────


class TestBeadResolver:

    def test_status_field_routes_to_bd_show_json(self) -> None:
        from nexus.doc.resolvers import BeadResolver

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "id": "nexus-123",
                    "title": "T",
                    "status": "closed",
                    "assignee": "hal",
                }),
            )
            r = BeadResolver()
            value = r.resolve("nexus-123", field="status", filters={})
        assert value == "closed"

    def test_default_field_returns_title(self) -> None:
        from nexus.doc.resolvers import BeadResolver

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"id": "x", "title": "Ship feature", "status": "open"}),
            )
            r = BeadResolver()
            # Default when caller omits .field → title
            value = r.resolve("x", field=None, filters={})
        assert value == "Ship feature"

    def test_unknown_bead_raises(self) -> None:
        from nexus.doc.resolvers import BeadResolver, ResolutionError

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")
            r = BeadResolver()
            with pytest.raises(ResolutionError):
                r.resolve("does-not-exist", field=None, filters={})

    def test_per_render_cache_avoids_double_subprocess(self) -> None:
        from nexus.doc.resolvers import BeadResolver

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"id": "x", "title": "T", "status": "open"}),
            )
            r = BeadResolver()
            r.resolve("x", field="title", filters={})
            r.resolve("x", field="status", filters={})
        # Two field reads of the same bead → one subprocess call.
        assert mock_run.call_count == 1


class TestRdrResolver:

    def test_reads_frontmatter_status(self, tmp_path: Path) -> None:
        from nexus.doc.resolvers import RdrResolver

        rdr_dir = tmp_path / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "rdr-072-foo.md").write_text(
            "---\n"
            "title: \"RDR-072: Foo\"\n"
            "status: accepted\n"
            "type: feature\n"
            "---\n\n# RDR-072: Foo\n"
        )
        r = RdrResolver(rdr_dir=rdr_dir)
        assert r.resolve("072", field="status", filters={}) == "accepted"
        assert r.resolve("072", field="title", filters={}) == "RDR-072: Foo"

    def test_unknown_rdr_raises(self, tmp_path: Path) -> None:
        from nexus.doc.resolvers import RdrResolver, ResolutionError

        (tmp_path / "docs" / "rdr").mkdir(parents=True)
        r = RdrResolver(rdr_dir=tmp_path / "docs" / "rdr")
        with pytest.raises(ResolutionError):
            r.resolve("999", field=None, filters={})

    def test_resolves_uppercase_RDR_prefix(self, tmp_path: Path) -> None:
        """nexus-51j: projects using the RDR-NNN-*.md (uppercase) convention
        must resolve the same as lowercase."""
        from nexus.doc.resolvers import RdrResolver

        rdr_dir = tmp_path / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "RDR-068-masking-field-plan-learning.md").write_text(
            "---\n"
            "title: \"RDR-068: Masking Field Plan Learning\"\n"
            "status: accepted\n"
            "---\n"
        )
        r = RdrResolver(rdr_dir=rdr_dir)
        assert r.resolve("068", field="status", filters={}) == "accepted"

    def test_mixed_case_cohabitation(self, tmp_path: Path) -> None:
        """Lowercase and uppercase RDRs in the same directory are both found."""
        from nexus.doc.resolvers import RdrResolver

        rdr_dir = tmp_path / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "rdr-072-lowercase.md").write_text(
            "---\nstatus: draft\n---\n"
        )
        (rdr_dir / "RDR-073-uppercase.md").write_text(
            "---\nstatus: accepted\n---\n"
        )
        r = RdrResolver(rdr_dir=rdr_dir)
        assert r.resolve("072", field="status", filters={}) == "draft"
        assert r.resolve("073", field="status", filters={}) == "accepted"

    def test_zero_padding_still_works_case_insensitive(
        self, tmp_path: Path,
    ) -> None:
        """Numeric key with or without zero-padding resolves against
        either case of prefix. Regression: the original impl had a
        zero-padding fallback; that behaviour is preserved."""
        from nexus.doc.resolvers import RdrResolver

        rdr_dir = tmp_path / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "RDR-9-foo.md").write_text("---\nstatus: x\n---\n")
        r = RdrResolver(rdr_dir=rdr_dir)
        # Padded key
        assert r.resolve("009", field="status", filters={}) == "x"
        # Unpadded key
        assert r.resolve("9", field="status", filters={}) == "x"

    def test_non_numeric_key_case_insensitive(self, tmp_path: Path) -> None:
        """Non-numeric keys also honour case-insensitive matching."""
        from nexus.doc.resolvers import RdrResolver

        rdr_dir = tmp_path / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "RDR-proposal-alpha-notes.md").write_text(
            "---\nstatus: draft\n---\n"
        )
        r = RdrResolver(rdr_dir=rdr_dir)
        assert r.resolve("proposal", field="status", filters={}) == "draft"


# ── Render engine ────────────────────────────────────────────────────────────


class TestRenderer:

    def test_substitutes_tokens(self) -> None:
        from nexus.doc.render import render_text
        from nexus.doc.resolvers import ResolverRegistry

        fake_bd = MagicMock()
        fake_bd.resolve = lambda key, field, filters: f"bd-{key}-{field or 'default'}"
        fake_rdr = MagicMock()
        fake_rdr.resolve = lambda key, field, filters: f"rdr-{key}-{field or 'default'}"
        reg = ResolverRegistry({"bd": fake_bd, "rdr": fake_rdr})

        md = "Status: {{bd:x.status}} and RDR: {{rdr:072.title}}"
        out, resolved, misses = render_text(md, reg)
        assert out == "Status: bd-x-status and RDR: rdr-072-title"
        assert resolved == 2
        assert misses == []

    def test_unresolved_namespace_raises(self) -> None:
        from nexus.doc.render import render_text, RenderError
        from nexus.doc.resolvers import ResolverRegistry

        reg = ResolverRegistry({})  # empty — no resolver for any namespace
        with pytest.raises(RenderError):
            render_text("{{bd:nexus-1.status}}", reg)

    def test_resolver_error_becomes_render_error(self) -> None:
        from nexus.doc.render import render_text, RenderError
        from nexus.doc.resolvers import ResolutionError, ResolverRegistry

        fake = MagicMock()
        fake.resolve = MagicMock(side_effect=ResolutionError("unknown bead"))
        reg = ResolverRegistry({"bd": fake})
        with pytest.raises(RenderError):
            render_text("{{bd:x.status}}", reg)

    def test_allow_unresolved_keeps_literal(self) -> None:
        """``--allow-unresolved`` (best-effort) preserves the token text
        in the output instead of raising."""
        from nexus.doc.render import render_text
        from nexus.doc.resolvers import ResolverRegistry

        reg = ResolverRegistry({})
        out, resolved, misses = render_text(
            "{{bd:x.status}} and plain text",
            reg,
            allow_unresolved=True,
        )
        assert "{{bd:x.status}}" in out
        assert resolved == 0
        assert len(misses) == 1
        assert "no resolver for namespace 'bd'" in misses[0][1]

    def test_tokens_inside_fenced_code_untouched(self) -> None:
        from nexus.doc.render import render_text
        from nexus.doc.resolvers import ResolverRegistry

        fake = MagicMock()
        fake.resolve = lambda key, field, filters: "RESOLVED"
        reg = ResolverRegistry({"bd": fake})
        md = (
            "```\n"
            "Sample: {{bd:x.status}}\n"
            "```\n"
            "Real: {{bd:y.status}}\n"
        )
        out, resolved, _ = render_text(md, reg)
        # Fenced instance preserved verbatim; non-fenced substituted
        assert "```\nSample: {{bd:x.status}}\n```\n" in out
        assert "Real: RESOLVED" in out
        assert resolved == 1


# ── Extension point — RDR-083 forward-compat ─────────────────────────────────


class TestResolverRegistryExtensibility:
    """RDR-083's AnchorResolver must plug in without touching parser,
    engine, or CLI. This test protects that invariant."""

    def test_mirror_tree_preserves_relative_path_under_out_dir(
        self, tmp_path: Path,
    ) -> None:
        """Gate fix (082 Sig #2): --out-dir must mirror the source tree,
        not flatten every rendered file to the same directory."""
        from nexus.doc.render import render_file
        from nexus.doc.resolvers import ResolverRegistry

        # Build: <tmp>/src/docs/rdr/doc.md
        root = tmp_path / "src"
        nested = root / "docs" / "rdr"
        nested.mkdir(parents=True)
        source = nested / "doc.md"
        source.write_text("no tokens here\n")

        out_dir = tmp_path / "out"
        render_file(
            source, ResolverRegistry({}), out_dir=out_dir,
            emit=True, source_root=root,
        )
        expected = out_dir / "docs" / "rdr" / "doc.rendered.md"
        assert expected.exists(), (
            f"mirror-tree destination not found: {expected}. "
            f"Directory contents: {list(out_dir.rglob('*'))}"
        )

    def test_resolved_count_excludes_failed_resolutions(self) -> None:
        """Gate fix (082 Sig #1): render_file's resolved count must NOT
        include tokens that raised ResolutionError. Previously it counted
        every registered-namespace token as resolved regardless of outcome."""
        from nexus.doc.render import render_text
        from nexus.doc.resolvers import ResolutionError, ResolverRegistry

        fake = MagicMock()

        def _resolve(key, field, filters):
            if key == "bad":
                raise ResolutionError("boom")
            return "OK"

        fake.resolve = _resolve
        reg = ResolverRegistry({"bd": fake})

        md = "{{bd:good.x}} and {{bd:bad.x}} and {{bd:good.y}}"
        out, resolved, misses = render_text(md, reg, allow_unresolved=True)
        assert resolved == 2, (
            f"Expected 2 resolved, got {resolved}. Misses: {misses}"
        )
        assert len(misses) == 1
        assert "boom" in misses[0][1]

    def test_third_party_namespace_registers_and_resolves(self) -> None:
        from nexus.doc.render import render_text
        from nexus.doc.resolvers import ResolverRegistry

        class FakeAnchor:
            def resolve(self, key, field, filters):
                top = filters.get("top", "3")
                return f"top-{top} topics for {key}"

        reg = ResolverRegistry({})
        reg.register("nx-anchor", FakeAnchor())
        out, _, _ = render_text("Anchors: {{nx-anchor:docs__x|top=5}}", reg)
        assert "top-5 topics for docs__x" in out
