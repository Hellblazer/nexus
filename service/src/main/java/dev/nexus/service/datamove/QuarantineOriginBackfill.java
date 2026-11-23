// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 Phase 3 blocking row 13 (nexus-p2m1x.22): the data move that writes {@code metadata.origin_collection_id} on every quarantine
 * chunk that carries only the {@code origin_collection} label, from the registry row the label resolves to while names are still exact
 * (JSON null where none does, the no-id marker). The changeset {@code vectors-082-1} registers it; its template, verification and values
 * are there. Its label is in gate 2 ({@link CompletionCheck.ReadSet#p35c}), so gate 3 cannot latch before it completes; P3.5c's gate-3
 * clause, which refuses gate 3 while any quarantine chunk lacks the key, stays as the live check.
 */
public final class QuarantineOriginBackfill {

    /** The move's label in {@code nexus.data_moves}. */
    public static final String LABEL = "rdr229-quarantine-origin-id";

    private QuarantineOriginBackfill() { /* constants */ }
}
