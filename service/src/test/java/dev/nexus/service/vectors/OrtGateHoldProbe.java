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
 * milliseconds (default 1500).
 */
public final class OrtGateHoldProbe {

    public static void main(String[] args) throws Exception {
        long holdMs = args.length > 0 ? Long.parseLong(args[0]) : 1500L;
        OrtInitGate gate = OrtInitGate.process();
        gate.installSignalHandlers();
        OrtInitGate.Scope scope = gate.enter("hold-probe");
        System.out.println("HOLDING");
        System.out.flush();
        Thread.sleep(holdMs);
        System.out.println("RELEASED");
        System.out.flush();
        scope.close();
        // Stay alive so only the deferred exit can end the process.
        Thread.currentThread().join();
    }

    private OrtGateHoldProbe() {}
}
