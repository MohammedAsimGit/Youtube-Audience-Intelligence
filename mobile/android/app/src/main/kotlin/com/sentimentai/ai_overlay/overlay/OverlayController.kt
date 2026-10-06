package com.sentimentai.ai_overlay.overlay

import android.content.Context

/**
 * Seam over the Android service so the state machine is unit-testable on the
 * JVM (no Android framework required). Production wires [ServiceOverlayGateway].
 */
interface OverlayServiceGateway {
    fun isRunning(): Boolean
    /** Returns false only when the start could not even be dispatched (§27). */
    fun start(): Boolean
    fun stop()
}

/** Bridge-level command outcome (§27): failures carry a stable error code. */
sealed interface OverlayCommandResult {
    data class Ok(val state: OverlayRuntimeState) : OverlayCommandResult
    data class Failed(val code: String, val message: String) : OverlayCommandResult
}

/**
 * Sprint 10.1 §13/§14/§15 — the ONE source of truth for overlay runtime state.
 *
 * Pure Kotlin by design: Android specifics arrive through injected seams
 * ([OverlayServiceGateway], permission/settings lambdas, log sink) so every
 * lifecycle rule below is covered by plain JVM unit tests (§29).
 *
 * Guarantees:
 * - Idempotent start: duplicate calls never start a second service (§14).
 * - Idempotent stop: stopping an already-stopped runtime is a safe no-op (§14).
 * - Permission is checked in exactly one place ([OverlayPermissionManager] on
 *   the Android side; this controller owns every STATE transition) (§25).
 * - Revocation while ACTIVE stops the service and lands on
 *   PERMISSION_REQUIRED (§27).
 * - Every transition is notified to listeners (the Flutter event stream).
 */
class OverlayController(
    private val hasPermission: () -> Boolean,
    private val gateway: OverlayServiceGateway,
    private val openPermissionSettings: () -> Unit = {},
    private val log: (name: String, detail: String?) -> Unit = { name, detail ->
        OverlayLog.event(name, detail)
    },
) {
    var state: OverlayRuntimeState =
        if (hasPermission()) OverlayRuntimeState.DISABLED
        else OverlayRuntimeState.PERMISSION_REQUIRED
        private set

    private var listener: ((OverlayRuntimeState) -> Unit)? = null
    private var lastPermission: Boolean = hasPermission()

    /** Event-stream hookup (EventChannel onListen/onCancel, §17). */
    fun setStateListener(listener: ((OverlayRuntimeState) -> Unit)?) {
        this.listener = listener
    }

    private fun transition(next: OverlayRuntimeState) {
        if (next == state) return
        state = next
        listener?.invoke(next)
    }

    /**
     * Permission query AND reconciliation point (§7.4 is driven from the
     * activity's onResume). Logs `overlay_permission_changed` on any flip and
     * performs recovery when permission was revoked mid-run (§27).
     */
    fun permissionGranted(): Boolean {
        val granted = hasPermission()
        if (granted != lastPermission) {
            lastPermission = granted
            log("overlay_permission_changed", if (granted) "granted" else "revoked")
        }
        if (!granted && state != OverlayRuntimeState.PERMISSION_REQUIRED) {
            log("overlay_permission_denied", "revoked")
            if (gateway.isRunning()) gateway.stop()
            transition(OverlayRuntimeState.PERMISSION_REQUIRED)
        }
        return granted
    }

    /** Opens the system overlay-permission screen (§7.3). */
    fun requestPermissionSettings(): Boolean {
        if (hasPermission()) return true
        openPermissionSettings()
        return true
    }

    /**
     * Truthful current state: permission wins over everything, a live service
     * wins over a stale UI, and unexpected termination degrades cleanly (§27).
     */
    fun currentState(): OverlayRuntimeState {
        val granted = permissionGranted()
        if (!granted) return state // transitioned to PERMISSION_REQUIRED above

        when (state) {
            OverlayRuntimeState.STARTING ->
                return state // awaiting the service callback; never downgrade
            OverlayRuntimeState.PERMISSION_REQUIRED ->
                transition(OverlayRuntimeState.DISABLED) // granted meanwhile
            OverlayRuntimeState.ACTIVE ->
                if (!gateway.isRunning()) {
                    // Unexpected termination recovered: service is gone.
                    transition(OverlayRuntimeState.DISABLED)
                }
            OverlayRuntimeState.STOPPING ->
                if (!gateway.isRunning()) {
                    transition(OverlayRuntimeState.DISABLED)
                }
            else -> Unit
        }
        if (gateway.isRunning() && state == OverlayRuntimeState.DISABLED) {
            // Service alive though this UI never saw it (engine restart).
            transition(OverlayRuntimeState.ACTIVE)
        }
        return state
    }

    /**
     * Idempotent start (§14):
     * already running -> no-op answering ACTIVE (NOT an error, §27).
     */
    fun start(): OverlayCommandResult {
        if (!permissionGranted()) {
            log("overlay_permission_denied", "start")
            transition(OverlayRuntimeState.PERMISSION_REQUIRED)
            return OverlayCommandResult.Failed(
                "permission_denied",
                "Overlay permission is required.",
            )
        }
        if (gateway.isRunning()) {
            transition(OverlayRuntimeState.ACTIVE)
            return OverlayCommandResult.Ok(OverlayRuntimeState.ACTIVE)
        }
        transition(OverlayRuntimeState.STARTING)
        return if (gateway.start()) {
            OverlayCommandResult.Ok(OverlayRuntimeState.STARTING)
        } else {
            log("overlay_start_failed", "dispatch")
            transition(OverlayRuntimeState.ERROR)
            OverlayCommandResult.Failed("start_failed", "Unable to start AI overlay.")
        }
    }

    /**
     * Idempotent stop (§14): stopping an already-stopped runtime answers the
     * permission-aware resting state, never an error.
     */
    fun stop(): OverlayCommandResult {
        val granted = permissionGranted()
        val running = gateway.isRunning()
        val pending = state == OverlayRuntimeState.STARTING
        if (!running && !pending) {
            val target =
                if (!granted) OverlayRuntimeState.PERMISSION_REQUIRED
                else OverlayRuntimeState.DISABLED
            transition(target)
            return OverlayCommandResult.Ok(target)
        }
        if (running) {
            transition(OverlayRuntimeState.STOPPING)
            gateway.stop() // final state arrives via onServiceStopped()
            return OverlayCommandResult.Ok(OverlayRuntimeState.STOPPING)
        }
        // Start dispatched but the service has not created its window yet:
        // cancel it and resolve synchronously (no callback may ever come).
        // If creation still races through, onServiceCreated() tears it down.
        gateway.stop()
        val target =
            if (!granted) OverlayRuntimeState.PERMISSION_REQUIRED
            else OverlayRuntimeState.DISABLED
        transition(target)
        return OverlayCommandResult.Ok(target)
    }

    // ---- Service callbacks (same-process, main thread) -------------------

    /** Overlay window added successfully. */
    fun onServiceCreated() {
        when (state) {
            // Late/racy creation after a stop, a revocation or a failed
            // start: the window must not outlive the decision — tear down.
            OverlayRuntimeState.STOPPING,
            OverlayRuntimeState.DISABLED,
            OverlayRuntimeState.ERROR,
            OverlayRuntimeState.PERMISSION_REQUIRED,
            -> gateway.stop()
            else -> transition(OverlayRuntimeState.ACTIVE)
        }
    }

    /** Service fully stopped and its window removed. */
    fun onServiceStopped() {
        if (state == OverlayRuntimeState.ERROR) return // keep the failure visible
        val target =
            if (!permissionGranted()) OverlayRuntimeState.PERMISSION_REQUIRED
            else OverlayRuntimeState.DISABLED
        transition(target)
    }

    /** Foreground/window failure reported by the service (§27). */
    fun onServiceStartFailed() {
        log("overlay_start_failed", "service")
        transition(OverlayRuntimeState.ERROR)
    }

    // ---- Activity lifecycle ---------------------------------------------

    /** Activity resumed: permission re-check + state reconciliation (§7.4). */
    fun onAppForeground() {
        currentState()
    }

    companion object {
        @Volatile
        private var _instance: OverlayController? = null

        /**
         * Process-wide controller so the overlay SERVICE (which can outlive
         * the Flutter engine) can always report into the single state model.
         * Re-attach on engine restart reuses the same instance; state is
         * reconciled from the real permission/service facts on first query.
         */
        val instance: OverlayController?
            get() = _instance

        fun attach(context: Context): OverlayController {
            synchronized(this) {
                _instance?.let { return it }
                val app = context.applicationContext
                val permissionManager = OverlayPermissionManager(app)
                val controller = OverlayController(
                    hasPermission = { permissionManager.hasPermission() },
                    gateway = ServiceOverlayGateway(app),
                    openPermissionSettings = { permissionManager.openSettings() },
                )
                _instance = controller
                return controller
            }
        }
    }
}
