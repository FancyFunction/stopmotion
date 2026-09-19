package de.kruse.stopmotion

import android.graphics.ImageFormat
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.params.StreamConfigurationMap
import android.util.Log
import android.util.Range
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.CameraInfo
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs

/**
 * What this device can actually do, read once from [CameraCharacteristics] and
 * shipped to the desktop in HELLO.
 *
 * The JSON keys are fixed by `CameraCapabilities.from_hello` in
 * docs/interfaces.md - do not rename them.
 */
class Capabilities(
    val hardwareLevel: String,
    val sensorWidth: Int,
    val sensorHeight: Int,
    val previewSizes: List<Pair<Int, Int>>,
    val exposureNsLo: Long,
    val exposureNsHi: Long,
    val isoLo: Int,
    val isoHi: Int,
    val focusDioptersLo: Float,
    val focusDioptersHi: Float,
    val focusCalibration: String,
    val awbModes: List<String>,
    /** Largest JPEG the device can produce. Not part of HELLO; used to size ImageCapture. */
    val maxStillSize: Pair<Int, Int>,
    /** True when CONTROL_AE_MODE=OFF / CONTROL_AF_MODE=OFF are honoured. */
    val supportsManualSensor: Boolean,
) {

    /**
     * The HELLO payload. Exactly the keys documented in docs/interfaces.md and
     * nothing more - the desktop's parser is the contract, so extra keys stay
     * out of it.
     */
    fun toHelloJson(): JSONObject = JSONObject().apply {
        put("hardware_level", hardwareLevel)
        put("sensor_size", JSONArray().put(sensorWidth).put(sensorHeight))
        put(
            "preview_sizes",
            JSONArray().apply {
                previewSizes.forEach { (w, h) -> put(JSONArray().put(w).put(h)) }
            },
        )
        put("exposure_ns", JSONObject().put("lo", exposureNsLo).put("hi", exposureNsHi))
        put("iso", JSONObject().put("lo", isoLo).put("hi", isoHi))
        put(
            "focus_diopters",
            JSONObject()
                .put("lo", focusDioptersLo.toDouble())
                .put("hi", focusDioptersHi.toDouble()),
        )
        put("focus_calibration", focusCalibration)
        put("awb_modes", JSONArray().apply { awbModes.forEach { put(it) } })
    }

    /**
     * Pick the supported analysis size closest to what was asked for. Exact
     * match wins; otherwise the nearest by pixel count, nudged towards the
     * closer aspect ratio.
     */
    fun nearestPreviewSize(width: Int, height: Int): Pair<Int, Int> {
        if (previewSizes.isEmpty()) return width to height
        previewSizes.firstOrNull { it.first == width && it.second == height }?.let { return it }
        val wantPixels = width.toLong() * height.toLong()
        val wantAspect = if (height > 0) width.toDouble() / height.toDouble() else 16.0 / 9.0
        return previewSizes.minByOrNull { (w, h) ->
            val pixelDelta = abs(w.toLong() * h.toLong() - wantPixels).toDouble()
            val aspect = if (h > 0) w.toDouble() / h.toDouble() else 0.0
            pixelDelta + abs(aspect - wantAspect) * wantPixels * 0.05
        } ?: (width to height)
    }

    /**
     * Reads characteristic keys. One implementation wraps a raw
     * [CameraCharacteristics], the other CameraX's `Camera2CameraInfo`.
     */
    interface Reader {
        fun <T> get(key: CameraCharacteristics.Key<T>): T?
    }

    companion object {
        private const val TAG = "Capabilities"

        /** Safety net so a device that reports nothing still gives the UI a usable range. */
        private const val FALLBACK_EXPOSURE_LO = 100_000L // 1/10000 s
        private const val FALLBACK_EXPOSURE_HI = 500_000_000L // 1/2 s
        private const val FALLBACK_ISO_LO = 50
        private const val FALLBACK_ISO_HI = 3200

        @androidx.annotation.OptIn(ExperimentalCamera2Interop::class)
        fun from(cameraInfo: CameraInfo): Capabilities {
            val info = Camera2CameraInfo.from(cameraInfo)
            return read(object : Reader {
                override fun <T> get(key: CameraCharacteristics.Key<T>): T? =
                    try {
                        info.getCameraCharacteristic(key)
                    } catch (e: Exception) {
                        Log.w(TAG, "characteristic ${key.name} unreadable: ${e.message}")
                        null
                    }
            })
        }

        fun from(characteristics: CameraCharacteristics): Capabilities =
            read(object : Reader {
                override fun <T> get(key: CameraCharacteristics.Key<T>): T? =
                    try {
                        characteristics.get(key)
                    } catch (e: Exception) {
                        Log.w(TAG, "characteristic ${key.name} unreadable: ${e.message}")
                        null
                    }
            })

        private fun read(c: Reader): Capabilities {
            val hardwareLevel =
                hardwareLevelName(c.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL))

            val activeArray = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            val pixelArray = c.get(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE)
            val sensorWidth = activeArray?.width() ?: pixelArray?.width ?: 0
            val sensorHeight = activeArray?.height() ?: pixelArray?.height ?: 0

            val map: StreamConfigurationMap? =
                c.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)

            val previewSizes = (map?.getOutputSizes(ImageFormat.YUV_420_888) ?: emptyArray())
                .map { it.width to it.height }
                .distinct()
                .sortedByDescending { it.first.toLong() * it.second.toLong() }

            val stillSizes = (map?.getOutputSizes(ImageFormat.JPEG) ?: emptyArray())
                .map { it.width to it.height }
                .sortedByDescending { it.first.toLong() * it.second.toLong() }
            val maxStill = stillSizes.firstOrNull()
                ?: previewSizes.firstOrNull()
                ?: (sensorWidth to sensorHeight)

            val exposureRange: Range<Long>? =
                c.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
            val isoRange: Range<Int>? =
                c.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)

            val minFocusDistance: Float =
                c.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE) ?: 0f
            val focusCalibration =
                focusCalibrationName(c.get(CameraCharacteristics.LENS_INFO_FOCUS_DISTANCE_CALIBRATION))

            val awbModes = (c.get(CameraCharacteristics.CONTROL_AWB_AVAILABLE_MODES) ?: IntArray(0))
                .map { awbModeName(it) }
                .distinct()
                .ifEmpty { listOf("AUTO") }

            val capabilities =
                c.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES) ?: IntArray(0)
            val manual = capabilities.contains(
                CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR,
            )

            return Capabilities(
                hardwareLevel = hardwareLevel,
                sensorWidth = sensorWidth,
                sensorHeight = sensorHeight,
                previewSizes = previewSizes,
                exposureNsLo = exposureRange?.lower ?: FALLBACK_EXPOSURE_LO,
                exposureNsHi = exposureRange?.upper ?: FALLBACK_EXPOSURE_HI,
                isoLo = isoRange?.lower ?: FALLBACK_ISO_LO,
                isoHi = isoRange?.upper ?: FALLBACK_ISO_HI,
                // LENS_FOCUS_DISTANCE is in diopters: 0 is infinity, and the
                // minimum focus *distance* is the maximum diopter value.
                focusDioptersLo = 0f,
                focusDioptersHi = minFocusDistance,
                focusCalibration = focusCalibration,
                awbModes = awbModes,
                maxStillSize = maxStill,
                supportsManualSensor = manual,
            ).also {
                Log.i(
                    TAG,
                    "level=$hardwareLevel sensor=${sensorWidth}x$sensorHeight " +
                        "still=${maxStill.first}x${maxStill.second} " +
                        "analysisSizes=${previewSizes.size} manual=$manual",
                )
            }
        }

        private fun hardwareLevelName(level: Int?): String = when (level) {
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL -> "EXTERNAL"
            else -> "UNKNOWN"
        }

        private fun focusCalibrationName(value: Int?): String = when (value) {
            CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_CALIBRATED -> "CALIBRATED"
            CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_APPROXIMATE -> "APPROXIMATE"
            CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_UNCALIBRATED -> "UNCALIBRATED"
            else -> "UNCALIBRATED"
        }

        private val AWB_NAMES = mapOf(
            CameraMetadata.CONTROL_AWB_MODE_OFF to "OFF",
            CameraMetadata.CONTROL_AWB_MODE_AUTO to "AUTO",
            CameraMetadata.CONTROL_AWB_MODE_INCANDESCENT to "INCANDESCENT",
            CameraMetadata.CONTROL_AWB_MODE_FLUORESCENT to "FLUORESCENT",
            CameraMetadata.CONTROL_AWB_MODE_WARM_FLUORESCENT to "WARM_FLUORESCENT",
            CameraMetadata.CONTROL_AWB_MODE_DAYLIGHT to "DAYLIGHT",
            CameraMetadata.CONTROL_AWB_MODE_CLOUDY_DAYLIGHT to "CLOUDY_DAYLIGHT",
            CameraMetadata.CONTROL_AWB_MODE_TWILIGHT to "TWILIGHT",
            CameraMetadata.CONTROL_AWB_MODE_SHADE to "SHADE",
        )

        fun awbModeName(value: Int): String = AWB_NAMES[value] ?: "AWB_$value"

        fun awbModeValue(name: String): Int =
            AWB_NAMES.entries.firstOrNull { it.value.equals(name, ignoreCase = true) }?.key
                ?: CameraMetadata.CONTROL_AWB_MODE_AUTO
    }
}
