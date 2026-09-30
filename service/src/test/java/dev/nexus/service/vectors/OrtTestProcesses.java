// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.io.IOException;

/**
 * nexus-o5xyx.1 — signal a child without {@code Process.destroy()}.
 *
 * <p>{@code Process.destroy()} sends SIGTERM and then CLOSES the parent's ends
 * of the child's stdio, so a reader thread loses everything the child prints
 * while it handles the signal (measured on hellmini: {@code IOException: Stream
 * closed}). These tests exist to observe exactly that output, so they signal
 * with {@code kill -TERM} and leave the streams open.
 */
final class OrtTestProcesses {

    static void sigterm(Process p) throws IOException, InterruptedException {
        Process k = new ProcessBuilder("kill", "-TERM", Long.toString(p.pid()))
                .redirectErrorStream(true).start();
        if (k.waitFor() != 0) {
            throw new IOException("kill -TERM " + p.pid() + " failed: "
                    + new String(k.getInputStream().readAllBytes()));
        }
    }

    private OrtTestProcesses() {}
}
