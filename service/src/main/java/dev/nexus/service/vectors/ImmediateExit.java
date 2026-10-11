// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.lang.foreign.FunctionDescriptor;
import java.lang.foreign.Linker;
import java.lang.foreign.ValueLayout;
import java.lang.invoke.MethodHandle;

/**
 * nexus-y6aj0 — end the process with a status and nothing else: libc {@code _exit}.
 *
 * <p>{@code Runtime.halt} is not that. It skips Java shutdown hooks, but HotSpot's
 * halt ends in libc {@code exit}, which runs {@code atexit} handlers and the static
 * destructors of every loaded library. With a thread still inside ONNX Runtime
 * that tears the library's globals down under it. Measured on Linux amd64
 * (OpenJDK 25.0.4, ORT 1.20.0): a halt during {@code OrtSession} creation exited
 * 143 and still wrote an {@code hs_err} file (SIGSEGV in libonnxruntime on the
 * creating thread) in 2 of 3 eleven-point sweeps. {@code _exit} runs none of it.
 *
 * <p>The call goes through {@code java.lang.foreign}. Where that is unavailable (a
 * native image built without the downcall registered, a platform with no linker,
 * a missing symbol) it logs {@code event=ort_init_immediate_exit_fallback} and
 * falls back to {@code Runtime.halt}.
 */
final class ImmediateExit {

    private static final Logger log = LoggerFactory.getLogger(ImmediateExit.class);

    private ImmediateExit() {}

    /** Does not return. */
    static void exit(int status) {
        try {
            Linker linker = Linker.nativeLinker();
            MethodHandle underscoreExit = linker.downcallHandle(
                    linker.defaultLookup().find("_exit").orElseThrow(),
                    FunctionDescriptor.ofVoid(ValueLayout.JAVA_INT));
            underscoreExit.invokeExact(status);
        } catch (Throwable t) {
            log.warn("event=ort_init_immediate_exit_fallback using=runtime_halt error=\"{}\"", t.toString());
        }
        Runtime.getRuntime().halt(status);
    }
}
