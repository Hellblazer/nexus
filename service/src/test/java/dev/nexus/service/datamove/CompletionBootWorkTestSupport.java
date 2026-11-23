// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.util.function.BooleanSupplier;

/**
 * Test-only reach into {@link CompletionBootWork}'s package-private seams (linda's census of 19d19081dc, item 1): the id-readers signal the one-row rename waits
 * for is {@code IdReaders.present()} in production and cannot be replaced by any production code; a test of the one-row regime injects {@code () -> true}
 * here. This class lives in the test source set, so it does not exist in the engine.
 */
public final class CompletionBootWorkTestSupport {

    private CompletionBootWorkTestSupport() { /* static */ }

    /** Makes {@code work} treat the id readers as present ({@code () -> true}) or as {@code present} says. */
    public static void idReadersPresent(CompletionBootWork work, BooleanSupplier present) {
        work.idReadersPresent(present);
    }
}
