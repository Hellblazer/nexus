// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.ArrayList;
import java.util.List;

/**
 * nexus-o5xyx.3 — child-process entry point for {@link OrtRunShutdownSafetyTest}.
 *
 * <p>Installs the signal gate first (as {@code Main} does), builds the real
 * {@link Bge768Embedder}, then starts {@code threads} workers that call
 * {@code embed()} in a loop, so native {@code session.run()} is in flight almost
 * all the time. Prints {@code RUN_BEGIN} once the first inference has returned,
 * so the parent knows the loop is hot before it sends SIGTERM.
 *
 * <p>Usage: {@code [--no-gate] [threads]}. {@code --no-gate} skips the gate
 * install and reproduces an ungated process (manual red evidence only).
 */
public final class OrtRunShutdownProbe {

    public static void main(String[] args) throws Exception {
        boolean gate = true;
        int threads = 4;
        for (String a : args) {
            if (a.equals("--no-gate")) gate = false;
            else threads = Integer.parseInt(a);
        }
        if (gate) OrtInitGate.process().installSignalHandlers();

        Bge768Embedder embedder = new Bge768Embedder();
        List<String> batch = new ArrayList<>();
        String text = "The quick brown fox jumps over the lazy dog while the engine embeds. ".repeat(20);
        for (int i = 0; i < 16; i++) batch.add(i + " " + text);

        embedder.embed(batch);
        System.out.println("RUN_BEGIN");
        System.out.flush();

        for (int t = 0; t < threads; t++) {
            Thread w = new Thread(() -> {
                while (true) {
                    try {
                        embedder.embed(batch);
                    } catch (RuntimeException e) {
                        // Refused during shutdown: stop this worker, keep the process alive
                        // so the exit is the signal handler's, never ours.
                        System.out.println("RUN_REFUSED " + e.getClass().getSimpleName());
                        System.out.flush();
                        return;
                    }
                }
            }, "embed-" + t);
            w.start();
        }
        Thread.currentThread().join();
    }

    private OrtRunShutdownProbe() {}
}
