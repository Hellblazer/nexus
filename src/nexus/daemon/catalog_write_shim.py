# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tumbler<->str serialization shim for the daemon-hosted rich Catalog.

RDR-146 Phase 1 (bead nexus-5p2ci.20). The T2 daemon hosts exactly one
rich :class:`nexus.catalog.catalog.Catalog` (the sole owner of the
``.catalog.db`` write handle and the JSONL append path) and exposes a
WRITE-ONLY WHITELIST of the 16 mutating methods over the framed-JSON RPC
boundary.

Why a whitelist and not the default ``_build_dispatch_table`` auto-
enumeration: the default exposes every public method minus a denylist,
which would auto-expose dataclass-returning reads (``links_from`` /
``links_to`` / ``resolve`` returning ``CatalogLink`` / ``CatalogEntry``).
Those do not round-trip framed JSON usefully (``_t2_decode`` hands the
client a plain dict, not the typed object), and per RF-8 Q5 reads stay
local anyway. The whitelist serves only the writes.

Why a Tumbler shim: :class:`~nexus.catalog.tumbler.Tumbler` is in the
wire encoder's dataclass allowlist, so a raw Tumbler would survive the
transport as a tagged dataclass and decode to a bare dict on the far
side rather than a usable Tumbler. The only types the 16 write ops put
on the wire that need special handling are Tumblers; ``Path``
(``ensure_owner_for_repo``'s ``repo`` arg) already round-trips natively
via the ``_TAG_PATH`` encoder. So the shim narrows to a single concern:
serialize Tumbler arguments to ``str`` on the way out and parse the
three Tumbler-returning ops back to Tumbler on the way in.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from nexus.catalog.tumbler import Tumbler

#: The 16 mutating ops the daemon hosts on behalf of the rich Catalog.
#: This is a closed whitelist, not a denylist: adding a method to the
#: rich Catalog does NOT auto-expose it. Every entry must be a method on
#: :class:`nexus.catalog.catalog.Catalog`; the
#: ``test_every_write_op_exists_on_rich_catalog`` regression locks that.
CATALOG_WRITE_OPS: tuple[str, ...] = (
    "register_owner",
    "ensure_owner_for_repo",
    "register",
    "update",
    "link",
    "link_if_absent",
    "unlink",
    "delete_document",
    "register_collection",
    "delete_collection_projection",
    "supersede_collection",
    "set_owner_head_hash",
    "write_manifest",
    "append_manifest_chunks",
    "atomic_manifest_replace",
    "resync_chunk_count_cache",
)

#: Parameter names across the 16 ops whose value is a Tumbler. The
#: daemon-side shim coerces an inbound ``str`` for any of these back to
#: Tumbler before invoking the hosted method; the client-side encoder
#: serialises any Tumbler argument to ``str`` regardless of position.
#: ``set_owner_head_hash`` declares ``owner: Tumbler | str`` and accepts
#: either, so coercing its str form to Tumbler is safe.
TUMBLER_PARAM_NAMES: frozenset[str] = frozenset(
    {"owner", "tumbler", "from_t", "to_t"}
)

#: Ops whose return value is a Tumbler. Their daemon-side result is
#: serialised to ``str`` and the client parses it back to Tumbler. Every
#: other op returns a JSON-native scalar (``int`` / ``bool`` / ``None``).
TUMBLER_RETURN_OPS: frozenset[str] = frozenset(
    {"register_owner", "ensure_owner_for_repo", "register"}
)

#: RPC op prefix for the write whitelist. Distinct from the ``catalog.*``
#: namespace that serves the low-level CatalogStore reads, so the two
#: never collide in the dispatch table.
CATALOG_WRITE_PREFIX = "catalog_write."


def _encode_value(value: Any) -> Any:
    """Serialise a single argument value for the wire (Tumbler -> str)."""
    return str(value) if isinstance(value, Tumbler) else value


def encode_tumbler_args(
    args: tuple[Any, ...] | list[Any], kwargs: dict[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    """Client-side: convert any Tumbler in *args* / *kwargs* to ``str``.

    Position-agnostic on purpose: the only special type the write ops
    accept is Tumbler, so converting every Tumbler instance (wherever it
    sits) is sufficient and avoids tracking per-op argument positions.
    The daemon re-coerces by parameter name.
    """
    enc_args = [_encode_value(a) for a in args]
    enc_kwargs = {k: _encode_value(v) for k, v in kwargs.items()}
    return enc_args, enc_kwargs


def decode_return(op: str, result: Any) -> Any:
    """Client-side: parse a Tumbler-returning op's ``str`` back to Tumbler."""
    if op in TUMBLER_RETURN_OPS and isinstance(result, str):
        return Tumbler.parse(result)
    return result


def make_write_shim(method: Callable[..., Any], op: str) -> Callable[..., Any]:
    """Wrap a hosted rich-Catalog *method* with the daemon-side Tumbler shim.

    The returned callable binds incoming ``args`` / ``kwargs`` against the
    method signature, coerces any ``str`` bound to a Tumbler-typed
    parameter back to Tumbler, invokes the method, and serialises a
    Tumbler return to ``str``. ``**fields`` / ``**meta`` var-keyword
    arguments are passed through untouched (they carry scalar metadata,
    never Tumblers).
    """
    sig = inspect.signature(method)

    def _shim(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        for pname, pval in list(bound.arguments.items()):
            param = sig.parameters[pname]
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                continue
            if pname in TUMBLER_PARAM_NAMES and isinstance(pval, str):
                bound.arguments[pname] = Tumbler.parse(pval)
        result = method(*bound.args, **bound.kwargs)
        if op in TUMBLER_RETURN_OPS and isinstance(result, Tumbler):
            return str(result)
        return result

    _shim.__name__ = op
    _shim.__qualname__ = f"catalog_write.{op}"
    return _shim


def build_catalog_write_dispatch(catalog: Any) -> dict[str, Callable[..., Any]]:
    """Build the ``{catalog_write.<op>: shimmed_callable}`` dispatch subset.

    *catalog* is the daemon-hosted rich :class:`Catalog` instance. Every
    op in :data:`CATALOG_WRITE_OPS` must resolve to a bound method; a
    missing method is a programming error (API drift) and raises
    ``AttributeError`` at daemon start rather than surfacing as a runtime
    ``unknown op`` much later.
    """
    table: dict[str, Callable[..., Any]] = {}
    for op in CATALOG_WRITE_OPS:
        method = getattr(catalog, op)
        if not callable(method):
            raise TypeError(f"catalog.{op} is not callable")
        table[f"{CATALOG_WRITE_PREFIX}{op}"] = make_write_shim(method, op)
    return table
