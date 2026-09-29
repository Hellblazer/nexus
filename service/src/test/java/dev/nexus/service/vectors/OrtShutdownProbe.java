// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * nexus-o5xyx.1 — child-process entry point for {@link OrtShutdownSafetyTest}.
 *
 * <p>Mirrors {@code Main}'s ordering exactly where it matters: install the
 * shutdown hook FIRST, then construct the real {@link Bge768Embedder} on the
 * main thread (native {@code OrtSession} creation), then park. Prints
 * {@code INIT_BEGIN} immediately before the constructor and {@code INIT_DONE}
 * immediately after, so the parent can time a SIGTERM into the constructor and
 * can tell whether the kill landed mid-init.
 *
 * <p>{@code --no-hook} skips the hook install, which reproduces the
 * pre-fix process (used for manual red evidence, never by the test).
 */
public final class OrtShutdownProbe {

    public static void main(String[] args) throws Exception {
        if (!(args.length > 0 && args[0].equals("--no-hook"))) {
            OrtInitGate.process().installShutdownHook();
        }
        System.out.println("INIT_BEGIN");
        System.out.flush();
        try {
            new Bge768Embedder();
        } catch (OrtInitGate.ShutdownInProgressException e) {
            System.out.println("INIT_REFUSED");
            System.out.flush();
            Thread.currentThread().join();
            return;
        }
        System.out.println("INIT_DONE");
        System.out.flush();
        Thread.currentThread().join();
    }

    private OrtShutdownProbe() {}
}
