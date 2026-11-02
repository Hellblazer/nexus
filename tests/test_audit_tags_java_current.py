# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-238 Phase 1 Step 1 (nexus-qo7ie.6): the generated Java mirror of AUDIT_TAGS is current.

``AuditTags.java`` is rendered from ``audit-tags.toml`` by ``scripts/gen_audit_tags_java.py``.
This is the Python half of the pin (``AuditTagsTest`` is the Java half and reads the TOML on its
own): a table edit without a regeneration is red here, on the default loop.
"""
from __future__ import annotations

import gen_audit_tags_java as gen  # scripts/ is on the test pythonpath (pyproject)

from nexus.tables import audit_tags


def test_generated_java_equals_what_the_generator_renders_now():
    assert gen.OUTPUT.exists(), f"{gen.OUTPUT} is missing; run the generator with --write"
    assert gen.OUTPUT.read_bytes() == gen.render().encode("utf-8"), (
        "AuditTags.java is stale; run `uv run python scripts/gen_audit_tags_java.py --write`"
    )


def test_generated_java_names_every_tag_and_states_the_rule():
    text = gen.OUTPUT.read_text(encoding="utf-8")
    assert len(audit_tags.AUDIT_TAGS) == 6
    for tag in audit_tags.AUDIT_TAGS:
        assert f'"{tag}"' in text, tag
    for needle in ("split on ','", "trim", "case-sensitive", "whole tag"):
        assert needle in text, needle


def test_a_stale_file_is_caught(tmp_path, monkeypatch):
    """Non-vacuity: the check mode fails on a file that differs from the render."""
    stale = tmp_path / "AuditTags.java"
    stale.write_text(gen.render().replace('"critique"', '"critiquex"'), encoding="utf-8")
    monkeypatch.setattr(gen, "OUTPUT", stale)
    assert gen.main(["--check"]) == 1


def test_a_crlf_file_is_caught(tmp_path, monkeypatch):
    crlf = tmp_path / "AuditTags.java"
    crlf.write_bytes(gen.render().replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(gen, "OUTPUT", crlf)
    assert gen.main(["--check"]) == 1


def test_a_missing_file_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "OUTPUT", tmp_path / "Absent.java")
    assert gen.main(["--check"]) == 1


def test_generated_java_names_every_title_pattern_and_states_the_rule():
    text = gen.OUTPUT.read_text(encoding="utf-8")
    assert len(audit_tags.TITLE_PATTERNS) == 9
    for pattern in audit_tags.TITLE_PATTERNS:
        assert gen.java_string(pattern) in text, pattern
    for needle in ("case-insensitively", "anywhere in the title", "quantifier"):
        assert needle in text, needle


def test_a_pattern_gained_on_one_side_is_caught(tmp_path, monkeypatch):
    """Non-vacuity of the parity: the checked-in file differs from a render that has one more pattern."""
    monkeypatch.setattr(audit_tags, "TITLE_PATTERNS", (*audit_tags.TITLE_PATTERNS, "(^|[-_])audit([-_]|$)"))
    stale = tmp_path / "AuditTags.java"
    stale.write_bytes(gen.OUTPUT.read_bytes())
    monkeypatch.setattr(gen, "OUTPUT", stale)
    assert gen.main(["--check"]) == 1
