# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's SIGTERM-deferral bound must stay under every stop grace that ends in SIGKILL.

nexus-o5xyx.1: ``OrtInitGate`` defers signal-driven exit until in-flight ONNX model init
finishes, waiting at most ``DEFAULT_WAIT_MILLIS``. A wait longer than the grace the stopper
gives the engine is answered by SIGKILL, which is no better than the crash it prevents.
The Java constant cannot import the Python graces, so this pins them together: it parses
the Java default and the two 5 s graces and fails if the default stops leaving headroom.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.daemon.storage_service_daemon import _GRACEFUL_STOP_TIMEOUT

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1]
_GATE = _ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "vectors" / "OrtInitGate.java"
_SUBSTRATE = _ROOT / "tests" / "_engine_substrate.py"

#: Headroom the rest of the engine's shutdown (service.stop, backend reaper, pool close)
#: needs inside the grace once the deferral has ended.
_MIN_HEADROOM_MS = 1_000


def _java_default_ms() -> int:
    m = re.search(r"DEFAULT_WAIT_MILLIS\s*=\s*([\d_]+)L", _GATE.read_text())
    assert m, f"DEFAULT_WAIT_MILLIS not found in {_GATE}; update this lint with the rename"
    return int(m.group(1).replace("_", ""))


def _substrate_teardown_grace_s() -> float:
    src = _SUBSTRATE.read_text()
    m = re.search(r"def _teardown\(\).*?svc\.wait\(timeout=([\d.]+)\)", src, re.S)
    assert m, "the substrate teardown's SIGTERM grace was not found; update this lint"
    return float(m.group(1))


def test_the_deferral_bound_leaves_headroom_under_the_supervisors_grace() -> None:
    default_ms = _java_default_ms()
    grace_ms = int(_GRACEFUL_STOP_TIMEOUT * 1000)
    assert default_ms + _MIN_HEADROOM_MS <= grace_ms, (
        f"OrtInitGate.DEFAULT_WAIT_MILLIS={default_ms} ms leaves under {_MIN_HEADROOM_MS} ms of the "
        f"supervisor's {grace_ms} ms SIGTERM grace (_GRACEFUL_STOP_TIMEOUT): the supervisor would "
        f"SIGKILL a deferred exit. Lower the Java default or raise the grace together with it."
    )


def test_the_deferral_bound_leaves_headroom_under_the_substrate_teardown_grace() -> None:
    default_ms = _java_default_ms()
    grace_ms = int(_substrate_teardown_grace_s() * 1000)
    assert default_ms + _MIN_HEADROOM_MS <= grace_ms, (
        f"OrtInitGate.DEFAULT_WAIT_MILLIS={default_ms} ms leaves under {_MIN_HEADROOM_MS} ms of the "
        f"test substrate's {grace_ms} ms teardown grace (tests/_engine_substrate.py::_teardown)."
    )
