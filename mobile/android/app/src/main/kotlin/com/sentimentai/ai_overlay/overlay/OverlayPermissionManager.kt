package com.sentimentai.ai_overlay.overlay

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings

/**
 * Sprint 10.1 §7 — the SINGLE source of truth for overlay permission.
 *
 * Nothing else in the app calls `Settings.canDrawOverlays` directly; both the
 * controller and the Flutter bridge go through this manager so permission
 * state can never diverge between layers (§25).
 */
class OverlayPermissionManager(private val context: Context) {

    /** Current "Display over other apps" grant. */
    fun hasPermission(): Boolean = Settings.canDrawOverlays(context)

    /**
     * System screen for this app's overlay permission. Package-scoped so the
     * user lands directly on THIS app's toggle (§7.3).
     */
    fun createRequestIntent(): Intent =
        Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse("package:${context.packageName}"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    /**
     * Opens the system permission screen. Returns false when no activity can
     * handle the intent (handled as a soft failure, never a crash).
     */
    fun openSettings(): Boolean = try {
        context.startActivity(createRequestIntent())
        true
    } catch (_: ActivityNotFoundException) {
        OverlayLog.event("overlay_error", "permission_settings_unavailable")
        false
    }
}
