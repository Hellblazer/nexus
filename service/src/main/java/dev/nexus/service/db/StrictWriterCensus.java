// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.ExecuteContext;
import org.jooq.impl.DefaultExecuteListener;
import org.jooq.impl.DefaultExecuteListenerProvider;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The engine-suite-wide census of the registry check (RDR-229 P3.5a, linda 6(a)): every statement a transaction runs that writes a
 * table filed by collection must run in a transaction that has read a registry row {@code FOR KEY SHARE} (the memo holds it), or that
 * is tagged exempt ({@link WriteRegistryMemo#exempt}: a verb that locks the registry itself, a background writer that waits for no
 * lock, a transaction that wrote the registry row). A write that is neither is a writer whose registry check is missing or after its
 * first write.
 *
 * <p>Off by default and free then (a mode read in {@code TenantScope}). {@code -Dnexus.writer.census=record} keeps each finding for
 * {@link #violations()}; {@code strict} also throws at the statement, so the test that ran the writer fails where the write is. The
 * engine suite runs it in {@code strict} once the census passes in {@code record}.
 *
 * <p>It looks at the statement text a jOOQ query renders, the same text the Postgres server runs: {@code INSERT INTO},
 * {@code UPDATE} or {@code DELETE FROM} of one of {@link #GUARDED}. A history table is not listed (stamped unlocked, exempt by
 * ruling), nor is a table that is not filed by collection.
 */
final class StrictWriterCensus {

    static final String PROPERTY = "nexus.writer.census";

    /** The tables a collection-scoped writer fills, from {@code p31-writer-census.tsv}: the ones with a P3.1 CHECK, history tables left out. */
    static final Set<String> GUARDED = Set.of(
        "aspect_extraction_queue", "catalog_document_chunks", "catalog_documents", "chash_remap", "chunk_orphaned_at", "chunks",
        "document_aspects", "document_highlights", "pdf_pipeline", "reembed_jobs", "taxonomy_centroids", "taxonomy_meta",
        "topic_assignments", "topics");

    private static final Pattern WRITE = Pattern.compile(
        "^\\s*(?:insert\\s+into|update|delete\\s+from)\\s+(?:\"?nexus\"?\\.)?\"?([a-z_]+)\"?", Pattern.CASE_INSENSITIVE);
    private static final Logger log = LoggerFactory.getLogger(StrictWriterCensus.class);
    private static final ConcurrentLinkedQueue<String> FOUND = new ConcurrentLinkedQueue<>();
    private static volatile String mode = System.getProperty(PROPERTY, "off");
    private static volatile int examined;

    private StrictWriterCensus() {
    }

    /** Called by {@code TenantScope} for each transaction it opens; a no-op while the census is off. */
    static void install(DSLContext ctx) {
        if (!"off".equals(mode)) {
            ctx.configuration().set(new DefaultExecuteListenerProvider(new Listener()));
        }
    }

    static boolean on() {
        return !"off".equals(mode);
    }

    /** Test seam: {@code off}, {@code record} or {@code strict}. */
    static void setModeForTests(String m) {
        mode = m;
        FOUND.clear();
        examined = 0;
    }

    /** The findings so far: {@code table @ first service frame} of each write the census refused. */
    static List<String> violations() {
        return List.copyOf(FOUND);
    }

    /** How many guarded-table writes the census looked at: a run that looked at none proves nothing. */
    static int examined() {
        return examined;
    }

    private static final class Listener extends DefaultExecuteListener {
        @Override
        public void executeStart(ExecuteContext c) {
            String sql = c.sql();
            if (sql == null) {
                return;
            }
            Matcher m = WRITE.matcher(sql);
            if (!m.find() || !GUARDED.contains(m.group(1).toLowerCase())) {
                return;
            }
            examined++;
            if (WriteRegistryMemo.heldOrExempt(c.configuration())) {
                return;
            }
            String where = "?";
            for (StackTraceElement f : new Throwable().getStackTrace()) {
                String cn = f.getClassName();
                if (cn.startsWith("dev.nexus.service.") && !cn.endsWith("StrictWriterCensus$Listener") && !cn.endsWith("TenantScope")) {
                    where = f.getClassName().substring(f.getClassName().lastIndexOf('.') + 1) + "." + f.getMethodName() + ":" + f.getLineNumber();
                    break;
                }
            }
            String finding = m.group(1) + " @ " + where;
            FOUND.add(finding);
            if ("strict".equals(mode)) {
                throw new IllegalStateException("registry check missing: a write of " + m.group(1) + " in a transaction that read no registry row ("
                    + where + "); read the collection's row FOR KEY SHARE first, or tag the transaction exempt with the reason");
            }
            log.warn("event=writer_census_violation table={} at={}", m.group(1), where);
        }
    }
}
