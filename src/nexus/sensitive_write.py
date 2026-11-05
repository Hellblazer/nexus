# SPDX-License-Identifier: AGPL-3.0-or-later
"""The write side of ``--sensitive`` (RDR-243 Technical Design items 2, 3 and 8; nexus-rfvyy.6.9).

Every ``--sensitive`` write path (``nx index pdf``, ``nx index md``, ``nx dt index``, ``nx dt
capture``, ``nx store put`` and MCP ``store_put``) goes through here for the three things they share:

* :func:`require_sensitive_engine` refuses, before any register call, an engine that cannot hold
  the flag (an engine without the column would drop it silently).
* :func:`raise_flag` moves the flag on a document that already exists. A fresh document carries it
  in its register request; a document the verb resolves instead of minting does not register, so
  the audited route (``set_sensitivity``) raises the flag before any chunk is written.
* The exceptions the verbs turn into a clean message.

Imports no heavy module at load: ``nexus.catalog.http_catalog_client`` is loaded when a flagged
write runs.
"""
from __future__ import annotations

from typing import Any

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "SENSITIVE_REASON", "SensitiveFlagNotSetError", "raise_flag", "require_sensitive_engine", "sensitive_help",
    "sensitive_write_kwargs",
]

#: The audit reason recorded when a verb raises the flag on an existing document.
SENSITIVE_REASON = "--sensitive write"


def sensitive_help() -> str:
    """The ``--sensitive`` option's help text: generated from the registry rows enforced in this release."""
    from nexus.tables import llm_surfaces as ls  # noqa: PLC0415 — deferred: read at command definition, not at module load

    return ls.render_sensitive_help(ls.RELEASE_ENFORCED_PHASE)


class SensitiveFlagNotSetError(RuntimeError):
    """A ``--sensitive`` write could not put the flag on the document, so nothing was written to it."""


def require_catalog_sensitivity(cat: Any, *, what: str = "a sensitive write") -> None:
    """The refusal every ``--sensitive`` write calls (a name here so the module a verb imports is the one a pin patches)."""
    from nexus.catalog.http_catalog_client import require_catalog_sensitivity as _require  # noqa: PLC0415 — deferred: the client module is heavy

    _require(cat, what=what)


def require_sensitive_engine(what: str) -> None:
    """Refuse *what* unless the engine advertises ``catalog_sensitivity_supported``.

    Raises :class:`~nexus.catalog.http_catalog_client.CatalogSensitivityUnsupportedError` (a
    ``RuntimeError``) with "Nothing was written" in the text. Called before the register call, and
    before the T3 handle and any extraction, embedding or catalog write of the verb.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: the factory pulls the catalog client

    cat = make_catalog_reader()
    try:
        require_catalog_sensitivity(cat, what=what)
    finally:
        close = getattr(cat, "close", None)
        if callable(close):
            close()


def sensitive_write_kwargs(sensitive: bool, what: str, *, dry_run: bool = False, enrich: bool = False) -> dict[str, bool]:
    """The CLI front door of a ``--sensitive`` verb: ``{}`` when the flag is off, else the engine
    refusal and ``{"sensitive": True}`` to splat into the indexer calls.

    The keyword rides only when set, so a test double or an older indexer signature is untouched
    when the flag is off. A dry run writes nothing, so it skips the engine probe. ``--enrich``
    sends the document's title to Semantic Scholar or CrossRef (census rows 7 and 8), which a
    sensitive document must not: refused here until the document gates land (Step 4b).
    """
    import click  # noqa: PLC0415 — deferred: this module also loads under MCP

    if not sensitive:
        return {}
    if enrich:
        raise click.UsageError(
            f"{what}: --enrich sends the title and identifiers to Semantic Scholar or CrossRef; "
            f"a sensitive document is not sent there. Drop one of the two flags."
        )
    if not dry_run:
        try:
            require_sensitive_engine(what)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    return {"sensitive": True}


def raise_flag(writer: Any, tumbler: str, *, what: str) -> None:
    """Put ``sensitive = true`` on the existing document *tumbler* through the audited route.

    A document that is already sensitive is a no-op. Any failure raises
    :class:`SensitiveFlagNotSetError`: the caller has not written a chunk yet and must not.
    """
    try:
        out = writer.set_sensitivity([tumbler], True, reason=SENSITIVE_REASON)
    except Exception as exc:  # noqa: BLE001 — boundary: any failure of the audited route means the document is not protected
        raise SensitiveFlagNotSetError(
            f"{what} refused: could not mark {tumbler} sensitive ({type(exc).__name__}: {exc}). "
            f"Nothing was written to it."
        ) from exc
    _log.debug("sensitive_flag_raised", tumbler=tumbler, updated=(out or {}).get("updated") if isinstance(out, dict) else out)
