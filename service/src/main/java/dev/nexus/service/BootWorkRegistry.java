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
     */
    public record Entry(String id, String what, Instant startedAt, Long done, Long total, String unit,
                        boolean safeToStop, String onStop, Step step) { }

    private final Clock clock;
    private final Object lock = new Object();
    /** Registration order, so the status lists work in the order it started. Guarded by {@link #lock}. */
    private final Map<String, Entry> entries = new LinkedHashMap<>();

    public BootWorkRegistry(Clock clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    /**
     * Announce work. An id already registered is replaced, as a new run of the same work: it keeps its place in the
     * order and gets a new {@code startedAt} and no step.
     */
    public void register(String id, String what, Long done, Long total, String unit, boolean safeToStop,
                         String onStop) {
        Objects.requireNonNull(id, "id");
        Entry entry = new Entry(id, Objects.requireNonNull(what, "what"), clock.instant(), done, total, unit,
            safeToStop, Objects.requireNonNull(onStop, "onStop"), null);
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
                    old.safeToStop(), old.onStop(), old.step()));
            }
        }
    }

    /** Set the entry's inner level of progress; null clears it. */
    public void step(String id, Step step) {
        synchronized (lock) {
            Entry old = entries.get(id);
            if (old != null) {
                entries.put(id, new Entry(old.id(), old.what(), old.startedAt(), old.done(), old.total(),
                    old.unit(), old.safeToStop(), old.onStop(), step));
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
