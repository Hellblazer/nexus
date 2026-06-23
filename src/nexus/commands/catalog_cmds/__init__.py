# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Command families carved out of the ``nx catalog`` group (nexus-kgyoz).

Each submodule defines a cohesive family of ``nx catalog <cmd>`` commands as
plain ``click`` commands plus a ``register(group)`` hook. ``commands.catalog``
imports each module and calls its ``register`` against the shared ``catalog``
group, so command names and the ``nx catalog …`` invocation surface are
unchanged. Submodules reference shared helpers (e.g. ``_get_catalog``) lazily
through the ``nexus.commands.catalog`` module object so import stays acyclic
and test monkeypatches on those helpers continue to take effect.
"""
