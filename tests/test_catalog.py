# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tombstone (nexus-i711w terminal deletion): the local SQLite ``Catalog``
unit suite that lived here — 109 tests over register/resolve/link/span/alias/
rebuild/guard behavior of ``nexus.catalog.catalog.Catalog`` — retired with the
module itself. The service catalog's contract coverage lives in
``tests/catalog/`` (conformance, protocol fidelity, shape parity).

The one substrate-independent test survives below: it locks the source-URI
scheme registry in ``nexus.catalog.types``, which outlived the local catalog.
"""

from __future__ import annotations


class TestSourceUriRegistration:
    def test_known_uri_schemes_table_is_locked_to_planned_set(self):
        """Lock the scheme registry against silent additions OR
        shrinking. Phase 1: ``file`` + ``chroma``. Phase 4:
        ``nx-scratch`` (P4.1) + ``https`` (P4.2). nexus-bqda adds
        ``x-devonthink-item`` (macOS-only DT identity URLs).
        nexus-h2pm adds ``nx-orphan-backfill`` for synthetic
        Documents covering pre-catalog T3 chunks. Plain
        ``http`` is intentionally excluded — Phase 4's https reader
        does NOT cover plain http, so accepting http URIs at register
        would succeed silently and fail at extraction. Adding a new
        scheme requires landing the reader first AND updating this
        lock.
        """
        from nexus.catalog.types import _KNOWN_URI_SCHEMES
        assert _KNOWN_URI_SCHEMES == frozenset({
            "file", "chroma", "https", "nx-scratch", "x-devonthink-item",
            "nx-orphan-backfill",
        })
