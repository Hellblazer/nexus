# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine capability floors the client checks before a write the engine would otherwise drop or refuse.

Each floor is a hand-kept minimum engine version (``None`` until the engine tag that carries the feature is cut),
an error class, and a ``require_*`` function that asks the cached ``GET /version`` probe first. An UNKNOWN answer
refuses. The module sits in this subpackage because ``nexus/catalog/`` is at its module cap (RDR-254)."""
from __future__ import annotations

from typing import Any

import structlog

_log = structlog.get_logger(__name__)


#: RDR-243: the first engine release that carries the catalog sensitivity changeset and route. HAND-KEPT:
#: ``None`` until the engine tag that carries it is cut (nexus-rfvyy.6.8 records the value to fill in); the
#: refusal text says "not yet released" while it is ``None``. Not a gate: the probe is the gate.
CATALOG_SENSITIVITY_MIN_ENGINE_VERSION: tuple[int, int, int] | None = None


class CatalogSensitivityUnsupportedError(RuntimeError):
    """A ``--sensitive`` write was refused because the engine did not confirm ``catalog_sensitivity_supported``."""


def require_catalog_sensitivity(cat: Any, *, what: str = "a sensitive write") -> None:
    """Refuse *what* unless *cat*'s engine advertises ``catalog_sensitivity_supported`` as a JSON ``true``.

    Called before the register call of every ``--sensitive`` write path and before ``nx catalog sensitive``'s
    route call (RDR-243 Technical Design item 3): an engine without the column would drop the flag without an
    error, so the user would believe a document was protected that is not. An UNKNOWN answer refuses: a probe
    that failed, an edge that trimmed ``/version``, or a handle with no probe at all.
    """
    probe = getattr(cat, "catalog_sensitivity_probe", None)
    supported, version = False, ""
    if callable(probe):
        try:
            supported, version = probe()
        except Exception as exc:  # noqa: BLE001 - an unreadable probe is an unknown answer, which refuses
            _log.warning("http_catalog_client.sensitivity_probe_failed", error=str(exc))
    if supported is True:
        return
    if not version:
        raise CatalogSensitivityUnsupportedError(
            f"{what} refused: could not confirm that the engine supports document sensitivity "
            f"(GET /version gave no answer or no catalog_sensitivity_supported key). Nothing was written."
        )
    floor = (
        ".".join(map(str, CATALOG_SENSITIVITY_MIN_ENGINE_VERSION))
        if CATALOG_SENSITIVITY_MIN_ENGINE_VERSION is not None
        else "not yet released (RDR-243 Phase 1)"
    )
    raise CatalogSensitivityUnsupportedError(
        f"{what} refused: engine {version} does not support document sensitivity; minimum engine version: "
        f"{floor}. An engine without it would drop the flag silently. Nothing was written."
    )

