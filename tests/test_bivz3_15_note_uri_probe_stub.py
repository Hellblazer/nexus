# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 (nexus-bivz3.15/.16): the autouse serving-engine probe stub in ``tests/conftest.py``.

Setting ``NOTE_URI_MIN_ENGINE_VERSION`` arms the note-write guard for every test that reaches
``put_note`` or ``import_collection`` (138 test files). Without the stub each of them would resolve an
endpoint and call ``GET /version``. With it the guard runs against a version above any floor and no test
leaves the process.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.db import serving_engine_version as sev


def test_an_armed_floor_does_not_reach_the_endpoint_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import engine_version as ev

    monkeypatch.setattr(ev, "NOTE_URI_MIN_ENGINE_VERSION", (0, 1, 200))
    with patch("nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
               side_effect=AssertionError("an unmarked test probed the serving engine")) as resolver:
        sev.require_note_uri_engine()  # no raise: the stub reports a version above the floor
    resolver.assert_not_called()


@pytest.mark.real_engine_probe
def test_the_real_engine_probe_marker_opts_out_of_the_stub() -> None:
    assert sev.probe_serving_engine_version.__name__ == "probe_serving_engine_version"
    with patch("nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
               side_effect=RuntimeError("no lease")) as resolver:
        assert sev.probe_serving_engine_version() is None
    resolver.assert_called_once()
