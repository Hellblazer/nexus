// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.onnxruntime.OrtEnvironment;

/**
 * nexus-8ool4 -- child-process entry point for {@link OrtNativeDirProcessTest}.
 *
 * <p>Does what {@code Main} does before any model loads ({@link OrtNativeDir#applyAtBoot()}, unless
 * {@code --no-fix}), initialises ONNX Runtime for real, prints what it loaded from, then ends the way a
 * SIGKILLed engine does: {@code Runtime.halt}, so no shutdown hook and no {@code deleteOnExit} runs.
 */
public final class OrtNativeDirProbe {

    public static void main(String[] args) {
        boolean fix = !(args.length > 0 && args[0].equals("--no-fix"));
        if (fix) {
            OrtNativeDir.applyAtBoot();
        }
        String version = OrtEnvironment.getEnvironment().getVersion();
        System.out.println("NATIVE_PATH=" + System.getProperty(OrtNativeDir.PROPERTY));
        System.out.println("ORT_VERSION=" + version);
        System.out.flush();
        Runtime.getRuntime().halt(137);
    }

    private OrtNativeDirProbe() {}
}
