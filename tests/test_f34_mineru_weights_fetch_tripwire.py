# SPDX-License-Identifier: AGPL-3.0-or-later
"""F34: the suite-wide fixture in tests/conftest.py that keeps every test from a real MinerU weight download is live.

Removing ``_no_real_mineru_weights_fetch`` turns this red, so a dev box without the weights cannot start a
1.2 GB download from a test that happens to reach ``pdf_stack.ensure_or_exit``."""

from __future__ import annotations

import os

from nexus import mineru_weights


def test_the_fetch_is_stubbed_for_ordinary_tests_and_their_children():
    assert mineru_weights.ensure_pipeline_weights(lambda _line: None) is False
    assert os.environ.get(mineru_weights.SKIP_ENV) == "1"
