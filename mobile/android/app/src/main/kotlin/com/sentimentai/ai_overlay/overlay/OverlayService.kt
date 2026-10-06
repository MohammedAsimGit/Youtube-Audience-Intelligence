package com.sentimentai.ai_overlay.overlay

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.view.View
import android.view.WindowManager
import androidx.core.content.ContextCompat
import com.sentimentai.ai_overlay.MainActivity
import com.sentimentai.ai_overlay.R

/**
 * Sprint 10.1 §8/§12/§13 — the Android runtime that owns the overlay window.
 *
 * Responsibilities are strictly Android system work (§8): foreground
 * notification, WindowManager add/remove, lifecycle reporting. NO
 * intelligence/business logic ever lives here.
 *
 * Lifecycle decisions:
 * - [START_NOT_STICKY] (§19): the system never resurrects the overlay in the
 *   background without an explicit user action — restarts are predictable.
 * - Duplicate starts are impossible: Android creates one service instance per
 *   process and [onCreate] adds the window exactly once (§14).
 * - Every exit path (stop, revocation, crash, start failure) funnels through
 *   [onDestroy] so WindowManager references are always released (§13).
 */
class OverlayService : Service() {

    private lateinit var windowManager: WindowManager
    private var badge: View? = null
    private var started = false

    override fun onCreate() {
        super.onCreate()
        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager

        try {
            createNotificationChannel()
            startAsForeground()
        } catch (error: Throwable) {
            // §27: foreground-start failure must not crash the process.
            OverlayLog.event("overlay_start_failed", "foreground")
            OverlayController.instance?.onServiceStartFailed()
            stopSelf()
            return
        }
        started = true
        isRunning = true
        OverlayLog.event("overlay_service_started")

        try {
            badge = TestOverlayBadge.add(windowManager, this)
            OverlayLog.event("overlay_created")
            OverlayController.instance?.onServiceCreated()
        } catch (error: Throwable) {
            OverlayLog.event("overlay_start_failed", "window")
            OverlayController.instance?.onServiceStartFailed()
            stopSelf()
        }
    }

    /**
     * Extra START intents only re-affirm the notification — the badge is
     * created once in [onCreate], so repeated starts can never duplicate the
     * window (§14). No background work happens here (§31).
     */
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_NOT_STICKY

    override fun onDestroy() {
        badge?.let { view ->
            runCatching { windowManager.removeViewImmediate(view) }
                .onFailure { OverlayLog.event("overlay_error", "remove_view") }
            OverlayLog.event("overlay_removed")
            badge = null
        }
        if (started) OverlayLog.event("overlay_service_stopped")
        isRunning = false
        OverlayController.instance?.onServiceStopped()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            NOTIFICATION_CHANNEL_ID,
            getString(R.string.overlay_notification_channel),
            NotificationManager.IMPORTANCE_LOW, // silent, minimal (§12)
        ).apply {
            description = getString(R.string.overlay_notification_channel)
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun startAsForeground() {
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(): android.app.Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            android.app.Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            android.app.Notification.Builder(this)
        }
        return builder
            .setContentTitle(getString(R.string.overlay_notification_title))
            .setContentText(getString(R.string.overlay_notification_text))
            .setSmallIcon(R.drawable.ic_overlay_status)
            .setContentIntent(mainActivityIntent())
            .setOngoing(true)
            .build()
    }

    private fun mainActivityIntent(): android.app.PendingIntent {
        val intent = packageManager.getLaunchIntentForPackage(packageName)
            ?: Intent(this, MainActivity::class.java)
        return android.app.PendingIntent.getActivity(
            this,
            0,
            intent,
            android.app.PendingIntent.FLAG_IMMUTABLE,
        )
    }

    companion object {
        @Volatile
        var isRunning: Boolean = false
            private set

        private const val NOTIFICATION_CHANNEL_ID = "overlay_runtime"
        private const val NOTIFICATION_ID = 41

        /** Idempotent dispatch: false only when the start could not be sent (§27). */
        fun start(context: Context): Boolean {
            if (isRunning) return true // §14 duplicate start = no-op
            return try {
                ContextCompat.startForegroundService(
                    context,
                    Intent(context, OverlayService::class.java),
                )
                true
            } catch (_: IllegalStateException) {
                // Background-start restriction or system refusal (§27).
                OverlayLog.event("overlay_start_failed", "dispatch")
                false
            }
        }

        /** Always safe, even when already stopped (§14). */
        fun stop(context: Context) {
            context.stopService(Intent(context, OverlayService::class.java))
        }
    }
}

/** Production [OverlayServiceGateway] bound to this service. */
class ServiceOverlayGateway(private val context: Context) : OverlayServiceGateway {
    override fun isRunning(): Boolean = OverlayService.isRunning
    override fun start(): Boolean = OverlayService.start(context)
    override fun stop() = OverlayService.stop(context)
}
