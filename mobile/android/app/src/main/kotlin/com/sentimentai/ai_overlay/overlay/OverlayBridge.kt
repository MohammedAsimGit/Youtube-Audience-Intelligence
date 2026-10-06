package com.sentimentai.ai_overlay.overlay

import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Sprint 10.1 §16/§17 — the ONE Flutter ↔ Kotlin bridge.
 *
 * A thin adapter only: it maps the five documented operations
 * (getOverlayPermissionStatus, requestOverlayPermission, startOverlay,
 * stopOverlay, getOverlayState) onto [OverlayController] and forwards state
 * events to Flutter. All state decisions live in the controller — this class
 * contains no logic of its own (§25).
 *
 * The event envelope is `{'event': 'overlay_state', 'state': '<WIRE>'}`.
 * Sprint 10.1 ships only this event kind; later sprints (10.2/10.3) can add
 * new `event` values without touching the method contract (§17).
 */
class OverlayBridge(private val controller: OverlayController) :
    MethodChannel.MethodCallHandler,
    EventChannel.StreamHandler {

    private var methodChannel: MethodChannel? = null
    private var eventChannel: EventChannel? = null
    private var eventSink: EventChannel.EventSink? = null
    private var listener: ((OverlayRuntimeState) -> Unit)? = null

    fun attach(messenger: BinaryMessenger) {
        val methods = MethodChannel(messenger, METHOD_CHANNEL)
        methods.setMethodCallHandler(this)
        methodChannel = methods

        val events = EventChannel(messenger, EVENT_CHANNEL)
        events.setStreamHandler(this)
        eventChannel = events
    }

    /** Detach handlers (engine teardown); the controller itself survives. */
    fun detach() {
        methodChannel?.setMethodCallHandler(null)
        eventChannel?.setStreamHandler(null)
        methodChannel = null
        eventChannel = null
        eventSink = null
        listener = null
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "getOverlayPermissionStatus" ->
                result.success(controller.permissionGranted())

            "requestOverlayPermission" ->
                result.success(controller.requestPermissionSettings())

            "getOverlayState" ->
                result.success(controller.currentState().wire)

            "startOverlay" ->
                when (val outcome = controller.start()) {
                    is OverlayCommandResult.Ok -> result.success(outcome.state.wire)
                    is OverlayCommandResult.Failed ->
                        result.error(outcome.code, outcome.message, null)
                }

            "stopOverlay" ->
                when (val outcome = controller.stop()) {
                    is OverlayCommandResult.Ok -> result.success(outcome.state.wire)
                    is OverlayCommandResult.Failed ->
                        result.error(outcome.code, outcome.message, null)
                }

            else -> result.notImplemented()
        }
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink?) {
        eventSink = events
        if (listener == null) {
            listener = { state -> eventSink?.success(stateEnvelope(state)) }
            controller.setStateListener(listener)
        }
    }

    override fun onCancel(arguments: Any?) {
        eventSink = null
        controller.setStateListener(null)
        listener = null
    }

    private fun stateEnvelope(state: OverlayRuntimeState): Map<String, String> = mapOf(
        "event" to EVENT_STATE_CHANGED,
        "state" to state.wire,
    )

    companion object {
        const val METHOD_CHANNEL = "sentimentai/overlay"
        const val EVENT_CHANNEL = "sentimentai/overlay_events"
        private const val EVENT_STATE_CHANGED = "overlay_state"
    }
}
