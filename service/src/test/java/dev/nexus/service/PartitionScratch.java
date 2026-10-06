// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * RDR-225 P1.2 (nexus-3wh8d.7): the scratch partitioned parents {@code nexus.chunks_new} and
 * {@code nexus.taxonomy_centroids_new} (db.changelog-test-p225-scratch.xml) and the typed helpers the
 * partition-function tests share. No raw SQL: the DDL is a test changelog, the calls are the generated
 * {@code Routines}, the catalog reads are typed DSL over {@code pg_catalog}.
 */
final class PartitionScratch {

    static final String CHUNKS_NEW = "chunks_new";
    static final String CENTROIDS_NEW = "taxonomy_centroids_new";

    static final String CODE_3 = "voyage-code-3";
    static final String CONTEXT_3 = "voyage-context-3";
    static final String BGE_768 = "bge-base-en-v15-768";
    static final String MINILM_384 = "minilm-l6-v2-384";
    static final List<String> REAL_MODELS = List.of(CODE_3, CONTEXT_3, BGE_768, MINILM_384);

    private PartitionScratch() {}

    /**
     * Once per database, right after migration: copy the live column sets and parent-level index definitions
     * into template tables, then drop the walk's live trigger and partitioned parents, so the scratch parents'
     * partition names (which strip {@code _new}) cannot collide with the live ones. See the changelog's header.
     */
    static void captureTemplatesAndClearLiveParents(Connection admin) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(admin,
            "db/changelog-test/db.changelog-test-p225-templates.xml",
            "databasechangelog_test_p225_templates", Map.of());
    }

    /** Drop and rebuild the scratch parents on the schema owner's connection. */
    static void reset(Connection admin) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(admin,
            "db/changelog-test/db.changelog-test-p225-scratch.xml",
            "databasechangelog_test_p225_scratch", Map.of());
    }

    // ── the oracle: an independent implementation of the naming rule ─────────

    static String sha256Hex(String s, int chars) {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : d) sb.append(String.format("%02x", b));
            return sb.substring(0, chars);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** {@code <base>_m<8hex>[_t_<16hex>]}, base = the parent's relname without a trailing {@code _new}. */
    static String expectedName(String parentRel, String model, String tenantOrNull) {
        String base = parentRel.replaceAll("_new$", "");
        String mp = base + "_m" + sha256Hex(model, 8);
        return tenantOrNull == null ? mp : mp + "_t_" + sha256Hex(tenantOrNull, 16);
    }

    // ── the functions under test ─────────────────────────────────────────────

    @SuppressWarnings("deprecation")
    static int createTenantPartitions(DSLContext ctx, String parentRel, String tenant, boolean force) {
        return dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
            ctx.configuration(), "nexus." + parentRel, tenant, force);
    }

    @SuppressWarnings("deprecation")
    static String createModelPartition(DSLContext ctx, String parentRel, String model, boolean force) {
        return String.valueOf(dev.nexus.service.jooq.nexus.Routines.createModelPartition(
            ctx.configuration(), "nexus." + parentRel, model, force));
    }

    @SuppressWarnings("deprecation")
    static int syncAccess(DSLContext ctx, String parentRel, boolean force) {
        return dev.nexus.service.jooq.nexus.Routines.partitionSyncAccess(
            ctx.configuration(), "nexus." + parentRel, force);
    }

    static String partitionName(DSLContext ctx, String parentRelName, String model, String tenantOrNull) {
        return dev.nexus.service.jooq.nexus.Routines.partitionName(
            ctx.configuration(), parentRelName, model, tenantOrNull);
    }

    @SuppressWarnings("deprecation")
    static int dropTenantPartitions(DSLContext ctx, String tenant) {
        return dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
    }

    /**
     * {@code definer=<prosecdef>;config=<proconfig>;public=<PUBLIC may execute>;nexus_svc=<nexus_svc may execute>}
     * for a nexus function, read from {@code pg_proc.proacl} text (a null ACL is the default, PUBLIC may execute).
     */
    static String functionFacts(DSLContext ctx, String function) {
        var r = ctx.select(DSL.field(DSL.name("p", "prosecdef"), Boolean.class),
                DSL.function("array_to_string", String.class, DSL.field(DSL.name("p", "proconfig")), DSL.inline(",")),
                DSL.field(DSL.name("p", "proacl")).cast(String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq(function))
            .fetchOne();
        String acl = r.get(2, String.class);
        boolean pub = acl == null || acl.startsWith("{=X/") || acl.contains(",=X/");
        boolean svc = acl != null && acl.contains("nexus_svc=X/");
        return "definer=" + r.get(0, Boolean.class) + ";config=" + r.get(1, String.class) + ";public=" + pub + ";nexus_svc=" + svc;
    }

    // ── catalog reads ────────────────────────────────────────────────────────

    private static Field<String> relname(String alias) {
        return DSL.field(DSL.name(alias, "relname"), String.class);
    }

    private static Table<?> pgClass(String alias) {
        return DSL.table(DSL.name("pg_catalog", "pg_class")).as(alias);
    }

    /** A direct child partition: its relname and bound expression. */
    record Child(String name, String bound) {}

    static List<Child> children(DSLContext ctx, String parentRel) {
        Field<String> bound = DSL.function("pg_get_expr", String.class,
            DSL.field(DSL.name("c", "relpartbound")), DSL.field(DSL.name("c", "oid")));
        return ctx.select(relname("c"), bound)
            .from(DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("i"))
            .join(pgClass("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "inhrelid"))))
            .join(pgClass("p")).on(DSL.field(DSL.name("p", "oid")).eq(DSL.field(DSL.name("i", "inhparent"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(relname("p").eq(parentRel))
            .orderBy(relname("c"))
            .fetch(r -> new Child(r.get(0, String.class), r.get(1, String.class)));
    }

    /** Names of every relation (table, index, sequence, ...) in the nexus schema. */
    static int nexusRelationCount(DSLContext ctx) {
        return ctx.selectCount()
            .from(pgClass("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .fetchOne(0, Integer.class);
    }

    /** {@code pg_get_constraintdef} of the named constraint on a nexus relation, or null. */
    static String constraintDef(DSLContext ctx, String rel, String conname) {
        Field<String> def = DSL.function("pg_get_constraintdef", String.class, DSL.field(DSL.name("k", "oid")));
        return ctx.select(def)
            .from(DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("k"))
            .join(pgClass("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("k", "conrelid"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(relname("c").eq(rel))
            .and(DSL.field(DSL.name("k", "conname"), String.class).eq(conname))
            .fetchOne(def);
    }

    /** One policy, in the terms the copy must preserve. */
    record PolicyRow(String name, String permissive, String roles, String cmd, String qual, String withCheck) {}

    static List<PolicyRow> policies(DSLContext ctx, String rel) {
        Field<String> roles = DSL.function("array_to_string", String.class,
            DSL.field(DSL.name("roles")), DSL.inline(","));
        return ctx.select(DSL.field(DSL.name("policyname"), String.class),
                          DSL.field(DSL.name("permissive"), String.class), roles,
                          DSL.field(DSL.name("cmd"), String.class),
                          DSL.field(DSL.name("qual"), String.class),
                          DSL.field(DSL.name("with_check"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_policies")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("tablename"), String.class).eq(rel))
            .orderBy(DSL.field(DSL.name("policyname")))
            .fetch(r -> new PolicyRow(r.get(0, String.class), r.get(1, String.class), r.get(2, String.class),
                r.get(3, String.class), r.get(4, String.class), r.get(5, String.class)));
    }

    /** The relation's ACL entries ({@code grantee=privs/grantor}) as a set; empty when no ACL was ever written. */
    static TreeSet<String> acl(DSLContext ctx, String rel) {
        String text = ctx.select(DSL.field(DSL.name("c", "relacl")).cast(String.class))
            .from(pgClass("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(relname("c").eq(rel))
            .fetchOne(0, String.class);
        TreeSet<String> out = new TreeSet<>();
        if (text != null && text.length() > 2) {
            out.addAll(Arrays.asList(text.substring(1, text.length() - 1).split(",")));
        }
        return out;
    }

    /** {@code pg_get_partkeydef} of a nexus relation (for example {@code LIST (tenant_id)}). */
    static String partKeyDef(DSLContext ctx, String rel) {
        return ctx.select(DSL.function("pg_get_partkeydef", String.class, DSL.field(DSL.name("c", "oid"))))
            .from(pgClass("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(relname("c").eq(rel))
            .fetchOne(0, String.class);
    }

    /** {@code pg_proc.proconfig} of a nexus function, comma-joined (the function-level SET clauses). */
    static String procConfig(DSLContext ctx, String function) {
        return ctx.select(DSL.function("array_to_string", String.class, DSL.field(DSL.name("p", "proconfig")), DSL.inline(",")))
            .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq(function))
            .fetchOne(0, String.class);
    }

    /** Triggers on {@code nexus.<rel>} that call {@code nexus.<function>}. */
    static int triggersCalling(DSLContext ctx, String rel, String function) {
        return ctx.selectCount()
            .from(DSL.table(DSL.name("pg_catalog", "pg_trigger")).as("t"))
            .join(pgClass("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("t", "tgrelid"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                .on(DSL.field(DSL.name("p", "oid")).eq(DSL.field(DSL.name("t", "tgfoid"))))
            .where(relname("c").eq(rel))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq(function))
            .fetchOne(0, Integer.class);
    }

    /** Name and mode of the relation lock {@code pid} is currently WAITING for in nexus, or null. */
    static LockRow waitingLock(DSLContext ctx, int pid) {
        return ctx.select(relname("c"), DSL.field(DSL.name("l", "mode"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_locks")).as("l"))
            .join(pgClass("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("l", "relation"))))
            .where(DSL.field(DSL.name("l", "pid"), Integer.class).eq(pid))
            .and(DSL.field(DSL.name("l", "granted"), Boolean.class).isFalse())
            .and(DSL.field(DSL.name("l", "locktype"), String.class).eq("relation"))
            .limit(1)
            .fetchOne(r -> new LockRow(r.get(0, String.class), r.get(1, String.class), false));
    }

    /** {@code pg_get_userbyid(relowner)} of a nexus relation. */
    static String owner(DSLContext ctx, String rel) {
        return PgCatalogProbes.relationOwner(ctx, "nexus", rel);
    }

    static int backendPid(DSLContext ctx) {
        return ctx.select(DSL.function("pg_backend_pid", Integer.class)).fetchOne(0, Integer.class);
    }

    /** One relation lock a backend holds or waits for. */
    record LockRow(String relation, String mode, boolean granted) {
        @Override public String toString() { return relation + " " + mode + (granted ? "" : " (WAITING)"); }
    }

    /** Relation locks of {@code pid} on nexus relations, tables only (no indexes), sorted. */
    static List<LockRow> locks(DSLContext ctx, int pid) {
        List<LockRow> out = new ArrayList<>(ctx.select(relname("c"),
                DSL.field(DSL.name("l", "mode"), String.class),
                DSL.field(DSL.name("l", "granted"), Boolean.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_locks")).as("l"))
            .join(pgClass("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("l", "relation"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("l", "pid"), Integer.class).eq(pid))
            .and(DSL.field(DSL.name("l", "locktype"), String.class).eq("relation"))
            .and(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).in("r", "p"))
            .fetch(r -> new LockRow(r.get(0, String.class), r.get(1, String.class), r.get(2, Boolean.class))));
        out.sort(java.util.Comparator.comparing(LockRow::relation).thenComparing(LockRow::mode));
        return out;
    }

    /** {@code current_setting(name)}. */
    static String setting(DSLContext ctx, String name) {
        return ctx.select(DSL.function("current_setting", String.class, DSL.inline(name))).fetchOne(0, String.class);
    }

    // ── row writers (typed against the generated column data types, so vector binding is the production one) ──

    private static Field<Vector> vec(int dim) {
        return switch (dim) {
            case 384 -> DSL.field(DSL.name("embedding_384"), CHUNKS.EMBEDDING_384.getDataType());
            case 768 -> DSL.field(DSL.name("embedding_768"), CHUNKS.EMBEDDING_768.getDataType());
            case 1024 -> DSL.field(DSL.name("embedding_1024"), CHUNKS.EMBEDDING_1024.getDataType());
            default -> throw new IllegalArgumentException("dim " + dim);
        };
    }

    private static Vector vector(int dim) {
        float[] f = new float[dim];
        Arrays.fill(f, 0.1f);
        f[0] = 0.5f;
        return Vector.of(f);
    }

    /** Insert one chunk through the scratch parent, populating the embedding column of {@code populatedDim}. */
    static int insertChunk(DSLContext ctx, String tenant, String collection, byte[] chash, String model,
                           int populatedDim) {
        return ctx.insertInto(DSL.table(DSL.name("nexus", CHUNKS_NEW)))
            .set(DSL.field(DSL.name("tenant_id"), String.class), tenant)
            .set(DSL.field(DSL.name("collection"), String.class), collection)
            .set(DSL.field(DSL.name("chash"), byte[].class), chash)
            .set(DSL.field(DSL.name("embedding_model"), String.class), model)
            .set(DSL.field(DSL.name("chunk_text"), String.class), "scratch")
            .set(vec(populatedDim), vector(populatedDim))
            .execute();
    }

    /** Insert one centroid through the scratch parent. */
    static int insertCentroid(DSLContext ctx, String tenant, String collection, long topicId, String model,
                              int populatedDim) {
        return ctx.insertInto(DSL.table(DSL.name("nexus", CENTROIDS_NEW)))
            .set(DSL.field(DSL.name("tenant_id"), String.class), tenant)
            .set(DSL.field(DSL.name("collection"), String.class), collection)
            .set(DSL.field(DSL.name("topic_id"), Long.class), topicId)
            .set(DSL.field(DSL.name("embedding_model"), String.class), model)
            .set(DSL.field(DSL.name("label"), String.class), "scratch")
            .set(vec(populatedDim), vector(populatedDim))
            .execute();
    }

    /** {@code tableoid::regclass::text} of the one row with this {@code tenant_id} in the scratch relation. */
    static String leafHolding(DSLContext ctx, String parentRel, String tenant) {
        Field<String> tbl = DSL.field(DSL.name("tableoid")).cast(
            org.jooq.impl.DefaultDataType.getDefaultDataType("regclass")).cast(String.class);
        Record r = ctx.select(tbl)
            .from(DSL.table(DSL.name("nexus", parentRel)))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
            .fetchOne();
        return r == null ? null : r.get(0, String.class);
    }

    // ── evidence ─────────────────────────────────────────────────────────────

    private static final Path EVIDENCE = Path.of("target", "p225-evidence.txt");

    /** Append one line to target/p225-evidence.txt (read back by the person writing the RDR wording). */
    static synchronized void evidence(String line) {
        try {
            Files.createDirectories(EVIDENCE.getParent());
            Files.writeString(EVIDENCE, line + System.lineSeparator(),
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }
}
