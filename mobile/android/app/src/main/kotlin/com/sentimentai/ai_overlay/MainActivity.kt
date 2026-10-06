package com.sentimentai.ai_overlay

import com.sentimentai.ai_overlay.overlay.OverlayBridge
import com.sentimentai.ai_overlay.overlay.OverlayController
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {

    private var bridge: OverlayBridge? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        // One process-wide controller (§25): the service reports into it even
        // while this activity/engine is gone.
        val controller = OverlayController.attach(applicationContext)
        bridge = OverlayBridge(controller).also {
            it.attach(flutterEngine.dartExecutor.binaryMessenger)
        }
    }

    override fun onResume() {
        super.onResume()
        // §7.4: re-check overlay permission on every foreground transition —
        // grants from the settings screen land here, revocations recover.
        OverlayController.instance?.onAppForeground()
    }

    override fun onDestroy() {
        bridge?.detach()
        bridge = null
        super.onDestroy()
    }
}
