// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.ArbiterTwins;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Source census for how an upsert is written on a table that carries both a name key and its id twin (RDR-229,
 * nexus-p2m1x.20 finding #7; the ruling is linda's of 2026-11-22). {@code INSERT ... ON CONFLICT (k) DO UPDATE} absorbs a
 * conflict only on its arbiter {@code k}; on a table with two unique keys over the same rows a racing insert trips the other
 * key as a plain 23505. The rule, enforced on every table of {@link ArbiterTwins#ALL} (read at test time, so a new twin is
 * covered the moment it is listed):
 * <ul>
 *   <li>a targeted {@code DO UPDATE} goes through {@link TwinKeyUpsert#run} (Java: lexically inside its argument), or, in a
 *       plpgsql function, the body carries the column-matched retry ({@code EXCEPTION WHEN unique_violation}, reading
 *       {@code CONSTRAINT_NAME}, consulting {@code pg_index} and {@code pg_partition_root});</li>
 *   <li>a {@code DO NOTHING} is target-less ({@code ON CONFLICT DO NOTHING} / {@code onConflictDoNothing()}), which
 *       arbitrates every unique index;</li>
 *   <li>anything else is listed in {@link #PERMANENT} (for good) or {@link #KNOWN_PENDING} (another lane's fix in flight) with a reason.</li>
 * </ul>
 *
 * <p><b>What is scanned.</b> (1) {@code service/src/main/java}: jOOQ {@code .onConflict(..).doUpdate()/.doNothing()} chains,
 * {@code .onConflictDoNothing()}, and any raw SQL string containing {@code ON CONFLICT}. The table is the nearest preceding
 * {@code insertInto(..)} (a literal TABLE constant, a {@code ch.table()} / {@code ct.table()} handle, or
 * {@code DSL.table(DSL.name("nexus", "t"))}), cross-checked against the conflict target's own column constants; a targeted
 * site whose table cannot be resolved, or whose two resolutions disagree, FAILS rather than being skipped. (2) The LAST
 * definition of each {@code nexus.*} function in master include order (rollback blocks and comments stripped, DROP FUNCTION
 * honoured, as {@code scripts/rdr229_p31_writer_census.py}): its {@code ON CONFLICT} sites, tabled by the nearest preceding
 * {@code INSERT INTO}. A plain unqualified table name resolves to {@code nexus}.
 *
 * <p><b>Blind spots, stated.</b> An {@code ON CONFLICT} in a changelog outside a function body (a registered data-move
 * template) is not scanned; a conflict clause assembled from string pieces is not seen; a Java site split across a variable is
 * found by its {@code .onConflict} token (and tabled by the nearest preceding {@code insertInto}), not by following the variable.
 * Non-vacuity: the live scan must find the known wrapped sites and the known target-less {@code DO NOTHING}, and
 * {@link #aPlantedUnwrappedDoUpdateAndATargetedDoNothingAreReported} proves the checker bites.
 */
public class TwinKeyUpsertCoverageCensusTest {

    private static final Path MAIN_JAVA = Path.of("src", "main", "java");
    private static final Path RESOURCES = Path.of("src", "main", "resources");

    enum Kind { JAVA_DO_UPDATE, JAVA_DO_NOTHING, SQL_DO_UPDATE, SQL_DO_NOTHING }

    /**
     * One ON CONFLICT site. {@code table} is the resolved table, or {@code "?"}; {@code targeted} says whether the clause names a
     * conflict target; {@code satisfied} is whether the site carries the protection its kind requires (wrapped / retry);
     * {@code problem} is non-null when the site could not be classified or resolved.
     */
    record Site(String where, int line, Kind kind, String table, boolean targeted, boolean satisfied, String problem) {
        String key() {
            return where + ":" + line;
        }
    }

    /**
     * Violations the owner has acknowledged: {@code "<file or function>::<table>::<KIND>::<n>"} (n counts same file, table and
     * kind from 1, in source order) to the reason. Empty: every violation the census finds is reported to the ruling's owner.
     */
    /** Sites outside the rule for good, each with its reason. */
    static final Map<String, String> PERMANENT = new TreeMap<>();

    /** Sites another lane is fixing; it must be EMPTY at colophon's final pass (linda, 2026-11-22), see
     * {@link #knownPendingIsEmptyOnceTheIdReadersArePresent}. */
    static final Map<String, String> KNOWN_PENDING = new TreeMap<>();

    static {
        PERMANENT.put("dev/nexus/service/db/TaxonomyRepository.java::topics::JAVA_DO_UPDATE::1",
            "arbiter is topics' id primary key (ON CONFLICT (TOPICS.ID)), a third key: a racing insert carries a different id, so a retry would meet the same label collision again; the name key and its id twin cover the same rows, so the twin adds no failure the name key did not already raise (bob, 2026-11-22)");  // line 2111
        PERMANENT.put("dev/nexus/service/db/TaxonomyRepository.java::topics::JAVA_DO_UPDATE::2",
            "arbiter is topics' id primary key (ON CONFLICT (TOPICS.ID)), a third key: a racing insert carries a different id, so a retry would meet the same label collision again; the name key and its id twin cover the same rows, so the twin adds no failure the name key did not already raise (bob, 2026-11-22)");  // line 2354
    }

    static {
        // RDR-229 #7 coverage (bob, 2026-11-22). Sites in elsinore's P3.5b/c mapping are fixed on her branch and leave this list
        // when it stacks (a stale entry fails), and the list is empty once IdReaders.PRESENT (P3.5c) is true.
        KNOWN_PENDING.put("dev/nexus/service/db/AspectRepository.java::document_aspects::JAVA_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 345
        KNOWN_PENDING.put("dev/nexus/service/db/AspectRepository.java::document_aspects::JAVA_DO_UPDATE::2",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 727
        KNOWN_PENDING.put("dev/nexus/service/db/AspectRepository.java::document_aspects::JAVA_DO_UPDATE::3",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 801
        KNOWN_PENDING.put("dev/nexus/service/db/RemapRepository.java::chash_remap::JAVA_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 152
        KNOWN_PENDING.put("dev/nexus/service/db/TaxonomyRepository.java::taxonomy_meta::JAVA_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 1696
        KNOWN_PENDING.put("dev/nexus/service/db/TaxonomyRepository.java::taxonomy_meta::JAVA_DO_UPDATE::2",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 2234
        KNOWN_PENDING.put("dev/nexus/service/db/TaxonomyRepository.java::taxonomy_meta::JAVA_DO_UPDATE::3",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 2490
        KNOWN_PENDING.put("dev/nexus/service/db/TaxonomyRepository.java::topics::JAVA_DO_NOTHING::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 3158
        KNOWN_PENDING.put("dev/nexus/service/db/TaxonomyRepository.java::topics::JAVA_DO_NOTHING::2",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 3288
        KNOWN_PENDING.put("nexus.canary_query_ring_upsert [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::canary_query_ring::SQL_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 7789
        KNOWN_PENDING.put("nexus.canary_query_ring_upsert [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::canary_query_ring::SQL_DO_UPDATE::2",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 7795
        KNOWN_PENDING.put("nexus.canary_query_ring_upsert [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::canary_query_ring::SQL_DO_UPDATE::3",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 7801
        KNOWN_PENDING.put("nexus.gc_quarantine_orphans [db/changelog/vectors-040-gc-audit-labels-by-text-and-manifest.xml]::chunks::SQL_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 221
        KNOWN_PENDING.put("nexus.gc_quarantine_orphans_bounded [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunks::SQL_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 715
        KNOWN_PENDING.put("nexus.gc_restore_rereferenced [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunks::SQL_DO_NOTHING::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 1908
        KNOWN_PENDING.put("nexus.gc_restore_rereferenced_bounded [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunks::SQL_DO_NOTHING::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 2294
        KNOWN_PENDING.put("nexus.quarantine_restore_chunks [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunks::SQL_DO_NOTHING::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 2819
        KNOWN_PENDING.put("nexus.reaper_quarantine_chunks [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunks::SQL_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 1521
        KNOWN_PENDING.put("nexus.stamp_chunks_on_manifest_delete [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunk_orphaned_at::SQL_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 4590
        KNOWN_PENDING.put("nexus.stamp_chunks_on_manifest_update [db/changelog/vectors-069-collection-id-dual-write-functions.xml]::chunk_orphaned_at::SQL_DO_UPDATE::1",
            "elsinore, RDR-229 P3.5b/c (nexus-p2m1x.24/.25): fixed on her branch (vectors-077/078; Java through TwinKeyUpsert); remove when it stacks");  // line 4690
    }

    /**
     * KNOWN_PENDING lists sites elsinore's P3.5b/c is fixing; an entry her stack misses would stay pending forever. So once P3.5c has
     * flipped {@link dev.nexus.service.vectors.IdReaders#PRESENT} (her readers select by id: P3.5b and P3.5c are on the tree), the list
     * must be empty. Skipped before that.
     */
    @Test
    void knownPendingIsEmptyOnceTheIdReadersArePresent() {
        org.junit.jupiter.api.Assumptions.assumeTrue(dev.nexus.service.vectors.IdReaders.PRESENT,
            "P3.5b/c not on this tree yet (IdReaders.PRESENT is false): KNOWN_PENDING may still name their sites");
        assertThat(KNOWN_PENDING).as("P3.5b/c are on the tree, so every pending twin-key site must be fixed: wrap it, make it target-less, "
            + "or move it to PERMANENT with a reason").isEmpty();
    }

    // ── the census over the live tree ────────────────────────────────────────────────────────────

    @Test
    void everyTargetedUpsertOnATwinKeyTableIsWrappedAndEveryDoNothingIsTargetless() throws IOException {
        Set<String> paired = pairedTables();
        assertThat(paired).as("ArbiterTwins.ALL names the paired tables").hasSizeGreaterThanOrEqualTo(10);

        List<Site> sites = new ArrayList<>();
        int javaFiles = 0;
        try (Stream<Path> walk = Files.walk(MAIN_JAVA)) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                javaFiles++;
                sites.addAll(scanJava(MAIN_JAVA.relativize(p).toString(), Files.readString(p), paired));
            }
        }
        Map<String, Fn> live = liveFunctions(masterChangelogFiles());
        for (Fn fn : live.values()) {
            sites.addAll(scanSqlBody(fn, paired));
        }

        List<Site> examined = sites.stream().filter(s -> paired.contains(s.table())).toList();
        String summary = summary(sites, examined, javaFiles, live.size());
        System.out.println(summary);
        System.out.println("unresolved-table sites: " + sites.stream().filter(s -> s.table().equals("?")).map(Site::key).toList());

        // non-vacuity: the walk reached the tree, and the known sites are among the examined ones
        assertThat(javaFiles).as("java files walked. " + summary).isGreaterThan(100);
        assertThat(live).as("live nexus.* functions. " + summary).hasSizeGreaterThan(50);
        assertThat(examined).as("sites on twin-key tables. " + summary).hasSizeGreaterThanOrEqualTo(4);
        for (String[] known : new String[][] {
            {"dev/nexus/service/vectors/PgVectorRepository.java", "chunks"},
            {"dev/nexus/service/db/CatalogRepository.java", "chunks"},
            {"dev/nexus/service/vectors/TaxonomyCentroidRepository.java", "taxonomy_centroids"}}) {
            assertThat(examined)
                .as("the wrapped " + known[1] + " upsert in " + known[0] + " is examined and wrapped. " + summary)
                .anyMatch(s -> s.where().endsWith(known[0]) && s.table().equals(known[1]) && s.kind() == Kind.JAVA_DO_UPDATE
                    && s.targeted() && s.satisfied());
        }
        assertThat(examined)
            .as("PipelineRepository's target-less DO NOTHING on pdf_pipeline is examined and passes. " + summary)
            .anyMatch(s -> s.where().endsWith("dev/nexus/service/db/PipelineRepository.java") && s.table().equals("pdf_pipeline")
                && s.kind() == Kind.JAVA_DO_NOTHING && !s.targeted());

        List<String> violations = violations(sites, paired);
        // KNOWN_PENDING: acknowledged violations leave the list; an entry that no longer matches one is stale and fails
        Map<String, String> byKey = pendingKeys(sites, paired);
        List<String> unacknowledged = new ArrayList<>();
        for (String v : violations) {
            String k = pendingKeyOf(v, byKey);
            if (!KNOWN_PENDING.containsKey(k) && !PERMANENT.containsKey(k)) {
                unacknowledged.add(v);
            }
        }
        Set<String> stale = new TreeSet<>(KNOWN_PENDING.keySet());
        stale.addAll(PERMANENT.keySet());
        stale.removeAll(byKey.values());
        assertThat(stale).as("KNOWN_PENDING or PERMANENT entries that match no violation").isEmpty();
        assertThat(KNOWN_PENDING.keySet()).as("an entry is pending or permanent, never both").doesNotContainAnyElementsOf(PERMANENT.keySet());
        assertThat(unacknowledged)
            .as("twin-key upserts outside the rule (a targeted DO UPDATE must run inside TwinKeyUpsert.run or carry the plpgsql "
                + "column-matched retry; a DO NOTHING must be target-less). " + summary)
            .isEmpty();
    }

    // ── the planted-violation proof ──────────────────────────────────────────────────────────────

    @Test
    void aPlantedUnwrappedDoUpdateAndATargetedDoNothingAreReported() {
        Set<String> paired = pairedTables();
        String java = """
            package dev.nexus.service.db;
            class Planted {
                void unwrappedDoUpdate(DSLContext ctx) {
                    ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID)
                       .values(tenant)
                       .onConflict(DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH)
                       .doUpdate()
                       .set(DOCUMENT_ASPECTS.MODEL, "m")
                       .execute();
                }
                void targetedDoNothing(DSLContext ctx) {
                    ctx.insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID)
                       .values(tenant)
                       .onConflict(CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION, CHASH_REMAP.OLD_ID)
                       .doNothing()
                       .execute();
                }
                void targetlessDoNothingIsFine(DSLContext ctx) {
                    ctx.insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID).values(tenant).onConflictDoNothing().execute();
                }
                void wrappedDoUpdateIsFine(DSLContext ctx) {
                    TwinKeyUpsert.run(ctx, "chash_remap", TwinKeyUpsert.Arbiter.NAME_KEY, c ->
                        c.insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID)
                         .values(tenant)
                         .onConflict(CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION, CHASH_REMAP.OLD_ID)
                         .doUpdate().set(CHASH_REMAP.NEW_ID, "n").execute());
                }
                void wrappedUnderTheWrongTable(DSLContext ctx) {
                    TwinKeyUpsert.run(ctx, "topics", TwinKeyUpsert.Arbiter.NAME_KEY, c ->
                        c.insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID)
                         .values(tenant)
                         .onConflict(CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION, CHASH_REMAP.OLD_ID)
                         .doUpdate().set(CHASH_REMAP.NEW_ID, "n").execute());
                }
                void notATwinTable(DSLContext ctx) {
                    ctx.insertInto(MEMORY, MEMORY.TENANT_ID).values(tenant)
                       .onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT).doUpdate().set(MEMORY.TITLE, "t").execute();
                }
                void rawSql(DSLContext ctx) {
                    ctx.execute("INSERT INTO nexus.taxonomy_meta (tenant_id) VALUES (?) ON CONFLICT (tenant_id, collection) DO UPDATE SET x = 1");
                }
            }
            """;
        List<Site> sites = scanJava("Planted.java", java, paired);
        List<String> violations = violations(sites, paired);
        System.out.println("planted java sites=" + sites.size() + " violations=" + violations);
        assertThat(sites).as("every ON CONFLICT of the fixture is seen").hasSize(7);
        assertThat(violations)
            .as("the unwrapped DO UPDATE, the targeted DO NOTHING, the mis-wrapped one and the raw SQL one are reported; "
                + "the target-less, the wrapped and the non-twin ones are not")
            .hasSize(4)
            .anyMatch(v -> v.contains("Planted.java:6") && v.contains("document_aspects") && v.contains("JAVA_DO_UPDATE"))
            .anyMatch(v -> v.contains("Planted.java:14") && v.contains("chash_remap") && v.contains("JAVA_DO_NOTHING"))
            .anyMatch(v -> v.contains("Planted.java:32") && v.contains("chash_remap") && v.contains("topics"))
            .anyMatch(v -> v.contains("Planted.java:40") && v.contains("taxonomy_meta") && v.contains("JAVA_DO_UPDATE"));

        String retry = """
            EXCEPTION WHEN unique_violation THEN
              GET STACKED DIAGNOSTICS c = CONSTRAINT_NAME;
              SELECT coalesce(pg_partition_root(i.indrelid), i.indrelid) FROM pg_index i WHERE true;
            """;
        String f1 = fnXml("nexus.planted_unwrapped", """
            BEGIN
              INSERT INTO nexus.chunk_orphaned_at (tenant_id) VALUES (p_tenant)
              ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET orphaned_at = now();
            END;""");
        String f2 = fnXml("nexus.planted_targeted_nothing", """
            BEGIN
              INSERT INTO canary_query_ring (tenant_id) VALUES (p_tenant)
              ON CONFLICT (tenant_id, collection, vector_digest) DO NOTHING;
            END;""");
        String f3 = fnXml("nexus.fine_retry", "BEGIN INSERT INTO nexus.canary_query_ring (tenant_id) VALUES (1)\n"
            + " ON CONFLICT (tenant_id, collection, vector_digest) DO UPDATE SET n = 1;\n" + retry + " END;");
        String f4 = fnXml("nexus.fine_targetless", "BEGIN INSERT INTO nexus.canary_query_ring (tenant_id) VALUES (1) ON CONFLICT DO NOTHING; END;");
        String f5 = fnXml("nexus.fine_other_table", "BEGIN INSERT INTO nexus.memory (tenant_id) VALUES (1) ON CONFLICT (tenant_id) DO UPDATE SET n = 1; END;");
        // redefined later in order, so the earlier unwrapped definition is not live; a dropped function is not live
        String f6a = fnXml("nexus.superseded", "BEGIN INSERT INTO nexus.topics (tenant_id) VALUES (1) ON CONFLICT (tenant_id, collection, label) DO UPDATE SET n = 1; END;");
        String f6b = fnXml("nexus.superseded", "BEGIN INSERT INTO nexus.topics (tenant_id) VALUES (1) ON CONFLICT DO NOTHING; END;");
        String f7a = fnXml("nexus.dropped", "BEGIN INSERT INTO nexus.topics (tenant_id) VALUES (1) ON CONFLICT (tenant_id, collection, label) DO UPDATE SET n = 1; END;");
        String f7b = "<sql>DROP FUNCTION IF EXISTS nexus.dropped(text);</sql>";
        Map<String, Fn> live = liveFunctions(List.of(
            Map.entry("a.xml", f1 + f2 + f3 + f4 + f5 + f6a + f7a),
            Map.entry("b.xml", f6b + f7b)));
        assertThat(live.keySet()).as("last definition wins, a dropped function is gone")
            .containsExactlyInAnyOrder("nexus.planted_unwrapped", "nexus.planted_targeted_nothing", "nexus.fine_retry",
                "nexus.fine_targetless", "nexus.fine_other_table", "nexus.superseded");
        List<Site> sqlSites = new ArrayList<>();
        for (Fn fn : live.values()) {
            sqlSites.addAll(scanSqlBody(fn, paired));
        }
        List<String> sqlViolations = violations(sqlSites, paired);
        System.out.println("planted sql sites=" + sqlSites.size() + " violations=" + sqlViolations);
        assertThat(sqlSites).hasSize(6);
        assertThat(sqlViolations).as("the unwrapped SQL DO UPDATE and the targeted SQL DO NOTHING are reported, nothing else")
            .hasSize(2)
            .anyMatch(v -> v.contains("nexus.planted_unwrapped") && v.contains("chunk_orphaned_at") && v.contains("SQL_DO_UPDATE"))
            .anyMatch(v -> v.contains("nexus.planted_targeted_nothing") && v.contains("canary_query_ring")
                && v.contains("SQL_DO_NOTHING"));
    }

    // ── the table list, from main code ───────────────────────────────────────────────────────────

    static Set<String> pairedTables() {
        return ArbiterTwins.ALL.stream().map(ArbiterTwins::table).collect(Collectors.toCollection(TreeSet::new));
    }

    // ── violations and the pending map ───────────────────────────────────────────────────────────

    static List<String> violations(List<Site> sites, Set<String> paired) {
        List<String> out = new ArrayList<>();
        for (Site s : sites) {
            String why = violation(s, paired);
            if (why != null) {
                out.add(s.where() + ":" + s.line() + " table=" + s.table() + " kind=" + s.kind() + " " + why);
            }
        }
        return out;
    }

    /** Null when the site passes (or is not on a twin-key table). */
    static String violation(Site s, Set<String> paired) {
        if (s.problem() != null) {
            // an unclassifiable site on an unresolved or twin table cannot be proven safe
            if (s.table().equals("?") || paired.contains(s.table())) {
                return "UNCLASSIFIED: " + s.problem();
            }
            return null;
        }
        if (s.table().equals("?")) {
            return s.targeted() ? "UNRESOLVED table on a targeted upsert (cannot prove it is not a twin-key table)" : null;
        }
        if (!paired.contains(s.table())) {
            return null;
        }
        boolean doNothing = s.kind() == Kind.JAVA_DO_NOTHING || s.kind() == Kind.SQL_DO_NOTHING;
        if (doNothing) {
            return s.targeted() ? "targeted DO NOTHING on a twin-key table: drop the conflict target so every unique index arbitrates"
                : null;
        }
        if (s.satisfied()) {
            return null;
        }
        return s.kind() == Kind.JAVA_DO_UPDATE
            ? "targeted DO UPDATE on a twin-key table outside TwinKeyUpsert.run (or wrapped under another table)"
            : "targeted DO UPDATE on a twin-key table in a function body without the column-matched unique_violation retry";
    }

    /** Violation line to its KNOWN_PENDING key, numbering same file, table and kind from 1 in source order. */
    private static Map<String, String> pendingKeys(List<Site> sites, Set<String> paired) {
        Map<String, String> byLine = new LinkedHashMap<>();
        Map<String, Integer> counter = new TreeMap<>();
        for (Site s : sites.stream().sorted(Comparator.comparing(Site::where).thenComparingInt(Site::line)).toList()) {
            if (violation(s, paired) == null) {
                continue;
            }
            String base = s.where() + "::" + s.table() + "::" + s.kind();
            int n = counter.merge(base, 1, Integer::sum);
            byLine.put(s.where() + ":" + s.line(), base + "::" + n);
        }
        return byLine;
    }

    private static String pendingKeyOf(String violationLine, Map<String, String> byLine) {
        for (Map.Entry<String, String> e : byLine.entrySet()) {
            if (violationLine.startsWith(e.getKey() + " ")) {
                return e.getValue();
            }
        }
        return "";
    }

    private static String summary(List<Site> all, List<Site> examined, int javaFiles, int liveFns) {
        Map<Kind, Integer> byKind = new EnumMap<>(Kind.class);
        Map<String, Integer> byTable = new TreeMap<>();
        for (Site s : examined) {
            byKind.merge(s.kind(), 1, Integer::sum);
            byTable.merge(s.table(), 1, Integer::sum);
        }
        Map<Kind, Integer> allByKind = new EnumMap<>(Kind.class);
        for (Site s : all) {
            allByKind.merge(s.kind(), 1, Integer::sum);
        }
        long unresolved = all.stream().filter(s -> s.table().equals("?")).count();
        return "TwinKeyUpsertCoverageCensus: sites examined on twin-key tables=" + examined.size() + " by kind=" + byKind
            + " by table=" + byTable + "; all ON CONFLICT sites seen=" + all.size() + " by kind=" + allByKind
            + " (table unresolved=" + unresolved + "); java files=" + javaFiles + ", live nexus.* functions=" + liveFns;
    }

    // ── the Java scanner ─────────────────────────────────────────────────────────────────────────

    /** Handles over DimTables: {@code ch.table()} is chunks, {@code ct.table()} is taxonomy_centroids. */
    private static final Map<String, String> HANDLES = Map.of("ch", "chunks", "ct", "taxonomy_centroids");

    private static final Pattern ON_CONFLICT_CALL = Pattern.compile("\\.\\s*onConflict\\s*\\(");
    private static final Pattern ON_CONFLICT_DO_NOTHING_CALL = Pattern.compile("\\.\\s*onConflictDoNothing\\s*\\(");
    private static final Pattern DO_CALL = Pattern.compile("\\.\\s*(doUpdate|doNothing)\\s*\\(");
    private static final Pattern RAW_ON_CONFLICT = Pattern.compile("\\bON\\s+CONFLICT\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern RAW_DO = Pattern.compile("\\bDO\\s+(UPDATE|NOTHING)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern RAW_INSERT = Pattern.compile(
        "\\bINSERT\\s+INTO\\s+(?:(\\w+)\\.)?\"?(%I|\\w+)\"?", Pattern.CASE_INSENSITIVE);
    private static final Pattern RUN_CALL = Pattern.compile("\\bTwinKeyUpsert\\s*\\.\\s*run\\s*\\(");

    /** A {@code TwinKeyUpsert.run(} call: the span of its arguments and the table literal it names. */
    private record RunSpan(int open, int close, String table) { }

    static List<Site> scanJava(String file, String source, Set<String> paired) {
        String code = ChunksWriterLastWrittenAtScanTest.blank(source, true);     // comments and literals blanked: structure
        String text = ChunksWriterLastWrittenAtScanTest.blank(source, false);    // comments blanked only: literals readable
        if (code.length() != text.length()) {
            throw new IllegalStateException("blank() changed the length of " + file);
        }
        List<RunSpan> runs = new ArrayList<>();
        Matcher rm = RUN_CALL.matcher(code);
        while (rm.find()) {
            int open = rm.end() - 1;
            int close = matchParen(code, open);
            Matcher lit = Pattern.compile("^\\(\\s*\\w+\\s*,\\s*\"([a-z_0-9]+)\"").matcher(text.substring(open, Math.max(open, close)));
            runs.add(new RunSpan(open, close, lit.find() ? lit.group(1) : "?"));
        }

        List<Site> out = new ArrayList<>();
        Matcher m = ON_CONFLICT_CALL.matcher(code);
        while (m.find()) {
            if (classQualified(code, m.start())) {
                continue;     // a static helper named onConflict, not the jOOQ step
            }
            int open = m.end() - 1;
            int close = matchParen(code, open);
            String target = text.substring(open + 1, close).trim();
            Kind kind = null;
            int semi = code.indexOf(';', close);
            Matcher d = DO_CALL.matcher(code);
            d.region(close, semi < 0 ? code.length() : Math.min(semi, close + 800));
            if (d.find()) {
                kind = d.group(1).equals("doUpdate") ? Kind.JAVA_DO_UPDATE : Kind.JAVA_DO_NOTHING;
            }
            addJavaSite(out, file, code, text, m.start(), kind, !target.isEmpty(), target, runs, paired);
        }
        Matcher n = ON_CONFLICT_DO_NOTHING_CALL.matcher(code);
        while (n.find()) {
            if (classQualified(code, n.start())) {
                continue;
            }
            addJavaSite(out, file, code, text, n.start(), Kind.JAVA_DO_NOTHING, false, "", runs, paired);
        }
        Matcher raw = RAW_ON_CONFLICT.matcher(text);
        while (raw.find()) {
            Matcher d = RAW_DO.matcher(text);
            d.region(raw.end(), Math.min(text.length(), raw.end() + 2000));
            if (!d.find()) {
                continue;     // prose that says "on conflict", not a clause
            }
            String between = text.substring(raw.end(), d.start());
            if (between.indexOf(';') >= 0 || between.indexOf('"') >= 0 && between.indexOf(')') < 0 && between.trim().length() > 60) {
                continue;
            }
            boolean targeted = !between.isBlank();
            Matcher ins = RAW_INSERT.matcher(text.substring(0, raw.start()));
            String table = "?";
            while (ins.find()) {
                table = rawTable(ins.group(1), ins.group(2));
            }
            Kind kind = d.group(1).equalsIgnoreCase("UPDATE") ? Kind.JAVA_DO_UPDATE : Kind.JAVA_DO_NOTHING;
            out.add(javaSite(file, code, raw.start(), kind, table, targeted, between.trim(), runs, null));
        }
        out.sort(Comparator.comparingInt(Site::line));
        return out;
    }

    private static String rawTable(String schema, String name) {
        if (name.equals("%I")) {
            return "?";
        }
        return schema == null || schema.equalsIgnoreCase("nexus") ? name.toLowerCase() : schema.toLowerCase() + "." + name.toLowerCase();
    }

    private static void addJavaSite(List<Site> out, String file, String code, String text, int pos, Kind kind, boolean targeted,
                                    String target, List<RunSpan> runs, Set<String> paired) {
        String fromInsert = insertTable(code, text, pos);
        String fromTarget = targetTable(target);
        String table = fromInsert;
        String problem = null;
        if (fromInsert.equals("?")) {
            table = fromTarget;
        } else if (!fromTarget.equals("?") && !fromTarget.equals(fromInsert)) {
            table = "?";
            problem = "the nearest insertInto names " + fromInsert + " but the conflict target names " + fromTarget;
        }
        if (kind == null) {
            problem = "no doUpdate()/doNothing() follows this onConflict(..)";
            kind = Kind.JAVA_DO_UPDATE;
        }
        out.add(javaSite(file, code, pos, kind, table, targeted, target, runs, problem));
    }

    private static Site javaSite(String file, String code, int pos, Kind kind, String table, boolean targeted, String target,
                                 List<RunSpan> runs, String problem) {
        int line = (int) code.substring(0, pos).chars().filter(c -> c == '\n').count() + 1;
        boolean satisfied = false;
        for (RunSpan r : runs) {
            if (pos > r.open() && pos < r.close()) {
                if (r.table().equals(table)) {
                    satisfied = true;
                } else {
                    problem = problem != null ? problem : "inside TwinKeyUpsert.run(\"" + r.table() + "\") but upserts " + table;
                }
            }
        }
        return new Site(file, line, kind, table, targeted, satisfied, problem);
    }

    /** True when the token before the dot is a class name (initial capital): {@code AspectDocIdAttribution.onConflict(..)}. */
    private static boolean classQualified(String code, int dot) {
        int i = dot - 1;
        while (i >= 0 && Character.isWhitespace(code.charAt(i))) {
            i--;
        }
        int end = i + 1;
        while (i >= 0 && (Character.isJavaIdentifierPart(code.charAt(i)))) {
            i--;
        }
        return end > i + 1 && Character.isUpperCase(code.charAt(i + 1)) && code.charAt(end - 1) != ')';
    }

    /** The table of the nearest {@code insertInto(..)} before {@code pos}, or "?". */
    private static String insertTable(String code, String text, int pos) {
        int at = code.lastIndexOf("insertInto", pos);
        if (at < 0) {
            return "?";
        }
        int open = code.indexOf('(', at);
        if (open < 0) {
            return "?";
        }
        int close = matchParen(code, open);
        int end = open + 1;
        int depth = 0;
        while (end < close) {
            char c = code.charAt(end);
            if (c == '(' || c == '[' || c == '{') {
                depth++;
            } else if (c == ')' || c == ']' || c == '}') {
                depth--;
            } else if (c == ',' && depth == 0) {
                break;
            }
            end++;
        }
        String arg = text.substring(open + 1, end).replaceAll("\\s+", "");
        Matcher constant = Pattern.compile("^(?:[\\w.]*\\.)?([A-Z][A-Z0-9_]*)$").matcher(arg);
        if (constant.find()) {
            return constant.group(1).toLowerCase();
        }
        Matcher handle = Pattern.compile("^(\\w+)\\.table\\(\\)$").matcher(arg);
        if (handle.find()) {
            return HANDLES.getOrDefault(handle.group(1), "?");
        }
        Matcher named = Pattern.compile("^DSL\\.table\\(DSL\\.name\\(\"nexus\",\"(\\w+)\"\\)\\)$").matcher(arg);
        if (named.find()) {
            return named.group(1);
        }
        return "?";
    }

    /** The table named by the first column of a conflict target ({@code TOPICS.TENANT_ID}, {@code ch.tenantId()}), or "?". */
    private static String targetTable(String target) {
        if (target.isEmpty()) {
            return "?";
        }
        String first = target.split(",", 2)[0].replaceAll("\\s+", "");
        Matcher constant = Pattern.compile("^(?:[\\w.]*\\.)?([A-Z][A-Z0-9_]*)\\.[A-Z][A-Z0-9_]*$").matcher(first);
        if (constant.find()) {
            return constant.group(1).toLowerCase();
        }
        Matcher handle = Pattern.compile("^(\\w+)\\.\\w+\\(\\)$").matcher(first);
        if (handle.find()) {
            return HANDLES.getOrDefault(handle.group(1), "?");
        }
        return "?";
    }

    /** Index of the ')' matching the '(' at {@code open} in literal-blanked code. */
    private static int matchParen(String code, int open) {
        int depth = 0;
        for (int i = open; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '(') {
                depth++;
            } else if (c == ')' && --depth == 0) {
                return i;
            }
        }
        return code.length();
    }

    // ── the SQL function scanner ─────────────────────────────────────────────────────────────────

    /** A live function: the changelog file it was last defined in, the line of its body's first character, and the body. */
    record Fn(String name, String file, int bodyLine, String body) { }

    private static final Pattern FN_RE = Pattern.compile(
        "CREATE\\s+(?:OR\\s+REPLACE\\s+)?FUNCTION\\s+(nexus\\.[a-z0-9_]+)\\s*\\((.*?)\\)\\s*RETURNS",
        Pattern.CASE_INSENSITIVE | Pattern.DOTALL);
    private static final Pattern DROP_RE = Pattern.compile(
        "DROP\\s+FUNCTION\\s+(?:IF\\s+EXISTS\\s+)?(nexus\\.[a-z0-9_]+)\\s*(\\([^)]*\\))?", Pattern.CASE_INSENSITIVE);
    private static final Pattern DOLLAR_QUOTE = Pattern.compile("AS\\s+(\\$[a-zA-Z_]*\\$)");
    private static final Pattern XML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);
    private static final Pattern ROLLBACK = Pattern.compile("<rollback>.*?</rollback>", Pattern.DOTALL);

    /** The master changelog's included files, in include order, as (relative path, text). Missing files are not skipped silently. */
    static List<Map.Entry<String, String>> masterChangelogFiles() throws IOException {
        String master = XML_COMMENT.matcher(
            Files.readString(RESOURCES.resolve("db/changelog/db.changelog-master.xml"))).replaceAll(m -> blankKeepingNewlines(m.group()));
        List<Map.Entry<String, String>> out = new ArrayList<>();
        Matcher inc = Pattern.compile("<include\\s+file=\"([^\"]+)\"").matcher(master);
        while (inc.find()) {
            Path p = RESOURCES.resolve(inc.group(1));
            assertThat(p).as("master include " + inc.group(1)).exists();
            out.add(Map.entry(inc.group(1), Files.readString(p)));
        }
        return out;
    }

    private static String blankKeepingNewlines(String s) {
        return s.replaceAll("[^\\n]", " ");
    }

    /** The last definition of each nexus.* function in include order; rollback blocks and comments stripped; DROP honoured. */
    static Map<String, Fn> liveFunctions(List<Map.Entry<String, String>> filesInOrder) {
        Map<String, Fn> live = new TreeMap<>();
        for (Map.Entry<String, String> f : filesInOrder) {
            String text = XML_COMMENT.matcher(ROLLBACK.matcher(f.getValue()).replaceAll(m -> blankKeepingNewlines(m.group())))
                .replaceAll(m -> blankKeepingNewlines(m.group()));
            Matcher defMatcher = FN_RE.matcher(text);
            List<int[]> defs = new ArrayList<>();
            List<String> defNames = new ArrayList<>();
            while (defMatcher.find()) {
                defs.add(new int[] {defMatcher.start(), defMatcher.end()});
                defNames.add(defMatcher.group(1).toLowerCase());
            }
            Matcher dr = DROP_RE.matcher(text);
            List<int[]> drops = new ArrayList<>();
            List<String> dropNames = new ArrayList<>();
            while (dr.find()) {
                drops.add(new int[] {dr.start(), dr.end()});
                dropNames.add(dr.group(1).toLowerCase());
            }
            record Item(int pos, boolean drop, int idx) { }
            List<Item> items = new ArrayList<>();
            for (int i = 0; i < defs.size(); i++) {
                items.add(new Item(defs.get(i)[0], false, i));
            }
            for (int i = 0; i < drops.size(); i++) {
                items.add(new Item(drops.get(i)[0], true, i));
            }
            items.sort(Comparator.comparingInt(Item::pos));
            for (Item it : items) {
                if (it.drop()) {
                    live.remove(dropNames.get(it.idx()));
                    continue;
                }
                int end = defs.get(it.idx())[1];
                String body = "";
                int bodyStart = end;
                Matcher dq = DOLLAR_QUOTE.matcher(text);
                dq.region(end, text.length());
                if (dq.find()) {
                    String tag = dq.group(1);
                    bodyStart = dq.end();
                    int e = text.indexOf(tag, bodyStart);
                    body = text.substring(bodyStart, e > 0 ? e : text.length());
                }
                int bodyLine = (int) text.substring(0, bodyStart).chars().filter(ch -> ch == '\n').count() + 1;
                live.put(defNames.get(it.idx()), new Fn(defNames.get(it.idx()), f.getKey(), bodyLine, body));
            }
        }
        return live;
    }

    private static final Pattern RETRY_UNIQUE = Pattern.compile("EXCEPTION\\s+WHEN\\s+unique_violation", Pattern.CASE_INSENSITIVE);

    /** Whether a function body carries the column-matched retry: the handler, the constraint name, pg_index and the partition root. */
    static boolean hasColumnMatchedRetry(String body) {
        String b = body.toLowerCase();
        return RETRY_UNIQUE.matcher(body).find() && b.contains("constraint_name") && b.contains("pg_index")
            && b.contains("pg_partition_root");
    }

    static List<Site> scanSqlBody(Fn fn, Set<String> paired) {
        String body = fn.body().replaceAll("--[^\\n]*", "");
        // the same length as before for line arithmetic: a replaced comment keeps its newline only if it was a whole line, so
        // count lines on the original body
        String original = fn.body();
        boolean retry = hasColumnMatchedRetry(body);
        List<Site> out = new ArrayList<>();
        Matcher oc = RAW_ON_CONFLICT.matcher(body);
        while (oc.find()) {
            Matcher d = RAW_DO.matcher(body);
            d.region(oc.end(), body.length());
            if (!d.find()) {
                continue;
            }
            String between = body.substring(oc.end(), d.start());
            if (between.indexOf(';') >= 0) {
                continue;
            }
            boolean targeted = !between.isBlank();
            Matcher ins = RAW_INSERT.matcher(body.substring(0, oc.start()));
            String table = "?";
            while (ins.find()) {
                table = rawTable(ins.group(1), ins.group(2));
            }
            Kind kind = d.group(1).equalsIgnoreCase("UPDATE") ? Kind.SQL_DO_UPDATE : Kind.SQL_DO_NOTHING;
            int line = fn.bodyLine() + lineOffset(original, body, oc.start());
            out.add(new Site(fn.name() + " [" + fn.file() + "]", line, kind, table, targeted, retry, null));
        }
        return out;
    }

    /** The number of newlines before {@code pos} of the comment-stripped body, which equals the original's (a comment never spans lines). */
    private static int lineOffset(String original, String stripped, int pos) {
        return (int) stripped.substring(0, pos).chars().filter(c -> c == '\n').count();
    }

    private static String fnXml(String name, String body) {
        return "<sql>CREATE OR REPLACE FUNCTION " + name + "(p_tenant text) RETURNS void LANGUAGE plpgsql AS $$\n" + body
            + "\n$$;</sql>\n";
    }
}
