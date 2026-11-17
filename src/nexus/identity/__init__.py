# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-253 Step 3: the client side of session identity.

A session is bound to a long-lived identity (a seat such as ``marginalia``, or an ephemeral one) by the
SessionStart hook; the MCP server then keeps the binding alive, and every process of the session resolves to it.
The engine owns the records and the compare-and-set (``/v1/identity``); this package owns what only a client can
see: the Claude registry files, the process table, the token file and the pending file.

* :mod:`~nexus.identity.bind_client`: the HTTP calls to ``/v1/identity``.
* :mod:`~nexus.identity.token_contract`: the one place the identity token's wire contract is spelled (Step 2).
* :mod:`~nexus.identity.holder`: the registry reader, the pid probe, the UTC start-time compare, the holder check.
* :mod:`~nexus.identity.token_file`: ``~/.config/nexus/identity/<claude pid>.<startedAt>.token``.
* :mod:`~nexus.identity.pending`: ``pending.<claude pid>.<OS start epoch>``, the bind a hook could not do.
* :mod:`~nexus.identity.resolver`: ``resolve_identity()``, which only reads.
* :mod:`~nexus.identity.binder`: the SessionStart policy table; :mod:`~nexus.identity.sweep`: the host sweep.

Nothing here is imported at module scope by the hook path beyond what it uses: a hook pays its import cost on
every invocation.
"""
