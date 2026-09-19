package de.kruse.stopmotion

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.EOFException
import java.nio.charset.StandardCharsets

/**
 * Wire protocol, Plan.md section 4 / docs/interfaces.md.
 *
 *   [4B BE total_len][1B type][payload]
 *
 * where `total_len` covers the type byte plus the payload, and the payload is
 *
 *   [2B BE header_len][utf-8 JSON header][optional raw bytes]
 *
 * Pure-JSON messages simply have no trailing raw bytes.
 */
object Protocol {

    const val HELLO = 0x01
    const val SET_CONFIG = 0x02
    const val CONFIG_ACK = 0x03
    const val PREVIEW_CTL = 0x04
    const val PREVIEW_FRAME = 0x05
    const val CAPTURE = 0x06
    const val CAPTURE_RESULT = 0x07
    const val ERROR = 0x08
    const val PING = 0x09
    const val PONG = 0x0A

    /** Matches the desktop's limit in docs/interfaces.md. */
    const val MAX_MESSAGE = 64 * 1024 * 1024

    fun typeName(type: Int): String = when (type) {
        HELLO -> "HELLO"
        SET_CONFIG -> "SET_CONFIG"
        CONFIG_ACK -> "CONFIG_ACK"
        PREVIEW_CTL -> "PREVIEW_CTL"
        PREVIEW_FRAME -> "PREVIEW_FRAME"
        CAPTURE -> "CAPTURE"
        CAPTURE_RESULT -> "CAPTURE_RESULT"
        ERROR -> "ERROR"
        PING -> "PING"
        PONG -> "PONG"
        else -> "0x%02X".format(type)
    }

    /** A decoded inbound message. [blob] is null when nothing followed the header. */
    data class Message(val type: Int, val header: JSONObject, val blob: ByteArray?) {
        override fun equals(other: Any?): Boolean = this === other
        override fun hashCode(): Int = System.identityHashCode(this)
    }

    /**
     * Serialise one message into a single byte array, ready to be handed to the
     * writer thread. Building the whole frame up front is what keeps a large
     * still from interleaving with a preview frame on the socket.
     */
    fun encode(type: Int, header: JSONObject?, blob: ByteArray? = null): ByteArray {
        val headerBytes = (header ?: JSONObject()).toString().toByteArray(StandardCharsets.UTF_8)
        require(headerBytes.size <= 0xFFFF) { "header too large: ${headerBytes.size}" }
        val blobLen = blob?.size ?: 0
        val payloadLen = 2 + headerBytes.size + blobLen
        val totalLen = 1 + payloadLen

        val out = ByteArrayOutputStream(4 + totalLen)
        out.write((totalLen ushr 24) and 0xFF)
        out.write((totalLen ushr 16) and 0xFF)
        out.write((totalLen ushr 8) and 0xFF)
        out.write(totalLen and 0xFF)
        out.write(type and 0xFF)
        out.write((headerBytes.size ushr 8) and 0xFF)
        out.write(headerBytes.size and 0xFF)
        out.write(headerBytes)
        if (blob != null && blobLen > 0) out.write(blob)
        return out.toByteArray()
    }

    /**
     * Read exactly one message off [input], blocking until it is complete.
     * Returns null on a clean end of stream.
     */
    @Throws(java.io.IOException::class)
    fun readMessage(input: DataInputStream): Message? {
        val totalLen = try {
            input.readInt()
        } catch (_: EOFException) {
            return null
        }
        if (totalLen < 1 || totalLen > MAX_MESSAGE) {
            throw ProtocolException("bad frame length $totalLen")
        }
        val type = input.readUnsignedByte()
        val payloadLen = totalLen - 1
        if (payloadLen < 2) {
            // Tolerate a bare type byte (an empty PING/PONG from a sloppy peer).
            return Message(type, JSONObject(), null)
        }
        val headerLen = input.readUnsignedShort()
        if (headerLen > payloadLen - 2) {
            throw ProtocolException("header length $headerLen overruns payload $payloadLen")
        }
        val headerBytes = ByteArray(headerLen)
        input.readFully(headerBytes)
        val blobLen = payloadLen - 2 - headerLen
        val blob = if (blobLen > 0) ByteArray(blobLen).also { input.readFully(it) } else null

        val header = if (headerLen == 0) {
            JSONObject()
        } else {
            try {
                JSONObject(String(headerBytes, StandardCharsets.UTF_8))
            } catch (e: Exception) {
                throw ProtocolException("malformed JSON header: ${e.message}")
            }
        }
        return Message(type, header, blob)
    }
}

class ProtocolException(message: String) : java.io.IOException(message)
