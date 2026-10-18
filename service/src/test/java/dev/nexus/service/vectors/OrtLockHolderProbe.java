// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

/**
 * nexus-1g32a -- child-process entry point for {@code OrtNativeDirTest}: takes the file lock named by
 * {@code args[0]} (so a different process, not just a different channel, holds it), prints
 * {@code LOCKED}, and keeps it until its stdin reaches end of file or it is killed.
 */
public final class OrtLockHolderProbe {

    public static void main(String[] args) throws Exception {
        try (FileChannel ch = FileChannel.open(Path.of(args[0]),
                StandardOpenOption.CREATE, StandardOpenOption.WRITE);
             FileLock ignored = ch.lock()) {
            System.out.println("LOCKED");
            System.out.flush();
            while (System.in.read() != -1) {
                // hold
            }
        }
    }

    private OrtLockHolderProbe() {}
}
