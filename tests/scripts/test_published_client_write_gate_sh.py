# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/e2e/published-client-write-gate.sh's step-1 opt-in (nexus-jspsn).

THE FINDING (resolved by reading the script, not by running the sandbox --
see the bead for the two competing hypotheses this ruled between). Step 1
(`nx init --service`) provisions the candidate engine via THIS CHECKOUT's
own `uv run nx` -- a dev-checkout process, deliberately, so it can install
a not-yet-published NEXUS_SERVICE_TAG. Its write target is $ENGINE_HOME, a
throwaway scratch NEXUS_CONFIG_DIR this script creates a few lines below --
never production.

`src/nexus/db/service_endpoint.py::is_dev_checkout_process()` keys off
THIS MODULE's own `__file__` (walking its ancestors for a conexus
pyproject.toml + .git), never cwd or sys.argv[0] -- so hypothesis (a) from
the bead (the classifier misreading a published install as dev-checkout by
cwd) does not hold; see that function's own docstring and
tests/test_install_ping.py for the cwd-independence proof. Hypothesis (b)
does hold: the provisioning `nx init --service` genuinely IS a dev-checkout
process, and until this fix it ran with no NX_ALLOW_PROD_WRITE opt-in, so
the nexus-a2qhz guard silently refused its builtin plan-template seed write
(event=init_plan_seed_failed, a WARNING the harness logged and continued
past) on every run -- three green runs (v0.1.122-v0.1.124) all carried it,
invisible to the gate's own PASS/FAIL verdict.

tests/e2e/local-service-gate.sh already carries the identical opt-in for
the identical shape (a self-provisioned throwaway service in a scratch
NEXUS_CONFIG_DIR) -- this pin is the sibling script catching up, checked
mechanically so a future edit cannot silently drop the export again.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "published-client-write-gate.sh"

_EXPORT_RE = re.compile(
    r'^export NX_ALLOW_PROD_WRITE="published-client-write-gate: .*nexus-jspsn.*"$',
    re.MULTILINE,
)
_INIT_RE = re.compile(r'^\S.*\buv run nx init --service\b', re.MULTILINE)


def test_the_opt_in_is_exported_before_the_provisioning_init_call() -> None:
    text = SCRIPT.read_text()
    export_m = _EXPORT_RE.search(text)
    init_m = _INIT_RE.search(text)
    assert export_m, (
        "published-client-write-gate.sh no longer exports the NX_ALLOW_PROD_WRITE "
        "opt-in naming nexus-jspsn -- step 1's provisioning `nx init --service` "
        "will silently refuse its plan-template seed write again"
    )
    assert init_m, "the provisioning `nx init --service` call moved or was reworded"
    assert export_m.start() < init_m.start(), (
        "the NX_ALLOW_PROD_WRITE export must be set BEFORE the provisioning "
        "`nx init --service` call it exists to unblock"
    )


def test_dev_checkout_classifier_is_independent_of_cwd() -> None:
    """Hypothesis (a) from the bead, ruled out empirically: changing cwd
    (to an unrelated tmp dir, outside any conexus checkout) does not change
    the classifier's verdict for the real default *start* (this module's
    own __file__, still under this checkout)."""
    import os

    from nexus.db.service_endpoint import (
        _dev_checkout_root,
        reset_dev_checkout_cache_for_tests,
    )

    here = os.getcwd()
    try:
        os.chdir("/tmp")
        reset_dev_checkout_cache_for_tests()  # force real recomputation, not the cache
        assert _dev_checkout_root() is not None, (
            "the classifier's verdict for the default start (this module's own "
            "__file__) changed when cwd moved outside any checkout -- it must "
            "key on __file__, not cwd"
        )
    finally:
        os.chdir(here)
        reset_dev_checkout_cache_for_tests()
