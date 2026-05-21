# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage daemons for the nexus substrate (RDR-120).

P1.A (nexus-41unl) introduces the T3 daemon: a managed ``chroma run``
subprocess with discovery + start/stop/status lifecycle. The T3Client
factory (P1.B) and call-site flips (P2) ship in subsequent beads.

Import contract: callers reach the public types through their defining
submodules, not through this package root. No facade re-exports.

  - ``from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon``
  - ``from nexus.daemon.discovery import discovery_resolve, find_t3_daemon``
"""
