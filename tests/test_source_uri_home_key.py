# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-n3md: ``_source_uri_home_key`` must collapse DEVONthink
URIs (opaque UUID-netlocked handles per RDR-099) to a single bucket
and keep empty URIs in a distinct bucket from real file homes.

Pre-fix the function returned ``<scheme>://<uuid>`` per DEVONthink
chunk, making every imported item look like its own home. The
2026-05-08 audit reported 110+ homes for ``knowledge__art-
grossberg-papers__voyage-context-3__v1`` when the collection has
at most 4 logical roots.
"""
from __future__ import annotations

from nexus.commands.catalog import (
    _DEVONTHINK_HOME_KEY,
    _EMPTY_HOME_KEY,
    _source_uri_home_key,
)


class TestFileUri:
    def test_file_uri_groups_by_first_four_path_segments(self) -> None:
        """``file://``-rooted URIs at any depth under the same repo
        return the same key, so an audit can count "homes per
        collection" without inflating by per-file granularity.
        """
        a = "file:///Users/hal.hildebrand/git/ART/docs/rdr/X.md"
        b = "file:///Users/hal.hildebrand/git/ART/docs/something/else/Y.md"
        c = "file:///Users/hal.hildebrand/git/nexus/src/foo.py"
        assert _source_uri_home_key(a) == _source_uri_home_key(b)
        assert _source_uri_home_key(a) != _source_uri_home_key(c)


class TestDevonthinkUri:
    """nexus-n3md primary fix. Reverting the special-case for
    ``scheme == 'x-devonthink-item'`` returns the per-UUID
    ``<scheme>://<uuid>`` shape and these assertions fail.
    """

    def test_distinct_uuids_collapse_to_one_bucket(self) -> None:
        """Two DEVONthink items with different UUIDs must map to
        the same key. RDR-099 treats the UUID as opaque; the audit
        must not count them as separate "homes".
        """
        u1 = "x-devonthink-item://CB1234AB-5678-90AB-CDEF-1234567890AB"
        u2 = "x-devonthink-item://DC9876FE-3210-FEDC-BA98-76543210FEDC"
        assert _source_uri_home_key(u1) == _source_uri_home_key(u2)
        assert _source_uri_home_key(u1) == _DEVONTHINK_HOME_KEY

    def test_devonthink_does_not_collide_with_file_or_empty(self) -> None:
        """The DEVONthink bucket is its own; it must not match the
        file:// home bucket or the empty bucket so the audit can
        report DEVONthink contributions separately from file imports.
        """
        dt = _source_uri_home_key(
            "x-devonthink-item://CB1234AB-5678-90AB-CDEF-1234567890AB"
        )
        file_home = _source_uri_home_key(
            "file:///Users/hal.hildebrand/git/ART/X.md"
        )
        empty = _source_uri_home_key("")
        assert dt != file_home
        assert dt != empty


class TestEmptySourceUri:
    """nexus-n3md secondary fix. Empty source_uri rows (knowledge
    notes with no source file) must form their OWN bucket so a
    single self-marker row doesn't flip a small collection from
    clean to contaminated.
    """

    def test_empty_uri_returns_empty_sentinel(self) -> None:
        assert _source_uri_home_key("") == _EMPTY_HOME_KEY

    def test_empty_uri_distinct_from_any_real_home(self) -> None:
        """The empty bucket must not match a real file home. Audit
        contamination logic (``≥ 2 distinct non-empty homes``) can
        then filter the empty key explicitly.
        """
        empty = _source_uri_home_key("")
        file_home = _source_uri_home_key(
            "file:///Users/hal.hildebrand/git/ART/X.md"
        )
        assert empty != file_home


class TestOtherSchemes:
    def test_http_uri_keys_on_scheme_and_netloc(self) -> None:
        """Non-file/non-DEVONthink URIs keep the prior
        ``<scheme>://<netloc>`` shape so other curators (web
        imports etc.) cluster by host.
        """
        a = _source_uri_home_key("https://arxiv.org/abs/1234.5678")
        b = _source_uri_home_key("https://arxiv.org/abs/2345.6789")
        c = _source_uri_home_key("https://example.com/page")
        assert a == b == "https://arxiv.org"
        assert a != c
