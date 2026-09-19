package de.kruse.stopmotion

import android.Manifest
import android.content.pm.ActivityInfo
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import de.kruse.stopmotion.databinding.ActivityMainBinding
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * The whole phone-side UI: a mirrored live preview plus a status overlay.
 *
 * There is deliberately no shutter button. The desktop is the only trigger for
 * a capture (Plan.md section 2) - a button here would let the operator nudge
 * the phone at the exact moment that must not be nudged.
 */
class MainActivity : AppCompatActivity(), CameraController.Listener, SocketServer.Handler {

    private lateinit var binding: ActivityMainBinding
    private lateinit var server: SocketServer
    private var cameraController: CameraController? = null

    private val main = Handler(Looper.getMainLooper())
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.US)

    private val helloPending = AtomicBoolean(false)
    private val capturesServed = AtomicLong(0)

    @Volatile
    private var lastCaptureLabel: String = "none yet"

    @Volatile
    private var cameraReady = false

    private val requestCamera =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                startCamera()
            } else {
                showBanner(getString(R.string.permission_rationale))
                setSocketLine("camera permission denied", R.color.status_bad)
            }
        }

    private val statusTick = object : Runnable {
        override fun run() {
            refreshStatus()
            main.postDelayed(this, STATUS_INTERVAL_MS)
        }
    }

    // ------------------------------------------------------------- lifecycle

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        server = SocketServer(SocketServer.DEFAULT_PORT, this)

        setSocketLine("starting…", R.color.status_warn)
        binding.statusStream.text = "preview  idle"
        binding.statusCapture.text = "capture  none yet"
        binding.statusSettings.text = "settings –"

        if (hasCameraPermission()) {
            startCamera()
        } else {
            requestCamera.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onStart() {
        super.onStart()
        server.start()
        main.post(statusTick)
    }

    override fun onStop() {
        main.removeCallbacks(statusTick)
        cameraController?.setStreaming(false)
        server.stop()
        super.onStop()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        cameraController?.release()
        cameraController = null
        super.onDestroy()
    }

    override fun onConfigurationChanged(newConfig: android.content.res.Configuration) {
        super.onConfigurationChanged(newConfig)
        cameraController?.updateRotation()
    }

    private fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    private fun startCamera() {
        if (cameraController != null) return
        hideBanner()
        val controller = CameraController(this, this, binding.previewView, this)
        cameraController = controller
        controller.start()
    }

    // ------------------------------------------------- CameraController.Listener

    override fun onPreviewFrame(jpeg: ByteArray, width: Int, height: Int, seq: Long) {
        // Analysis thread. The server drops the frame if the writer is busy.
        server.sendPreviewFrame(jpeg, width, height, seq)
    }

    override fun onCameraReady(capabilities: Capabilities, applied: CameraSettings) {
        cameraReady = true
        hideBanner()
        refreshStatus()
        if (helloPending.compareAndSet(true, false) && server.isConnected) {
            server.sendHello(capabilities.toHelloJson())
        }
    }

    override fun onCameraError(message: String) {
        cameraReady = false
        Log.e(TAG, "camera error: $message")
        showBanner(message)
        server.sendError("camera_error", message)
    }

    // ------------------------------------------------------ SocketServer.Handler

    override fun buildHello(): JSONObject? {
        val caps = cameraController?.capabilities
        if (caps == null) {
            // Camera still warming up; HELLO goes out from onCameraReady.
            helloPending.set(true)
            return null
        }
        return caps.toHelloJson()
    }

    override fun onClientConnected() {
        main.post { refreshStatus() }
    }

    override fun onClientDisconnected(reason: String) {
        // A disconnected desktop must not leave the encoder burning battery.
        cameraController?.setStreaming(false)
        helloPending.set(false)
        main.post { refreshStatus() }
    }

    override fun onSetConfig(json: JSONObject) {
        main.post {
            val controller = cameraController
            val current = controller?.settings
            val caps = controller?.capabilities
            if (controller == null || current == null || caps == null) {
                server.sendError("not_ready", "camera not bound yet")
                return@post
            }
            try {
                val applied = controller.applySettings(current.merged(json))
                server.sendConfigAck(applied.toJson())
                refreshStatus()
            } catch (e: Exception) {
                Log.w(TAG, "SET_CONFIG failed", e)
                server.sendError("set_config_failed", e.message ?: "unknown error")
            }
        }
    }

    override fun onPreviewCtl(json: JSONObject) {
        val on = when {
            json.has("start") -> json.optBoolean("start", true)
            json.has("stop") -> !json.optBoolean("stop", true)
            json.has("on") -> json.optBoolean("on", true)
            json.has("enabled") -> json.optBoolean("enabled", true)
            else -> json.optString("action").equals("start", ignoreCase = true)
        }
        main.post {
            cameraController?.setStreaming(on)
            refreshStatus()
        }
    }

    override fun onCapture(json: JSONObject) {
        val requestId = json.optString("request_id").ifEmpty { "unknown" }
        main.post {
            val controller = cameraController
            if (controller == null || !cameraReady) {
                server.sendError("not_ready", "camera not bound yet", requestId)
                return@post
            }
            controller.capture(
                onResult = { jpeg, width, height, settings ->
                    // Capture executor. Bytes go straight to the socket; they
                    // are never written to phone storage.
                    server.sendCaptureResult(
                        requestId,
                        width,
                        height,
                        settings?.toJson() ?: JSONObject(),
                        jpeg,
                    )
                    capturesServed.incrementAndGet()
                    lastCaptureLabel = "${width}x$height  ${jpeg.size / 1024} KiB  " +
                        timeFormat.format(Date())
                    main.post { refreshStatus() }
                },
                onError = { message ->
                    server.sendError("capture_failed", message, requestId)
                    lastCaptureLabel = "failed: $message"
                    main.post { refreshStatus() }
                },
            )
        }
    }

    override fun onStateChanged(state: SocketServer.State) {
        main.post { refreshStatus() }
    }

    // ----------------------------------------------------------------- status

    private fun refreshStatus() {
        val controller = cameraController
        when (server.state) {
            SocketServer.State.CONNECTED ->
                setSocketLine("connected  127.0.0.1:${SocketServer.DEFAULT_PORT}", R.color.status_ok)
            SocketServer.State.LISTENING ->
                setSocketLine("listening  127.0.0.1:${SocketServer.DEFAULT_PORT}", R.color.status_warn)
            SocketServer.State.STOPPED ->
                setSocketLine("disconnected  socket down", R.color.status_bad)
        }

        val streaming = controller?.isStreaming == true
        val size = controller?.lastPreviewSize ?: (0 to 0)
        val sizeLabel = if (size.first > 0) "${size.first}x${size.second}" else "–"
        binding.statusStream.text = buildString {
            append("preview  ")
            append(if (streaming) "on " else "off ")
            append(sizeLabel)
            append("  q")
            append(controller?.settings?.previewQuality ?: 0)
            append("  sent ")
            append(server.previewFramesSent)
            append("  dropped ")
            append(server.previewFramesDropped)
        }

        binding.statusCapture.text =
            "capture  ${capturesServed.get()} served  last: $lastCaptureLabel"

        binding.statusSettings.text = controller?.settings?.let { s ->
            val caps = controller.capabilities
            val manual = if (caps?.supportsManualSensor == true) "" else "  (no manual sensor)"
            "settings " + s.summary() + manual
        } ?: "settings –"
    }

    private fun setSocketLine(text: String, colorRes: Int) {
        binding.statusSocket.text = "socket   $text"
        binding.statusSocket.setTextColor(ContextCompat.getColor(this, colorRes))
    }

    private fun showBanner(message: String) {
        binding.messageBanner.text = message
        binding.messageBanner.visibility = View.VISIBLE
    }

    private fun hideBanner() {
        binding.messageBanner.visibility = View.GONE
    }

    companion object {
        private const val TAG = "MainActivity"
        private const val STATUS_INTERVAL_MS = 500L
    }
}
