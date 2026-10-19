# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe cases for tests/test_optin_tenant_end_to_end.py (nexus-j23ni).

Not collected by the suite (the file name matches no ``python_files`` pattern):
the end-to-end test passes this path to a child pytest that runs under
``NX_TEST_T2_SUBSTRATE=optin`` (the default substrate) with THIS repo's real
``tests/conftest.py``, so the cases assert the opt-in fixtures' real behaviour.
Under ``per-test`` the "unprovisioned" cases would fail by design: every test
has a tenant there.
"""
import os

import pytest


@pytest.mark.t2_tenant
def test_marker_provisions_tenant_t1_and_local_posture():
    from nexus.db.http_vector_client import _resolve_endpoint

    assert os.environ["NX_SERVICE_URL"] and os.environ["NX_SERVICE_TOKEN"]
    assert os.environ["NX_LOCAL"] == "1" and os.environ["NX_T1_SESSION"]
    assert os.environ.get("NX_STORAGE_BACKEND") == "service"
    _resolve_endpoint()


def test_parameter_provisions_tenant(t2_tenant):
    assert t2_tenant and os.environ["NX_T1_SESSION"]
    assert os.environ.get("NX_STORAGE_BACKEND") == "service"


def test_t2_service_env_is_the_same_tenant_with_a_t1_session(t2_service_env):
    # 137 test files request this fixture directly; under optin it aliases t2_tenant
    assert t2_service_env and os.environ["NX_T1_SESSION"] and os.environ["NX_T1_SESSION_ID"]
    assert os.environ["NX_LOCAL"] == "1" and os.environ.get("NX_STORAGE_BACKEND") == "service"


def test_an_untouched_test_gets_no_tenant_and_the_local_posture():
    assert "NX_SERVICE_URL" not in os.environ and "NX_T1_SESSION" not in os.environ
    assert os.environ["NX_LOCAL"] == "1"


def test_an_unprovisioned_storage_touch_is_recorded():
    from nexus.db.http_vector_client import _resolve_endpoint

    with pytest.raises(RuntimeError):
        _resolve_endpoint()


def test_cloud_mode_still_wins_over_the_posture_pin(cloud_mode, t2_tenant):
    from nexus.config import is_local_mode

    assert not is_local_mode()


@pytest.mark.integration
def test_an_integration_marked_test_keeps_the_per_test_tenant():
    # The census never ran integration/slow tests (the default addopts deselect
    # them), so they keep a tenant per test until one is taken with them selected.
    assert os.environ["NX_SERVICE_URL"] and os.environ["NX_SERVICE_TOKEN"] and os.environ["NX_T1_SESSION"]
    assert os.environ["NX_LOCAL"] == "1"
