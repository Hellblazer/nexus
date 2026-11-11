# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the probe battery's capture step under the suite's self-provisioned local engine (nexus-3ceda.7).

Not collected by the default suite (no ``test_`` prefix). Run it by path where an engine can be
provisioned (chas), with ``NX_PROBE_OUT`` naming the output file:

    NX_PROBE_OUT=probe-errors.json uv run pytest tests/scripts/probe_capture_entry.py -p no:randomly
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_capture_against_the_local_engine(t2_service_env) -> None:
    spec = importlib.util.spec_from_file_location("agent_error_probes", _REPO / "scripts" / "agent_error_probes.py")
    assert spec and spec.loader
    aep = importlib.util.module_from_spec(spec)
    sys.modules["agent_error_probes"] = aep
    spec.loader.exec_module(aep)
    assert aep.local_engine_refusal() == "", "the fixture must give a local engine"
    out = Path(os.environ.get("NX_PROBE_OUT", "probe-errors.json")).resolve()
    assert aep.capture(out) == 0, out.read_text() if out.exists() else "no output"
