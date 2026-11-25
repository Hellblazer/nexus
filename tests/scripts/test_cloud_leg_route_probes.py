# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cloud leg's route probes send the requests they name (nexus-clfse.1).

Pins for the four probe defects the 8.5.6 post-deploy leg r2611250807567b5a found: the rename-refusal probes went through
the registering store wrappers and never sent the route, the by-id probes used a 19-digit id, and the persist-split probe
put its second collection on the home row's live tuple. Kept apart from test_cloud_leg.py, which is at its size cap.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

LEG_PY = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "lib" / "cloud_leg.py"

if "cloud_leg" in sys.modules:
    cl = sys.modules["cloud_leg"]
else:
    _spec = importlib.util.spec_from_file_location("cloud_leg", LEG_PY)
    cl = importlib.util.module_from_spec(_spec)
    sys.modules["cloud_leg"] = cl
    _spec.loader.exec_module(cl)


def test_rename_refusal_probes_post_the_route_directly() -> None:
    """The store's rename_collection registers the target first and the engine refuses a minted label there (400), so the
    rename route was never sent and the probe passed on the registration's refusal."""
    routes = cl._routes
    chash = routes.CHASH_RENAME_REFUSAL
    tax = routes.TAXONOMY_RENAME_REFUSAL
    assert 'store._post("/v1/chash/rename_collection"' in chash and "store.rename_collection(" not in chash
    assert 'store._post("/rename_collection"' in tax and "store.rename_collection(" not in tax
    for code in (chash, tax):
        assert "exc.response.url.path.endswith(" in code  # the refusal must come from the rename route, not the registration


def test_retiring_by_id_probe_id_fits_the_engines_18_digit_cap() -> None:
    """The engine answers 400 for a collection_id over 18 digits; the probe wants 404 unknown_collection_id."""
    m = re.search(r"probe_id = ([\d_]+)", cl._routes._RETIRING_BY_ID)
    assert m, "the probe names no probe_id"
    digits = m.group(1).replace("_", "")
    assert len(digits) <= 18 and int(digits) > 0, digits


def test_persist_split_probe_registers_other_on_a_distinct_tuple() -> None:
    """A second live collection on the home row's tuple is refused 409, so `other` must differ from the home row."""
    code = cl._routes.PERSIST_SPLIT_REFUSAL
    call = code[code.index("cat.register_collection(other"):]
    call = call[: call.index(")\n") + 1]
    assert 'owner_id=row["owner_id"]' not in call
    assert re.search(r'owner_id="1\.\d+"', call), call
    assert 'model_version=row["model_version"]' in call, "a different model_version would capture the store steps' newest-version resolution"
