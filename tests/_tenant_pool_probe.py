# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe cases for tests/test_tenant_pool_end_to_end.py (nexus-fpwj1).

Not collected by the suite (the file name matches no ``python_files`` pattern): the end-to-end test passes this path
to a child pytest, with THIS repo's real ``tests/conftest.py``, once with ``NX_TEST_TENANT_POOL=1`` and once without.
Each case appends the tenant it was given to the file named by ``NX_POOL_PROBE_OUT``.
"""
import os
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


def _record(tenant: str) -> None:
    with open(os.environ["NX_POOL_PROBE_OUT"], "a") as out:
        out.write(f"{os.environ['PYTEST_CURRENT_TEST'].split('::')[-1].split(' ')[0]} {tenant}\n")


def test_a_writes_a_note(t2_service_env: str, tmp_path: Path) -> None:
    _record(t2_service_env)
    db = T2Database(tmp_path / "m.db")
    assert db.memory.get("proj", "pool-probe-note") is None
    db.memory.put("proj", "pool-probe-note", "left behind on purpose", tags="")
    assert db.memory.get("proj", "pool-probe-note") is not None


def test_b_sees_no_note_from_the_test_before(t2_service_env: str, tmp_path: Path) -> None:
    _record(t2_service_env)
    assert T2Database(tmp_path / "m.db").memory.get("proj", "pool-probe-note") is None


@pytest.mark.fresh_tenant
def test_c_opts_out_and_gets_its_own_tenant(t2_service_env: str) -> None:
    _record(t2_service_env)


def test_d_touches_no_storage() -> None:
    assert "NX_SERVICE_URL" not in os.environ
