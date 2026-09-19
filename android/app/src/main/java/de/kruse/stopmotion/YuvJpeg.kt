package de.kruse.stopmotion

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream

/**
 * YUV_420_888 -> NV21 -> JPEG, without going through a Bitmap. This runs on the
 * analysis executor, never the main thread, and reuses one output buffer per
 * thread to keep the allocation churn down at 15 fps.
 */
object YuvJpeg {

    /** Reusable scratch, one per encoding thread. */
    private val scratch = ThreadLocal.withInitial { ByteArrayOutputStream(512 * 1024) }

    /**
     * Encode [image] as JPEG at [quality], rotating by [rotationDegrees] so the
     * desktop receives an upright frame with no EXIF to interpret.
     */
    fun encode(image: ImageProxy, quality: Int, rotationDegrees: Int): EncodedFrame {
        val width = image.width
        val height = image.height
        var nv21 = toNv21(image)
        var outWidth = width
        var outHeight = height

        val rotation = ((rotationDegrees % 360) + 360) % 360
        if (rotation != 0) {
            nv21 = rotateNv21(nv21, width, height, rotation)
            if (rotation == 90 || rotation == 270) {
                outWidth = height
                outHeight = width
            }
        }

        val out = scratch.get()!!
        out.reset()
        val yuvImage = YuvImage(nv21, ImageFormat.NV21, outWidth, outHeight, null)
        yuvImage.compressToJpeg(Rect(0, 0, outWidth, outHeight), quality, out)
        return EncodedFrame(out.toByteArray(), outWidth, outHeight)
    }

    data class EncodedFrame(val jpeg: ByteArray, val width: Int, val height: Int) {
        override fun equals(other: Any?): Boolean = this === other
        override fun hashCode(): Int = System.identityHashCode(this)
    }

    /**
     * Pack the three planes into NV21 (Y plane, then interleaved V/U), honouring
     * row and pixel strides. The fast path - a tightly packed Y plane and a
     * pixelStride-2 chroma plane - is the common case on real devices.
     */
    fun toNv21(image: ImageProxy): ByteArray {
        val width = image.width
        val height = image.height
        val ySize = width * height
        val out = ByteArray(ySize + ySize / 2)

        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        // --- Y ---
        val yBuffer = yPlane.buffer
        val yRowStride = yPlane.rowStride
        if (yRowStride == width) {
            yBuffer.get(out, 0, minOf(yBuffer.remaining(), ySize))
        } else {
            var pos = 0
            val row = ByteArray(yRowStride)
            for (r in 0 until height) {
                val toRead = minOf(yRowStride, yBuffer.remaining())
                if (toRead <= 0) break
                yBuffer.get(row, 0, toRead)
                System.arraycopy(row, 0, out, pos, minOf(width, toRead))
                pos += width
            }
        }

        // --- VU interleaved ---
        val chromaHeight = height / 2
        val chromaWidth = width / 2
        val uBuffer = uPlane.buffer
        val vBuffer = vPlane.buffer
        val uRowStride = uPlane.rowStride
        val vRowStride = vPlane.rowStride
        val uPixelStride = uPlane.pixelStride
        val vPixelStride = vPlane.pixelStride

        var outPos = ySize
        val uRow = ByteArray(uRowStride)
        val vRow = ByteArray(vRowStride)
        for (r in 0 until chromaHeight) {
            val uStart = r * uRowStride
            val vStart = r * vRowStride
            val uLen = minOf(uRowStride, uBuffer.limit() - uStart)
            val vLen = minOf(vRowStride, vBuffer.limit() - vStart)
            if (uLen <= 0 || vLen <= 0) break
            uBuffer.position(uStart)
            uBuffer.get(uRow, 0, uLen)
            vBuffer.position(vStart)
            vBuffer.get(vRow, 0, vLen)
            for (col in 0 until chromaWidth) {
                val uIdx = col * uPixelStride
                val vIdx = col * vPixelStride
                if (uIdx >= uLen || vIdx >= vLen || outPos + 1 >= out.size) break
                out[outPos++] = vRow[vIdx]
                out[outPos++] = uRow[uIdx]
            }
        }
        return out
    }

    /**
     * Rotate an NV21 buffer clockwise by 90, 180 or 270 degrees. Chroma is
     * sampled per 2x2 block, so the output stays valid NV21.
     */
    fun rotateNv21(input: ByteArray, width: Int, height: Int, rotation: Int): ByteArray {
        if (rotation == 0) return input
        require(rotation == 90 || rotation == 180 || rotation == 270) {
            "unsupported rotation $rotation"
        }
        val output = ByteArray(input.size)
        val frameSize = width * height
        val swap = rotation % 180 != 0
        val xflip = rotation % 270 != 0
        val yflip = rotation >= 180
        val outWidth = if (swap) height else width
        val outHeight = if (swap) width else height

        for (j in 0 until height) {
            for (i in 0 until width) {
                val yIn = j * width + i
                val uIn = frameSize + (j shr 1) * width + (i and 1.inv())
                val vIn = uIn + 1
                if (vIn >= input.size) continue

                val iSwapped = if (swap) j else i
                val jSwapped = if (swap) i else j
                val iOut = if (xflip) outWidth - iSwapped - 1 else iSwapped
                val jOut = if (yflip) outHeight - jSwapped - 1 else jSwapped

                val yOut = jOut * outWidth + iOut
                val uOut = frameSize + (jOut shr 1) * outWidth + (iOut and 1.inv())
                val vOut = uOut + 1
                if (vOut >= output.size) continue

                output[yOut] = input[yIn]
                output[uOut] = input[uIn]
                output[vOut] = input[vIn]
            }
        }
        return output
    }
}
