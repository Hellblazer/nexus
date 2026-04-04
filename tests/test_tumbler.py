# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from nexus.catalog.tumbler import Tumbler


class TestTumblerParse:
    def test_parse_three_segments(self):
        t = Tumbler.parse("1.2.42")
        assert t.segments == (1, 2, 42)

    def test_parse_four_segments(self):
        t = Tumbler.parse("1.2.42.7")
        assert t.segments == (1, 2, 42, 7)

    def test_parse_two_segments(self):
        t = Tumbler.parse("1.1")
        assert t.segments == (1, 1)

    def test_parse_single_segment(self):
        t = Tumbler.parse("5")
        assert t.segments == (5,)

    def test_parse_invalid_non_numeric(self):
        with pytest.raises(ValueError):
            Tumbler.parse("1.abc.3")

    def test_parse_empty_string(self):
        with pytest.raises(ValueError):
            Tumbler.parse("")


class TestTumblerRoundtrip:
    def test_roundtrip_three(self):
        t = Tumbler.parse("1.2.42")
        assert str(t) == "1.2.42"

    def test_roundtrip_four(self):
        t = Tumbler.parse("1.2.42.7")
        assert str(t) == "1.2.42.7"


class TestTumblerProperties:
    def test_store(self):
        t = Tumbler.parse("1.2.42")
        assert t.store == 1

    def test_owner(self):
        t = Tumbler.parse("1.2.42")
        assert t.owner == 2

    def test_document(self):
        t = Tumbler.parse("1.2.42")
        assert t.document == 42

    def test_chunk_present(self):
        t = Tumbler.parse("1.2.42.7")
        assert t.chunk == 7

    def test_chunk_absent(self):
        t = Tumbler.parse("1.2.42")
        assert t.chunk is None


class TestTumblerPrefix:
    def test_owner_is_prefix_of_doc(self):
        owner = Tumbler.parse("1.1")
        doc = Tumbler.parse("1.1.42")
        assert owner.is_prefix_of(doc)

    def test_doc_not_prefix_of_owner(self):
        owner = Tumbler.parse("1.1")
        doc = Tumbler.parse("1.1.42")
        assert not doc.is_prefix_of(owner)

    def test_self_is_prefix_of_self(self):
        t = Tumbler.parse("1.2.3")
        assert t.is_prefix_of(t)

    def test_different_branch_not_prefix(self):
        a = Tumbler.parse("1.1")
        b = Tumbler.parse("1.2.42")
        assert not a.is_prefix_of(b)


class TestTumblerAddressMethods:
    def test_document_address(self):
        chunk = Tumbler.parse("1.1.42.7")
        assert chunk.document_address() == Tumbler.parse("1.1.42")

    def test_owner_address(self):
        chunk = Tumbler.parse("1.1.42.7")
        assert chunk.owner_address() == Tumbler.parse("1.1")

    def test_document_address_from_doc_level(self):
        doc = Tumbler.parse("1.1.42")
        assert doc.document_address() == doc


class TestTumblerEquality:
    def test_equal(self):
        a = Tumbler.parse("1.2.3")
        b = Tumbler.parse("1.2.3")
        assert a == b

    def test_not_equal(self):
        a = Tumbler.parse("1.2.3")
        b = Tumbler.parse("1.2.4")
        assert a != b

    def test_hashable(self):
        t = Tumbler.parse("1.2.3")
        s = {t, t}
        assert len(s) == 1

    def test_usable_as_dict_key(self):
        t = Tumbler.parse("1.2.3")
        d = {t: "val"}
        assert d[Tumbler.parse("1.2.3")] == "val"
