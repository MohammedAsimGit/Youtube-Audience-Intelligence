package com.sentimentai.ai_overlay.overlay

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Sprint 10.1 §29 — JVM unit tests for the central overlay state machine.
 *
 * Everything Android is faked through the injected seams, so these run on a
 * plain JVM (`./gradlew test`) with no emulator: permission facts, service
 * gateway, settings intent and the §28 log sink.
 */
class OverlayControllerTest {

    /** Fake service gateway mirroring production dispatch semantics. */
    private class FakeGateway : OverlayServiceGateway {
        var running = false
        var startResult = true
        var startCalls = 0
        var stopCalls = 0

        override fun isRunning(): Boolean = running

        override fun start(): Boolean {
            startCalls++
            return startResult
        }

        override fun stop() {
            stopCalls++
            running = false
        }
    }

    private class Harness(initialPermission: Boolean = true) {
        var permission = initialPermission
        val gateway = FakeGateway()
        val logs = mutableListOf<Pair<String, String?>>()
        var settingsOpens = 0
        val states = mutableListOf<OverlayRuntimeState>()

        val controller = OverlayController(
            hasPermission = { permission },
            gateway = gateway,
            openPermissionSettings = { settingsOpens++ },
            log = { name, detail -> logs.add(name to detail) },
        ).also { c -> c.setStateListener { states.add(it) } }

        /** Simulates the service finishing onCreate (window added). */
        fun serviceCreated() {
            gateway.running = true
            controller.onServiceCreated()
        }

        /** Simulates the service finishing onDestroy (window removed). */
        fun serviceStopped() {
            gateway.running = false
            controller.onServiceStopped()
        }
    }

    private fun ok(result: OverlayCommandResult): OverlayRuntimeState {
        assertTrue("expected Ok, got $result", result is OverlayCommandResult.Ok)
        return (result as OverlayCommandResult.Ok).state
    }

    private fun failed(result: OverlayCommandResult): OverlayCommandResult.Failed {
        assertTrue("expected Failed, got $result", result is OverlayCommandResult.Failed)
        return result as OverlayCommandResult.Failed
    }

    // ---- Initial state ---------------------------------------------------

    @Test
    fun `initial state is DISABLED when permission granted`() {
        val h = Harness(initialPermission = true)
        assertEquals(OverlayRuntimeState.DISABLED, h.controller.state)
    }

    @Test
    fun `initial state is PERMISSION_REQUIRED when permission denied`() {
        val h = Harness(initialPermission = false)
        assertEquals(OverlayRuntimeState.PERMISSION_REQUIRED, h.controller.state)
    }

    // ---- Start -----------------------------------------------------------

    @Test
    fun `start with permission dispatches and answers STARTING`() {
        val h = Harness()
        assertEquals(OverlayRuntimeState.STARTING, ok(h.controller.start()))
        assertEquals(1, h.gateway.startCalls)
        assertEquals(OverlayRuntimeState.STARTING, h.controller.state)
    }

    @Test
    fun `duplicate start after service is up is a no-op`() {
        val h = Harness()
        h.controller.start()
        h.serviceCreated()
        assertEquals(OverlayRuntimeState.ACTIVE, h.controller.state)

        // Idempotent (§14): second call must not start a second service.
        assertEquals(OverlayRuntimeState.ACTIVE, ok(h.controller.start()))
        assertEquals(1, h.gateway.startCalls)
        assertEquals(OverlayRuntimeState.ACTIVE, h.controller.state)
    }

    @Test
    fun `start without permission fails with permission_denied and never dispatches`() {
        val h = Harness(initialPermission = false)
        val f = failed(h.controller.start())
        assertEquals("permission_denied", f.code)
        assertEquals("Overlay permission is required.", f.message)
        assertEquals(0, h.gateway.startCalls)
        assertEquals(OverlayRuntimeState.PERMISSION_REQUIRED, h.controller.state)
        assertTrue(h.logs.contains("overlay_permission_denied" to "start"))
    }

    @Test
    fun `start dispatch failure answers start_failed and lands in ERROR`() {
        val h = Harness()
        h.gateway.startResult = false
        val f = failed(h.controller.start())
        assertEquals("start_failed", f.code)
        assertEquals("Unable to start AI overlay.", f.message)
        assertEquals(OverlayRuntimeState.ERROR, h.controller.state)
        assertTrue(h.logs.contains("overlay_start_failed" to "dispatch"))
    }

    // ---- Stop ------------------------------------------------------------

    @Test
    fun `stop while active transitions STOPPING then DISABLED`() {
        val h = Harness()
        h.controller.start()
        h.serviceCreated()

        assertEquals(OverlayRuntimeState.STOPPING, ok(h.controller.stop()))
        assertEquals(1, h.gateway.stopCalls)
        assertEquals(OverlayRuntimeState.STOPPING, h.controller.state)

        h.serviceStopped()
        assertEquals(OverlayRuntimeState.DISABLED, h.controller.state)
        assertFalse(h.gateway.running)
    }

    @Test
    fun `duplicate stop on stopped runtime is a safe no-op`() {
        val h = Harness()
        assertEquals(OverlayRuntimeState.DISABLED, ok(h.controller.stop()))
        assertEquals(OverlayRuntimeState.DISABLED, ok(h.controller.stop()))
        assertEquals(0, h.gateway.stopCalls)
        assertEquals(OverlayRuntimeState.DISABLED, h.controller.state)
    }

    @Test
    fun `stop while still STARTING resolves synchronously`() {
        val h = Harness()
        h.controller.start()
        assertEquals(OverlayRuntimeState.STARTING, h.controller.state)

        // No service callback may ever come; stop must not hang in STARTING.
        assertEquals(OverlayRuntimeState.DISABLED, ok(h.controller.stop()))
        assertEquals(1, h.gateway.stopCalls)
        assertEquals(OverlayRuntimeState.DISABLED, h.controller.state)
    }

    @Test
    fun `service creation racing a stop is torn down immediately`() {
        val h = Harness()
        h.controller.start()
        assertEquals(OverlayRuntimeState.DISABLED, ok(h.controller.stop()))

        // The start still managed to create the window: must not outlive stop.
        h.serviceCreated()
        assertFalse(h.gateway.running)
        assertEquals(OverlayRuntimeState.DISABLED, h.controller.state)
        assertEquals(2, h.gateway.stopCalls)
    }

    // ---- Service callbacks ------------------------------------------------

    @Test
    fun `service created callback moves STARTING to ACTIVE`() {
        val h = Harness()
        h.controller.start()
        h.serviceCreated()
        assertEquals(OverlayRuntimeState.ACTIVE, h.controller.state)
        assertTrue(h.states.contains(OverlayRuntimeState.ACTIVE))
    }

    @Test
    fun `start failure callback keeps ERROR through onServiceStopped`() {
        val h = Harness()
        h.controller.start()
        h.controller.onServiceStartFailed()
        assertEquals(OverlayRuntimeState.ERROR, h.controller.state)
        assertTrue(h.logs.contains("overlay_start_failed" to "service"))

        h.serviceStopped()
        // Failure must stay visible until the user retries (§27).
        assertEquals(OverlayRuntimeState.ERROR, h.controller.state)
    }

    // ---- Permission revocation -------------------------------------------

    @Test
    fun `revoking permission while ACTIVE stops the service`() {
        val h = Harness()
        h.controller.start()
        h.serviceCreated()
        assertEquals(OverlayRuntimeState.ACTIVE, h.controller.state)

        h.permission = false
        val granted = h.controller.permissionGranted()
        assertFalse(granted)
        assertEquals(OverlayRuntimeState.PERMISSION_REQUIRED, h.controller.state)
        assertTrue(h.gateway.stopCalls >= 1)
        assertFalse(h.gateway.running)
    }

    @Test
    fun `permission change is logged with the documented vocabulary`() {
        val h = Harness()
        h.permission = false
        h.controller.permissionGranted()
        assertTrue(h.logs.contains("overlay_permission_changed" to "revoked"))
        assertTrue(h.logs.contains("overlay_permission_denied" to "revoked"))

        h.permission = true
        h.controller.permissionGranted()
        assertTrue(h.logs.contains("overlay_permission_changed" to "granted"))
    }

    @Test
    fun `requestPermissionSettings opens the system screen when denied`() {
        val h = Harness(initialPermission = false)
        h.controller.requestPermissionSettings()
        assertEquals(1, h.settingsOpens)

        h.permission = true
        h.controller.requestPermissionSettings()
        assertEquals(1, h.settingsOpens) // already granted: no settings trip
    }

    // ---- Reconciliation (currentState / onAppForeground) ------------------

    @Test
    fun `ghost running service is adopted as ACTIVE`() {
        val h = Harness()
        h.gateway.running = true // engine restarted; UI never saw the start
        assertEquals(OverlayRuntimeState.ACTIVE, h.controller.currentState())
    }

    @Test
    fun `unexpected termination of ACTIVE service degrades to DISABLED`() {
        val h = Harness()
        h.controller.start()
        h.serviceCreated()
        h.gateway.running = true // process killed the service behind our back
        h.gateway.running = false
        assertEquals(OverlayRuntimeState.DISABLED, h.controller.currentState())
    }

    @Test
    fun `foreground reconciliation re-checks permission`() {
        val h = Harness()
        h.controller.start()
        h.serviceCreated()
        h.permission = false
        h.controller.onAppForeground()
        assertEquals(OverlayRuntimeState.PERMISSION_REQUIRED, h.controller.state)
    }

    // ---- Wire contract ----------------------------------------------------

    @Test
    fun `wire strings match the Flutter contract exactly`() {
        val expected = mapOf(
            OverlayRuntimeState.DISABLED to "DISABLED",
            OverlayRuntimeState.PERMISSION_REQUIRED to "PERMISSION_REQUIRED",
            OverlayRuntimeState.STARTING to "STARTING",
            OverlayRuntimeState.ACTIVE to "ACTIVE",
            OverlayRuntimeState.STOPPING to "STOPPING",
            OverlayRuntimeState.ERROR to "ERROR",
        )
        assertEquals(expected, OverlayRuntimeState.entries.associateWith { it.wire })
        assertEquals(OverlayRuntimeState.entries.size, expected.size)
    }
}
