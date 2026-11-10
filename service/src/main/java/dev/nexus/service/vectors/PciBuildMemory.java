// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.lang.management.ManagementFactory;
import java.util.OptionalInt;
import java.util.function.LongSupplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd (E): the {@code maintenance_work_mem} of a per-collection index build, set on the builder's session
 * only.
 *
 * <p>pgvector builds an HNSW graph in memory while it fits in {@code maintenance_work_mem}, and past that point flushes
 * what it has and inserts the rest on disk, one WAL-logged page change at a time. At PostgreSQL's 64 MB default a
 * 156 MB index took about 0.9 GB of writes and 45.7 s (the debugger's measurement). A parallel build keeps ONE graph in
 * one shared-memory area sized by {@code maintenance_work_mem}, not one per worker, so the bound is the total.
 *
 * <p>The value is {@code min(1 GB, 1/8 of the RAM of the host Postgres runs on)}. The RAM that matters is Postgres's
 * host, which is the engine's own host only when Postgres runs there: the bundled Postgres of a local install. The
 * engine has no mode flag, so "Postgres is on this host" is read from the builder's JDBC URL (a loopback name or a
 * socket); a managed Postgres in cloud mode is on another host, and without the override the build keeps Postgres's own
 * setting. {@code NX_SEARCH_PCI_BUILD_MEM_MB} wins in either case.
 */
final class PciBuildMemory {

    /** The cap, in MB. */
    static final int CAP_MB = 1024;
    /** One eighth of the host's RAM. */
    static final int RAM_DIVISOR = 8;
    /** Never below this: PostgreSQL's own default is 64 MB and a tiny host should not go lower than a sane graph. */
    static final int FLOOR_MB = 16;

    private static final Pattern HOST = Pattern.compile("^jdbc:postgresql://(\\[[^\\]]*\\]|[^/:?,]*)");

    private PciBuildMemory() { }

    /**
     * @param overrideMb  {@code NX_SEARCH_PCI_BUILD_MEM_MB}, already validated, or {@code null} when unset
     * @param pgOnThisHost whether Postgres runs on the machine {@code totalRamBytes} describes
     * @param totalRamBytes total RAM of this machine in bytes, read only when needed; {@code <= 0} when unreadable
     * @return the MB to set, or empty to keep Postgres's own setting
     */
    static OptionalInt resolve(Integer overrideMb, boolean pgOnThisHost, LongSupplier totalRamBytes) {
        if (overrideMb != null) {
            return OptionalInt.of(overrideMb);
        }
        if (!pgOnThisHost) {
            return OptionalInt.empty();
        }
        long ram = totalRamBytes.getAsLong();
        if (ram <= 0) {
            return OptionalInt.empty();
        }
        long fractionMb = ram / RAM_DIVISOR / (1L << 20);
        return OptionalInt.of((int) Math.max(FLOOR_MB, Math.min(CAP_MB, fractionMb)));
    }

    /** True when {@code jdbcUrl} names a loopback host or no host at all (a Unix socket, or the driver's localhost). */
    static boolean pgOnThisHost(String jdbcUrl) {
        if (jdbcUrl == null) {
            return false;
        }
        if (jdbcUrl.startsWith("jdbc:postgresql:") && !jdbcUrl.startsWith("jdbc:postgresql://")) {
            return true;   // jdbc:postgresql:db is localhost
        }
        Matcher m = HOST.matcher(jdbcUrl);
        if (!m.find()) {
            return false;
        }
        String host = m.group(1).toLowerCase(java.util.Locale.ROOT);
        return host.isEmpty() || host.equals("localhost") || host.equals("[::1]")
            || host.matches("127(\\.\\d{1,3}){3}");
    }

    /** This machine's total RAM (the container's limit when the JVM runs under one); 0 when it cannot be read. */
    static LongSupplier hostRamBytes() {
        return () -> {
            try {
                if (ManagementFactory.getOperatingSystemMXBean() instanceof com.sun.management.OperatingSystemMXBean os) {
                    return os.getTotalMemorySize();
                }
            } catch (RuntimeException | LinkageError e) {
                // fall through: unreadable
            }
            return 0L;
        };
    }
}
