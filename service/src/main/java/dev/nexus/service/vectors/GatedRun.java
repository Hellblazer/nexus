// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.onnxruntime.OrtException;
import ai.onnxruntime.OrtSession;

/**
 * nexus-o5xyx.3 — one {@code session.run()} held inside the {@link OrtInitGate},
 * cancellable when shutdown begins.
 *
 * <p>Open it BEFORE the first tensor is created and let it close AFTER the last
 * tensor is released (declare it as the outermost resource), then pass
 * {@link #options()} to {@code session.run}. On shutdown the gate sets the run's
 * terminate flag; ORT returns from the run at its next kernel boundary with an
 * {@link OrtException}, which {@link #cancelledOr} turns into
 * {@link OrtInitGate.ShutdownInProgressException}.
 */
public final class GatedRun implements AutoCloseable {

    private final OrtInitGate.Scope scope;
    private final OrtSession.RunOptions options;

    /**
     * @throws OrtInitGate.ShutdownInProgressException if shutdown has begun; nothing
     *         native was touched
     */
    public static GatedRun open(String what) throws OrtException {
        OrtInitGate.Scope scope = OrtInitGate.process().enter(what);
        OrtSession.RunOptions options = null;
        try {
            options = new OrtSession.RunOptions();
            GatedRun run = new GatedRun(scope, options);
            scope.onShutdown(run::terminate);
            return run;
        } catch (OrtException | RuntimeException e) {
            if (options != null) options.close();
            scope.close();
            throw e;
        }
    }

    private GatedRun(OrtInitGate.Scope scope, OrtSession.RunOptions options) {
        this.scope = scope;
        this.options = options;
    }

    /** The run options to pass to {@code session.run}. */
    public OrtSession.RunOptions options() {
        return options;
    }

    /** Runs under the gate's lock, before the options can be released. */
    private void terminate() {
        try {
            options.setTerminate(true);
        } catch (OrtException e) {
            throw new IllegalStateException("could not set ORT run terminate flag", e);
        }
    }

    /**
     * The exception to throw for a failed run: {@link OrtInitGate.ShutdownInProgressException}
     * when shutdown cancelled it, otherwise {@code e} unchanged.
     */
    public Exception cancelledOr(OrtException e) {
        if (!scope.cancelled()) return e;
        var refused = new OrtInitGate.ShutdownInProgressException(
                "native inference cancelled: shutdown in progress");
        refused.initCause(e);
        return refused;
    }

    @Override
    public void close() {
        try {
            scope.onShutdown(null);   // the canceller must never see released options
            options.close();
        } finally {
            scope.close();
        }
    }
}
