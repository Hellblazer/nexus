# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""chash: span validation must actually RUN on the production T3 handle.

nexus-at2ff. ``catalog_links`` passed ``t3._client`` to span resolution:

    result = cat.resolve_span(span, entry.physical_collection, t3._client)

``T3Database`` — the TEST facade, constructed with an injected client — has
``_client``. ``HttpVectorClient`` — the PRODUCTION handle since RDR-155 P4a —
does not. So in production that line raised ``AttributeError``, and it sat
inside::

    except Exception:
        pass  # T3 unavailable — skip validation

Every ``chash:`` span was therefore accepted UNVALIDATED in production, while
the test suite proved validation worked — because the suite ran the one handle
shape that happened to have the attribute. The comment froze a wrong diagnosis
into the code: T3 was available; the attribute name was stale Chroma-era
unwrapping.

THE TESTS BELOW DELIBERATELY USE A PRODUCTION-SHAPED HANDLE — ``get_collection``
present, ``_client`` ABSENT. A regression test built on ``T3Database`` would
pass against the bug, which is the whole reason this went unnoticed.
"""
from __future__ import annotations

import pytest

from nexus.catalog.catalog import Catalog


# ── Handles ─────────────────────────────────────────────────────────────────


class _Collection:
    def __init__(self, hashes: set[str]) -> None:
        self._hashes = hashes

    def get(self, where=None, include=None):  # noqa: ANN001 - test double
        wanted = (where or {}).get("chunk_text_hash")
        if wanted in self._hashes:
            return {"ids": ["id0"], "documents": ["chunk text"], "metadatas": [{}]}
        return {"ids": [], "documents": [], "metadatas": []}


class _ProductionShapedT3:
    """Mimics HttpVectorClient: exposes get_collection, has NO ``_client``."""

    def __init__(self, hashes: set[str]) -> None:
        self._hashes = hashes
        self.get_collection_calls: list[str] = []

    def get_collection(self, name: str) -> _Collection:
        self.get_collection_calls.append(name)
        return _Collection(self._hashes)


class _UnreachableT3:
    """A handle whose T3 is genuinely down — a real environment condition."""

    def get_collection(self, name: str):  # noqa: ANN001, ANN201 - test double
        import httpx

        raise httpx.ConnectError("connection refused")


_KNOWN = "a" * 64
_UNKNOWN = "b" * 64


@pytest.fixture()
def linked(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    a = cat.register(owner, "a.py", content_type="code", file_path="a.py",
                     physical_collection="code__x")
    b = cat.register(owner, "b.py", content_type="code", file_path="b.py",
                     physical_collection="code__x")
    return cat, a, b


def test_validation_runs_on_the_production_handle(linked, monkeypatch) -> None:
    """The headline. With the bug, get_collection was NEVER reached (the
    AttributeError fired first and was swallowed), so a dangling span was
    accepted silently. Now the span is resolved and the bad one rejected.
    """
    cat, a, b = linked
    t3 = _ProductionShapedT3({_KNOWN})
    monkeypatch.setattr("nexus.db.make_t3", lambda: t3)

    with pytest.raises(ValueError, match="does not resolve"):
        cat.link(a, b, "quotes", created_by="user", to_span=f"chash:{_UNKNOWN}")

    assert t3.get_collection_calls, (
        "validation never reached T3 — the span was accepted unvalidated, "
        "which is exactly nexus-at2ff"
    )


def test_resolvable_span_still_accepted(linked, monkeypatch) -> None:
    """Non-regression: tightening validation must not reject good spans."""
    cat, a, b = linked
    t3 = _ProductionShapedT3({_KNOWN})
    monkeypatch.setattr("nexus.db.make_t3", lambda: t3)

    assert cat.link(a, b, "quotes", created_by="user", to_span=f"chash:{_KNOWN}") is True


def test_shape_error_is_raised_not_reported_as_unavailable(linked, monkeypatch) -> None:
    """The mechanism that hid the bug for as long as it lived.

    A handle missing the method is a DEFECT in this code, not a degraded
    environment. It must propagate rather than being swallowed under the
    'T3 unavailable' label — otherwise the next stale-attribute refactor
    disables validation just as silently.
    """
    cat, a, b = linked

    class _WrongShape:
        pass  # no get_collection at all — the at2ff situation, generalised

    monkeypatch.setattr("nexus.db.make_t3", lambda: _WrongShape())

    with pytest.raises((AttributeError, TypeError)):
        cat.link(a, b, "quotes", created_by="user", to_span=f"chash:{_KNOWN}")


def test_genuine_unreachability_still_degrades(linked, monkeypatch, caplog) -> None:
    """The swallow was not ALL wrong: a real T3 outage should not block a link
    write. That path survives — but it now says so out loud instead of passing
    silently, so 'not validated' is visible rather than indistinguishable from
    'validated fine'."""
    cat, a, b = linked
    monkeypatch.setattr("nexus.db.make_t3", lambda: _UnreachableT3())

    assert cat.link(a, b, "quotes", created_by="user", to_span=f"chash:{_KNOWN}") is True
