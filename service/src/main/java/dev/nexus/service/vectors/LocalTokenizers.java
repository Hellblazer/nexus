// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Function;

/**
 * nexus-17jvq -- the one place the engine builds a DJL HuggingFace tokenizer, and the reason it is one place.
 *
 * <p>DJL's {@code HuggingFaceTokenizer.newInstance} (and {@code Engine.initEngine}) calls
 * {@code ai.djl.util.Ec2Utils.callHome} at most once a day per JVM. Unless {@code OPT_OUT_TRACKING} is set, that dials
 * {@code http://169.254.169.254/latest/api/token} (an IMDSv2 token request, then the instance id, a second token and
 * the region: four connects at start on a box that is no cloud instance) and, on a real EC2 host, sends the instance
 * id and region in a GET to a DJL telemetry bucket. The engine has no AWS SDK; this was the whole source of the
 * link-local connects. A local install must not probe a cloud metadata service.
 *
 * <p>Two switches close DJL's outbound calls: {@code OPT_OUT_TRACKING=true} ends the call-home, and offline mode
 * ({@code DJL_OFFLINE=true} or {@code -Dai.djl.offline=true}) also stops DJL's CUDA JNI library download
 * ({@code LibUtils.downloadJniLib}, which fires on GPU hosts). The engine loads only local tokenizer files, so
 * {@link #goOffline()} sets both, once, at the very start of {@code Main.main}, before any DJL class can load;
 * {@link #build} sets them again as a second line, for a caller that never went through {@code Main}.
 *
 * <p>DJL reads each setting from the environment first and the system property second, and parses the value with
 * {@code Boolean.parseBoolean}. An operator who exports a value keeps it (a property cannot override the
 * environment), but an exported value that is not {@code true} (empty, {@code 1}, {@code yes}) is read by DJL as
 * false, so telemetry and downloads stay on; {@link #goOffline()} logs one warning per such variable per JVM.
 *
 * <p>Only the local ONNX path builds tokenizers (the Voyage/cloud branch of {@code Main} constructs no embedder or
 * reranker that does), so this does not change cloud behaviour.
 */
public final class LocalTokenizers {

    private static final Logger log = LoggerFactory.getLogger(LocalTokenizers.class);

    /** DJL's telemetry opt-out: an environment variable first, a system property of the same name second. */
    public static final String OPT_OUT_PROPERTY = "OPT_OUT_TRACKING";

    /** DJL's offline-mode environment variable. */
    public static final String OFFLINE_ENV = "DJL_OFFLINE";

    /** DJL's offline-mode system property. */
    public static final String OFFLINE_PROPERTY = "ai.djl.offline";

    /** Names already warned about, so a variable is reported once per JVM however many tokenizers are built. */
    private static final Set<String> warned = ConcurrentHashMap.newKeySet();

    private LocalTokenizers() { }

    /**
     * Makes DJL offline and opted out for this JVM, process-wide, unless the operator already chose a value.
     * Idempotent. Call before anything can touch a DJL class.
     */
    public static void goOffline() {
        for (String ignored : goOffline(System::getenv, System.getProperties())) {
            if (!warned.add(ignored)) {
                continue;
            }
            log.warn("event=djl_operator_env_not_true name={} detail=\"DJL parses the exported value as false, so its "
                + "telemetry call-home and library downloads stay on; a system property cannot override the "
                + "environment\"", ignored);
        }
    }

    /**
     * The seam under {@link #goOffline()}: {@code env} and {@code props} are injected so a test does not depend on
     * the shell. Returns the names of exported environment variables whose value DJL will read as false.
     */
    public static List<String> goOffline(Function<String, String> env, Properties props) {
        List<String> notTrue = new ArrayList<>();
        setUnlessChosen(env, props, OPT_OUT_PROPERTY, OPT_OUT_PROPERTY, notTrue);
        setUnlessChosen(env, props, OFFLINE_ENV, OFFLINE_PROPERTY, notTrue);
        return notTrue;
    }

    private static void setUnlessChosen(Function<String, String> env, Properties props, String envName,
                                        String propName, List<String> notTrue) {
        String exported = env.apply(envName);
        if (exported != null) {
            if (!Boolean.parseBoolean(exported)) {
                notTrue.add(envName);
            }
            return;
        }
        if (props.getProperty(propName) == null) {
            props.setProperty(propName, "true");
        }
    }

    /** A truncating, non-padding tokenizer loaded from {@code tokenizerPath}; the callers pad per batch. */
    public static HuggingFaceTokenizer build(String tokenizerPath, int maxLength) throws IOException {
        goOffline();
        return HuggingFaceTokenizer.builder()
                .optTokenizerPath(Path.of(tokenizerPath))
                .optMaxLength(maxLength)
                .optTruncation(true)
                .optPadding(false)
                .build();
    }
}
