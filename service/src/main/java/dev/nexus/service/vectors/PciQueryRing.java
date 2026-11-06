// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.LockSupport;
import java.util.regex.Pattern;

/**
 * Admission to the recall canary's query ring, and the off-path writer behind it (RDR-230 Technical Design 6, bead
 * nexus-4lnn1.1.3). The ring itself is {@code nexus.canary_query_ring}, kept to the 32 newest distinct vectors per
 * (tenant, collection) by the function {@code nexus.canary_query_ring_upsert} (bead nexus-4lnn1.1.2).
 *
 * <p><b>The search never waits for the ring.</b> {@link #admit} is called by the handler after the response is sent. It
 * decides from memory (the router's valid-index set answers without a query), clones the vector and offers it to a
 * bounded queue ({@value #QUEUE_CAPACITY} entries, provisional); a full queue drops the entry and counts it in the next
 * flush's log line. It never throws. The writer is a daemon thread that drains the queue every
 * {@link #FLUSH_EVERY} or as soon as {@value #FLUSH_AT} entries are waiting (both provisional), and makes one call of
 * the function per tenant under that tenant's scope as nexus_svc. The digest (SHA-256 of the vector's values in order,
 * {@link #digest}) is computed there, not on the search path.
 *
 * <p><b>Best effort.</b> A failed write is logged once per batch ({@code event=pci_ring_write_failed tenant= entries=
 * error=}) and the batch is dropped; nothing retries it. An entry whose collection was deleted after it was enqueued is
 * skipped inside the function (gate S6) and costs the rest of its batch nothing; the flush line counts the shortfall as
 * {@code skipped}. Entries still queued when the engine stops are lost. Every engine runs a writer, not only the
 * builder-lock holder, so the holder's canary sees every engine's queries.
 *
 * <p><b>Logs carry counts.</b> Never query text (this class never receives it) and never a vector: an error text from
 * the driver can quote the value that failed, so {@link #sanitize} masks any bracketed run of numbers.
 */
public final class PciQueryRing {

    private static final Logger log = LoggerFactory.getLogger(PciQueryRing.class);

    /** Queue bound (provisional; about 4 MB at 1024 dimensions). */
    public static final int QUEUE_CAPACITY = 1024;
    /** The writer wakes early when this many entries are waiting (provisional). */
    public static final int FLUSH_AT = 64;
    /** The writer's longest sleep (provisional). */
    public static final Duration FLUSH_EVERY = Duration.ofSeconds(30);
    /** Name of the writer thread; a thread dump and the stop test look for it. */
    static final String THREAD_NAME = "pci-ring-writer";
    private static final int MAX_ERROR_CHARS = 200;
    /** A bracketed run of numbers and commas, closed or cut off: a pgvector literal or an array of floats. */
    private static final Pattern NUMBER_RUN = Pattern.compile("\\[[-+0-9.eE,\\s]{6,}\\]?");

    private record Entry(String tenant, String collection, String model, float[] vector) { }

    private final TenantScope scope;
    private final PciIndexSet validIndexes;
    private final ArrayBlockingQueue<Entry> queue;
    private final int flushAt;
    private final long flushEveryNanos;
    private final AtomicLong dropped = new AtomicLong();
    private final AtomicLong seen = new AtomicLong();
    private final Object flushLock = new Object();
    private final Object lifecycle = new Object();
    private volatile Thread writer;       // set once by start()
    private volatile boolean stopped;

    public PciQueryRing(TenantScope scope, PciIndexSet validIndexes) {
        this(scope, validIndexes, QUEUE_CAPACITY, FLUSH_AT, FLUSH_EVERY);
    }

    /** As above with the bounds injected (tests; a production engine takes the provisional defaults). */
    public PciQueryRing(TenantScope scope, PciIndexSet validIndexes, int capacity, int flushAt, Duration flushEvery) {
        this.scope = Objects.requireNonNull(scope, "scope");
        this.validIndexes = Objects.requireNonNull(validIndexes, "validIndexes");
        this.queue = new ArrayBlockingQueue<>(capacity);
        this.flushAt = flushAt;
        this.flushEveryNanos = flushEvery.toNanos();
    }

    // -- admission, on the request thread, after the response is sent ---------------------------------------------

    /**
     * Hand an admitted query vector to the writer. Returns at once and never throws: a ring failure is never a search
     * failure. Does nothing when {@code query} names no collection or the collection has no valid per-collection index.
     */
    public void admit(String tenant, QueryVector query) {
        try {
            admitUnchecked(tenant, query);
        } catch (RuntimeException e) {
            log.warn("event=pci_ring_admit_failed error_class={}", e.getClass().getSimpleName());
        } finally {
            seen.incrementAndGet();
        }
    }

    /** Calls of {@link #admit} that have finished, whatever they decided; a test waits on it before it reads the queue. */
    public long admissionsSeen() {
        return seen.get();
    }

    private void admitUnchecked(String tenant, QueryVector query) {
        if (query == null || query.collection() == null || query.vector() == null || tenant == null || stopped) {
            return;
        }
        if (!validIndexes.hasValidIndex(query.model(), tenant, query.collection())) {
            return;
        }
        Entry entry = new Entry(tenant, query.collection(), query.model(), query.vector().clone());
        if (!queue.offer(entry)) {
            dropped.incrementAndGet();
            return;
        }
        if (queue.size() >= flushAt) {
            Thread w = writer;
            if (w != null) {
                LockSupport.unpark(w);
            }
        }
    }

    /** Entries waiting for the writer. */
    public int queued() {
        return queue.size();
    }

    // -- the writer -----------------------------------------------------------------------------------------------

    /** Start the writer. Idempotent. */
    public void start() {
        synchronized (lifecycle) {
            if (writer != null || stopped) {
                return;
            }
            Thread t = new Thread(this::run, THREAD_NAME);
            t.setDaemon(true);
            writer = t;
            t.start();
        }
        log.info("event=pci_ring_writer_started capacity={} flush_at={} flush_every_seconds={}",
            queue.remainingCapacity() + queue.size(), flushAt, flushEveryNanos / 1_000_000_000L);
    }

    /** End the writer without waiting for it and without flushing: what is still queued is lost, as designed. */
    public void stop() {
        Thread t;
        synchronized (lifecycle) {
            stopped = true;
            t = writer;
        }
        if (t != null) {
            LockSupport.unpark(t);
        }
    }

    private void run() {
        while (!stopped) {
            long deadline = System.nanoTime() + flushEveryNanos;
            while (!stopped && queue.size() < flushAt) {
                long remaining = deadline - System.nanoTime();
                if (remaining <= 0) {
                    break;
                }
                LockSupport.parkNanos(this, remaining);
            }
            if (stopped) {
                return;
            }
            try {
                flushNow();
            } catch (Throwable t) {
                // flushNow catches a database failure itself; this is the net under the thread.
                log.error("event=pci_ring_writer_error error_class={}", t.getClass().getName());
            }
        }
    }

    /**
     * Drain the queue and write it, on the calling thread: the writer's body, and the way a test flushes. One call of
     * the function per tenant. Returns the number of entries the database reports it wrote.
     */
    public int flushNow() {
        synchronized (flushLock) {
            List<Entry> batch = new ArrayList<>(Math.min(queue.size(), QUEUE_CAPACITY));
            queue.drainTo(batch);
            long drops = dropped.getAndSet(0);
            if (batch.isEmpty()) {
                if (drops > 0) {
                    log.warn("event=pci_ring_flush tenants=0 entries=0 written=0 skipped=0 failed=0 dropped={}", drops);
                }
                return 0;
            }
            Map<String, List<Entry>> byTenant = new LinkedHashMap<>();
            for (Entry e : batch) {
                byTenant.computeIfAbsent(e.tenant(), k -> new ArrayList<>()).add(e);
            }
            int written = 0;
            int failed = 0;
            for (Map.Entry<String, List<Entry>> t : byTenant.entrySet()) {
                int n = writeTenant(t.getKey(), t.getValue());
                if (n < 0) {
                    failed += t.getValue().size();
                } else {
                    written += n;
                }
            }
            int skipped = batch.size() - failed - written;
            log.info("event=pci_ring_flush tenants={} entries={} written={} skipped={} failed={} dropped={}",
                byTenant.size(), batch.size(), written, skipped, failed, drops);
            return written;
        }
    }

    /** @return entries written, or -1 when the call failed (logged, batch dropped) */
    private int writeTenant(String tenant, List<Entry> entries) {
        try {
            int n = entries.size();
            String[] collections = new String[n];
            String[] models = new String[n];
            String[] vectors = new String[n];
            byte[][] digests = new byte[n][];
            for (int i = 0; i < n; i++) {
                Entry e = entries.get(i);
                collections[i] = e.collection();
                models[i] = e.model();
                vectors[i] = Vector.of(e.vector()).toString();
                digests[i] = digest(e.vector());
            }
            Integer written = scope.withTenant(tenant, ctx ->
                Routines.canaryQueryRingUpsert(ctx.configuration(), tenant, collections, models, vectors, digests));
            return written == null ? 0 : written;
        } catch (RuntimeException e) {
            log.warn("event=pci_ring_write_failed tenant={} entries={} error=\"{}\"", tenant, entries.size(),
                sanitize(e));
            return -1;
        }
    }

    // -- digest and log text --------------------------------------------------------------------------------------

    /**
     * SHA-256 of the vector's values in order, each as the four bytes of its IEEE 754 binary32 bits, big-endian: the
     * ring's dedupe key. The same vector in one ring is one row.
     */
    public static byte[] digest(float[] vector) {
        ByteBuffer buf = ByteBuffer.allocate(vector.length * Float.BYTES);
        for (float f : vector) {
            buf.putFloat(f);
        }
        try {
            return MessageDigest.getInstance("SHA-256").digest(buf.array());
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is a required JDK algorithm", e);
        }
    }

    /**
     * The exception's class and a bounded message with every bracketed run of numbers masked: PostgreSQL quotes the
     * offending value in some errors, and the value here would be a vector.
     */
    static String sanitize(Throwable e) {
        String msg = e.getMessage() == null ? "" : e.getMessage().replace('\n', ' ').replace('"', '\'');
        msg = NUMBER_RUN.matcher(msg).replaceAll("[vector]");
        if (msg.length() > MAX_ERROR_CHARS) {
            msg = msg.substring(0, MAX_ERROR_CHARS);
        }
        return e.getClass().getSimpleName() + ": " + msg;
    }
}
