package com.sentimentai.ai_overlay.overlay

/**
 * Sprint 10.1 §15 — the ONE central overlay runtime state model.
 *
 * Wire values are shared verbatim with Flutter's `OverlayRuntimeState`
 * (lib/core/platform/overlay_platform.dart); change both sides together.
 *
 * DISABLED            service stopped, permission granted (startable)
 * PERMISSION_REQUIRED overlay permission missing/revoked
 * STARTING            start requested, service not yet confirmed
 * ACTIVE              service running and the test overlay window exists
 * STOPPING            stop requested, teardown not yet confirmed
 * ERROR               last start failed; recoverable by retrying
 */
enum class OverlayRuntimeState(val wire: String) {
    DISABLED("DISABLED"),
    PERMISSION_REQUIRED("PERMISSION_REQUIRED"),
    STARTING("STARTING"),
    ACTIVE("ACTIVE"),
    STOPPING("STOPPING"),
    ERROR("ERROR"),
}
