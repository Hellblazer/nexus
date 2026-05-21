# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.B (nexus-beoh1) — T3Client factory.

The integration seam the P2 call-site flip consumes. Returns a real
``T3Database`` whose ``_client`` is a ``chromadb.HttpClient`` pointed at
the running ``nx daemon t3``. Surface parity by construction: same
class as direct-mode ``make_t3()``, no shim, no method drift.

Resolution chain:
1. ``is_local_mode()`` must be True. Cloud mode raises ``T3DaemonError``
   because chromadb's ``CloudClient`` is already HTTP-served; running a
   local daemon would be a no-op. The daemon-start side gate in
   ``t3_daemon.start_t3_daemon`` is defence-in-depth.
2. ``discovery_resolve('t3')`` resolves the daemon's address via the
   ``NX_T3_ADDR`` env-var or the discovery file (RDR-120 C2
   precedence: env wins when set + non-empty; file fallback when env
   unset; a set-but-unreachable env-var surfaces at connect time).
   Missing daemon raises ``T3DaemonError`` carrying the same recovery
   hint as ``DaemonNotRunningError``.
3. Construct ``chromadb.HttpClient(host, port)`` + apply the
   ``_apply_chroma_http_timeout`` override; chromadb defaults to
   ``httpx.Client(timeout=None)`` which hangs forever on stalled reads.
4. Construct + inject ``LocalEmbeddingFunction``; the daemon is
   local-only, so Voyage embeddings are not in play.
5. Return ``T3Database(_client=httpclient, _ef_override=ef,
   local_mode=True, local_path=<discovery payload>)``.

No auto-spawn. ``nx daemon t3 start`` is the only path that spawns the
chroma subprocess (RDR-120 §Approach C2: auto-spawn is excluded from
P1; the explicit-start contract matches the locked phasing).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from nexus.db.t3 import T3Database

_log = structlog.get_logger(__name__)


class T3DaemonError(RuntimeError):
    """Raised by ``make_t3_client`` when the T3 daemon is not reachable.

    Two cases share this type:
    - Cloud mode: no daemon should exist; caller should use the direct
      ``make_t3()`` (which builds a ``CloudClient`` directly).
    - Local mode + no daemon running: caller must ``nx daemon t3 start``
      before retrying. Message embeds the recovery hint verbatim.
    """


def make_t3_client(*, config_dir: Optional[Path] = None) -> "T3Database":
    """Return a ``T3Database`` connected to the running T3 daemon.

    Args:
        config_dir: Optional override for the discovery file location
            (defaults to ``nexus.config.nexus_config_dir()``).

    Raises:
        T3DaemonError: when cloud mode is active OR the T3 daemon is
            not reachable. The message names the recovery action.
    """
    from nexus.config import is_local_mode
    from nexus.daemon.discovery import (
        DaemonNotRunningError,
        discovery_resolve,
    )
    from nexus.db.t3 import T3Database, _apply_chroma_http_timeout

    if not is_local_mode():
        raise T3DaemonError(
            "T3 daemon is a no-op in cloud mode. chromadb's CloudClient "
            "is already HTTP-served; use the direct ``make_t3()`` factory "
            "from ``nexus.db`` for cloud access. Set NX_LOCAL=1 to opt "
            "into the local daemon path."
        )

    try:
        payload = discovery_resolve("t3", config_dir=config_dir)
    except DaemonNotRunningError as exc:
        raise T3DaemonError(str(exc)) from exc

    host = payload.get("tcp_host")
    port = payload.get("tcp_port")
    if not isinstance(host, str) or not isinstance(port, int):
        raise T3DaemonError(
            f"Discovery payload missing or invalid tcp_host/tcp_port: "
            f"host={host!r}, port={port!r}. Re-start with: "
            f"`nx daemon t3 stop && nx daemon t3 start`."
        )

    import chromadb
    http_client = chromadb.HttpClient(host=host, port=port)
    _apply_chroma_http_timeout(http_client)

    from nexus.db.local_ef import LocalEmbeddingFunction
    import os
    model_override = os.environ.get("NX_LOCAL_EMBED_MODEL", "")
    ef = LocalEmbeddingFunction(
        model_name=model_override if model_override else None,
    )

    # Discovery payloads from the file branch carry the daemon's
    # actual on-disk path; the env branch (NX_T3_ADDR) does not, since
    # the operator may be pointing at a daemon under a foreign data
    # path or even a remote-bridged loopback. Fall back to the
    # configured XDG default so T3Database's defensive ``local_path``
    # mkdir does not receive an empty string and silently chmod cwd.
    # The daemon owns the on-disk store; T3Database.local_path is
    # diagnostic-only when ``_client`` is injected.
    local_path_str = payload.get("local_path") or ""
    if not local_path_str:
        from nexus.config import _default_local_path
        local_path_str = str(_default_local_path())
    _log.info(
        "t3_client_constructed",
        host=host,
        port=port,
        source=payload.get("source"),
        local_path=local_path_str,
    )
    return T3Database(
        local_mode=True,
        local_path=local_path_str,
        _client=http_client,
        _ef_override=ef,
    )
