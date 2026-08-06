# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-armable import-or-skip gate for MinerU-dependent tests (nexus-6xkdu).

``pytest.importorskip("mineru.cli.common")`` alone makes "MinerU is not
installed" and "nobody is running this test" indistinguishable in the
output: both render as a plain skip. That is fine on a dev box where
MinerU is an optional extra, but it is exactly the wrong behavior in an
environment that is SUPPOSED to have MinerU (the release shakedown,
a CI leg that provisions ``mineru[all]``) — there, a missing import is a
provisioning regression and must fail loudly, not skip quietly.

Same non-vacuity shape as ``NX_T2_SUBSTRATE_EXPECTED``
(``tests/conftest.py`` ``t2_service_env``): unset, the graceful skip
holds for dev boxes; set, an absent module raises instead of skipping.
"""
from __future__ import annotations

import importlib
import os

import pytest

#: Env var name. When set (any non-empty value), a missing MinerU import
#: fails the test instead of skipping it. Mirrors NX_T2_SUBSTRATE_EXPECTED.
NX_MINERU_EXPECTED = "NX_MINERU_EXPECTED"


def mineru_importorskip(
    modname: str = "mineru.cli.common",
    env_var: str = NX_MINERU_EXPECTED,
) -> None:
    """Import ``modname``; skip if absent, unless ``env_var`` is set.

    ``env_var`` set (release shakedown, a CI leg that provisions MinerU):
    a missing import propagates as ``ModuleNotFoundError`` -- an ERROR,
    not a skip. ``env_var`` unset (the default, dev boxes): falls through
    to ``pytest.importorskip``, which raises pytest's ``Skipped`` outcome
    on a missing import and returns the module on success.

    ``modname`` and ``env_var`` are parameters (not hardcoded) so the
    mechanism itself is unit-testable against a module name that is
    guaranteed absent, without needing to actually uninstall mineru.
    """
    if os.environ.get(env_var):
        importlib.import_module(modname)
        return
    pytest.importorskip(modname)
