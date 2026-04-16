# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authoring-trust helpers for user-facing prose documentation.

``ref_scanner`` — scan markdown for collection-name references + proximate
chunk-count claims, compare against current T3 state, flag drift.  See
RDR-081.
"""
