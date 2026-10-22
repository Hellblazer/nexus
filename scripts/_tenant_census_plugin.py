# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest plugin for scripts/tenant_delta.py (nexus-g37hn): at collection, count the tests in
each module that will mint a test tenant, and write ``{module: count}`` to ``$TENANT_CENSUS_OUT``.

A test mints one under the default substrate (``optin``, nexus-j23ni) when it carries the
``t2_tenant`` marker (module ``pytestmark`` included), or its fixture closure holds
``t2_tenant`` or ``t2_service_env`` (an alias for it under ``optin``; tests/conftest.py).
Measured 2026-10-22 against CI's runtime count (tests/storage_touch_ceiling.toml's
python_unit): 5042 at collection against 4940 at run time; the difference is tests that skip
at run time, which a delta between two collections cancels.
"""
from __future__ import annotations

import collections
import json
import os

_TENANT_FIXTURES = ("t2_tenant", "t2_service_env")


def pytest_collection_modifyitems(session, config, items) -> None:
    counts: collections.Counter[str] = collections.Counter()
    for item in items:
        fixtures = getattr(item, "fixturenames", ())
        if item.get_closest_marker("t2_tenant") is not None or any(f in fixtures for f in _TENANT_FIXTURES):
            # Keyed by the file's path under the rootdir: a nodeid is relative to the invocation and
            # reads differently when the rootdir is an unpacked archive.
            try:
                key = item.path.resolve().relative_to(config.rootpath.resolve()).as_posix()
            except ValueError:
                key = item.nodeid.split("::", 1)[0]
            counts[key] += 1
    with open(os.environ["TENANT_CENSUS_OUT"], "w", encoding="utf-8") as out:
        json.dump(dict(counts), out)
