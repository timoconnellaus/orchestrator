package dev.tim.orchestrator.orchestrator

import android.Manifest
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var visible = false
    private var pendingArm: MethodChannel.Result? = null
    private var mediaSession: MediaSession? = null

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

                "mediaSession" -> {
                    val active = call.argument<Boolean>("active") ?: false
                    val muted = call.argument<Boolean>("muted") ?: true
                    updateMediaSession(channel, active, muted)
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

    private fun updateMediaSession(
        channel: MethodChannel,
        active: Boolean,
        muted: Boolean,
    ) {
        if (!active) {
            mediaSession?.isActive = false
            mediaSession?.release()
            mediaSession = null
            return
        }
        val session =
            mediaSession ?: MediaSession(this, "OrchestratorVoice").also { created ->
                val open =
                    PendingIntent.getActivity(
                        this,
                        0,
                        Intent(this, MainActivity::class.java),
                        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                    )
                created.setSessionActivity(open)
                created.setCallback(
                    object : MediaSession.Callback() {
                        override fun onPlay() {
                            channel.invokeMethod("playRequested", null)
                        }

                        override fun onPause() {
                            channel.invokeMethod("pauseRequested", null)
                        }

                        override fun onStop() {
                            channel.invokeMethod("stopRequested", null)
                        }
                    },
                )
                created.setMetadata(
                    MediaMetadata
                        .Builder()
                        .putString(MediaMetadata.METADATA_KEY_TITLE, "Orchestrator Voice")
                        .putString(MediaMetadata.METADATA_KEY_ARTIST, "Voice conversation")
                        .build(),
                )
                mediaSession = created
            }
        val actions =
            PlaybackState.ACTION_PLAY or
                PlaybackState.ACTION_PAUSE or
                PlaybackState.ACTION_PLAY_PAUSE or
                PlaybackState.ACTION_STOP
        session.setPlaybackState(
            PlaybackState
                .Builder()
                .setActions(actions)
                .setState(
                    if (muted) PlaybackState.STATE_PAUSED else PlaybackState.STATE_PLAYING,
                    PlaybackState.PLAYBACK_POSITION_UNKNOWN,
                    if (muted) 0f else 1f,
                ).build(),
        )
        session.isActive = true
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        // Engine teardown destroys the WebRTC plugin and its native audio tracks.
        mediaSession?.isActive = false
        mediaSession?.release()
        mediaSession = null
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
