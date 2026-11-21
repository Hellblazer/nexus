// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.time.Clock;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * The heavy work this engine is doing right now (nexus-f3yqd.3), for {@code GET /v1/status}'s {@code boot_work}: one
 * entry per piece, so an operator or a client can see what runs, how far along it is and whether stopping the engine
 * is safe. Empty when the engine is idle.
 *
 * <p>A producer calls {@link #register} when the work starts, {@link #progress} and {@link #step} as it advances, and
 * {@link #finish} when it ends, in a {@code finally}. The registry sends no durations: the entry carries
 * {@code started_at} and counts, and the client derives rate and time left from them.
 *
 * <p>Thread-safe: producers and the status route run on different threads. Every method takes one lock for a few map
 * operations; a status read is {@link #snapshot()}, a copy of immutable entries, so a route never sees an entry change
 * under it and never holds the lock while it renders. {@link #progress}, {@link #step} and {@link #finish} on an id
 * that is not registered do nothing, so a sampler that fires once after its work has finished cannot bring the entry
 * back. Constructed once by {@code Main} and handed to the producers and the status route: no singleton.
 */
public final class BootWorkRegistry {

    /**
     * An inner level of progress (nexus-f3yqd.3): the same shape as the entry's own {@code what}, {@code done},
     * {@code total} and {@code unit}. A null count says the producer does not know it yet.
     */
    public record Step(String what, Long done, Long total, String unit) { }

    /**
     * One piece of work. {@code done}, {@code total} and {@code unit} are null when the producer has no count;
     * {@code step} is null when there is no inner level.
     *
     * @param id         a stable name, such as {@code pci-reconcile}; a client keys on it
     * @param what       what the work is, in words an operator reads
     * @param startedAt  when {@link #register} was called
     * @param safeToStop whether stopping the engine now loses nothing that cannot be redone
     * @param onStop     what stopping the engine does to this work
     * @param reasonClass nexus-f3yqd.2.23, [additive]: a fixed class of why the work is held ({@code "missing_prerequisite"}:
     *                   a data move blocked because an extension or library it needs is not installed), for a client that
     *                   must tell it from other blocks without reading {@code what}; null for every other entry, and then
     *                   absent from the wire
     */
    public record Entry(String id, String what, Instant startedAt, Long done, Long total, String unit,
                        boolean safeToStop, String onStop, Step step, String reasonClass) {
        public Entry(String id, String what, Instant startedAt, Long done, Long total, String unit, boolean safeToStop,
                     String onStop, Step step) {
            this(id, what, startedAt, done, total, unit, safeToStop, onStop, step, null);
        }
    }

    private final Clock clock;
    private final Object lock = new Object();
    /** Registration order, so the status lists work in the order it started. Guarded by {@link #lock}. */
    private final Map<String, Entry> entries = new LinkedHashMap<>();

    public BootWorkRegistry(Clock clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    private volatile java.util.function.Supplier<String> rdr229Completion;

    /**
     * RDR-229 P3.1 (fix round A, critique B1), [additive]: the source of {@code /v1/status}'s {@code rdr229_completion} field,
     * a JSON object rendered from memory. It is held here, beside {@code boot_work}, because the status route already reads this
     * registry, but it is NOT work and is never an entry: the three-gate state waits by design until P3.4, and an entry for it
     * would count as work in every idle rule that reads {@code boot_work}. Null unbinds.
     */
    public void rdr229Completion(java.util.function.Supplier<String> source) {
        this.rdr229Completion = source;
    }

    /** The {@code rdr229_completion} JSON object, or null when no check is bound or it has not read yet. */
    public String rdr229Completion() {
        var source = rdr229Completion;
        return source == null ? null : source.get();
    }

    /**
     * Announce work. An id already registered is replaced, as a new run of the same work: it keeps its place in the
     * order and gets a new {@code startedAt} and no step.
     */
    public void register(String id, String what, Long done, Long total, String unit, boolean safeToStop,
                         String onStop) {
        Objects.requireNonNull(id, "id");
        Entry entry = new Entry(id, Objects.requireNonNull(what, "what"), clock.instant(), done, total, unit,
            safeToStop, Objects.requireNonNull(onStop, "onStop"), null, null);
        synchronized (lock) {
            entries.put(id, entry);
        }
    }

    /** Set the entry's {@code done} and {@code total}; a null is "not known". */
    public void progress(String id, Long done, Long total) {
        synchronized (lock) {
            Entry old = entries.get(id);
            if (old != null) {
                entries.put(id, new Entry(old.id(), old.what(), old.startedAt(), done, total, old.unit(),
                    old.safeToStop(), old.onStop(), old.step(), old.reasonClass()));
            }
        }
    }

    /** Set the entry's inner level of progress; null clears it. */
    public void step(String id, Step step) {
        synchronized (lock) {
            Entry old = entries.get(id);
            if (old != null) {
                entries.put(id, new Entry(old.id(), old.what(), old.startedAt(), old.done(), old.total(),
                    old.unit(), old.safeToStop(), old.onStop(), step, old.reasonClass()));
            }
        }
    }

    /**
     * Replace what an already registered entry says, in one step: the words, the counts and the inner level, keeping its
     * place, its {@code startedAt}, its unit and its stop notes. For work whose state changes while it runs (a data move
     * goes from working to blocked and back), where {@link #register} would restart the clock. Does nothing for an id that
     * is not registered.
     */
    public void update(String id, String what, Long done, Long total, Step step) {
        update(id, what, done, total, step, null);
    }

    /** As {@link #update(String, String, Long, Long, Step)}, and sets the entry's {@code reasonClass} (null clears it). */
    public void update(String id, String what, Long done, Long total, Step step, String reasonClass) {
        Objects.requireNonNull(what, "what");
        synchronized (lock) {
            Entry old = entries.get(id);
            if (old != null) {
                entries.put(id, new Entry(old.id(), what, old.startedAt(), done, total, old.unit(), old.safeToStop(),
                    old.onStop(), step, reasonClass));
            }
        }
    }

    /** End the work: the entry leaves the list. Idempotent. */
    public void finish(String id) {
        synchronized (lock) {
            entries.remove(id);
        }
    }

    /** The entries now, in registration order, as an immutable list of immutable entries. */
    public List<Entry> snapshot() {
        synchronized (lock) {
            return List.copyOf(entries.values());
        }
    }
}
