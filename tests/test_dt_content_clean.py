# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mok9x: boilerplate stripping for --dt-content web-archive text.

The measured incident (tumbler 1.12.102): 59% of a CACM article's indexed
chars were Cookiebot output + base64 image payloads, 32/70 chunks matched a
cookie-tracking query, and the junk outranked the article's own subject.
These tests pin the two cleaning passes (data-URI payloads, signature-dense
consent runs), the run threshold that PROTECTS prose that merely discusses
cookies, and the writer wiring (strip-before-cache, loud warning past the
ratio, all-boilerplate fail-soft).
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.dt_content_clean import (
    BOILERPLATE_WARN_RATIO,
    CleanResult,
    clean_dt_content,
)

_ARTICLE = "\n".join(
    f"Paragraph {i}: formal reasoning meets large language models in Lean proofs."
    for i in range(20)
)

# The measured Cookiebot shape: name/provider lines interleaved with the
# regular signature pairs.
_COOKIE_WALL = """\
We use cookies to personalise content and analyse our traffic.
Consent Selection
Necessary cookies help make a website usable.
CookieConsent
Stores the user's cookie consent state
Maximum Storage Duration: 1 year
Type: HTTP Cookie
_ga
Registers a unique ID for statistics.
Maximum Storage Duration: 2 years
Type: HTTP Cookie
_gid
Statistics cookies help owners understand visitor interaction.
Maximum Storage Duration: 1 day
Type: HTTP Cookie
Marketing cookies are used to track visitors across websites.
test_cookie
Maximum Storage Duration: 1 day
Type: HTTP Cookie
"""

_DATA_URI_LINE = (
    "logo: data:image/png;base64,"
    + "iVBORw0KGgoAAAANSUhEUgAA" * 20
    + "\n"
)


def test_cookie_wall_before_article_is_dropped_whole():
    result = clean_dt_content(_COOKIE_WALL + "\n" + _ARTICLE)
    assert "Maximum Storage Duration" not in result.text
    assert "Consent Selection" not in result.text
    assert "formal reasoning" in result.text
    assert result.consent_runs_removed >= 1
    # The article body must survive intact (every paragraph).
    assert result.text.count("Paragraph") == 20


def test_data_uri_payloads_are_replaced_not_indexed():
    result = clean_dt_content(_DATA_URI_LINE + _ARTICLE)
    assert "iVBORw0KGgo" not in result.text
    assert result.data_uris_removed == 1
    assert "formal reasoning" in result.text


def test_prose_discussing_cookies_survives():
    """The run threshold is the safety mechanism: an article ABOUT consent
    UX mentions the vocabulary in isolation, never as a dense run."""
    prose = (
        "The paper studies consent dialogs. We use cookies as the running "
        "example of a dark pattern.\n"
        + _ARTICLE
        + "\nIts appendix notes that a cookie policy is often unread.\n"
    )
    result = clean_dt_content(prose)
    assert "running example" in result.text
    assert "appendix" in result.text
    assert result.consent_runs_removed == 0


def test_measured_incident_shape_ratio_crosses_warn_threshold():
    """59% junk on the measured document — a synthetic reproduction of that
    shape must land past BOILERPLATE_WARN_RATIO."""
    junk_heavy = _COOKIE_WALL * 6 + _DATA_URI_LINE * 3 + "\n" + _ARTICLE[: len(_ARTICLE) // 2]
    result = clean_dt_content(junk_heavy)
    assert result.stripped_ratio > BOILERPLATE_WARN_RATIO
    assert "Type: HTTP Cookie" not in result.text


def test_clean_text_is_untouched():
    result = clean_dt_content(_ARTICLE)
    assert result.text == _ARTICLE
    assert result.stripped_chars == 0
    assert result.stripped_ratio == 0.0
    assert result.consent_runs_removed == 0
    assert result.data_uris_removed == 0


def test_data_uri_payload_threshold_boundary():
    """Payloads under the 64-char threshold stay (too short to be a real
    embedded asset; stripping them risks eating short legitimate tokens)."""
    short = "icon: data:image/png;base64," + "A" * 63
    long = "icon: data:image/png;base64," + "A" * 64
    assert clean_dt_content(short).data_uris_removed == 0
    assert clean_dt_content(long).data_uris_removed == 1


def test_non_base64_data_uri_is_untouched():
    text = "inline: data:text/html,<p>hello</p>\n" + _ARTICLE
    result = clean_dt_content(text)
    assert result.data_uris_removed == 0
    assert "data:text/html" in result.text


def test_near_miss_lines_are_not_signatures():
    """Bare product-ish tokens ('CookieConsent', a lone 'Type:' line) must
    not count toward a run — only the specific multi-word phrases do."""
    near_misses = "CookieConsent\nType: session\ncookies\n" * 3 + _ARTICLE
    result = clean_dt_content(near_misses)
    assert result.consent_runs_removed == 0
    assert result.text.count("CookieConsent") == 3


def test_empty_input_is_stable():
    result = clean_dt_content("")
    assert result == CleanResult(
        text="", original_chars=0, stripped_chars=0,
        data_uris_removed=0, consent_runs_removed=0,
    )
    assert result.stripped_ratio == 0.0


# ── writer wiring (mirrors tests/test_dt_content_layer_d.py's mock shape) ──


@patch("nexus.mcp_client.devonthink.dt_record_name", return_value="CACM Article")
@patch("nexus.mcp_client.devonthink.dt_extract_content")
def test_writer_strips_before_caching(mock_extract, _name, tmp_path, monkeypatch):
    from nexus.commands.dt import _index_dt_content_record

    mock_extract.return_value = _COOKIE_WALL + "\n" + _ARTICLE
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)

    written: dict = {}

    def fake_index_markdown(path, **kwargs):
        written["content"] = path.read_text(encoding="utf-8")
        return 5

    monkeypatch.setattr("nexus.doc_indexer.index_markdown", fake_index_markdown)
    monkeypatch.setattr(
        "nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **k: True
    )

    assert _index_dt_content_record(
        "UUID-CLEAN-1", collection="knowledge__t__bge-base-en-v15-768__v1", corpus="dt"
    ) is True
    assert "Maximum Storage Duration" not in written["content"]
    assert "formal reasoning" in written["content"]


@patch("nexus.mcp_client.devonthink.dt_record_name", return_value="Pure Junk")
@patch("nexus.mcp_client.devonthink.dt_extract_content")
def test_writer_all_boilerplate_fails_soft(mock_extract, _name, tmp_path, monkeypatch):
    from nexus.commands.dt import _index_dt_content_record

    mock_extract.return_value = _COOKIE_WALL
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)
    called: list = []
    monkeypatch.setattr(
        "nexus.doc_indexer.index_markdown", lambda *a, **k: called.append(1) or 5
    )

    assert _index_dt_content_record(
        "UUID-JUNK-1", collection="knowledge__t__bge-base-en-v15-768__v1", corpus="dt"
    ) is False
    assert called == [], "an all-boilerplate husk must never reach index_markdown"
