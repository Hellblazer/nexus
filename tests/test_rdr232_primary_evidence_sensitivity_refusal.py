# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F5 (nexus-rfvyy.7.11): the primary-evidence selector's per-batch chunk fetch swallowed
every exception as "those chunks are missing and said so". A refused ``exclude`` read
(``SensitivityExcludeUnavailable``) is not a missing chunk: the primary read must refuse, not hand the operator a
document with silently absent text."""
from __future__ import annotations

import pytest

from nexus.db.http_vector_client import SensitivityExcludeUnavailable
from nexus.plans.primary_evidence import resolve_named_sources, select_primary_evidence
from tests.test_rdr232_primary_evidence import ART_DOC, ART_OWNER, ART_PATH, RDR084, FakeCatalog, FakeT3, _doc, _resolved


def _selection(fetch):
    cat = FakeCatalog()
    _doc(cat, FakeT3(), ART_DOC, ART_PATH, ["## Proposed Solution\nthe text of the document"])
    res = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat)
    return select_primary_evidence(res, cat=cat, fetch_chunks=fetch)


def test_a_refused_fetch_is_not_reported_as_missing_chunks():
    def refusing(collection, chashes):
        raise SensitivityExcludeUnavailable("sensitivity='exclude' read refused: the engine does not filter")

    with pytest.raises(SensitivityExcludeUnavailable):
        _selection(refusing)


def test_the_control_an_ordinary_fetch_failure_is_still_isolated_and_counted():
    def broken(collection, chashes):
        raise RuntimeError("boom")

    primary = _selection(broken)  # does not raise: a per-batch failure is reported, never raised
    assert primary is not None
