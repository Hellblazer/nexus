# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242: sandboxed Lua scripts run in a worker process.

The shared seams (T2 ``nexus/rdr242-interfaces``): ``errors`` (what a run
raises), ``verb_types`` (what a verb is) and ``verbs`` (the verb table).
``errors`` and ``verb_types`` import nothing from nexus, so the worker loads
them under ``python -I`` with an empty environment.
"""
