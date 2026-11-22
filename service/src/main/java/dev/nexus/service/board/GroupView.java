// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import java.util.List;
import java.util.Map;

/**
 * The live group that serves a topic, as the decision reads it: its leader, its members in the order their
 * memberships were created, the acknowledgement deadline in whole minutes (the group default, with the per-topic
 * overrides), and the stall factor: an acknowledged red still open after {@code stallFactor} times the deadline,
 * counted from the last acknowledgement, is escalated (RDR-252 audit S1c).
 */
public record GroupView(String name, String leader, List<String> members, int ackDeadlineMinutes,
                        Map<String, Integer> deadlineByTopic, int stallFactor) {

    /** The stall factor of a group record that sets none. */
    public static final int DEFAULT_STALL_FACTOR = 3;

    /** A group that takes the default stall factor. */
    public GroupView(String name, String leader, List<String> members, int ackDeadlineMinutes,
                     Map<String, Integer> deadlineByTopic) {
        this(name, leader, members, ackDeadlineMinutes, deadlineByTopic, DEFAULT_STALL_FACTOR);
    }

    /** The deadline of a topic family in minutes: its override, else the group default. */
    public int deadlineMinutes(String baseTopic) {
        Integer override = deadlineByTopic == null ? null : deadlineByTopic.get(baseTopic);
        return override != null ? override : ackDeadlineMinutes;
    }

    public boolean isMember(String identity) {
        return members.contains(identity);
    }
}
