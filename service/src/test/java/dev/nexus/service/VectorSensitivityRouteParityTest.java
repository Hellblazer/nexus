// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.SQLDataType;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_FLOORED;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-rfvyy.7.4 (RDR-243 Phase 2 Step 2.1b, Test Plan "Route parity"): every engine route that returns chunk text
 * accepts {@code sensitivity} and filters. The list of such routes is DERIVED, never typed:
 * <ol>
 *   <li>from {@code pg_proc}: every {@code nexus} function whose result row carries chunk text must end in {@code
 *       p_exclude_sensitive} (a function added later joins the set by its result shape);</li>
 *   <li>from the route table: {@link ChunkTextRouteCensus} reads {@code VectorHandler}'s dispatch, follows each
 *       handler into {@code PgVectorRepository}, and calls a route a chunk-text route when a reachable method
 *       selects {@code chunk_text} (the jOOQ reads {@code pg_proc} cannot see: get-by-id) or calls one of the
 *       functions of step 1. Each such route's handler must pass the parsed {@code sensitivity} on every repository
 *       call that can read text, and each method that reads the text must gate it on {@code excludeSensitive};</li>
 *   <li>a sink census over the other engine sources: a method that reads chunk text and is not reached by a
 *       vector route must be either gated (the catalog by-address routes) or named below with the reason it serves
 *       no read route, so a new text read cannot slip in unclassified.</li>
 * </ol>
 * The behaviour half (an {@code exclude} request drops a planted sensitive row on each derived route) is in {@code
 * VectorSensitivityExcludeIntegrationTest}, which also asserts it holds a recipe for every route derived here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorSensitivityRouteParityTest {

    private static final Path MAIN = Path.of("src/main/java/dev/nexus/service");
    private static final Path VECTOR_HANDLER = MAIN.resolve("http/VectorHandler.java");
    private static final Path VECTOR_REPO = MAIN.resolve("vectors/PgVectorRepository.java");
    private static final Pattern TEXT_COLUMN = Pattern.compile("[(,]\\s*(content|chunk_text)\\s+text\\b");

    PostgreSQLContainer<?> pg;
    /** every nexus function whose result row carries chunk text, one entry per overload. */
    List<FnSig> textFunctions;
    /** their names: the set the route census matches {@code X.call(} against. */
    Set<String> textFunctionNames;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        textFunctions = textFunctionsFromPgProc(pg);
        textFunctionNames = names(textFunctions);
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    // ── pg_proc ───────────────────────────────────────────────────────────────

    /** One function signature of a schema; overloads are separate entries, never collapsed by name. */
    record FnSig(String name, String args, String result) {
        String key() { return name + "(" + args + ")"; }
    }

    static Set<String> names(List<FnSig> sigs) {
        Set<String> out = new TreeSet<>();
        for (FnSig s : sigs) out.add(s.name());
        return out;
    }

    /** Every {@code nexus} function whose result row carries chunk text, one entry per overload, from {@code pg_proc}. */
    static List<FnSig> textFunctionsFromPgProc(PostgreSQLContainer<?> pg) throws Exception {
        return textFunctionsFromPgProc(pg, "nexus");
    }

    /** The result-shape derivation over any schema (the negative control plants a function in its own). */
    static List<FnSig> textFunctionsFromPgProc(PostgreSQLContainer<?> pg, String schema) throws Exception {
        List<FnSig> out = new ArrayList<>();
        for (FnSig s : allFunctions(pg, schema)) {
            if (s.result() != null && resultCarriesChunkText(s.result())) out.add(s);
        }
        return out;
    }

    /** Every function and procedure of a schema with its argument list and result type, one entry per overload. */
    static List<FnSig> allFunctions(PostgreSQLContainer<?> pg, String schema) throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
        Field<Object> oid = DSL.field(DSL.name("p", "oid"), Object.class);
        Field<String> args = DSL.function("pg_get_function_arguments", String.class, oid);
        Field<String> result = DSL.function("pg_get_function_result", String.class, oid);
        List<FnSig> out = new ArrayList<>();
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(name, args, result).from(proc)
                .join(nsp).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
                .orderBy(name, args)
                .fetch();
            for (var r : rows) out.add(new FnSig(r.get(name), r.get(args), r.get(result)));
        }
        return out;
    }

    /** True when a function's result type names a {@code content} or {@code chunk_text} text column. */
    static boolean resultCarriesChunkText(String result) {
        return TEXT_COLUMN.matcher(result).find();
    }

    /** The relations of a schema that have a {@code chunk_text} column (tables, partitions and views), by name. */
    static Set<String> chunkTextRelations(PostgreSQLContainer<?> pg, String schema) throws Exception {
        Set<String> out = new TreeSet<>();
        try (Connection su = pg.createConnection("")) {
            Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(relname)
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .join(DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a"))
                    .on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("c", "oid")))
                        .and(DSL.field(DSL.name("a", "attname"), String.class).eq("chunk_text"))
                        .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).eq(false)))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
                .and(DSL.field(DSL.name("c", "relkind"), String.class).in("r", "p", "v", "m", "f"))
                .fetch();
            for (var r : rows) out.add(r.get(relname));
        }
        return out;
    }

    /**
     * nexus-rfvyy.7.17: the second derivation. The result-shape matcher above sees only a result column NAMED {@code
     * content} or {@code chunk_text}; a function that returns the text as {@code body} or {@code snippet}, or as a row
     * of a chunk table ({@code SETOF nexus.chunks}, {@code select *}, a view), would slip past it and out of the
     * exclude-argument check. So also list every function of {@code schema} whose definition or result type either
     * mentions the {@code chunk_text} column or names a relation of {@code relationSchema} that has it (r1 F5), one
     * entry per overload.
     */
    static List<FnSig> functionsMentioningChunkText(PostgreSQLContainer<?> pg, String schema,
                                                     String relationSchema) throws Exception {
        Set<String> relations = chunkTextRelations(pg, relationSchema);
        if (relations.isEmpty()) throw new AssertionError("no relation of " + relationSchema + " has a chunk_text column");
        String relationRegex = "\\m(" + String.join("|", relations) + ")\\M";
        List<FnSig> out = new ArrayList<>();
        try (Connection su = pg.createConnection("")) {
            Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
            Field<Object> oid = DSL.field(DSL.name("p", "oid"), Object.class);
            Field<String> args = DSL.function("pg_get_function_arguments", String.class, oid);
            Field<String> result = DSL.function("pg_get_function_result", String.class, oid);
            Field<String> def = DSL.function("pg_get_functiondef", String.class, oid);
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(name, args.as("args"), result.as("res"))
                .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
                .and(DSL.field(DSL.name("p", "prokind"), String.class).in("f", "p"))
                .and(def.likeRegex("\\mchunk_text\\M").or(def.likeRegex(relationRegex))
                    .or(DSL.coalesce(result, "").likeRegex(relationRegex)))
                .orderBy(name, args)
                .fetch();
            for (var r : rows) {
                out.add(new FnSig(r.get(name), r.get("args", String.class), r.get("res", String.class)));
            }
        }
        return out;
    }

    static List<FnSig> functionsMentioningChunkText(PostgreSQLContainer<?> pg) throws Exception {
        return functionsMentioningChunkText(pg, "nexus", "nexus");
    }

    /** How much of a chunk a classified function's result can carry. */
    enum Exposure {
        /** The result row holds no chunk text at all. */
        NO_TEXT,
        /** The result holds an 80-character prefix of a chunk's text, only for chunks that have no owner. */
        OWNERLESS_LABEL
    }

    /** A classified definition reader: its pinned result type, how much text the result can carry, and why. */
    record Classified(String result, Exposure exposure, String reason) { }

    /**
     * The problems in a classification of the definition readers (r1 F5), one line each: a reader that is neither in
     * the result-shape set nor classified; a classified entry no definition scan finds any more (the function was
     * dropped, renamed or its arguments changed: the entry is stale); and a classified entry whose function now
     * returns something other than the result type pinned for it.
     */
    static List<String> classificationProblems(List<FnSig> byDefinition, Set<String> byShapeKeys,
                                               Map<String, Classified> classified) {
        List<String> problems = new ArrayList<>();
        Map<String, FnSig> found = new TreeMap<>();
        for (FnSig s : byDefinition) found.put(s.key(), s);
        for (var e : found.entrySet()) {
            if (!byShapeKeys.contains(e.getKey()) && !classified.containsKey(e.getKey())) {
                problems.add("UNCLASSIFIED " + e.getKey() + " returns " + e.getValue().result());
            }
        }
        for (var e : classified.entrySet()) {
            FnSig s = found.get(e.getKey());
            if (s == null) {
                problems.add("STALE " + e.getKey() + ": no function with this signature reads chunk text any more");
            } else if (!String.valueOf(s.result()).equals(e.getValue().result())) {
                problems.add("RESULT CHANGED " + e.getKey() + ": pinned " + e.getValue().result() + ", now "
                    + s.result());
            }
        }
        return problems;
    }

    static Set<String> keys(List<FnSig> sigs) {
        Set<String> out = new TreeSet<>();
        for (FnSig s : sigs) out.add(s.key());
        return out;
    }

    @Test
    void theResultShapeMatcherFiresOnTheColumnNamesItClaimsAndOnlyThose() {
        assertThat(resultCarriesChunkText("TABLE(chash bytea, content text, distance double precision)")).isTrue();
        assertThat(resultCarriesChunkText("TABLE(chash bytea, chunk_text text)")).isTrue();
        assertThat(resultCarriesChunkText("TABLE(content text)")).as("first column").isTrue();
        assertThat(resultCarriesChunkText("TABLE(chash bytea, chunk_text_hash text)"))
            .as("a hash column is not chunk text").isFalse();
        assertThat(resultCarriesChunkText("TABLE(chash bytea, body text)"))
            .as("a text column under another name is invisible to the shape matcher: the definition scan covers it")
            .isFalse();
    }

    /**
     * Create a fresh throwaway schema, plant functions in it through a {@code PgContainerHelper.plantCensusProbe*}
     * helper (jOOQ has no CREATE FUNCTION), run {@code body}, and drop the schema again.
     */
    private void withProbeSchema(String probe, Planter plant, ThrowingRunnable body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.dropSchemaIfExists(DSL.name(probe)).cascade().execute();
            dsl.createSchema(DSL.name(probe)).execute();
            plant.plant(su, probe);
        }
        try {
            body.run();
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).dropSchemaIfExists(DSL.name(probe)).cascade().execute();
            }
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable { void run() throws Exception; }

    @FunctionalInterface
    private interface Planter { void plant(Connection su, String schema) throws Exception; }

    @Test
    void aFunctionThatReturnsChunkTextUnderAnotherColumnNameIsCaughtByTheDefinitionScan() throws Exception {
        String probe = "census_probe";
        withProbeSchema(probe, PgContainerHelper::plantCensusProbeLeak, () -> {
            List<FnSig> byShape = textFunctionsFromPgProc(pg, probe);
            List<FnSig> byDefinition = functionsMentioningChunkText(pg, probe, "nexus");
            assertThat(names(byShape)).as("the planted function returns chunk text as 'body': the shape matcher misses it")
                .doesNotContain("leak");
            assertThat(names(byDefinition)).as("the definition scan sees the planted function").contains("leak");
            assertThat(classificationProblems(byDefinition, keys(byShape), Map.of()))
                .as("so the cross-check names it").singleElement().asString().startsWith("UNCLASSIFIED leak(");
        });
    }

    @Test
    void aFunctionThatReturnsAWholeChunkRowWithoutNamingTheColumnIsCaughtByTheRelationScan() throws Exception {
        String probe = "census_probe_rel";
        withProbeSchema(probe, PgContainerHelper::plantCensusProbeRel, () -> {
            List<FnSig> byDefinition = functionsMentioningChunkText(pg, probe, "nexus");
            assertThat(names(byDefinition))
                .as("setof nexus.chunks and a select over the chunk table are both seen, no chunk_text written")
                .containsExactlyInAnyOrder("setof_leak", "star_leak");
            assertThat(classificationProblems(byDefinition, Set.of(), Map.of())).hasSize(2);
        });
    }

    @Test
    void overloadsAreSeparateEntriesAndOneClassifiedOverloadDoesNotCoverTheOther() throws Exception {
        String probe = "census_probe_ovl";
        withProbeSchema(probe, PgContainerHelper::plantCensusProbeOverloads, () -> {
            List<FnSig> byDefinition = functionsMentioningChunkText(pg, probe, "nexus");
            assertThat(byDefinition).as("both overloads are listed").hasSize(2);
            Map<String, Classified> onlyText = Map.of("twin(p text)",
                new Classified("TABLE(chash bytea, body text)", Exposure.NO_TEXT, "plant"));
            assertThat(classificationProblems(byDefinition, Set.of(), onlyText))
                .as("the integer overload is still unclassified").singleElement().asString()
                .startsWith("UNCLASSIFIED twin(p integer)");
        });
    }

    @Test
    void aClassifiedEntryWhoseFunctionIsGoneOrChangedItsResultIsReported() throws Exception {
        String probe = "census_probe_stale";
        withProbeSchema(probe, PgContainerHelper::plantCensusProbeKept, () -> {
            List<FnSig> byDefinition = functionsMentioningChunkText(pg, probe, "nexus");
            Map<String, Classified> classified = Map.of(
                "kept(p text)", new Classified("TABLE(chash bytea, n bigint)", Exposure.NO_TEXT, "pinned as a count"),
                "gone(p text)", new Classified("bigint", Exposure.NO_TEXT, "no such function"));
            assertThat(classificationProblems(byDefinition, Set.of(), classified))
                .anyMatch(p -> p.startsWith("RESULT CHANGED kept(p text)"))
                .anyMatch(p -> p.startsWith("STALE gone(p text)"))
                .hasSize(2);
        });
    }

    /**
     * Functions whose definition mentions the {@code chunk_text} column (or a chunk table) and that are not in the
     * result-shape set, keyed by signature, with the result type pinned, how much text the result can carry, and the
     * reason. An entry here is a claim a reviewer can read; the test below fails on a function that changed its
     * result, on an entry no function matches any more, and on an unlisted reader.
     */
    static final Map<String, Classified> CLASSIFIED_DEFINITION_READERS = new TreeMap<>();

    static {
        CLASSIFIED_DEFINITION_READERS.put("assign_from_chashes_1024(p_collection text, p_chashes text[], p_cross_collection boolean, p_embedding_model text, p_tenant text)",
            new Classified("TABLE(chash text, topic_id bigint, similarity double precision)", Exposure.NO_TEXT,
                "topic assignment preview: returns chash, topic id and similarity"));
        CLASSIFIED_DEFINITION_READERS.put("assign_from_chashes_384(p_collection text, p_chashes text[], p_cross_collection boolean, p_embedding_model text, p_tenant text)",
            new Classified("TABLE(chash text, topic_id bigint, similarity double precision)", Exposure.NO_TEXT,
                "topic assignment preview: returns chash, topic id and similarity"));
        CLASSIFIED_DEFINITION_READERS.put("assign_from_chashes_768(p_collection text, p_chashes text[], p_cross_collection boolean, p_embedding_model text, p_tenant text)",
            new Classified("TABLE(chash text, topic_id bigint, similarity double precision)", Exposure.NO_TEXT,
                "topic assignment preview: returns chash, topic id and similarity"));
        CLASSIFIED_DEFINITION_READERS.put("chash_conformance_report(dim integer)",
            new Classified("TABLE(table_name text, total bigint, non_conformant bigint, sample_chashes text[])", Exposure.NO_TEXT,
                "operator report: counts and a sample of chashes (hashes, not text)"));
        CLASSIFIED_DEFINITION_READERS.put("cross_preview_1024(p_collection text, p_chashes text[], p_embedding_model text, p_tenant text)",
            new Classified("TABLE(chash text, topic_id bigint, similarity double precision)", Exposure.NO_TEXT,
                "topic assignment preview: returns chash, topic id and similarity"));
        CLASSIFIED_DEFINITION_READERS.put("cross_preview_384(p_collection text, p_chashes text[], p_embedding_model text, p_tenant text)",
            new Classified("TABLE(chash text, topic_id bigint, similarity double precision)", Exposure.NO_TEXT,
                "topic assignment preview: returns chash, topic id and similarity"));
        CLASSIFIED_DEFINITION_READERS.put("cross_preview_768(p_collection text, p_chashes text[], p_embedding_model text, p_tenant text)",
            new Classified("TABLE(chash text, topic_id bigint, similarity double precision)", Exposure.NO_TEXT,
                "topic assignment preview: returns chash, topic id and similarity"));
        CLASSIFIED_DEFINITION_READERS.put("drop_tenant_partitions(p_tenant text)",
            new Classified("integer", Exposure.NO_TEXT,
                "partition maintenance: returns a count, a flag or nothing"));
        CLASSIFIED_DEFINITION_READERS.put("gc_expire_quarantine(dim integer, p_tenant_id text, p_quarantine_collection text, p_origin_collection text, p_cutoff text, p_floor_fraction double precision, p_floor_min_chunks integer, p_force boolean)",
            new Classified("TABLE(expired bigint, refused bigint)", Exposure.NO_TEXT,
                "moves or expires quarantine rows: returns counts and flags"));
        CLASSIFIED_DEFINITION_READERS.put("gc_expire_quarantine_bounded(dim integer, p_tenant_id text, p_quarantine_collection text, p_origin_collection text, p_cutoff text, p_floor_fraction double precision, p_floor_min_chunks integer, p_force boolean, p_row_limit integer, p_count_refused boolean)",
            new Classified("TABLE(expired bigint, refused bigint, more boolean, floor_refused boolean)", Exposure.NO_TEXT,
                "moves or expires quarantine rows: returns counts and flags"));
        CLASSIFIED_DEFINITION_READERS.put("gc_quarantine_orphans(dim integer, p_tenant_id text, p_collection text, p_quarantine_collection text, p_quarantined_at text, p_sample_limit integer)",
            new Classified("TABLE(moved bigint, sample jsonb)", Exposure.OWNERLESS_LABEL,
                "moves reapable(c) chunks, which have no manifest row in any owner state, to quarantine; the sample it returns labels each moved chunk by left(chunk_text, 80) as text_prefix. Those chunks have no owner, so none is sensitive (chunk_sensitive counts live owners); pinned by aGcSampleNamesOnlyChunksThatHaveNoOwner"));
        CLASSIFIED_DEFINITION_READERS.put("gc_quarantine_orphans_bounded(dim integer, p_tenant_id text, p_collection text, p_quarantine_collection text, p_quarantined_at text, p_sample_limit integer, p_row_limit integer)",
            new Classified("TABLE(moved bigint, sample jsonb, remaining bigint)", Exposure.OWNERLESS_LABEL,
                "moves reapable(c) chunks, which have no manifest row in any owner state, to quarantine; the sample it returns labels each moved chunk by left(chunk_text, 80) as text_prefix. Those chunks have no owner, so none is sensitive (chunk_sensitive counts live owners); pinned by aGcSampleNamesOnlyChunksThatHaveNoOwner"));
        CLASSIFIED_DEFINITION_READERS.put("gc_quarantine_orphans_floored(dim integer, p_tenant_id text, p_collection text, p_quarantine_collection text, p_quarantined_at text, p_sample_limit integer, p_row_limit integer, p_floor_fraction double precision, p_floor_min_chunks integer, p_force boolean, p_quarantine_expires_at text DEFAULT NULL::text)",
            new Classified("TABLE(moved bigint, sample jsonb, remaining bigint, refused boolean, reapable_count bigint, total_count bigint)", Exposure.OWNERLESS_LABEL,
                "moves reapable(c) chunks, which have no manifest row in any owner state, to quarantine; the sample it returns labels each moved chunk by left(chunk_text, 80) as text_prefix. Those chunks have no owner, so none is sensitive (chunk_sensitive counts live owners); pinned by aGcSampleNamesOnlyChunksThatHaveNoOwner; the floored form only calls the unbounded or the bounded form"));
        CLASSIFIED_DEFINITION_READERS.put("gc_restore_rereferenced(dim integer, p_tenant_id text, p_quarantine_collection text, p_origin_collection text)",
            new Classified("bigint", Exposure.NO_TEXT,
                "moves or expires quarantine rows: returns counts and flags"));
        CLASSIFIED_DEFINITION_READERS.put("gc_restore_rereferenced_bounded(dim integer, p_tenant_id text, p_quarantine_collection text, p_origin_collection text, p_row_limit integer)",
            new Classified("TABLE(restored bigint, remaining bigint)", Exposure.NO_TEXT,
                "moves or expires quarantine rows: returns counts and flags"));
        CLASSIFIED_DEFINITION_READERS.put("manifest_qualifying_hits(p_collections text[], p_where jsonb, p_where_path text, p_manifest_where jsonb, p_manifest_path text, p_cap integer, p_embedding_model text, p_tenant text, p_query_text text, p_exclude_sensitive boolean DEFAULT false)",
            new Classified("TABLE(hit integer, chunk_collection text, chunk_chash bytea)", Exposure.NO_TEXT,
                "text gate over manifest chunks: returns hit, collection and chash key, no text"));
        CLASSIFIED_DEFINITION_READERS.put("manifest_verify(p_doc_id text)",
            new Classified("TABLE(referenced bigint, present bigint, missing bigint)", Exposure.NO_TEXT,
                "manifest integrity check: counts"));
        CLASSIFIED_DEFINITION_READERS.put("partition_ensure_leaf(p_parent regclass, p_model_part regclass, p_tenant text, p_force boolean)",
            new Classified("boolean", Exposure.NO_TEXT,
                "partition maintenance: returns a count, a flag or nothing"));
        CLASSIFIED_DEFINITION_READERS.put("partition_parent_check(p_parent regclass)",
            new Classified("void", Exposure.NO_TEXT,
                "partition maintenance: returns a count, a flag or nothing"));
        CLASSIFIED_DEFINITION_READERS.put("purge_trash(older_than interval)",
            new Classified("bigint", Exposure.NO_TEXT,
                "purges tombstoned documents past the window: returns a count"));
        CLASSIFIED_DEFINITION_READERS.put("quarantine_reattach_plan(p_tenant_id text, p_origin_collection text, p_quarantine_collection text, p_chashes text[], p_from_quarantine boolean)",
            new Classified("jsonb", Exposure.NO_TEXT,
                "read-only classifier: jsonb of chash, verdict, owner tumbler and title, position, chunk title, line and char offsets; no chunk text"));
        CLASSIFIED_DEFINITION_READERS.put("quarantine_restore_chunks(p_tenant_id text, p_origin_collection text, p_quarantine_collection text, p_chashes text[], p_actor text, p_source_audit_id bigint, p_dry_run boolean, p_reattach boolean)",
            new Classified("TABLE(r_chash text, r_outcome text, r_audit_id bigint, r_no_manifest boolean, r_reapable_after timestamp with time zone, r_reattach text, r_attached boolean, r_owner text, r_owner_title text, r_position integer, r_chunk_title text, r_reason text, r_owner_rows integer, r_owner_chunks integer)", Exposure.NO_TEXT,
                "moves quarantine rows back: verdict rows of chash, outcome, owner id and titles, counts; no chunk text"));
        CLASSIFIED_DEFINITION_READERS.put("reaper_expire_client_quarantine(p_tenant_id text, p_quarantine_collection text, p_origin_collection text, p_cutoff text, p_row_limit integer)",
            new Classified("TABLE(expired bigint, protected_count bigint)", Exposure.NO_TEXT,
                "moves or expires quarantine rows: returns counts and flags"));
        CLASSIFIED_DEFINITION_READERS.put("reaper_expire_quarantine(p_tenant_id text, p_quarantine_collection text, p_origin_collection text, p_cutoff text, p_row_limit integer)",
            new Classified("TABLE(expired bigint, protected_count bigint)", Exposure.NO_TEXT,
                "moves or expires quarantine rows: returns counts and flags"));
        CLASSIFIED_DEFINITION_READERS.put("reaper_quarantine_chunks(p_tenant_id text, p_collection text, p_quarantine_collection text, p_quarantined_at text, p_row_limit integer, p_grace interval, p_floor_fraction double precision, p_floor_min_chunks integer, p_dry_run boolean)",
            new Classified("TABLE(moved bigint, reapable_count bigint, total_count bigint, refused boolean, remaining bigint)", Exposure.NO_TEXT,
                "moves reapable chunks to quarantine: returns counts and a refusal flag"));
        CLASSIFIED_DEFINITION_READERS.put("service_tokens_create_tenant_partitions()",
            new Classified("trigger", Exposure.NO_TEXT,
                "partition maintenance: returns a count, a flag or nothing"));
        CLASSIFIED_DEFINITION_READERS.put("stamp_chunks_on_manifest_delete()",
            new Classified("trigger", Exposure.NO_TEXT,
                "trigger function that stamps chunk_orphaned_at; no result row"));
        CLASSIFIED_DEFINITION_READERS.put("stamp_chunks_on_manifest_update()",
            new Classified("trigger", Exposure.NO_TEXT,
                "trigger function that stamps chunk_orphaned_at; no result row"));
        CLASSIFIED_DEFINITION_READERS.put("taxonomy_unassigned_chashes_1024(p_collection text, p_limit integer, p_after text)",
            new Classified("TABLE(has_taxonomy boolean, chashes text[], next_after text)", Exposure.NO_TEXT,
                "chashes of chunks without a topic assignment: hashes, not text"));
        CLASSIFIED_DEFINITION_READERS.put("taxonomy_unassigned_chashes_384(p_collection text, p_limit integer, p_after text)",
            new Classified("TABLE(has_taxonomy boolean, chashes text[], next_after text)", Exposure.NO_TEXT,
                "chashes of chunks without a topic assignment: hashes, not text"));
        CLASSIFIED_DEFINITION_READERS.put("taxonomy_unassigned_chashes_768(p_collection text, p_limit integer, p_after text)",
            new Classified("TABLE(has_taxonomy boolean, chashes text[], next_after text)", Exposure.NO_TEXT,
                "chashes of chunks without a topic assignment: hashes, not text"));
        CLASSIFIED_DEFINITION_READERS.put("text_gate_probe_1024(p_text text, p_collections text[], p_where jsonb, p_where_path text, p_cap integer, p_embedding_model text, p_tenant text)",
            new Classified("SETOF bytea", Exposure.NO_TEXT,
                "text gate; SETOF bytea, the chashes whose text matches, no text. Its count only picks the plan: the chashes are ranked by text_gated_search_by_chash_<dim>, which carries p_exclude_sensitive"));
        CLASSIFIED_DEFINITION_READERS.put("text_gate_probe_384(p_text text, p_collections text[], p_where jsonb, p_where_path text, p_cap integer, p_embedding_model text, p_tenant text)",
            new Classified("SETOF bytea", Exposure.NO_TEXT,
                "text gate; SETOF bytea, the chashes whose text matches, no text. Its count only picks the plan: the chashes are ranked by text_gated_search_by_chash_<dim>, which carries p_exclude_sensitive"));
        CLASSIFIED_DEFINITION_READERS.put("text_gate_probe_768(p_text text, p_collections text[], p_where jsonb, p_where_path text, p_cap integer, p_embedding_model text, p_tenant text)",
            new Classified("SETOF bytea", Exposure.NO_TEXT,
                "text gate; SETOF bytea, the chashes whose text matches, no text. Its count only picks the plan: the chashes are ranked by text_gated_search_by_chash_<dim>, which carries p_exclude_sensitive"));
    }

    @Test
    void everyNexusFunctionThatMentionsChunkTextIsInTheShapeSetOrClassified() throws Exception {
        List<FnSig> byDefinition = functionsMentioningChunkText(pg);
        assertThat(names(byDefinition)).as("non-vacuity: the definition scan sees the search families")
            .containsAll(textFunctionNames);
        assertThat(classificationProblems(byDefinition, keys(textFunctions), CLASSIFIED_DEFINITION_READERS))
            .as("nexus functions whose definition reads chunk text or a chunk table, returned under a column name or "
                + "type the shape matcher does not know: add the shape to it, or classify the function with its "
                + "result pinned")
            .isEmpty();
        assertThat(CLASSIFIED_DEFINITION_READERS).as("non-vacuity: the classification is not empty").isNotEmpty();
    }

    @Test
    void aFunctionClassifiedAsReturningNoTextHasNoTextShapedResult_andTheLabelOnesAreTheGcSamples() {
        int noText = 0;
        int labels = 0;
        for (var e : CLASSIFIED_DEFINITION_READERS.entrySet()) {
            Classified c = e.getValue();
            if (c.exposure() == Exposure.NO_TEXT) {
                noText++;
                assertThat(resultCarriesChunkText(c.result())).as("%s: a content or chunk_text column", e.getKey()).isFalse();
                assertThat(c.result()).as("%s: a whole chunk row", e.getKey()).doesNotContainIgnoringCase("nexus.chunks");
            } else {
                labels++;
                assertThat(e.getKey()).as("only the gc orphan sweeps hand back a label").startsWith("gc_quarantine_orphans");
                assertThat(c.result()).contains("sample jsonb");
            }
            assertThat(c.reason()).as("%s has a reason", e.getKey()).isNotBlank();
        }
        assertThat(noText).as("non-vacuity").isGreaterThan(20);
        assertThat(labels).as("non-vacuity: the three gc orphan sweeps").isEqualTo(3);
    }

    @Test
    void everyNexusFunctionThatReturnsChunkTextEndsInTheExcludeArgument() {
        assertThat(textFunctionNames)
            .as("non-vacuity: the 10 search families x 3 dimensions return chunk text")
            .hasSizeGreaterThanOrEqualTo(27);
        List<String> missing = new ArrayList<>();
        for (FnSig f : textFunctions) {
            if (NO_ROUTE_FUNCTIONS.containsKey(f.name())) continue;
            if (!f.args().endsWith("p_exclude_sensitive boolean DEFAULT false")) {
                missing.add(f.key());
            }
        }
        assertThat(missing)
            .as("a nexus function returning chunk text without the trailing p_exclude_sensitive argument")
            .isEmpty();
    }

    /**
     * SQL functions that return chunk text and that no engine route calls: a client with a database login can call
     * them, the HTTP service never does. The test below pins that premise (no engine source names the function), so
     * wiring one into a route removes the exemption's ground and the route census then judges that route.
     */
    static final Map<String, String> NO_ROUTE_FUNCTIONS = Map.of(
        "document_text", "ordered manifest-chunk join of one document (catalog-004); no engine caller");

    @Test
    void theFunctionsExemptedFromTheArgumentAreNamedByNoEngineSource() throws Exception {
        for (String fn : NO_ROUTE_FUNCTIONS.keySet()) {
            assertThat(textFunctionNames).as("the exemption names a function that still returns chunk text")
                .contains(fn);
            try (var files = Files.walk(MAIN)) {
                for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                    String src = KeyedRoutesCensusTest.blank(Files.readString(p));
                    assertThat(src).as("%s must not call %s", p, fn)
                        .doesNotContain(fn.toUpperCase(java.util.Locale.ROOT) + ".call");
                }
            }
        }
    }

    /**
     * {@code PgVectorRepository} methods that read chunk text (so they are sinks of the census above) but that no
     * route reaches and no main source calls, with the reason. The route census only judges a sink a route reaches,
     * so a method like this one is invisible to it: it takes no {@code excludeSensitive}, and the day a route or any
     * caller wires it in, it would serve sensitive text. The test below pins the premise (still a sink, reached by no
     * route, named by no main source) so that wiring it in fails here and the author must gate it first (review fix F4,
     * the way {@link #NO_ROUTE_FUNCTIONS} pins {@code document_text}).
     */
    static final Map<String, String> NO_CALLER_SINKS = Map.of(
        "fetchDocumentChunks", "a document's chunk text in manifest order (RDR-108); public, no flag, no main caller "
            + "(only tests call it)");

    @Test
    void theSinksExemptedForHavingNoCallerAreStillSinksReachedByNoRouteAndCalledByNoMainSource() throws Exception {
        String repoSrc = Files.readString(VECTOR_REPO);
        Set<String> sinks = ChunkTextRouteCensus.allSinks(repoSrc, textFunctionNames);
        Set<String> reachedByRoutes = new TreeSet<>();
        for (var f : findings()) {
            reachedByRoutes.addAll(f.sinkMethods());
            reachedByRoutes.addAll(f.exemptSinksReached());
        }
        assertThat(NO_CALLER_SINKS).as("non-vacuity").isNotEmpty();
        int files = 0;
        for (String method : NO_CALLER_SINKS.keySet()) {
            assertThat(sinks).as("%s still reads chunk text: the exemption has ground", method).contains(method);
            assertThat(reachedByRoutes).as("%s is now reached by a route: gate it with excludeSensitive and "
                + "remove the exemption", method).doesNotContain(method);
            // a call, or a method reference (this::m, Repo::m); comments and string literals are blanked first
            Pattern use = Pattern.compile("\\b" + method + "\\s*\\(|::\\s*" + method + "\\b");
            try (var walk = Files.walk(MAIN)) {
                for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                    files++;
                    int uses = (int) use.matcher(KeyedRoutesCensusTest.blank(Files.readString(p))).results().count();
                    // the declaration is the one occurrence in the repository itself
                    int allowed = p.equals(VECTOR_REPO) ? 1 : 0;
                    assertThat(uses).as("%s: main source %s uses %s, which serves chunk text with no sensitivity "
                        + "flag", method, p, method).isEqualTo(allowed);
                }
            }
        }
        assertThat(files).as("non-vacuity: the walk read the engine sources").isGreaterThan(100);
    }

    /** The by-address catalog routes that return chunk text: gated like the vector reads, checked by name below. */
    static final String GATED = "GATED";

    /**
     * Methods outside PgVectorRepository that read chunk text, and why each is not an ungated read route. {@link
     * #GATED} marks the two catalog by-address lookups (resolve_span, resolve_chash): they return chunk text, take
     * {@code sensitivity} and carry the flag, which the test below checks in the method and in its handler.
     */
    static final Map<String, String> OTHER_TEXT_READERS = new TreeMap<>(Map.of(
        "CatalogRepository.java#resolveSpan", GATED,
        "CatalogRepository.java#resolveChash", GATED,
        "CatalogRepository.java#upsertManifestChunkVectors", "write: sets chunk_text on the manifest upsert",
        "ChunkOwnerLabel.java#textPrefix", "left(chunk_text, 80) label of chunks reapable(c) selects (no live owner, "
            + "so never sensitive: chunk_sensitive counts live owners only) and of refusal audit rows",
        "ReaperRepository.java#describe", "80-char labels of the chunks a gc sweep selected or refused to move: "
            + "reapable(c) chunks, which have no live owner, in an operator audit row",
        "VectorHandler.java#handleReapable", "serves PgVectorRepository#reapableChunks, an exempt sink above",
        "CombinedWriteService.java#selectExistingText", "write: compares stored text, returns nothing",
        "PipelineRepository.java#writeChunks", "write: pdf_chunks, a different table",
        "ReembedSourceReader.java#chunks", "internal re-embed job input; the job route returns state and counts",
        "ReembedSourceReader.java#planCounts", "octet_length of the text: byte counts"));

    static {
        // found when the census began reading string literals and field initialisers (nexus-rfvyy.7.17)
        OTHER_TEXT_READERS.put("CatalogRepository.java#chunkRow", "map builder resolveSpan (GATED above) calls with "
            + "the values it read; it reads nothing itself");
        OTHER_TEXT_READERS.put("DimTables.java#<type ChunkTable>", "the column handle of the chunk tables "
            + "(t.field(\"chunk_text\")): a reference, not a read; every query that selects it is a method this "
            + "census judges");
        // found when the census began reading whole-row reads (selectFrom of a table that has the column), r1 F4
        OTHER_TEXT_READERS.put("PipelineHandler.java#handleChunks", "POST: request validation of the chunk_text key of "
            + "an uploaded pdf chunk, input to a write of pdf_chunks. GET: serves PipelineRepository#readChunks, "
            + "classified on its own entry");
        OTHER_TEXT_READERS.put("PipelineRepository.java#readChunks", "selectFrom(PDF_CHUNKS): the staging rows of an "
            + "indexing run (nexus.pdf_chunks), served by GET /v1/pipeline/chunks to the indexing client that wrote "
            + "them, to resume the upload of the same run. A row is keyed by (tenant, pipeline run, chunk index), not "
            + "by catalog document: it has no tumbler, so no live owner and no document sensitivity flag to filter "
            + "on, and RDR-243's census lists no pipeline staging read. It is not a corpus read: no search, get or "
            + "resolve route reaches it, and a sensitive document's text reaches pdf_chunks only through its own "
            + "indexing run");
        OTHER_TEXT_READERS.put("StoreGetProjection.java#selectFields", "the select list of PgVectorRepository#get, "
            + "named there as a column handle (ch.chunkText()) so a metadata-only store-get can leave it out. It "
            + "reads nothing itself; the statement that runs it is PgVectorRepository#get's, whose WHERE applies "
            + "notSensitiveCondition under sensitivity=exclude and whose envelope is then flagged by withSensitives "
            + "(GATED above, nexus-rfvyy.7.4), exactly as before the projection");
    }

    // ── route table ───────────────────────────────────────────────────────────

    private List<ChunkTextRouteCensus.Finding> findings() throws Exception {
        return findings(textFunctionNames);
    }

    /** The census over the real sources; the behaviour test uses it to check it holds a recipe per derived route. */
    static List<ChunkTextRouteCensus.Finding> findings(Set<String> textFunctions) throws Exception {
        return ChunkTextRouteCensus.census(Files.readString(VECTOR_HANDLER), Files.readString(VECTOR_REPO),
                                           textFunctions, EXEMPT_SINKS.keySet());
    }

    /** A repository method that touches the text without serving it, and the one route that may reach it. */
    record Exempt(String route, String reason) { }

    /**
     * Repository methods that touch {@code chunk_text} without returning a chunk a live owner could have marked
     * sensitive. They are not chunk-text reads in the sense of this test; each is checked below to still be a sink
     * and to be reached by exactly the route named, so the entry cannot rot into a hole.
     */
    static final Map<String, Exempt> EXEMPT_SINKS = Map.of(
        "upsertChunksInternal", new Exempt("/upsert-chunks", "sets chunk_text on insert and conflict; returns ids"),
        "selectExistingChashTextCtx", new Exempt("/upsert-chunks",
            "reads stored text only to decide whether an id needs re-embedding"),
        "reapableChunks", new Exempt("/reapable", "lists the chunks reapable(c) selects, which have no live owner "
            + "and so are never sensitive (chunk_sensitive counts live owners only); text is an 80-char label"));

    /** The routes the census derives as chunk-text routes, for the integration test's coverage check. */
    static Set<String> derivedTextRoutes(List<ChunkTextRouteCensus.Finding> findings) {
        Set<String> out = new TreeSet<>();
        for (var f : findings) if (f.readsChunkText()) out.add("/v1/vectors" + f.route());
        return out;
    }

    @Test
    void everyDerivedChunkTextRouteParsesSensitivityAndFiltersInTheMethodThatReadsTheText() throws Exception {
        var findings = findings();
        Set<String> routes = derivedTextRoutes(findings);
        // non-vacuity: the eight searches (query is an alias of search), get and store-get
        assertThat(routes).as("derived chunk-text routes").contains(
            "/v1/vectors/search", "/v1/vectors/query", "/v1/vectors/search-per-collection",
            "/v1/vectors/hybrid-search", "/v1/vectors/search-metadata-scoped", "/v1/vectors/search-topic-scoped",
            "/v1/vectors/search-graph-hop", "/v1/vectors/search-aspect-scoped",
            "/v1/vectors/get", "/v1/vectors/store-get");
        Map<String, String> nonConforming = new LinkedHashMap<>();
        for (var f : findings) {
            if (f.readsChunkText() && !f.conforms()) {
                nonConforming.put(f.route(), "handler " + f.handler() + " passes the parsed flag on every call="
                    + f.handlerParsesSensitivity() + " (calls without it: " + f.handlerCallsWithoutFlag()
                    + "); reads chunk text in " + f.sinkMethods()
                    + " without " + ChunkTextRouteCensus.FLAG + " gating the read in " + f.sinkMethodsWithoutFlag());
            }
        }
        assertThat(nonConforming).as("chunk-text routes that do not accept sensitivity and filter").isEmpty();
    }

    @Test
    void theExemptSinksAreStillSinksAndEachIsReachedByExactlyItsNamedRoute() throws Exception {
        Set<String> sinks = ChunkTextRouteCensus.allSinks(Files.readString(VECTOR_REPO), textFunctionNames);
        assertThat(sinks).containsAll(EXEMPT_SINKS.keySet());
        Map<String, Set<String>> routesBySink = new TreeMap<>();
        for (var f : findings()) {
            for (String s : f.exemptSinksReached()) routesBySink.computeIfAbsent(s, k -> new TreeSet<>()).add(f.route());
        }
        for (var e : EXEMPT_SINKS.entrySet()) {
            assertThat(routesBySink.getOrDefault(e.getKey(), Set.of()))
                .as("routes reaching the exempt sink %s", e.getKey()).containsExactly(e.getValue().route());
        }
    }

    @Test
    void theTwoCatalogByAddressReadsAreGatedInTheMethodAndTheirHandler() throws Exception {
        String repoSrc = Files.readString(MAIN.resolve("db/CatalogRepository.java"));
        String handlerSrc = Files.readString(MAIN.resolve("http/CatalogHandler.java"));
        int gated = 0;
        for (var e : OTHER_TEXT_READERS.entrySet()) {
            if (!e.getValue().equals(GATED)) continue;
            gated++;
            String name = e.getKey().substring(e.getKey().indexOf('#') + 1);
            String handlerName = "handle" + Character.toUpperCase(name.charAt(0)) + name.substring(1);
            var repoTexts = ChunkTextRouteCensus.memberTexts(repoSrc, name);
            assertThat(repoTexts).as("%s exists", name).isNotEmpty();
            assertThat(repoTexts.stream().anyMatch(
                    t -> ChunkTextRouteCensus.flagGatesTheRead(t, Set.of(), Set.of(), name)))
                .as("%s puts %s into the gate of the read, not just into a log line", name, ChunkTextRouteCensus.FLAG)
                .isTrue();
            var handlerTexts = ChunkTextRouteCensus.memberTexts(handlerSrc, handlerName);
            assertThat(handlerTexts).as("%s exists", handlerName).isNotEmpty();
            assertThat(handlerTexts.stream().allMatch(
                    t -> ChunkTextRouteCensus.everyRepoCallCarriesTheParsedFlag(t, name, "excludeFlag")))
                .as("%s passes the parsed sensitivity to repo.%s", handlerName, name).isTrue();
        }
        assertThat(gated).isEqualTo(2);
    }

    @Test
    void everyOtherMethodThatReadsChunkTextIsGatedOrClassified() throws Exception {
        Set<String> found = new TreeSet<>();
        int files = 0;
        try (var walk = Files.walk(MAIN)) {
            for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                if (p.equals(VECTOR_REPO)) continue;
                files++;
                found.addAll(ChunkTextRouteCensus.textReaders(p.getFileName().toString(),
                                                              Files.readString(p), textFunctionNames));
            }
        }
        assertThat(files).as("non-vacuity: the sink census read the engine sources").isGreaterThan(100);
        assertThat(found).as("methods outside PgVectorRepository that read chunk text: classify each in "
            + "OTHER_TEXT_READERS or gate it").containsExactlyInAnyOrderElementsOf(OTHER_TEXT_READERS.keySet());
    }

    @Test
    void theRoutesThatReadNoChunkTextAreTheOnesThatReturnNone() throws Exception {
        // the complement is derived too; this pins the two read routes that return metadata only, so a change that
        // makes one of them return text moves it into the set above instead of past it
        Set<String> none = new TreeSet<>();
        for (var f : findings()) if (!f.readsChunkText()) none.add(f.route());
        assertThat(none).contains("/get-all-metadata", "/store-list", "/count", "/collections");
    }

    // ── the census must catch what it exists to catch ─────────────────────────

    private static final String HANDLER = """
        final class H {
            public void handle(HttpExchange ex) {
                switch (op) {
                    case "/search" -> handleSearch(ex, m);
                    case "/brand-new" -> handleBrandNew(ex, m);
                }
            }
            private void handleSearch(HttpExchange ex, String m) {
                boolean excludeSensitive = optExcludeSensitive(body);
                repo.plain(a, excludeSensitive);
            }
            private void handleBrandNew(HttpExchange ex, String m) {
                repo.fresh(a);
            }
        }
        """;

    private static final String REPO = """
        final class R {
            List plain(String a, boolean excludeSensitive) { return PLAIN_SEARCH_384.call(a, excludeSensitive); }
            List fresh(String a) { return ctx.select(ch.chash(), ch.chunkText()).from(ch.table()).fetch(); }
        }
        """;

    private static ChunkTextRouteCensus.Finding route(List<ChunkTextRouteCensus.Finding> findings, String route) {
        return findings.stream().filter(f -> f.route().equals(route)).findFirst().orElseThrow();
    }

    @Test
    void aNewChunkTextRouteWithoutTheParameterFailsTheCensus() {
        var findings = ChunkTextRouteCensus.census(HANDLER, REPO, Set.of("plain_search_384"), Set.of());
        var search = route(findings, "/search");
        var fresh = route(findings, "/brand-new");
        assertThat(search.readsChunkText()).isTrue();
        assertThat(search.conforms()).as("the search route has the parameter at both ends").isTrue();
        assertThat(fresh.readsChunkText()).as("a jOOQ read of chunk_text is a chunk-text route").isTrue();
        assertThat(fresh.conforms()).as("and without the parameter it does not conform").isFalse();
        assertThat(fresh.handlerParsesSensitivity()).isFalse();
        assertThat(fresh.sinkMethodsWithoutFlag()).containsExactly("fresh");
    }

    @Test
    void aHandlerThatDropsTheParseOrARepositoryThatDropsTheFlagFailsTheCensus() {
        var noParse = ChunkTextRouteCensus.census(
            HANDLER.replace("optExcludeSensitive(body)", "false"), REPO, Set.of("plain_search_384"), Set.of());
        assertThat(route(noParse, "/search").conforms())
            .as("the route no longer parses sensitivity").isFalse();
        var noFlag = ChunkTextRouteCensus.census(
            HANDLER, REPO.replace(", boolean excludeSensitive", "").replace(", excludeSensitive", ""),
            Set.of("plain_search_384"), Set.of());
        assertThat(route(noFlag, "/search").conforms())
            .as("the method that reads the text no longer carries the flag").isFalse();
    }

    @Test
    void aReceiverOtherThanRepoIsNotFollowed() {
        String handler = """
            final class H {
                public void handle(HttpExchange ex) { switch (op) { case "/count" -> handleCount(ex, m); } }
                private void handleCount(HttpExchange ex, String m) { var x = body.get("a"); repo.count(a); }
            }
            """;
        String repo = """
            final class R {
                long count(String a) { return 1; }
                Map get(String a) { return ctx.select(ch.chunkText()).fetch(); }
            }
            """;
        var f = ChunkTextRouteCensus.census(handler, repo, Set.of(), Set.of()).get(0);
        assertThat(f.readsChunkText()).as("body.get(...) is not repo.get(...)").isFalse();
    }

    // ── nexus-rfvyy.7.17: literals are read, and the flag must be USED ─────────

    private static ChunkTextRouteCensus.Finding freshOf(String freshBody, Set<String> textFunctions) {
        String repo = "final class R {\n    List fresh(String a) { " + freshBody + " }\n}\n";
        return route(ChunkTextRouteCensus.census(HANDLER, repo, textFunctions, Set.of()), "/brand-new");
    }

    @Test
    void aRawSqlReadOfChunkTextInsideAStringLiteralIsDetected() {
        assertThat(freshOf("return ctx.fetch(\"select chash, chunk_text from nexus.chunks\");", Set.of())
            .readsChunkText()).as("select chunk_text in a string literal").isTrue();
        assertThat(freshOf("return ctx.fetch(\"\"\"\n            select left(chunk_text, 80)\n"
            + "            from nexus.chunks\n            \"\"\");", Set.of())
            .readsChunkText()).as("select chunk_text in a text block").isTrue();
        assertThat(freshOf("return ctx.fetch(\"select * from nexus.plain_search_384(?, ?)\");",
                           Set.of("plain_search_384")).readsChunkText())
            .as("a raw-SQL call of a function the pg_proc derivation found").isTrue();
    }

    @Test
    void aLiteralThatMerelyResemblesChunkTextIsNotARead() {
        assertThat(freshOf("return ctx.fetch(\"select chunk_text_hash from nexus.chunks\");", Set.of())
            .readsChunkText()).as("chunk_text_hash is a different column").isFalse();
        assertThat(freshOf("// select chunk_text from nexus.chunks\n return ctx.fetch(\"select 1\");", Set.of())
            .readsChunkText()).as("a comment is not a read").isFalse();
        assertThat(freshOf("return ctx.fetch(\"select 1\");", Set.of("plain_search_384"))
            .readsChunkText()).isFalse();
    }

    @Test
    void aRawSqlConstantInAFieldInitialiserFailsTheCensusRatherThanBeingMissed() {
        String repo = """
            final class R {
                private static final String SQL = "select chunk_text from nexus.chunks";
                List fresh(String a) { return ctx.fetch(SQL); }
            }
            """;
        assertThatThrownBy(() -> ChunkTextRouteCensus.census(HANDLER, repo, Set.of(), Set.of()))
            .isInstanceOf(AssertionError.class).hasMessageContaining("field initialiser");
    }

    @Test
    void anAnnotatedFieldInitialiserIsSeenToo() {
        for (String annotation : new String[] {
            "@SuppressWarnings(\"x\")", "@SuppressWarnings({\"a\", \"b\"})", "@Deprecated(since = \"1\", forRemoval = false)",
            "@SuppressWarnings(\"x\") @Deprecated"}) {
            String repo = "final class R {\n    " + annotation + " private static final String SQL = "
                + "\"select chunk_text from nexus.chunks\";\n    List fresh(String a) { return ctx.fetch(SQL); }\n}\n";
            assertThatThrownBy(() -> ChunkTextRouteCensus.census(HANDLER, repo, Set.of(), Set.of()))
                .as(annotation).isInstanceOf(AssertionError.class).hasMessageContaining("field initialiser");
        }
        String plain = "final class R {\n    @SuppressWarnings(\"x\") private static final String SQL = \"select 1\";\n"
            + "    List fresh(String a) { return ctx.fetch(SQL); }\n}\n";
        assertThat(ChunkTextRouteCensus.membersOf(plain).stream()
            .filter(m -> m.name().equals(ChunkTextRouteCensus.FIELD)).count())
            .as("the annotated field is a member of its own").isEqualTo(1);
    }

    @Test
    void aWholeRowReadOfATableThatHasTheColumnIsAChunkTextRead() {
        assertThat(ChunkTextRouteCensus.TEXT_TABLES).as("derived from the generated tables, not typed")
            .contains("CHUNKS", "PDF_CHUNKS");
        for (String body : new String[] {
            "return ctx.selectFrom(PDF_CHUNKS).fetch();",
            "return ctx.select().from(CHUNKS).fetch();",
            "return ctx.select(CHUNKS.asterisk()).from(CHUNKS).fetch();",
            "return ctx.select(CHUNKS.fields()).from(CHUNKS).fetch();",
            "return ctx.fetch(CHUNKS, CHUNKS.COLLECTION.eq(a));"}) {
            assertThat(freshOf(body, Set.of()).readsChunkText()).as(body).isTrue();
        }
        assertThat(freshOf("return ctx.selectFrom(CATALOG_DOCUMENTS).fetch();", Set.of()).readsChunkText())
            .as("a table with no chunk_text column is not a text read").isFalse();
    }

    @Test
    void aWholeRowReadThroughAChunkHandleAQualifiedConstantOrRawSqlIsAChunkTextRead() {
        // nexus-rfvyy.7.17 r2: the handle forms PgVectorRepository uses (ch.table()), Tables. qualification,
        // an aliased table, fetchInto, and raw select * / x.* over a chunk relation or one of its children.
        for (String body : new String[] {
            "DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim); return ctx.selectFrom(ch.table()).fetch();",
            "var ch = DimTables.CHUNKS.get(dim); return ctx.select().from(ch.table()).fetch();",
            "DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim); return ctx.select(ch.table().asterisk()).from(ch.table()).fetch();",
            "DimTables.ChunkTable t = DimTables.CHUNKS.get(dim); return ctx.select(t.table().as(\"c\").fields()).fetch();",
            "return ctx.selectFrom(DimTables.CHUNKS.get(dim).table()).fetch();",
            "return ctx.selectFrom(Tables.CHUNKS).fetch();",
            "return ctx.select(Tables.PDF_CHUNKS.asterisk()).from(Tables.PDF_CHUNKS).fetch();",
            "return ctx.select(CHUNKS.as(\"c\").asterisk()).fetch();",
            "return ctx.fetchInto(CHUNKS, Row.class);",
            "return ctx.fetch(\"select * from nexus.chunks where tenant_id = ?\", t);",
            "return ctx.fetch(\"SELECT c.* FROM nexus.chunks_mad318272_t_af8b38277810aea6 c WHERE c.tenant_id = ?\", t);"}) {
            assertThat(freshOf(body, Set.of()).readsChunkText()).as(body).isTrue();
        }
        for (String body : new String[] {
            "CollectionScopedTable x = scoped(); return ctx.selectFrom(x.table()).fetch();",
            "DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim); return ctx.select(ch.collection()).from(ch.table()).fetch();",
            "return ctx.fetch(\"select * from nexus.catalog_documents where tenant_id = ?\", t);",
            "return ctx.selectFrom(Tables.CATALOG_DOCUMENTS).fetch();"}) {
            assertThat(freshOf(body, Set.of()).readsChunkText()).as("negative control: " + body).isFalse();
        }
    }

    @Test
    void aNamedColumnSelectIsNotAWholeRowRead() {
        assertThat(freshOf("return ctx.selectFrom(CATALOG_DOCUMENTS).fetch();", Set.of()).readsChunkText())
            .as("a table with no chunk_text column is not a text read").isFalse();
        assertThat(freshOf("return ctx.select(CHUNKS.COLLECTION).from(CHUNKS).fetch();", Set.of()).readsChunkText())
            .as("a named column other than chunk_text is not a text read").isFalse();
    }

    @Test
    void aParameterThatIsDeclaredButNeverUsedIsNotTheFlag() {
        var unused = ChunkTextRouteCensus.census(HANDLER,
            REPO.replace("PLAIN_SEARCH_384.call(a, excludeSensitive)", "PLAIN_SEARCH_384.call(a)"),
            Set.of("plain_search_384"), Set.of());
        assertThat(route(unused, "/search").conforms())
            .as("the method that reads the text declares the flag and never uses it").isFalse();
        var viaPlan = ChunkTextRouteCensus.census(HANDLER,
            REPO.replace("PLAIN_SEARCH_384.call(a, excludeSensitive)", "PLAIN_SEARCH_384.call(a, wherePlan.excludeSensitive())")
                .replace(", boolean excludeSensitive", ""),
            Set.of("plain_search_384"), Set.of());
        assertThat(route(viaPlan, "/search").conforms())
            .as("wherePlan.excludeSensitive() inside the call is a use").isTrue();
    }

    // ── nexus-rfvyy.7.17 r1 S2: the flag must sit inside the gate, not merely be read ─────

    private static ChunkTextRouteCensus.Finding searchWith(String plainBody) {
        String repo = "final class R {\n    List plain(String a, boolean excludeSensitive) { " + plainBody + " }\n}\n";
        return route(ChunkTextRouteCensus.census(HANDLER, repo, Set.of("plain_search_384"), Set.of()), "/search");
    }

    @Test
    void aMethodThatOnlyLogsOrComparesTheFlagIsNotGatedByIt() {
        assertThat(searchWith("return PLAIN_SEARCH_384.call(a, excludeSensitive);").conforms())
            .as("control: the flag is an argument of the text-function call").isTrue();
        assertThat(searchWith("log.debug(\"exclude={}\", excludeSensitive); return PLAIN_SEARCH_384.call(a, false);")
            .conforms()).as("the flag is read by a log line only").isFalse();
        assertThat(searchWith("if (excludeSensitive) { audit(a); } return PLAIN_SEARCH_384.call(a, false);")
            .conforms()).as("the flag is compared and never reaches the read").isFalse();
        assertThat(searchWith("return decorate(excludeSensitive, PLAIN_SEARCH_384.call(a, false));").conforms())
            .as("the flag is an argument of a call that is not the text function's or a sink's").isFalse();
        assertThat(searchWith("boolean again = excludeSensitive; return PLAIN_SEARCH_384.call(a, again);").conforms())
            .as("the flag is copied to a local that the call receives: a reader has to look, so the census refuses it")
            .isFalse();
    }

    @Test
    void theGateFormsTheRepositoryUsesAreAccepted() {
        String jooq = """
            final class R {
                List plain(String a, boolean excludeSensitive) {
                    return ctx.select(ch.chunkText()).from(ch.table())
                        .where(excludeSensitive ? ChunkSensitivity.notSensitive(x) : DSL.trueCondition()).fetch();
                }
            }
            """;
        assertThat(route(ChunkTextRouteCensus.census(HANDLER, jooq, Set.of(), Set.of()), "/search").conforms())
            .as("a ternary whose branch is not a known condition builder is not a gate").isFalse();
        String builder = jooq.replace("ChunkSensitivity.notSensitive(x)", "ChunkSensitivity.notSensitiveCondition(x)");
        assertThat(route(ChunkTextRouteCensus.census(HANDLER, builder, Set.of(), Set.of()), "/search").conforms())
            .as("flag ? notSensitiveCondition(...) : trueCondition()").isTrue();
        String viaSink = """
            final class R {
                List plain(String a, boolean excludeSensitive) { return inner(a, excludeSensitive); }
                List inner(String a, boolean excludeSensitive) { return PLAIN_SEARCH_384.call(a, excludeSensitive); }
            }
            """;
        assertThat(route(ChunkTextRouteCensus.census(HANDLER, viaSink, Set.of("plain_search_384"), Set.of()), "/search")
            .conforms()).as("the flag handed to another sink, which gates its own read").isTrue();
        String viaDrop = """
            final class R {
                List plain(String a, boolean excludeSensitive) {
                    var rows = PLAIN_SEARCH_384.call(a);
                    attachSensitive(tenant, rows, excludeSensitive);
                    return rows;
                }
            }
            """;
        assertThat(route(ChunkTextRouteCensus.census(HANDLER, viaDrop, Set.of("plain_search_384"), Set.of()), "/search")
            .conforms()).as("the flag handed to the post-lookup drop").isTrue();
    }

    // ── nexus-rfvyy.7.17 r1 S1: the handler must pass the flag on EVERY call that can read text ─────

    private static final String TWO_CALL_REPO = """
        final class R {
            List plain(String a, boolean excludeSensitive) { return PLAIN_SEARCH_384.call(a, excludeSensitive); }
            List other(String a, boolean excludeSensitive) { return ctx.select(ch.chunkText()).from(ch.table())
                .where(excludeSensitive ? notSensitiveCondition(ch) : DSL.trueCondition()).fetch(); }
        }
        """;

    @Test
    void aHandlerWhoseSecondRepositoryCallLeavesTheParsedFlagOutFailsTheCensus() {
        String both = HANDLER.replace("repo.plain(a, excludeSensitive);",
            "repo.plain(a, excludeSensitive); repo.other(a, excludeSensitive);");
        assertThat(route(ChunkTextRouteCensus.census(both, TWO_CALL_REPO, Set.of("plain_search_384"), Set.of()),
                         "/search").conforms()).as("control: both calls carry it").isTrue();
        String one = HANDLER.replace("repo.plain(a, excludeSensitive);",
            "repo.plain(a, excludeSensitive); repo.other(a, false);");
        var f = route(ChunkTextRouteCensus.census(one, TWO_CALL_REPO, Set.of("plain_search_384"), Set.of()), "/search");
        assertThat(f.conforms()).as("the parse reaches one call; the second reads text without it").isFalse();
        assertThat(f.handlerCallsWithoutFlag()).containsExactly("handleSearch -> repo.other(...)");
        assertThat(f.sinkMethodsWithoutFlag()).as("the repository side is fine: the defect is the call").isEmpty();
    }

    @Test
    void aHandlerThatParsesTheFlagButNeverPassesItToTheRepositoryFailsTheCensus() {
        var dropped = ChunkTextRouteCensus.census(
            HANDLER.replace("repo.plain(a, excludeSensitive)", "repo.plain(a, true)"), REPO,
            Set.of("plain_search_384"), Set.of());
        assertThat(route(dropped, "/search").conforms())
            .as("the parsed flag does not reach the repository call").isFalse();
        var nested = ChunkTextRouteCensus.census(
            HANDLER.replace("repo.plain(a, excludeSensitive)", "repo.plain(a, Boolean.valueOf(excludeSensitive))"),
            REPO, Set.of("plain_search_384"), Set.of());
        assertThat(route(nested, "/search").conforms())
            .as("the flag inside a nested call argument still reaches it").isTrue();
        var inline = ChunkTextRouteCensus.census(
            HANDLER.replace("repo.plain(a, excludeSensitive)", "repo.plain(a, optExcludeSensitive(body))")
                .replace("boolean excludeSensitive = optExcludeSensitive(body);", ""),
            REPO, Set.of("plain_search_384"), Set.of());
        assertThat(route(inline, "/search").conforms()).as("the parse written inside the call").isTrue();
    }

    @Test
    void aFunctionTheCensusDoesNotKnowIsNotAChunkTextRead() {
        var findings = ChunkTextRouteCensus.census(HANDLER, REPO, Set.of(), Set.of());
        assertThat(route(findings, "/search").readsChunkText())
            .as("with no text functions derived, PLAIN_SEARCH_384 reads none").isFalse();
    }

    @Test
    void theCatalogHandlerCheckIsPerCall() {
        String good = "private void handleResolveSpan(HttpExchange ex) { boolean excludeSensitive = "
            + "dev.nexus.service.vectors.ChunkSensitivity.excludeFlag(queryParam(ex, \" \")); "
            + "var result = repo.resolveSpan(tenant, collection, spanChash, excludeSensitive); }";
        assertThat(ChunkTextRouteCensus.everyRepoCallCarriesTheParsedFlag(good, "resolveSpan", "excludeFlag")).isTrue();
        assertThat(ChunkTextRouteCensus.everyRepoCallCarriesTheParsedFlag(
            good.replace("spanChash, excludeSensitive", "spanChash, false"), "resolveSpan", "excludeFlag"))
            .as("the parsed value does not reach the call").isFalse();
        assertThat(ChunkTextRouteCensus.everyRepoCallCarriesTheParsedFlag(
            good + " repo.resolveSpan(t, c, s, false);", "resolveSpan", "excludeFlag"))
            .as("a second call without it").isFalse();
        assertThat(ChunkTextRouteCensus.everyRepoCallCarriesTheParsedFlag(
            "private void handleResolveSpan(HttpExchange ex) { }", "resolveSpan", "excludeFlag"))
            .as("no call at all is not a pass").isFalse();
    }

    // ── nexus-rfvyy.7.17 r1 F3: the gc samples carry 80-character labels, of chunks with no owner ─────

    private static final ObjectMapper JSON = new ObjectMapper();

    /**
     * The premise under CLASSIFIED_DEFINITION_READERS' {@code OWNERLESS_LABEL} entries (gc_quarantine_orphans,
     * gc_quarantine_orphans_bounded, and gc_quarantine_orphans_floored, which calls them): the {@code sample} they
     * return holds {@code left(chunk_text, 80)} labels, which would be a leak if a sample could name a chunk with a
     * live sensitive owner. It cannot: the victim set is reapable(c), "no own-collection manifest row in any owner
     * state". This sweeps a collection that holds a chunk owned by a live sensitive document, a chunk owned by a
     * tombstoned sensitive document, and three ownerless chunks, through each of the three functions, and asserts
     * that every sample names only ownerless chunks and both owned chunks stay.
     */
    @Test
    void aGcSampleNamesOnlyChunksThatHaveNoOwner_soItsLabelsAreNeverSensitive() throws Exception {
        String tenant = "census-gc";
        String origin = "knowledge__census-gc__minilm-l6-v2-384__v1";
        String quarantine = "quarantine__census-gc__minilm-l6-v2-384__v1";
        Map<String, String> text = new LinkedHashMap<>();
        for (String k : List.of("owned-live", "owned-tomb", "orphan-1", "orphan-2", "orphan-3")) text.put(k, "label census " + k);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, "tok-census-gc-sample-0123456789abcdef0123", tenant, "census-gc");
            PgContainerHelper.insertCollection(dsl, tenant, origin);
            String model = PgContainerHelper.collectionModel(dsl, tenant, origin);
            for (var e : text.entrySet()) {
                dsl.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, origin)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(hexOf(e.getValue())))
                    .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, e.getValue())
                    .set(VectorSeeds.chunkEmbedding(384), VectorSeeds.vecXY(384, 1f, 0f))
                    .execute();
            }
            for (String owned : List.of("owned-live", "owned-tomb")) {
                dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "doc-" + owned)
                    .set(CATALOG_DOCUMENTS.TITLE, "Doc " + owned).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, origin)
                    .set(CATALOG_DOCUMENTS.SENSITIVE, true)
                    .set(CATALOG_DOCUMENTS.DELETED_AT, owned.equals("owned-tomb") ? OffsetDateTime.now().minusDays(60) : null)
                    .execute();
                dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                    .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "doc-" + owned)
                    .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
                    .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(hexOf(text.get(owned))))
                    .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, origin)
                    .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                    .execute();
            }
        }
        ReapableFixtures.agePastGrace(pg, tenant, origin);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            // the victim predicate itself: owned chunks are not reapable, in either owner state; the orphans are
            for (var e : text.entrySet()) {
                byte[] chash = HexFormat.of().parseHex(hexOf(e.getValue()));
                Field<OffsetDateTime> writtenAt = DSL.field(DSL.select(CHUNKS.LAST_WRITTEN_AT).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(origin)).and(CHUNKS.CHASH.eq(chash)));
                int reapable = dsl.fetchCount(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
                    DSL.val(tenant), DSL.val(origin), DSL.val(chash), writtenAt,
                    DSL.castNull(SQLDataType.INTERVAL))));
                assertThat(reapable).as("reapable(%s)", e.getKey()).isEqualTo(e.getKey().startsWith("orphan") ? 1 : 0);
            }
            Set<String> orphanHexes = new TreeSet<>();
            for (String k : List.of("orphan-1", "orphan-2", "orphan-3")) orphanHexes.add(hexOf(text.get(k)));
            Set<String> seen = new TreeSet<>();
            String at = "2026-11-06T00:00:00Z";
            Map<String, org.jooq.Record2<Long, org.jooq.JSONB>> sweeps = new LinkedHashMap<>();
            sweeps.put("gc_quarantine_orphans_bounded", dsl.select(GC_QUARANTINE_ORPHANS_BOUNDED.MOVED,
                GC_QUARANTINE_ORPHANS_BOUNDED.SAMPLE).from(GC_QUARANTINE_ORPHANS_BOUNDED.call(
                    384, tenant, origin, quarantine, at, 20, 1)).fetchOne());
            sweeps.put("gc_quarantine_orphans_floored", dsl.select(GC_QUARANTINE_ORPHANS_FLOORED.MOVED,
                GC_QUARANTINE_ORPHANS_FLOORED.SAMPLE).from(GC_QUARANTINE_ORPHANS_FLOORED.call(
                    384, tenant, origin, quarantine, at, 20, 1, null, null, false, null)).fetchOne());
            sweeps.put("gc_quarantine_orphans", dsl.select(GC_QUARANTINE_ORPHANS.MOVED,
                GC_QUARANTINE_ORPHANS.SAMPLE).from(GC_QUARANTINE_ORPHANS.call(
                    384, tenant, origin, quarantine, at, 20)).fetchOne());
            int swept = 0;
            for (var sweep : sweeps.entrySet()) {
                String fn = sweep.getKey();
                var row = sweep.getValue();
                assertThat(row).isNotNull();
                String sample = row.value2().data();
                assertThat(row.value1()).as(fn).isPositive();
                for (var e : text.entrySet()) {
                    if (e.getKey().startsWith("owned")) {
                        assertThat(sample).as("%s: the sample must not name %s", fn, e.getKey())
                            .doesNotContain(hexOf(e.getValue())).doesNotContain(e.getValue());
                    }
                }
                for (var node : JSON.readTree(sample)) {
                    seen.add(node.get("chash").asText());
                    swept++;
                }
            }
            assertThat(swept).as("non-vacuity: the three sweeps moved and sampled the three orphans").isEqualTo(3);
            assertThat(seen).as("every sampled chunk was an orphan").containsExactlyInAnyOrderElementsOf(orphanHexes);
            long left = dsl.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(origin)));
            assertThat(left).as("both owned chunks, the live and the tombstoned sensitive owner's, were not moved")
                .isEqualTo(2L);
        }
    }

    private static String hexOf(String text) {
        return dev.nexus.service.db.Chash.ofText(text).toHex();
    }
}
