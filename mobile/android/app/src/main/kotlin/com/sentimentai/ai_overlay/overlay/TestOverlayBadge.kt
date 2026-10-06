package com.sentimentai.ai_overlay.overlay

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.TextView
import android.widget.Toast
import com.sentimentai.ai_overlay.R

/**
 * Minimal test overlay used by Sprint 10.1 to prove the overlay runtime works.
 *
 * This is intentionally NOT the final AI orb and contains no intelligence logic:
 * it is a small, tappable "AI" badge drawn above other apps so start/stop,
 * permission handling, and touch pass-through can be verified end to end.
 */
object TestOverlayBadge {

    /** Roughly 64dp badge at typical densities; WRAP_CONTENT keeps it minimal. */
    private const val MARGIN_DP = 16

    @SuppressLint("ClickableViewAccessibility", "SetTextI18n")
    fun add(windowManager: WindowManager, context: Context): View {
        val density = context.resources.displayMetrics.density
        val marginPx = (MARGIN_DP * density).toInt()

        val view = TextView(context).apply {
            text = context.getString(R.string.overlay_badge_label)
            setTextColor(Color.WHITE)
            textSize = 13f
            typeface = android.graphics.Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
            val paddingPx = (10 * density).toInt()
            setPadding(paddingPx, paddingPx, paddingPx, paddingPx)
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.argb(210, 24, 20, 40))
                setStroke((2 * density).toInt(), Color.argb(255, 139, 92, 246))
            }
            // Proves the overlay can receive input (Sprint 10.1 interactivity check).
            setOnClickListener {
                Toast.makeText(context, "AI overlay is active", Toast.LENGTH_SHORT).show()
            }
        }

        // (width, height, type, flags, format) — the API-1 constructor; the
        // gravity/x/y fields place the badge bottom-end (minSdk-safe).
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            // Touches outside the badge pass through to the app below.
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.END or Gravity.BOTTOM
            x = marginPx
            y = marginPx
        }

        windowManager.addView(view, params)
        return view
    }
}
