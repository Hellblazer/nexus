// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * nexus-o5xyx.1 — MODEL-FREE child-process entry point for {@link OrtInitGateSignalTest}.
 *
 * <p>Installs the REAL handlers ({@code sun.misc.Signal}, not a fake installer),
 * holds a gate scope open as a stand-in for a native model init, then releases
 * it and parks. A SIGTERM sent while the scope is held must not exit the
 * process until {@code RELEASED} has been printed. Arguments: hold time in
 * milliseconds (default 1500). A second argument {@code raise-term} makes the probe deliver SIGTERM to
 * itself ({@code sun.misc.Signal.raise}) {@value #RAISE_AFTER_MS} ms after {@code HOLDING}: the only way
 * to send a real TERM to a JVM on Windows, where there is no {@code kill}.
 */
public final class OrtGateHoldProbe {

    static final long RAISE_AFTER_MS = 100;

    public static void main(String[] args) throws Exception {
        long holdMs = args.length > 0 ? Long.parseLong(args[0]) : 1500L;
        OrtInitGate gate = OrtInitGate.process();
        gate.installSignalHandlers();
        OrtInitGate.Scope scope = gate.enter("hold-probe");
        System.out.println("HOLDING");
        System.out.flush();
        if (args.length > 1 && args[1].equals("raise-term")) {
            Thread raiser = new Thread(() -> {
                try {
                    Thread.sleep(RAISE_AFTER_MS);
                } catch (InterruptedException e) {
                    return;
                }
                sun.misc.Signal.raise(new sun.misc.Signal("TERM"));
            }, "probe-raise-term");
            raiser.setDaemon(true);
            raiser.start();
        }
        Thread.sleep(holdMs);
        System.out.println("RELEASED");
        System.out.flush();
        scope.close();
        // Stay alive so only the deferred exit can end the process.
        Thread.currentThread().join();
    }

    private OrtGateHoldProbe() {}
}
