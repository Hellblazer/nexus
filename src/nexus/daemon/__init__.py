# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage daemons for the nexus substrate (RDR-120 / RDR-149).

The shared lifecycle primitive (discovery, single-writer lease,
self-heal, version-skew) lives in ``service_registry.py`` and applies
to every supervised tier. RDR-155 P4b: the managed-chroma T3 daemon is
retired — T3 is served by the Java storage service in every mode.
nexus-i711w Stage 2 sub-stage B: the T2 daemon is retired too, taking
``t2_daemon.py``, ``t2_client.py``, ``discovery.py`` (whose only remaining
arm was T2) and ``spin_guard.py`` (whose only consumer was the T2 event
loop) with it.

Import contract: callers reach the public types through their defining
submodules, not through this package root. No facade re-exports.

  - ``from nexus.daemon.service_registry import ServiceRegistry``
"""
