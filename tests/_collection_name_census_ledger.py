# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The client collection-name parse census ledger (RDR-229 P1.1,
nexus-p2m1x.1): every live site of the widened scan, one class each. The four
parts split by surface only to stay under the test-file size cap.
"""
from __future__ import annotations

from tests._collection_name_census_scan import Site
from tests._collection_name_census_sites_callers import SITES as _CALLERS
from tests._collection_name_census_sites_catalog import SITES as _CATALOG
from tests._collection_name_census_sites_commands import SITES as _COMMANDS
from tests._collection_name_census_sites_core import SITES as _CORE

LEDGER: tuple[Site, ...] = (*_CALLERS, *_CATALOG, *_COMMANDS, *_CORE)
