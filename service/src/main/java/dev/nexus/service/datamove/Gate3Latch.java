// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.util.Objects;
import java.util.Optional;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the install's gate 3 latch as the engine reads it AT BOOT, synchronously, before the listener binds. The
 * completion check's own sample is asynchronous and can fail, so an accessor that waited for it would answer "not latched" for a while after a
 * restart, or for good when the read fails, and a reader that goes by the latch could then route to stale names after a one-row rename. This reads
 * the row once, here, and never guesses: a failed read is retried with backoff ({@link #FIRST_BACKOFF} doubling to {@link #MAX_BACKOFF}), and
 * when {@link #BOOT_DEADLINE} passes without an answer {@link #readBlocking} throws, which {@code Main} turns into a failed boot (a supervisor
 * restarts it, as for every other boot step that cannot read the database). An answer of "no row" is an answer: the install has not latched.
 */
public final class Gate3Latch {

    private static final Logger log = LoggerFactory.getLogger(Gate3Latch.class);

    /** The first wait between two failed reads. */
    public static final Duration FIRST_BACKOFF = Duration.ofMillis(500);
    /** The longest wait between two failed reads. */
    public static final Duration MAX_BACKOFF = Duration.ofSeconds(30);
    /** How long a boot keeps retrying before it fails. */
    public static final Duration BOOT_DEADLINE = Duration.ofMinutes(5);

    /** One read of the latch row: empty when there is none, else its {@code pci_off}; throws when it cannot be read. */
    @FunctionalInterface
    public interface Source {
        Optional<Boolean> read();
    }

    /** The pause between two reads; a test replaces it. */
    @FunctionalInterface
    public interface Sleeper {
        void sleep(Duration d) throws InterruptedException;
    }

    private final boolean latched;
    private final boolean pciOff;

    private Gate3Latch(boolean latched, boolean pciOff) {
        this.latched = latched;
        this.pciOff = pciOff && latched;
    }

    /** A latch state given outright (a test, or a caller that read it another way). */
    public static Gate3Latch of(boolean latched, boolean pciOff) {
        return new Gate3Latch(latched, pciOff);
    }

    /** Whether the row exists. */
    public boolean latched() {
        return latched;
    }

    /** Whether the row says gate 3 latched while per-collection indexing was off. */
    public boolean pciOff() {
        return pciOff;
    }

    /** Each attempt's own {@code lock_timeout} and {@code statement_timeout}, in milliseconds (10 s): far under {@link #BOOT_DEADLINE}, so a hung attempt fails and is retried. */
    static final String ATTEMPT_TIMEOUT_MS = "10000";

    /**
     * The production read: the latch row on a short read-only admin session of its own, retried with backoff until it answers or
     * {@link #BOOT_DEADLINE} passes.
     *
     * @throws IllegalStateException when the database could not answer within the deadline: the caller must not serve
     */
    public static Gate3Latch readAtBoot(AdminConnection admin) {
        return readAtBoot(admin, ATTEMPT_TIMEOUT_MS, BOOT_DEADLINE);
    }

    /** {@link #readAtBoot(AdminConnection)} with each attempt's timeouts (milliseconds, as the GUC text) and the deadline given: a test shortens them. */
    static Gate3Latch readAtBoot(AdminConnection admin, String attemptTimeoutMs, Duration deadline) {
        Objects.requireNonNull(admin, "admin");
        return readBlocking(() -> {
            try (AdminDdlSession s = AdminDdlSession.open(admin, new AdminDdlSession.Settings("nexus-rdr229-gate-latch-boot", 0L,
                    "rdr229 gate latch boot read", "nexus", "rdr229_gate_latch", "none"), false)) {
                s.setSession("default_transaction_read_only", "on");
                // one hung attempt (a lock held by a migration, a stalled statement) must not outlive the deadline: the server ends it first
                s.setSession("lock_timeout", attemptTimeoutMs);
                s.setSession("statement_timeout", attemptTimeoutMs);
                return CompletionCheck.readLatch(s.ctx(), CompletionCheck.GATE3);
            }
        }, FIRST_BACKOFF, MAX_BACKOFF, deadline, d -> Thread.sleep(d.toMillis()), Clock.systemUTC());
    }

    /** {@link #readAtBoot} with the source, backoff, deadline, sleeper and clock injected. */
    public static Gate3Latch readBlocking(Source source, Duration first, Duration max, Duration deadline, Sleeper sleeper, Clock clock) {
        Objects.requireNonNull(source, "source");
        var until = clock.instant().plus(deadline);
        Duration wait = first;
        int attempt = 0;
        while (true) {
            attempt++;
            try {
                Optional<Boolean> row = source.read();
                return new Gate3Latch(row.isPresent(), row.orElse(false));
            } catch (RuntimeException e) {
                if (!clock.instant().plus(wait).isBefore(until)) {
                    throw new IllegalStateException("the gate 3 latch could not be read in " + deadline + " (" + attempt
                        + " attempts); not serving on a guess: " + e, e);
                }
                log.warn("event=rdr229_gate3_latch_boot_read_failed attempt={} retry_in_ms={} cause=\"{}\"", attempt, wait.toMillis(), e.toString());
                try {
                    sleeper.sleep(wait);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException("interrupted while waiting to read the gate 3 latch", ie);
                }
                wait = wait.multipliedBy(2).compareTo(max) > 0 ? max : wait.multipliedBy(2);
            }
        }
    }
}
