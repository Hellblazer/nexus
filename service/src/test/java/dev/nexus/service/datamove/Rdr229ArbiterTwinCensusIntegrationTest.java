// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22, linda's ruling 2026-11-22): every name-holding PRIMARY KEY or UNIQUE index that an upsert uses as its
 * ON CONFLICT arbiter has a VALID id twin, so P3.5c's id-keyed upserts have an arbiter to infer and P3.7's one-row rename cannot make a
 * duplicate. The arbiters come from elsinore's P3.5c mapping (the op=on_conflict rows of her reader census, 16 Java sites and the
 * vectors-069 SQL bodies): chunks and taxonomy_centroids (P3.3b/c's parent UNIQUE on the id key), document_aspects, chash_remap,
 * taxonomy_meta and topics ({@link IdUniqueIndexPass}); chunk_orphaned_at and canary_query_ring, the two SQL arbiters
 * (vectors-069's stamp_chunks_on_manifest_delete/_update and canary_query_ring_upsert) this census found beside the mapping, also
 * {@link IdUniqueIndexPass}'s; and catalog_collections, whose upserts keep the label (G15: ON CONFLICT (tenant_id,
 * name) WHERE superseded_by_id IS NULL infers P3.6's live-label index), so it needs no id twin.
 *
 * <p>Completeness: every name-holding unique index in the live catalog (a unique index with a collection-name column) is either an arbiter
 * above or exempt with its reason, so a new one fails here until someone says which it is.
 */
class Rdr229ArbiterTwinCensusIntegrationTest extends Rdr229SwapMovesFixtures {

    /** An arbiter: its table, the name key's columns (and predicate), and the id twin's columns (and predicate); null twin = label kept. */
    record Arbiter(String table, String nameColumns, String idColumns, String predicate) { }

    /** The checked list in main code ({@link ArbiterTwins}), and the registry, whose upserts keep the label (G15). */
    static final List<Arbiter> ARBITERS = java.util.stream.Stream.concat(
        ArbiterTwins.ALL.stream().map(a -> new Arbiter(a.table(), a.nameColumns(), a.idColumns(), a.predicate())),
        java.util.stream.Stream.of(new Arbiter("catalog_collections", "tenant_id, name", null, null))).toList();

    /** Name-holding unique indexes no upsert arbitrates on (elsinore's P3.5c census, op=on_conflict), by "table(columns)". */
    static final Map<String, String> EXEMPT = new LinkedHashMap<>();
    static {
        EXEMPT.put("catalog_collections(tenant_id, name, embedding_model)",
            "the registry: its upserts keep the label (G15), and this key carries no ON CONFLICT; the id twins are vectors-068's UNIQUE keys");
        EXEMPT.put("catalog_collections(tenant_id, name) WHERE superseded_by = ''::text",
            "P3.6's live-label index: the arbiter G15's upserts infer by label (ON CONFLICT (tenant_id, name) WHERE ...); label kept by design");
        EXEMPT.put("search_telemetry(tenant_id, ts, query_hash, collection)",
            "a duplicate telemetry row is harmless (linda 2026-11-22); TelemetryRepository's upserts are target-less ON CONFLICT DO NOTHING");
    }

    static String key(String table, String columns, String predicate) {
        return table + "(" + columns + ")" + (predicate == null ? "" : " WHERE " + predicate);
    }

    /** Column names whose presence in an index makes it name-holding; on catalog_collections the label column is {@code name}. */
    static final List<String> NAME_COLUMNS = List.of("collection", "source_collection", "target_collection", "physical_collection",
        "origin_collection", "quarantine_collection");

    /** Every VALID unique index in schema nexus on a top-level table (not a partition) whose key includes a collection-name column. */
    TreeSet<String> nameHoldingUniques() {
        var out = new TreeSet<String>();
        for (var index : IdIndexCatalogReads.uniqueIndexes(ctx())) {
            boolean holdsName = index.coveredColumns().stream()
                .anyMatch(c -> NAME_COLUMNS.contains(c) || (index.table().equals("catalog_collections") && c.equals("name")));
            if (holdsName) {
                out.add(key(index.table(), index.keyColumns(), index.predicate()));
            }
        }
        return out;
    }

    @Test
    void everyArbiterOnANameKeyHasAValidIdTwinAndEveryNameKeyIsAccountedFor() throws Exception {
        seed("arbiters");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the chunks swap completed").isNotNull();
        assertThat(completedAt(CENTROIDS_SWAP)).as("precondition: the centroids swap completed").isNotNull();
        new IdUniqueIndexPass(adminConnection, new BootWorkRegistry(Clock.systemUTC()), () -> true).runOnce();

        TreeSet<String> live = nameHoldingUniques();
        for (Arbiter a : ARBITERS) {
            assertThat(live).as("the name key " + a.table() + " arbitrates on is there").contains(key(a.table(), a.nameColumns(), a.predicate()));
            if (a.idColumns() != null) {
                assertThat(idUniques(a.table())).as(a.table() + ": a VALID unique index on the id key, the predicate the upsert names")
                    .contains(key(a.table(), a.idColumns(), a.predicate()));
            }
        }
        var accounted = new TreeSet<String>(EXEMPT.keySet());
        ARBITERS.forEach(a -> accounted.add(key(a.table(), a.nameColumns(), a.predicate())));
        assertThat(live).as("every name-holding unique index is an arbiter with its twin or exempt with a reason").isSubsetOf(accounted);
        assertThat(EXEMPT.keySet()).as("no stale exemption").isSubsetOf(live);
    }

    TreeSet<String> idUniques(String table) {
        var out = new TreeSet<String>();
        for (var index : IdIndexCatalogReads.uniqueIndexes(ctx())) {
            if (index.table().equals(table)) {
                out.add(key(table, index.keyColumns(), index.predicate()));
            }
        }
        return out;
    }
}
