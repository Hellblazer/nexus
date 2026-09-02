"""``nx dt index --collection`` must normalise a bare/legacy collection name
through ``t3_collection_name(for_write=True)`` exactly as ``nx index pdf``
does. Measured 2026-09-02: a verbatim ``knowledge__agentic-scholar`` reached
the engine and was refused as non-conformant AFTER the catalog Document had
been registered, leaving ``physical_collection`` stale."""
from __future__ import annotations

from unittest.mock import patch

from nexus.commands.dt import _resolve_dt_collection


def test_collection_override_is_normalised_to_conformant_name():
    with patch("nexus.corpus.t3_collection_name", return_value="knowledge__agentic-scholar__voyage-context-3__v1") as norm:
        out = _resolve_dt_collection("knowledge__agentic-scholar", "dt", ".pdf")
    norm.assert_called_once_with("knowledge__agentic-scholar", for_write=True)
    assert out == "knowledge__agentic-scholar__voyage-context-3__v1"


def test_conformant_override_passes_through():
    name = "knowledge__delos__voyage-context-3__v1"
    with patch("nexus.corpus.t3_collection_name", side_effect=lambda c, **_: c):
        assert _resolve_dt_collection(name, "dt", ".pdf") == name
