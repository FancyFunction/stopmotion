package de.kruse.stopmotion

import android.content.Context
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.util.Log
import android.util.Size
import android.view.Surface
import androidx.annotation.MainThread
import androidx.annotation.OptIn
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.CaptureRequestOptions
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Owns the CameraX session: Preview (mirrored on the phone screen),
 * ImageAnalysis (the MJPEG stream to the desktop) and ImageCapture (full-res
 * stills), all bound at once.
 *
 * Hard rule from the requirements: stills never touch phone storage. Capture
 * goes through [ImageCapture.OnImageCapturedCallback], so the JPEG bytes exist
 * only in memory on their way to the socket.
 */
class CameraController(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val previewView: PreviewView,
    private val listener: Listener,
) {

    interface Listener {
        /** A freshly encoded MJPEG frame. Called on the analysis thread. */
        fun onPreviewFrame(jpeg: ByteArray, width: Int, height: Int, seq: Long)

        /** Camera bound and capabilities known. Called on the main thread. */
        fun onCameraReady(capabilities: Capabilities, applied: CameraSettings)

        /** Unrecoverable camera trouble. Called on the main thread. */
        fun onCameraError(message: String)
    }

    private val mainExecutor = ContextCompat.getMainExecutor(context)
    private val analysisExecutor: ExecutorService =
        Executors.newSingleThreadExecutor { r -> Thread(r, "stopmotion-analysis") }
    private val captureExecutor: ExecutorService =
        Executors.newSingleThreadExecutor { r -> Thread(r, "stopmotion-capture") }

    private var cameraProvider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var preview: Preview? = null
    private var imageAnalysis: ImageAnalysis? = null
    private var imageCapture: ImageCapture? = null

    private val streaming = AtomicBoolean(false)
    private val frameSeq = AtomicInteger(0)
    private val released = AtomicBoolean(false)

    @Volatile
    var capabilities: Capabilities? = null
        private set

    @Volatile
    var settings: CameraSettings? = null
        private set

    /** Actual size of the last encoded preview frame, for the status overlay. */
    @Volatile
    var lastPreviewSize: Pair<Int, Int> = 0 to 0
        private set

    // ------------------------------------------------------------------ start

    @MainThread
    fun start() {
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            if (released.get()) return@addListener
            try {
                val provider = future.get()
                cameraProvider = provider
                bind(provider, settings)
            } catch (e: Exception) {
                Log.e(TAG, "camera provider failed", e)
                listener.onCameraError("camera unavailable: ${e.message}")
            }
        }, mainExecutor)
    }

    /**
     * (Re)bind all three use cases. Called on first start and whenever the
     * requested analysis resolution changes - everything else is applied
     * through [Camera2CameraControl] without touching the binding.
     */
    @OptIn(ExperimentalCamera2Interop::class)
    @MainThread
    private fun bind(provider: ProcessCameraProvider, desired: CameraSettings?) {
        val selector = CameraSelector.DEFAULT_BACK_CAMERA
        val rotation = currentRotation()

        // Capabilities must be known before we can clamp the requested sizes,
        // and they need a CameraInfo, which the provider can hand us unbound.
        val caps = capabilities ?: try {
            val info = selector.filter(provider.availableCameraInfos).firstOrNull()
                ?: throw IllegalStateException("no back camera on this device")
            Capabilities.from(info)
        } catch (e: Exception) {
            Log.e(TAG, "cannot read capabilities", e)
            listener.onCameraError("cannot read camera capabilities: ${e.message}")
            return
        }
        capabilities = caps

        val wanted = (desired ?: CameraSettings.defaults(caps)).clamped(caps)

        val previewUseCase = Preview.Builder()
            .setTargetRotation(rotation)
            .also { builder ->
                // Initial manual state, so the very first frames are already
                // correct; later changes go through CaptureRequestOptions.
                Camera2Interop.Extender(builder).applyManualDefaults(caps, wanted)
            }
            .build()
            .also { it.setSurfaceProvider(previewView.surfaceProvider) }

        val analysisSelector = ResolutionSelector.Builder()
            .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
            .setResolutionStrategy(
                ResolutionStrategy(
                    Size(wanted.previewWidth, wanted.previewHeight),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                ),
            )
            .build()

        val analysisUseCase = ImageAnalysis.Builder()
            .setResolutionSelector(analysisSelector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            .setTargetRotation(rotation)
            .build()
            .also { it.setAnalyzer(analysisExecutor, ::analyze) }

        val captureSelector = ResolutionSelector.Builder()
            .setAspectRatioStrategy(AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY)
            .setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY)
            .setAllowedResolutionMode(ResolutionSelector.PREFER_HIGHER_RESOLUTION_OVER_CAPTURE_RATE)
            .build()

        val captureUseCase = ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
            .setResolutionSelector(captureSelector)
            .setJpegQuality(STILL_JPEG_QUALITY)
            .setFlashMode(ImageCapture.FLASH_MODE_OFF)
            .setTargetRotation(rotation)
            .also { builder ->
                Camera2Interop.Extender(builder).applyManualDefaults(caps, wanted)
            }
            .build()

        try {
            provider.unbindAll()
            camera = provider.bindToLifecycle(
                lifecycleOwner,
                selector,
                previewUseCase,
                analysisUseCase,
                captureUseCase,
            )
        } catch (e: Exception) {
            Log.e(TAG, "bindToLifecycle failed", e)
            listener.onCameraError("cannot bind camera: ${e.message}")
            return
        }

        preview = previewUseCase
        imageAnalysis = analysisUseCase
        imageCapture = captureUseCase

        val actual = analysisUseCase.resolutionInfo?.resolution
        val effective = if (actual != null) {
            wanted.copy(previewWidth = actual.width, previewHeight = actual.height)
        } else {
            wanted
        }
        settings = effective
        pushCaptureRequestOptions(effective, caps)
        Log.i(TAG, "bound; analysis=${actual?.width}x${actual?.height} rotation=$rotation")
        listener.onCameraReady(caps, effective)
    }

    // --------------------------------------------------------------- settings

    /**
     * Apply what the desktop asked for and return what was actually applied
     * after clamping. Must be called on the main thread; the socket thread goes
     * through [MainActivity]'s handler to get here.
     */
    @MainThread
    fun applySettings(requested: CameraSettings): CameraSettings {
        val caps = capabilities ?: return requested
        val clamped = requested.clamped(caps)
        val current = settings
        val provider = cameraProvider

        if (current != null && provider != null && clamped.needsRebind(current)) {
            // Only the analysis resolution needs a new binding.
            bind(provider, clamped)
            return settings ?: clamped
        }

        settings = clamped
        pushCaptureRequestOptions(clamped, caps)
        return clamped
    }

    /**
     * Push the manual controls onto the live session. This is the path that
     * avoids a rebind: CameraX merges these into both the repeating request and
     * the still request.
     */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun pushCaptureRequestOptions(s: CameraSettings, caps: Capabilities) {
        val cam = camera ?: return
        val builder = CaptureRequestOptions.Builder()

        if (caps.supportsManualSensor) {
            builder.setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_MODE,
                CameraMetadata.CONTROL_AE_MODE_OFF,
            )
            builder.setCaptureRequestOption(CaptureRequest.SENSOR_EXPOSURE_TIME, s.exposureNs)
            builder.setCaptureRequestOption(CaptureRequest.SENSOR_SENSITIVITY, s.iso)
            builder.setCaptureRequestOption(
                CaptureRequest.CONTROL_AF_MODE,
                CameraMetadata.CONTROL_AF_MODE_OFF,
            )
            builder.setCaptureRequestOption(CaptureRequest.LENS_FOCUS_DISTANCE, s.focusDiopters)
        } else {
            // No MANUAL_SENSOR: the best we can do is auto plus the locks.
            builder.setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_MODE,
                CameraMetadata.CONTROL_AE_MODE_ON,
            )
            builder.setCaptureRequestOption(
                CaptureRequest.CONTROL_AF_MODE,
                if (s.afLock) {
                    CameraMetadata.CONTROL_AF_MODE_OFF
                } else {
                    CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE
                },
            )
        }

        builder.setCaptureRequestOption(
            CaptureRequest.CONTROL_AWB_MODE,
            Capabilities.awbModeValue(s.awbMode),
        )
        // AE/AWB lock are meaningful whenever the corresponding mode is auto;
        // with AE_MODE_OFF the exposure is already pinned, but setting the flag
        // keeps the device from drifting on the still request.
        builder.setCaptureRequestOption(CaptureRequest.CONTROL_AE_LOCK, s.aeLock)
        builder.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_LOCK, s.awbLock)

        try {
            Camera2CameraControl.from(cam.cameraControl)
                .setCaptureRequestOptions(builder.build())
        } catch (e: Exception) {
            Log.w(TAG, "setCaptureRequestOptions failed", e)
        }
    }

    @OptIn(ExperimentalCamera2Interop::class)
    private fun <T> Camera2Interop.Extender<T>.applyManualDefaults(
        caps: Capabilities,
        s: CameraSettings,
    ) {
        if (caps.supportsManualSensor) {
            setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_MODE,
                CameraMetadata.CONTROL_AE_MODE_OFF,
            )
            setCaptureRequestOption(CaptureRequest.SENSOR_EXPOSURE_TIME, s.exposureNs)
            setCaptureRequestOption(CaptureRequest.SENSOR_SENSITIVITY, s.iso)
            setCaptureRequestOption(
                CaptureRequest.CONTROL_AF_MODE,
                CameraMetadata.CONTROL_AF_MODE_OFF,
            )
            setCaptureRequestOption(CaptureRequest.LENS_FOCUS_DISTANCE, s.focusDiopters)
        }
        setCaptureRequestOption(
            CaptureRequest.CONTROL_AWB_MODE,
            Capabilities.awbModeValue(s.awbMode),
        )
        setCaptureRequestOption(CaptureRequest.CONTROL_AE_LOCK, s.aeLock)
        setCaptureRequestOption(CaptureRequest.CONTROL_AWB_LOCK, s.awbLock)
    }

    // ---------------------------------------------------------------- preview

    fun setStreaming(on: Boolean) {
        streaming.set(on)
        if (!on) frameSeq.set(0)
    }

    val isStreaming: Boolean get() = streaming.get()

    /** Runs on [analysisExecutor]. Must always close the proxy. */
    private fun analyze(image: ImageProxy) {
        try {
            if (!streaming.get()) return
            val quality = settings?.previewQuality ?: CameraSettings.DEFAULT_PREVIEW_QUALITY
            val frame = YuvJpeg.encode(image, quality, image.imageInfo.rotationDegrees)
            lastPreviewSize = frame.width to frame.height
            listener.onPreviewFrame(
                frame.jpeg,
                frame.width,
                frame.height,
                frameSeq.incrementAndGet().toLong(),
            )
        } catch (e: Throwable) {
            Log.w(TAG, "preview encode failed", e)
        } finally {
            image.close()
        }
    }

    // ---------------------------------------------------------------- capture

    /**
     * Full-resolution still, straight into memory. [onResult] and [onError] are
     * invoked on the capture executor.
     */
    fun capture(
        onResult: (jpeg: ByteArray, width: Int, height: Int, settings: CameraSettings?) -> Unit,
        onError: (message: String) -> Unit,
    ) {
        val capture = imageCapture
        if (capture == null) {
            onError("camera not ready")
            return
        }
        capture.takePicture(
            captureExecutor,
            object : ImageCapture.OnImageCapturedCallback() {
                override fun onCaptureSuccess(image: ImageProxy) {
                    try {
                        val buffer = image.planes[0].buffer
                        val bytes = ByteArray(buffer.remaining())
                        buffer.get(bytes)
                        onResult(bytes, image.width, image.height, settings)
                    } catch (e: Throwable) {
                        onError("capture read failed: ${e.message}")
                    } finally {
                        image.close()
                    }
                }

                override fun onError(exception: ImageCaptureException) {
                    Log.w(TAG, "takePicture failed", exception)
                    onError("capture failed: ${exception.message}")
                }
            },
        )
    }

    // --------------------------------------------------------------- lifecycle

    /** Keep the use cases pointing the right way if the display ever rotates. */
    @MainThread
    fun updateRotation() {
        val rotation = currentRotation()
        preview?.targetRotation = rotation
        imageAnalysis?.targetRotation = rotation
        imageCapture?.targetRotation = rotation
    }

    @MainThread
    fun release() {
        if (!released.compareAndSet(false, true)) return
        streaming.set(false)
        imageAnalysis?.clearAnalyzer()
        try {
            cameraProvider?.unbindAll()
        } catch (e: Exception) {
            Log.w(TAG, "unbindAll failed", e)
        }
        preview = null
        imageAnalysis = null
        imageCapture = null
        camera = null
        cameraProvider = null
        analysisExecutor.shutdown()
        captureExecutor.shutdown()
    }

    private fun currentRotation(): Int =
        previewView.display?.rotation ?: Surface.ROTATION_0

    companion object {
        private const val TAG = "CameraController"
        const val STILL_JPEG_QUALITY = 95
    }
}
