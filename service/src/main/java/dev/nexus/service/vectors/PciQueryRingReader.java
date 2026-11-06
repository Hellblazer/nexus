// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Objects;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;

/**
 * Reads one collection's query ring for the recall canary (RDR-230 Technical Design 6, bead nexus-4lnn1.1.3; the
 * canary runner, bead nexus-4lnn1.1.4, is the caller).
 *
 * <p>Newest first, at most {@value #READ_LIMIT} rows, under the tenant's scope as nexus_svc, so row-level security
 * confines the read to that tenant: a vector never crosses tenants. The vector is returned and nothing else about the
 * query exists to return, since the ring holds embeddings and no query text.
 */
public final class PciQueryRingReader {

    private static final Logger log = LoggerFactory.getLogger(PciQueryRingReader.class);

    /** The most rows a canary run reads from a ring. */
    public static final int READ_LIMIT = 10;

    /**
     * One ring entry.
     *
     * @param model   the collection's embedding model
     * @param vector  the query embedding
     * @param addedAt when the vector last entered the ring
     */
    public record RingEntry(String model, float[] vector, OffsetDateTime addedAt) { }

    private final TenantScope scope;

    public PciQueryRingReader(TenantScope scope) {
        this.scope = Objects.requireNonNull(scope, "scope");
    }

    /** The collection's ring, newest first (ties on the digest, as the writer's trim breaks them), at most ten. */
    public List<RingEntry> load(String tenant, String collection) {
        Objects.requireNonNull(collection, "collection");
        List<RingEntry> entries = scope.withTenant(tenant, ctx -> ctx
            .select(CANARY_QUERY_RING.EMBEDDING_MODEL, CANARY_QUERY_RING.EMBEDDING_384,
                CANARY_QUERY_RING.EMBEDDING_768, CANARY_QUERY_RING.EMBEDDING_1024, CANARY_QUERY_RING.ADDED_AT)
            .from(CANARY_QUERY_RING)
            .where(CANARY_QUERY_RING.TENANT_ID.eq(tenant))
            .and(CANARY_QUERY_RING.COLLECTION.eq(collection))
            .orderBy(CANARY_QUERY_RING.ADDED_AT.desc(), CANARY_QUERY_RING.VECTOR_DIGEST)
            .limit(READ_LIMIT)
            .fetch(r -> {
                Vector v = r.value2() != null ? r.value2() : r.value3() != null ? r.value3() : r.value4();
                return new RingEntry(r.value1(), v == null ? null : v.floats(), r.value5());
            }));
        log.debug("event=pci_ring_read tenant={} entries={}", tenant, entries.size());
        return entries;
    }
}
