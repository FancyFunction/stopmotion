package de.kruse.stopmotion

import org.json.JSONObject

/**
 * The camera state the desktop owns. The phone never invents values; it only
 * clamps what it is told to what the hardware actually supports and reports the
 * result back in CONFIG_ACK.
 *
 * Field names mirror `CameraSettings` in docs/interfaces.md so the JSON round
 * trips without translation.
 */
data class CameraSettings(
    val exposureNs: Long,
    val iso: Int,
    val focusDiopters: Float,
    val awbMode: String,
    val aeLock: Boolean,
    val afLock: Boolean,
    val awbLock: Boolean,
    val previewWidth: Int,
    val previewHeight: Int,
    val previewQuality: Int,
) {

    fun toJson(): JSONObject = JSONObject().apply {
        put("exposure_ns", exposureNs)
        put("iso", iso)
        put("focus_diopters", focusDiopters.toDouble())
        put("awb_mode", awbMode)
        put("ae_lock", aeLock)
        put("af_lock", afLock)
        put("awb_lock", awbLock)
        put("preview_width", previewWidth)
        put("preview_height", previewHeight)
        put("preview_quality", previewQuality)
    }

    /**
     * Apply whatever keys the desktop sent on top of the current state. Absent
     * keys keep their current value, so SET_CONFIG can be a partial update.
     */
    fun merged(json: JSONObject): CameraSettings = copy(
        exposureNs = if (json.has("exposure_ns")) json.optLong("exposure_ns", exposureNs) else exposureNs,
        iso = if (json.has("iso")) json.optInt("iso", iso) else iso,
        focusDiopters = if (json.has("focus_diopters")) {
            json.optDouble("focus_diopters", focusDiopters.toDouble()).toFloat()
        } else {
            focusDiopters
        },
        awbMode = json.optString("awb_mode", awbMode).ifEmpty { awbMode },
        aeLock = json.optBoolean("ae_lock", aeLock),
        afLock = json.optBoolean("af_lock", afLock),
        awbLock = json.optBoolean("awb_lock", awbLock),
        previewWidth = if (json.has("preview_width")) json.optInt("preview_width", previewWidth) else previewWidth,
        previewHeight = if (json.has("preview_height")) json.optInt("preview_height", previewHeight) else previewHeight,
        previewQuality = if (json.has("preview_quality")) {
            json.optInt("preview_quality", previewQuality)
        } else {
            previewQuality
        },
    )

    /** Clamp to what this device can actually do. The clamped values are what CONFIG_ACK reports. */
    fun clamped(caps: Capabilities): CameraSettings {
        val size = caps.nearestPreviewSize(previewWidth, previewHeight)
        val awb = if (caps.awbModes.contains(awbMode)) awbMode else caps.awbModes.firstOrNull() ?: "AUTO"
        return copy(
            exposureNs = exposureNs.coerceIn(caps.exposureNsLo, caps.exposureNsHi),
            iso = iso.coerceIn(caps.isoLo, caps.isoHi),
            focusDiopters = focusDiopters.coerceIn(caps.focusDioptersLo, caps.focusDioptersHi),
            awbMode = awb,
            previewWidth = size.first,
            previewHeight = size.second,
            previewQuality = previewQuality.coerceIn(1, 100),
        )
    }

    /** True when the two settings differ in a way that needs ImageAnalysis rebound. */
    fun needsRebind(other: CameraSettings): Boolean =
        previewWidth != other.previewWidth || previewHeight != other.previewHeight

    fun summary(): String =
        "%.1fms iso%d f%.2f %s%s".format(
            exposureNs / 1_000_000.0,
            iso,
            focusDiopters,
            awbMode.lowercase(),
            buildString {
                if (aeLock || afLock || awbLock) {
                    append(" lock:")
                    if (aeLock) append("ae")
                    if (afLock) append("af")
                    if (awbLock) append("awb")
                }
            },
        )

    companion object {
        const val DEFAULT_PREVIEW_WIDTH = 1920
        const val DEFAULT_PREVIEW_HEIGHT = 1080
        const val DEFAULT_PREVIEW_QUALITY = 70

        /** Sensible starting point, already clamped to the device. */
        fun defaults(caps: Capabilities): CameraSettings {
            val exposure = 8_000_000L // 1/125 s
            val iso = 200
            return CameraSettings(
                exposureNs = exposure,
                iso = iso,
                focusDiopters = 0f,
                awbMode = if (caps.awbModes.contains("AUTO")) "AUTO" else caps.awbModes.firstOrNull() ?: "AUTO",
                aeLock = false,
                afLock = false,
                awbLock = false,
                previewWidth = DEFAULT_PREVIEW_WIDTH,
                previewHeight = DEFAULT_PREVIEW_HEIGHT,
                previewQuality = DEFAULT_PREVIEW_QUALITY,
            ).clamped(caps)
        }
    }
}
