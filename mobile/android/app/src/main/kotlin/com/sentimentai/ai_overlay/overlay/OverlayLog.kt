package com.sentimentai.ai_overlay.overlay

import android.util.Log

/**
 * Sprint 10.1 §28 — structured development logging.
 *
 * One funnel so every native event uses the documented vocabulary:
 * overlay_permission_changed, overlay_service_started, overlay_service_stopped,
 * overlay_created, overlay_removed, overlay_start_failed,
 * overlay_permission_denied (plus overlay_error for bridge-level failures).
 *
 * Never log user data, comment contents, YouTube data, secrets or API keys.
 */
object OverlayLog {
    private const val TAG = "sentiment-ai"

    fun event(name: String, detail: String? = null) {
        if (detail == null) {
            Log.i(TAG, name)
        } else {
            Log.i(TAG, "$name $detail")
        }
    }
}
