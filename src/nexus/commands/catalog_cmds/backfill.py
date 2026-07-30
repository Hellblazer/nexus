# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Retired owner-id backfill family for the ``nx catalog`` group (nexus-kgyoz).

``backfill-owner-id`` — the one-time RDR-137 P1.5a migration that populated
``collections.owner_id`` on the LOCAL SQLite catalog — died with that catalog
(nexus-i711w terminal deletion): it wrote through a raw ``cat._db`` SQLite
handle by contract and already refused in service mode, which is now the only
mode. The module survives as an empty registration hook so the command-family
carve-out wiring in ``commands.catalog`` stays uniform.
"""
from __future__ import annotations

import click


def register(group: click.Group) -> None:
    """No commands to attach — the backfill verb family is retired."""
