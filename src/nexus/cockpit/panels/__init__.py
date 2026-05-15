# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cockpit Phase 3 panels (RDR-111 §Phase 3, nexus-ut5r).

Three read-only views over the tuplespace:

- ``active_claims``  -- in-flight claims grouped by subspace
- ``recent_events``  -- newest-first slice of the events table
- ``active_bindings`` -- bindings registered under each loaded profile

Each panel exposes a pure data-fetch function returning a dataclass.
Rendering is a separate concern (see ``nexus.cockpit.layout`` and the
``nx cockpit`` CLI surface).
"""
