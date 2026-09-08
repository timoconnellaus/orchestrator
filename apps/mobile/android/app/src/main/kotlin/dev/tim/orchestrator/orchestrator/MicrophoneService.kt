package dev.tim.orchestrator.orchestrator

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.Process
import io.flutter.plugin.common.MethodChannel

/** Android owns recording eligibility; Flutter/LiveKit owns the actual audio track.
 * Never sticky and never started from a boot, alarm, or network receiver.
 */
class MicrophoneService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(
        intent: Intent?,
        flags: Int,
        startId: Int,
    ): Int {
        if (intent?.action == STOP_AND_CLOSE) {
            stopAndCloseApp()
            return START_NOT_STICKY
        }
        // Only a visible Activity can authorize a new recording service.
        val ready = onArmed
        onArmed = null
        if (ready == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            val manager = getSystemService(NotificationManager::class.java)
            if (Build.VERSION.SDK_INT >= 26) {
                manager.createNotificationChannel(NotificationChannel(CHANNEL, "Voice microphone", NotificationManager.IMPORTANCE_LOW))
            }
            val open =
                PendingIntent.getActivity(
                    this,
                    0,
                    Intent(this, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                )
            val stop =
                PendingIntent.getService(
                    this,
                    1,
                    Intent(this, MicrophoneService::class.java).setAction(STOP_AND_CLOSE),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                )
            val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else Notification.Builder(this)
            val notification =
                builder
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setContentTitle("Orchestrator · microphone armed")
                    .setContentText("Voice may capture audio while the screen is off.")
                    .setContentIntent(open)
                    .setOngoing(true)
                    .setCategory(Notification.CATEGORY_SERVICE)
                    .addAction(Notification.Action.Builder(null, "Stop & close app", stop).build())
                    .build()
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(NOTIFICATION, notification)
            }
            setArmed(this, true)
            ready(null)
        } catch (error: Exception) {
            setArmed(this, false)
            ready(error.message ?: "Could not start microphone service")
            stopSelf()
        }
        return START_NOT_STICKY
    }

    private fun stopAndCloseApp() {
        // Deliberate notification action ONLY. Persist disarm synchronously before
        // any Dart callback. Never invoke this fallback for errors or lifecycle.
        setArmed(this, false)
        // Give LiveKit a brief opportunity to disconnect gracefully, but do not
        // depend on a widget/isolate responding to release native audio capture.
        Handler(Looper.getMainLooper()).postDelayed({
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            Process.killProcess(Process.myPid())
        }, 750)
        runCatching { bridge?.invokeMethod("stopRequested", null) }
    }

    override fun onDestroy() {
        val wasArmed = getSharedPreferences("voice_safety", Context.MODE_PRIVATE).getBoolean("armed", false)
        setArmed(this, false)
        // Unexpected service teardown must also disarm the Dart/LiveKit track.
        // Normal mute/disarm sets armed=false before stopService, preserving playback.
        if (wasArmed) runCatching { bridge?.invokeMethod("stopRequested", null) }
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        var bridge: MethodChannel? = null
        var onArmed: ((String?) -> Unit)? = null
        private const val CHANNEL = "orchestrator_microphone"
        private const val NOTIFICATION = 4072
        private const val STOP_AND_CLOSE = "dev.tim.orchestrator.STOP_AND_CLOSE"

        fun setArmed(
            context: Context,
            armed: Boolean,
        ) {
            // Intent is never restored to true at process/service startup.
            context
                .getSharedPreferences("voice_safety", Context.MODE_PRIVATE)
                .edit()
                .putBoolean("armed", armed)
                .commit()
        }
    }
}
