# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan-Centric Retrieval primitives (RDR-078).

Houses plan schema, dimensional identity helpers, and (Phase 1+) the
matcher/runner/traverse pipeline. The module intentionally starts with
``schema`` alone — downstream phases grow it incrementally per the
RDR-078 implementation plan.
"""
