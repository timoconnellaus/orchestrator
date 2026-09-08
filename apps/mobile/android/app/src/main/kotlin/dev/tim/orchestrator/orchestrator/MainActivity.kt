package dev.tim.orchestrator.orchestrator

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var visible = false
    private var pendingArm: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
        MicrophoneService.bridge = channel
        channel.setMethodCallHandler { call, result ->
            when (call.method) {
                "arm" -> {
                    if (!visible) {
                        result.error("not_visible", "Open the app to arm the microphone.", null)
                    } else if (pendingArm != null) {
                        result.error("busy", "A permission request is already active.", null)
                    } else {
                        pendingArm = result
                        val permissions = mutableListOf(Manifest.permission.RECORD_AUDIO)
                        if (Build.VERSION.SDK_INT >= 33) permissions.add(Manifest.permission.POST_NOTIFICATIONS)
                        val missing = permissions.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
                        if (missing.isEmpty()) {
                            startMicrophoneService()
                        } else {
                            requestPermissions(missing.toTypedArray(), PERMISSION_REQUEST)
                        }
                    }
                }

                "disarm" -> {
                    pendingArm?.error("cancelled", "Microphone arming cancelled.", null)
                    pendingArm = null
                    MicrophoneService.onArmed = null
                    MicrophoneService.setArmed(this, false)
                    stopService(Intent(this, MicrophoneService::class.java))
                    result.success(null)
                }

                else -> {
                    result.notImplemented()
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        visible = true
    }

    override fun onPause() {
        visible = false
        super.onPause()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != PERMISSION_REQUEST || pendingArm == null) return
        if (grantResults.isEmpty() || grantResults.any { it != PackageManager.PERMISSION_GRANTED }) {
            pendingArm?.error(
                "permission_denied",
                "Microphone and notification permissions are needed for visible listening controls.",
                null,
            )
            pendingArm = null
        } else {
            // Permission UI may deliver its result just before Activity.onResume.
            window.decorView.post { startMicrophoneService() }
        }
    }

    private fun startMicrophoneService() {
        val result = pendingArm ?: return
        if (!visible) {
            result.error("not_visible", "Return to the app and tap Join voice again.", null)
            pendingArm = null
            return
        }
        MicrophoneService.onArmed = { error ->
            pendingArm = null
            if (error == null) {
                result.success(null)
            } else {
                result.error("foreground_service", error, null)
            }
        }
        try {
            val intent = Intent(this, MicrophoneService::class.java)
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(intent) else startService(intent)
        } catch (error: Exception) {
            MicrophoneService.onArmed = null
            pendingArm = null
            result.error("foreground_service", error.message, null)
        }
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        // Engine teardown destroys the WebRTC plugin and its native audio tracks.
        MicrophoneService.bridge = null
        MicrophoneService.onArmed = null
        MicrophoneService.setArmed(this, false)
        stopService(Intent(this, MicrophoneService::class.java))
        super.cleanUpFlutterEngine(flutterEngine)
    }

    companion object {
        const val CHANNEL = "dev.tim.orchestrator/microphone"
        private const val PERMISSION_REQUEST = 4071
    }
}
