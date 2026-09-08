// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A write targeted a {@code (tenant, collection)} pair with no row in
 * {@code nexus.catalog_collections} (RDR-204 Phase 1, bead nexus-ft04v.7).
 *
 * <p>Seven call sites (AspectRepository, TaxonomyRepository, ChashRepository,
 * StagingPromoteOps, CombinedWriteService, and two in PgVectorRepository) used to
 * paper over this with a stub {@code INSERT ... ON CONFLICT DO NOTHING} carrying
 * blank {@code content_type}/{@code owner_id}/{@code embedding_model} — a row whose
 * presence proved nothing about its columns, which is Gap 2 this RDR closes. Those
 * stub inserts are deleted outright, not relocated: {@link CollectionRegistry
 * #requireRegistered} replaces each one with a read-only existence check and throws
 * this instead of ever writing a row.
 *
 * <p>Raised INSIDE {@code TenantScope.withTenant}'s work lambda, before any mutating
 * statement runs — same transactional position the stub insert used to occupy, and
 * the same shape as {@link CatalogIdentityConflictException} / {@link
 * PipelineConflictException} — so the enclosing transaction rolls back with no
 * partial write and no stub row, and the exception carries no SQLSTATE.
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps it to a typed 422
 * naming the registration route as the remedy, ahead of the generic class-23 walk.
 */
public final class UnregisteredCollectionException extends RuntimeException {

    private final String tenant;
    private final String collection;

    public UnregisteredCollectionException(String tenant, String collection) {
        super("collection '" + collection + "' is not registered for tenant '" + tenant
            + "' — register it first via POST /v1/catalog/collections/upsert");
        this.tenant = tenant;
        this.collection = collection;
    }

    public String tenant() {
        return tenant;
    }

    public String collection() {
        return collection;
    }
}
