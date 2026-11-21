// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): runs {@link CompletionCheck} after the schema walk, on its own admin session and its own
 * thread, keeps the latest {@link CompletionCheck.Result} for the engine's Java to read ({@link #latest}), and reports it in
 * {@code /v1/status} {@code boot_work}, where {@code nx doctor} reads it ({@code nexus/rdr229_completion_health.py}).
 *
 * <p><b>It reads and reports; it writes nothing.</b> The session is opened {@code default_transaction_read_only = on}, so a
 * write would raise at the server, and the check itself holds no write. Sam's rule is no boot-time data migrations.
 *
 * <p>The state is re-read every {@link #DEFAULT_INTERVAL}, so the engine's id readers, P3.3b's pass, P3.4's builds and the
 * one-row rename see a gate hold within seconds of the move worker recording a {@code completed_at}, with no restart (the
 * RDR names the worker's completion and a reconciler pass end as the re-read points; a consumer that must read at once calls
 * {@link #refresh}). The rows that keep a CHECK NOT VALID are counted by a bounded scan, so that count is cached for
 * {@link #VIOLATOR_TTL} per table.
 *
 * <p>The entries, none of which names a collection or a tenant ({@code /v1/status} is unauthenticated): {@value #SUMMARY_ID}
 * (the three gates, {@code done} of {@code total} gates holding; absent once all three hold, because {@code boot_work} is
 * work in progress and {@code nx upgrade} and {@code nx daemon status} repeat every entry), {@value #CHECK_PREFIX}{@code <constraint>} for a listed
 * CHECK that is missing (the superseded_by one waits on D2's data and says how many rows), and {@value #VALIDATE_PREFIX}
 * {@code <table>} for a table whose stamp is done and whose CHECK is still NOT VALID (orphan rows).
 */
public final class CompletionBootWork implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CompletionBootWork.class);

    /** The entry that says where the three gates stand. */
    public static final String SUMMARY_ID = "rdr229-completion";
    /** The prefix of the entry of a listed CHECK that is missing. */
    public static final String CHECK_PREFIX = "rdr229-check:";
    /** The prefix of the entry of a table whose CHECK is still NOT VALID after its stamp. */
    public static final String VALIDATE_PREFIX = "rdr229-validate:";
    public static final String UNIT = "gates";
    public static final String ON_STOP = "the check is read again on the next boot; nothing is lost";
    public static final Duration DEFAULT_INTERVAL = Duration.ofSeconds(5);
    /** How long a table's violating-row count is kept: the scan is bounded, not free. */
    public static final Duration VIOLATOR_TTL = Duration.ofMinutes(10);

    /** Where one evaluation comes from. {@link #abort} ends a read in flight; {@link #close} releases the session. */
    interface Reader extends AutoCloseable {
        CompletionCheck.Result read();

        void abort();

        @Override
        void close();
    }

    private final BootWorkRegistry registry;
    private final Reader reader;
    private final Object sampleLock = new Object();
    private final Set<String> published = new HashSet<>();
    private volatile CompletionCheck.Result latest;
    private volatile boolean closed;
    private String lastError;
    private ScheduledExecutorService scheduler;

    /**
     * @param gate3 P3.4's id-index coverage ({@link CompletionCheck.Gate3Source#NOT_INSTALLED} until it ships)
     */
    public CompletionBootWork(BootWorkRegistry registry, AdminConnection admin, CompletionCheck.ReadSet readSet,
                              CompletionCheck.Gate3Source gate3) {
        this(registry, new DbReader(admin, readSet, gate3, Clock.systemUTC()));
    }

    CompletionBootWork(BootWorkRegistry registry, Reader reader) {
        this.registry = Objects.requireNonNull(registry, "registry");
        this.reader = Objects.requireNonNull(reader, "reader");
    }

    /** The result of the last sample, or null before the first one. */
    public CompletionCheck.Result latest() {
        return latest;
    }

    /** Reads now, outside the schedule, for a consumer that must not wait for the next sample. */
    public CompletionCheck.Result refresh() {
        publish();
        return latest;
    }

    /** Samples every {@link #DEFAULT_INTERVAL}, on a daemon thread. A second call does nothing. */
    public synchronized void start() {
        start(DEFAULT_INTERVAL);
    }

    synchronized void start(Duration interval) {
        if (scheduler != null) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "rdr229-completion-check");
            t.setDaemon(true);
            return t;
        });
        scheduler.scheduleWithFixedDelay(() -> {
            try {
                publish();
            } catch (VirtualMachineError e) {
                throw e;
            } catch (Throwable t) {   // a surprise costs one sample, never the thread
                log.warn("event=rdr229_completion_failed cause=\"{}\"", FailureHandler.oneLine(t.toString()));
            }
        }, 0, interval.toMillis(), TimeUnit.MILLISECONDS);
    }

    @Override
    public void close() {
        ScheduledExecutorService s;
        synchronized (this) {
            s = scheduler;
            scheduler = null;
        }
        if (s != null) {
            s.shutdownNow();
        }
        closed = true;
        synchronized (published) {
            for (String id : published) {
                registry.finish(id);
            }
            published.clear();
        }
        try {
            reader.abort();
        } finally {
            reader.close();
        }
    }

    /**
     * One sample: evaluates the check and brings the {@code boot_work} entries to match.
     *
     * @return how many entries there are now, or -1 when the check could not be read (the entries stay as they were)
     */
    int publish() {
        synchronized (sampleLock) {
            if (closed) {
                return -1;
            }
            CompletionCheck.Result result;
            try {
                result = reader.read();
                lastError = null;
            } catch (RuntimeException e) {
                String cause = FailureHandler.oneLine(e.toString());
                if (!cause.equals(lastError)) {
                    log.warn("event=rdr229_completion_unreadable cause=\"{}\"", cause);
                    lastError = cause;
                }
                return -1;
            }
            latest = result;
            return apply(result);
        }
    }

    private int apply(CompletionCheck.Result r) {
        Map<String, Entry> desired = new LinkedHashMap<>();
        long holding = (r.gate1() ? 1 : 0) + (r.gate2() ? 1 : 0) + (r.gate3() ? 1 : 0);
        if (holding < 3) {   // boot_work is work in progress: nx upgrade and nx daemon status repeat every entry
            desired.put(SUMMARY_ID, new Entry(summary(r), holding, 3L));
        }
        for (CompletionCheck.CheckState c : r.checks()) {
            if (!c.present()) {
                desired.put(CHECK_PREFIX + c.constraint(), new Entry(missing(c, r), null, null));
            }
        }
        for (CompletionCheck.Violators v : r.violators()) {
            desired.put(VALIDATE_PREFIX + v.table(), new Entry(validateWaits(v), null, null));
        }
        synchronized (published) {
            Set<String> gone = new HashSet<>(published);
            for (var e : desired.entrySet()) {
                if (published.add(e.getKey())) {
                    registry.register(e.getKey(), e.getValue().what(), e.getValue().done(), e.getValue().total(), UNIT,
                        true, ON_STOP);
                }
                registry.update(e.getKey(), e.getValue().what(), e.getValue().done(), e.getValue().total(), null);
                gone.remove(e.getKey());
            }
            for (String id : gone) {
                registry.finish(id);
                published.remove(id);
            }
            return published.size();
        }
    }

    private record Entry(String what, Long done, Long total) { }

    static String summary(CompletionCheck.Result r) {
        long total = r.labels().size();
        long unsatisfied = total - r.labelsSatisfied();
        String g1 = r.gate1() ? "holds"
            : "waits (" + unsatisfied + " of " + total + " stamp moves incomplete"
                + (r.supersededByCheckPresent() ? "" : "; the superseded_by CHECK is missing") + ")";
        String g2 = r.gate2() ? "holds" : (r.gate1() ? "waits (a swap or validation move is incomplete)" : "waits on gate 1");
        String g3 = r.gate3() ? "holds" : "waits: " + r.gate3Why();
        return "gate 1 " + g1 + "; gate 2 " + g2 + "; gate 3 " + g3;
    }

    static String missing(CompletionCheck.CheckState c, CompletionCheck.Result r) {
        if (CompletionCheck.SUPERSEDED_BY_CHECK.equals(c.constraint())) {
            return "waits: " + c.constraint() + " is not on " + c.table() + " yet; " + r.supersededByWaiting()
                + " registry row(s) with a blank model_version and no superseded_by_id hold its changeset back "
                + "(D2's repair move or the superseded_by stamp clears them, then the next boot adds it); the switch is refused until then";
        }
        return "missing: " + c.constraint() + " is not on " + c.table() + "; the switch is refused until it is restored "
            + "(its changeset text is in vectors-068-collection-id-expand.xml)";
    }

    static String validateWaits(CompletionCheck.Violators v) {
        String counts = v.counted()
            ? v.orphans() + " orphan row(s) (a name with no registry row in its tenant) and " + v.unstamped() + " unstamped row(s)"
            : "rows that could not be counted";
        return "waits: " + counts + " keep " + v.constraint() + " on " + v.table()
            + " NOT VALID; its VALIDATE waits instead of failing a boot";
    }

    /** The reader of a running engine: its own read-only admin session, kept between samples and replaced after a failure. */
    private static final class DbReader implements Reader {
        private final AdminConnection admin;
        private final CompletionCheck.ReadSet readSet;
        private final CompletionCheck.Gate3Source gate3;
        private final CompletionCheck.ViolatorCounter counter;
        private volatile AdminDdlSession session;

        DbReader(AdminConnection admin, CompletionCheck.ReadSet readSet, CompletionCheck.Gate3Source gate3, Clock clock) {
            this.admin = Objects.requireNonNull(admin, "admin");
            this.readSet = Objects.requireNonNull(readSet, "readSet");
            this.gate3 = Objects.requireNonNull(gate3, "gate3");
            this.counter = new CachedCounter(CompletionCheck.DIRECT, clock, VIOLATOR_TTL);
        }

        @Override
        public CompletionCheck.Result read() {
            try {
                AdminDdlSession s = session;
                if (s == null || s.isCancelled()) {
                    s = AdminDdlSession.open(admin, new AdminDdlSession.Settings("nexus-rdr229-completion", 0L,
                        "rdr229 completion check", "nexus", "data_moves", "none"), false);
                    // The check reads and reports. Held at the server: a write on this session raises 25006.
                    s.setSession("default_transaction_read_only", "on");
                    session = s;
                }
                return CompletionCheck.evaluate(s.ctx(), readSet, gate3, counter);
            } catch (RuntimeException e) {
                close();   // a failed session is replaced on the next sample
                throw e;
            }
        }

        @Override
        public void abort() {
            AdminDdlSession s = session;
            if (s != null) {
                s.cancel();
            }
        }

        @Override
        public void close() {
            AdminDdlSession s = session;
            session = null;
            if (s != null) {
                s.close();
            }
        }
    }

    /** A violating-row counter that keeps each table's answer for {@code ttl}; a count that failed is not kept. */
    static final class CachedCounter implements CompletionCheck.ViolatorCounter {
        private final CompletionCheck.ViolatorCounter delegate;
        private final Clock clock;
        private final Duration ttl;
        private final Map<String, Kept> kept = new HashMap<>();

        private record Kept(CompletionCheck.Violators value, Instant at) { }

        CachedCounter(CompletionCheck.ViolatorCounter delegate, Clock clock, Duration ttl) {
            this.delegate = delegate;
            this.clock = clock;
            this.ttl = ttl;
        }

        @Override
        public synchronized CompletionCheck.Violators count(DSLContext ctx, P31Checks.Row row) {
            Kept k = kept.get(row.constraint());
            Instant now = clock.instant();
            if (k != null && Duration.between(k.at(), now).compareTo(ttl) < 0) {
                return k.value();
            }
            CompletionCheck.Violators fresh = delegate.count(ctx, row);
            if (fresh.counted()) {
                kept.put(row.constraint(), new Kept(fresh, now));
            } else {
                kept.remove(row.constraint());
            }
            return fresh;
        }
    }
}
