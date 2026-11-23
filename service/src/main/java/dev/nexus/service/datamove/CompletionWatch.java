// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;

/**
 * Watches one move while its validation runs with no statement bound, and cancels the statement when the move is paused or superseded
 * meanwhile (the same cancel as a stop, scoped to this move). The statement runs for as long as it takes, so the pause an operator sets for
 * a down-step or a rollback window would otherwise wait for it. A daemon thread reads the move's row on a connection of its own every
 * {@link #INTERVAL_MS}; the cancel goes through {@link DataMoveOrphans#cancelBackend}, which checks the backend is still the one started.
 */
final class CompletionWatch implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CompletionWatch.class);

    /** How often the move's row is read, in milliseconds. */
    static final long INTERVAL_MS = 1_000L;

    private final AdminConnection admin;
    private final String label;
    private final int pid;
    private final String applicationName;
    private final OffsetDateTime backendStart;
    private volatile boolean closed;
    private volatile boolean fired;
    private volatile boolean ended;
    private final Thread thread;
    /** Test seam: runs in the watch's thread after it has read the move's row and before it acts on what it read, where a close can slip in. */
    private final Runnable afterRead;

    private CompletionWatch(AdminConnection admin, String label, int pid, String applicationName, OffsetDateTime backendStart,
                            Runnable afterRead) {
        this.afterRead = afterRead;
        this.admin = admin;
        this.label = label;
        this.pid = pid;
        this.applicationName = applicationName;
        this.backendStart = backendStart;
        this.thread = new Thread(this::run, "data-move-completion-watch");
        thread.setDaemon(true);
    }

    static CompletionWatch start(AdminConnection admin, String label, int pid, String applicationName, OffsetDateTime backendStart) {
        return start(admin, label, pid, applicationName, backendStart, null);
    }

    static CompletionWatch start(AdminConnection admin, String label, int pid, String applicationName, OffsetDateTime backendStart,
                                 Runnable afterRead) {
        CompletionWatch w = new CompletionWatch(admin, label, pid, applicationName, backendStart, afterRead);
        w.thread.start();
        return w;
    }

    /** True when the watch cancelled the statement because the move was paused, superseded or gone. */
    boolean fired() {
        return fired;
    }

    /** True once the watch's thread has left its loop. */
    boolean ended() {
        return ended;
    }

    private void run() {
        try {
            loop();
        } finally {
            ended = true;
        }
    }

    private void loop() {
        while (!closed) {
            try {
                Thread.sleep(INTERVAL_MS);
            } catch (InterruptedException e) {
                return;
            }
            if (closed) {
                return;
            }
            try {
                if (heldOrGone()) {
                    if (closed) {
                        return;   // the validation has ended: the backend may be on its claim now, and must not be cancelled for the old statement
                    }
                    fired = true;
                    DataMoveOrphans.cancelBackend(admin, pid, applicationName, backendStart);
                    return;
                }
            } catch (RuntimeException e) {   // a refused session (the server is unreachable) or a failed read: look again at the next interval
                log.debug("event=data_move_completion_watch_failed label={} cause=\"{}\"", label, FailureHandler.oneLine(e.toString()));
            }
        }
    }

    private boolean heldOrGone() {
        try (var session = DataMoveOrphans.openSession(admin, "watch", DataMoveOrphans.CANCEL_SOCKET_SECONDS, 2_000, 3_000)) {
            var ctx = session.ctx();
            var row = DataMoveRegistry.reload(ctx, label);
            boolean held = row.isEmpty() || row.get().paused()
                || ctx.fetchExists(DSL.table(DSL.name("nexus", "data_moves")), DSL.field(DSL.name("supersedes"), String.class).eq(label));
            Runnable seam = afterRead;
            if (seam != null) {
                seam.run();
            }
            return held;
        }
    }

    /**
     * Ends the watch and WAITS for its thread (bounded): a watch in the middle of reading the move's row must not cancel the pooled connection after
     * the validation has ended and the same session has moved on to the claim, which would be a spurious 57014 there.
     */
    @Override
    public void close() {
        closed = true;
        thread.interrupt();
        try {
            thread.join(JOIN_MS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /**
     * How long close() waits for the thread, in milliseconds: a read of the row on a session of its own plus the cancel statement. Each of the two
     * gives up after 2 s waiting for a lock and 3 s running, and a server that stalls is cut by the session's 5 s network timeout, so the thread
     * ends within these bounds whatever holds a lock on the registry.
     */
    private static final long JOIN_MS = 6_000L;
}
