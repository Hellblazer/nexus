// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.BootWorkRegistry;

import java.util.List;
import java.util.function.Supplier;

/**
 * The three {@code /v1/status} fields that read the boot-work registry, bound in one place: {@code boot_work} (the heavy work in
 * flight), {@code rdr229_completion} (the completion check's state) and {@code data_moves_need_operator} (the data moves that need
 * a person). The registry is bound late, after the route is built, so each supplier reads it per request and answers empty (or
 * null, for the completion) while it is not there. Kept out of {@code NexusService}, which is at its size ceiling.
 */
public final class StatusBootWork {

    private StatusBootWork() { /* static */ }

    /** Binds the three suppliers to the registry {@code bootWork} holds now, and returns the handler. */
    public static StatusHandler bind(StatusHandler status, Supplier<BootWorkRegistry> bootWork) {
        return status
            .bootWork(() -> { var w = bootWork.get(); return w == null ? List.of() : w.snapshot(); })
            .rdr229Completion(() -> { var w = bootWork.get(); return w == null ? null : w.rdr229Completion(); })
            .dataMovesNeedOperator(() -> { var w = bootWork.get(); return w == null ? List.of() : w.dataMovesNeedOperator(); });
    }
}
