// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.function.UnaryOperator;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): what the by-id restore tests share. The sources they build, the file edits, and the audit
 * that asks every exported site the question the restore must answer: which registry row does each row's collection id name,
 * counted per registry name, so a restore that filed a renamed collection's rows under another collection that took its old
 * label shows as two different counts.
 */
final class ByIdRestoreSupport {

    static final String BGE = "bge-base-en-v15-768";

    /** The marker under which {@link #rowsByCollection} files a twin that names no registry row of the tenant. */
    static final String NO_REGISTRY_ROW = "(no registry row)";

    private ByIdRestoreSupport() {
    }

    // ── sources ─────────────────────────────────────────────────────────────────────────────────

    /**
     * The battery seed of {@code tenant} (one chunk row set per model, the 1024-wide collection retired under the 768-wide one,
     * every exported table seeded) with a history row for the 768-wide collection and one for the 384-wide one, every row's
     * collection id the registry's. Returns the tenant's token.
     */
    static String batterySource(ImportTestEnv env, String tenant) throws Exception {
        String token = env.newTenant(tenant);
        String successor = TenantExportBatteryTest.Seed.collection(tenant, 768);
        String small = TenantExportBatteryTest.Seed.collection(tenant, 384);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, tenant, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, tenant, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        env.su(ctx -> {
            CollectionIdSites.seedHistory(ctx, tenant, successor, small);
            return null;
        });
        return token;
    }

    static byte[] sha(String text) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static String hex(String text) {
        return HexFormat.of().formatHex(sha(text));
    }

    private static Table<?> table(String name) {
        return DSL.table(DSL.name("nexus", name));
    }

    private static Field<Object> column(String name) {
        return DSL.field(DSL.name(name));
    }

    /** The referenced columns of a foreign key definition into {@code catalog_collections}: {@code (tenant_id, name, ...)}. */
    private static final java.util.regex.Pattern REFERENCED = java.util.regex.Pattern
        .compile("REFERENCES\\s+\\S*catalog_collections\\s*\\(([^)]*)\\)");

    /**
     * Drops every foreign key that references the NAME of {@code nexus.catalog_collections}, the move RDR-229 P3.7's name-key
     * drop makes before its one-row rename. A key onto the registry's id (P3.3a's and P3.3c's twins) is kept, so it still guards
     * the rows if those branches meet this one. A partition's inherited copy is left to its parent's drop. Returns
     * {@code table.constraint} of each key dropped, so a test can name them.
     */
    static List<String> dropRegistryNameKeys(ImportTestEnv env) {
        return env.su(ctx -> {
            Table<?> con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
            Table<?> child = DSL.table(DSL.name("pg_catalog", "pg_class")).as("child");
            Table<?> parent = DSL.table(DSL.name("pg_catalog", "pg_class")).as("parent");
            Table<?> childNs = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("cns");
            Table<?> parentNs = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("pns");
            Field<Long> conRel = DSL.field(DSL.name("con", "conrelid"), Long.class);
            Field<Long> conRef = DSL.field(DSL.name("con", "confrelid"), Long.class);
            Field<String> conKind = DSL.field(DSL.name("con", "contype"), Object.class).cast(SQLDataType.VARCHAR);
            Field<Long> conParent = DSL.field(DSL.name("con", "conparentid"), Object.class).cast(SQLDataType.BIGINT);
            List<String[]> keys = new ArrayList<>();
            for (Record r : ctx.select(DSL.field(DSL.name("cns", "nspname"), String.class),
                        DSL.field(DSL.name("child", "relname"), String.class), DSL.field(DSL.name("con", "conname"), String.class),
                        DSL.function("pg_get_constraintdef", SQLDataType.VARCHAR, DSL.field(DSL.name("con", "oid"), Long.class)))
                    .from(con)
                    .join(child).on(DSL.field(DSL.name("child", "oid"), Long.class).eq(conRel))
                    .join(parent).on(DSL.field(DSL.name("parent", "oid"), Long.class).eq(conRef))
                    .join(childNs).on(DSL.field(DSL.name("cns", "oid"), Long.class).eq(DSL.field(DSL.name("child", "relnamespace"), Long.class)))
                    .join(parentNs).on(DSL.field(DSL.name("pns", "oid"), Long.class).eq(DSL.field(DSL.name("parent", "relnamespace"), Long.class)))
                    .where(DSL.field(DSL.name("parent", "relname"), String.class).eq("catalog_collections"))
                    .and(DSL.field(DSL.name("pns", "nspname"), String.class).eq("nexus"))
                    .and(conKind.eq("f")).and(conParent.eq(0L))
                    .fetch()) {
                java.util.regex.Matcher m = REFERENCED.matcher(r.get(3, String.class));
                boolean namesAName = m.find() && java.util.Arrays.stream(m.group(1).split(","))
                    .map(String::trim).anyMatch(c -> c.equals("name"));
                if (namesAName) keys.add(new String[] {r.get(0, String.class), r.get(1, String.class), r.get(2, String.class)});
            }
            List<String> dropped = new ArrayList<>();
            for (String[] k : keys) {
                ctx.alterTable(DSL.table(DSL.name(k[0], k[1]))).dropConstraint(DSL.name(k[2])).execute();
                dropped.add(k[1] + "." + k[2]);
            }
            return dropped;
        });
    }

    /** The registry's ids of {@code tenant}, by collection name. */
    static Map<String, Long> registryIds(ImportTestEnv env, String tenant) {
        return env.su(ctx -> {
            Map<String, Long> m = new LinkedHashMap<>();
            for (Record r : ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).fetch()) {
                m.put(r.get(CATALOG_COLLECTIONS.NAME), r.get(CATALOG_COLLECTIONS.COLLECTION_ID));
            }
            return m;
        });
    }

    /**
     * A registered collection {@code name} with its own rows, as it exists once another collection has taken the label:
     * the name is registered under the same model at {@code model_version} {@code v2}, so its tuple differs from the
     * renamed collection's, and it gets chunks, an owner document, a manifest row, a topic, an aspect row and a relevance-log
     * row, each carrying the new registry row's id. Name keys are not enforced (the caller dropped them), so the rows share
     * the old label with the renamed collection's, and are told apart only by their collection id.
     */
    static void seedCollectionTakingTheLabel(DSLContext ctx, String tenant, String name) {
        PgContainerHelper.insertCollection(ctx, tenant, name, BGE);
        ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v2")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
        Long id = dev.nexus.service.db.CollectionIds.require(ctx, tenant, name);
        List<String> chunkHex = new ArrayList<>();
        for (int i = 0; i < 3; i++) {
            String text = "taking-the-label chunk " + i + " of " + tenant;
            chunkHex.add(hex(text));
            CollectionIdSeeds.insertChunk(ctx, tenant, name, sha(text), VectorSeeds.chunkEmbedding(768),
                TenantExportBatteryTest.Seed.vector(768, 40 + i));
        }
        String doc = "1.1.50";
        CollectionIdSeeds.ownerDocument(ctx, tenant, doc, name);
        CollectionIdSeeds.manifestRows(ctx, tenant, doc, name, 0, chunkHex.get(0), chunkHex.get(1));
        OffsetDateTime now = OffsetDateTime.now();
        ctx.insertInto(table("topics"))
            .set(column("tenant_id"), tenant).set(column("label"), "label-taking").set(column("collection"), name)
            .set(column("collection_id"), id).set(column("created_at"), now).execute();
        ctx.insertInto(table("document_aspects"))
            .set(column("tenant_id"), tenant).set(column("collection"), name).set(column("collection_id"), id)
            .set(column("source_path"), "taking-p1").set(column("extracted_at"), now).set(column("model_version"), "m")
            .set(column("extractor_name"), "e").set(column("source_uri"), "taking-u1").set(column("doc_id"), doc).execute();
        ctx.insertInto(table("relevance_log"))
            .set(column("tenant_id"), tenant).set(column("query"), "taking-q").set(column("chunk_id"), hex("taking-chunk-" + tenant))
            .set(column("action"), "hit").set(column("timestamp"), now).set(column("collection"), name)
            .set(column("collection_id"), id).execute();
    }

    /**
     * Tags one chunk of {@code collectionId} with an origin by name ({@code origin_collection}) and by id
     * ({@code origin_collection_id}), as a quarantine move writes it; returns the chunk's chash in hex.
     */
    static String tagOrigin(DSLContext ctx, String tenant, long collectionId, String originName, long originId) {
        byte[] chash = ctx.select(CHUNKS.CHASH).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION_ID.eq(collectionId))
            .orderBy(CHUNKS.CHASH).limit(1).fetchOne(0, byte[].class);
        assertThat(chash).as("a chunk of collection id " + collectionId).isNotNull();
        ctx.update(CHUNKS).set(CHUNKS.METADATA,
                JSONB.valueOf("{\"origin_collection\": \"" + originName + "\", \"origin_collection_id\": " + originId + ", \"keep\": \"yes\"}"))
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION_ID.eq(collectionId)).and(CHUNKS.CHASH.eq(chash)).execute();
        return HexFormat.of().formatHex(chash);
    }

    /** The {@code origin_collection_id} tag of {@code tenant}'s chunk {@code chashHex}, as text; null when the key is absent. */
    static String originIdOf(ImportTestEnv env, String tenant, String chashHex) {
        return env.su(ctx -> ctx.select(DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, CHUNKS.METADATA,
                DSL.val("origin_collection_id"))).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chashHex)))
            .fetchOne(0, String.class));
    }

    // ── the audit ───────────────────────────────────────────────────────────────────────────────

    /**
     * For every site of a backup file, the rows of {@code tenant} per the registry row their collection id names, by that
     * row's name. A twin that names no registry row is filed under {@link #NO_REGISTRY_ROW}; a NULL twin is not counted.
     * Keyed {@code table.idColumn}.
     */
    static Map<String, Map<String, Long>> rowsByCollection(ImportTestEnv env, String tenant) {
        return env.su(ctx -> {
            Map<String, Map<String, Long>> out = new LinkedHashMap<>();
            for (CollectionIdSites.Site s : CollectionIdSites.exportedSites()) {
                String idName = CollectionIdSites.idColumn(ctx, s);
                Table<?> t = table(s.table()).as("t");
                Field<Long> id = DSL.field(DSL.name("t", idName), Long.class);
                Field<String> rowTenant = DSL.field(DSL.name("t", "tenant_id"), String.class);
                var c = CATALOG_COLLECTIONS.as("c");
                Map<String, Long> m = new TreeMap<>();
                for (Record r : ctx.select(c.NAME, DSL.count()).from(t).leftJoin(c)
                        .on(c.TENANT_ID.eq(rowTenant)).and(c.COLLECTION_ID.eq(id))
                        .where(rowTenant.eq(tenant)).and(id.isNotNull()).groupBy(c.NAME).fetch()) {
                    String name = r.get(c.NAME);
                    m.put(name == null ? NO_REGISTRY_ROW : name, r.get(1, Long.class));
                }
                out.put(s.table() + "." + idName, m);
            }
            return out;
        });
    }

    /** The chunks of {@code tenant}, as chash hex sets per the registry row their collection id names. */
    static Map<String, Set<String>> chunkHashesByCollection(ImportTestEnv env, String tenant) {
        return env.su(ctx -> {
            var c = CATALOG_COLLECTIONS.as("c");
            Map<String, Set<String>> out = new TreeMap<>();
            for (Record r : ctx.select(c.NAME, CHUNKS.CHASH).from(CHUNKS).leftJoin(c)
                    .on(c.TENANT_ID.eq(CHUNKS.TENANT_ID)).and(c.COLLECTION_ID.eq(CHUNKS.COLLECTION_ID))
                    .where(CHUNKS.TENANT_ID.eq(tenant)).fetch()) {
                String name = r.get(c.NAME);
                out.computeIfAbsent(name == null ? NO_REGISTRY_ROW : name, k -> new LinkedHashSet<>())
                    .add(HexFormat.of().formatHex(r.get(CHUNKS.CHASH)));
            }
            return out;
        });
    }

    /**
     * Non-history rows of {@code tenant} with a non-empty name and no id, per site: what the load's CHECK would refuse and a
     * by-id restore must never leave. Empty when every such row has its id.
     */
    static List<String> namedRowsWithoutId(ImportTestEnv env, String tenant) {
        return env.su(ctx -> {
            List<String> out = new ArrayList<>();
            for (CollectionIdSites.Site s : CollectionIdSites.exportedSites()) {
                if (s.history()) continue;
                String idName = CollectionIdSites.idColumn(ctx, s);
                int n = ctx.fetchCount(table(s.table()).as("t"),
                    DSL.field(DSL.name("t", "tenant_id"), String.class).eq(tenant),
                    DSL.field(DSL.name("t", s.nameColumn()), String.class).ne(""),
                    DSL.field(DSL.name("t", idName), Long.class).isNull());
                if (n > 0) out.add(s.table() + "." + idName + ": " + n);
            }
            return out;
        });
    }

    /**
     * Non-history rows of {@code tenant} whose name column is not the current name of the registry row their id names, per site:
     * what a name foreign key kept by the target would refuse. Empty when the restore wrote each row's collection's own name.
     */
    static List<String> rowsNamedOtherwiseThanTheirCollection(ImportTestEnv env, String tenant) {
        return env.su(ctx -> {
            List<String> out = new ArrayList<>();
            for (CollectionIdSites.Site s : CollectionIdSites.exportedSites()) {
                if (s.history()) continue;
                String idName = CollectionIdSites.idColumn(ctx, s);
                Table<?> t = table(s.table()).as("t");
                var c = CATALOG_COLLECTIONS.as("c");
                Field<String> rowTenant = DSL.field(DSL.name("t", "tenant_id"), String.class);
                int n = ctx.selectCount().from(t).join(c)
                    .on(c.TENANT_ID.eq(rowTenant)).and(c.COLLECTION_ID.eq(DSL.field(DSL.name("t", idName), Long.class)))
                    .where(rowTenant.eq(tenant)).and(DSL.field(DSL.name("t", s.nameColumn()), String.class).ne(c.NAME))
                    .fetchOne(0, int.class);
                if (n > 0) out.add(s.table() + "." + s.nameColumn() + ": " + n);
            }
            return out;
        });
    }

    /** The sites whose per-collection counts hold no row at all for {@code registryName}: a vacuous comparison. */
    static List<String> sitesWithoutRowsFor(Map<String, Map<String, Long>> byCollection, String registryName) {
        return byCollection.entrySet().stream().filter(e -> e.getValue().getOrDefault(registryName, 0L) == 0)
            .map(Map.Entry::getKey).toList();
    }

    // ── a file's rows ───────────────────────────────────────────────────────────────────────────

    static byte[] bigint(long v) {
        return ByteBuffer.allocate(8).putLong(v).array();
    }

    /** The file with {@code table}'s rows rewritten page by page by {@code edit}. */
    static ImportTestEnv.BackupFile editRows(ImportTestEnv.BackupFile file, String table,
                                             UnaryOperator<List<BinaryCopyTestSupport.Row>> edit) {
        ImportTestEnv.BackupFile c = file.copy();
        c.pages.computeIfPresent(table, (k, pages) -> pages.stream().map(p -> {
            BinaryCopyTestSupport.Stream s = BinaryCopyTestSupport.parse(p);
            return BinaryCopyTestSupport.encode(new BinaryCopyTestSupport.Stream(s.header(), edit.apply(s.rows())));
        }).toList());
        return c.seal();
    }

    /** A text field of a row, null when the row holds NULL. */
    static String text(BinaryCopyTestSupport.Row row, int index) {
        byte[] f = row.fields().get(index);
        return f == null ? null : new String(f, StandardCharsets.UTF_8);
    }

    static BinaryCopyTestSupport.Row withField(BinaryCopyTestSupport.Row row, int index, byte[] value) {
        List<byte[]> f = new ArrayList<>(row.fields());
        f.set(index, value);
        return new BinaryCopyTestSupport.Row(f);
    }
}
