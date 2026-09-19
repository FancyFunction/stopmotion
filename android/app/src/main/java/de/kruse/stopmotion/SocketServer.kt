package de.kruse.stopmotion

import android.util.Log
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * The phone's half of the wire. A ServerSocket on 127.0.0.1 only - the desktop
 * reaches it over `adb forward`, so nothing is exposed on the network and no
 * authentication is needed (Requirements.md: USB-only, personal tool).
 *
 * One client at a time. Three threads per connection:
 *   accept   - waits for the desktop, then hands off and blocks again
 *   reader   - parses inbound frames and dispatches to [Handler]
 *   writer   - the only thread that ever touches the output stream, so a
 *              multi-megabyte still can never interleave with a preview frame
 */
class SocketServer(
    private val port: Int = DEFAULT_PORT,
    private val handler: Handler,
) {

    /**
     * Inbound messages. All callbacks arrive on the reader thread; the
     * implementation is responsible for hopping to whatever thread it needs.
     */
    interface Handler {
        /** The HELLO payload to send the moment a client connects. */
        fun buildHello(): JSONObject?

        fun onClientConnected()
        fun onClientDisconnected(reason: String)
        fun onSetConfig(json: JSONObject)
        fun onPreviewCtl(json: JSONObject)
        fun onCapture(json: JSONObject)
        fun onStateChanged(state: State)
    }

    enum class State { STOPPED, LISTENING, CONNECTED }

    /** One queued outbound frame. A null [frame] is the writer's poison pill. */
    private class Item(val frame: ByteArray?, val critical: Boolean)

    private val running = AtomicBoolean(false)
    private val framesSent = AtomicLong(0)
    private val framesDropped = AtomicLong(0)

    @Volatile
    private var serverSocket: ServerSocket? = null

    @Volatile
    private var acceptThread: Thread? = null

    @Volatile
    private var connection: Connection? = null

    @Volatile
    var state: State = State.STOPPED
        private set

    val previewFramesSent: Long get() = framesSent.get()
    val previewFramesDropped: Long get() = framesDropped.get()
    val isConnected: Boolean get() = connection != null

    // ------------------------------------------------------------------ start

    fun start() {
        if (!running.compareAndSet(false, true)) return
        framesSent.set(0)
        framesDropped.set(0)
        val thread = Thread(::acceptLoop, "stopmotion-accept")
        thread.isDaemon = true
        acceptThread = thread
        thread.start()
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        try {
            serverSocket?.close()
        } catch (e: IOException) {
            Log.w(TAG, "closing server socket: ${e.message}")
        }
        serverSocket = null
        connection?.close("server stopping")
        connection = null
        acceptThread?.interrupt()
        acceptThread = null
        setState(State.STOPPED)
    }

    private fun acceptLoop() {
        while (running.get()) {
            val server = try {
                ServerSocket().apply {
                    reuseAddress = true
                    bind(InetSocketAddress(InetAddress.getByName(LOOPBACK), port))
                }
            } catch (e: IOException) {
                Log.e(TAG, "cannot bind $LOOPBACK:$port: ${e.message}")
                setState(State.STOPPED)
                sleepQuietly(1000)
                continue
            }
            serverSocket = server
            setState(State.LISTENING)
            Log.i(TAG, "listening on $LOOPBACK:$port")

            while (running.get()) {
                val socket = try {
                    server.accept()
                } catch (e: IOException) {
                    if (running.get()) Log.w(TAG, "accept failed: ${e.message}")
                    break
                }
                serve(socket)
                if (!running.get()) break
                setState(State.LISTENING)
            }

            try {
                server.close()
            } catch (_: IOException) {
                // already closing
            }
            if (serverSocket === server) serverSocket = null
        }
        setState(State.STOPPED)
    }

    /** Blocks for the lifetime of one client. */
    private fun serve(socket: Socket) {
        val conn = try {
            Connection(socket)
        } catch (e: IOException) {
            Log.w(TAG, "cannot set up connection: ${e.message}")
            return
        }
        connection = conn
        setState(State.CONNECTED)
        Log.i(TAG, "client connected from ${socket.remoteSocketAddress}")
        handler.onClientConnected()

        handler.buildHello()?.let { conn.enqueue(Protocol.encode(Protocol.HELLO, it), critical = true) }

        val reason = conn.readLoop()
        conn.close(reason)
        if (connection === conn) connection = null
        Log.i(TAG, "client gone: $reason")
        handler.onClientDisconnected(reason)
    }

    // -------------------------------------------------------------- outbound

    /**
     * Queue a preview frame. Preview frames are droppable by design: if the
     * writer is still pushing a still image we would rather lose the frame than
     * build a backlog.
     */
    fun sendPreviewFrame(jpeg: ByteArray, width: Int, height: Int, seq: Long) {
        val conn = connection ?: return
        if (conn.pendingPreviewFrames() >= MAX_QUEUED_PREVIEW) {
            framesDropped.incrementAndGet()
            return
        }
        val header = JSONObject()
            .put("seq", seq)
            .put("w", width)
            .put("h", height)
        if (conn.enqueue(Protocol.encode(Protocol.PREVIEW_FRAME, header, jpeg), critical = false)) {
            framesSent.incrementAndGet()
        } else {
            framesDropped.incrementAndGet()
        }
    }

    fun sendCaptureResult(
        requestId: String,
        width: Int,
        height: Int,
        settings: JSONObject,
        jpeg: ByteArray,
    ) {
        val header = JSONObject()
            .put("request_id", requestId)
            .put("w", width)
            .put("h", height)
            .put("settings", settings)
        send(Protocol.encode(Protocol.CAPTURE_RESULT, header, jpeg))
    }

    /**
     * Send HELLO out of band. Used when a client connects before the camera has
     * finished reporting its capabilities.
     */
    fun sendHello(hello: JSONObject) {
        send(Protocol.encode(Protocol.HELLO, hello))
    }

    fun sendConfigAck(applied: JSONObject) {
        send(Protocol.encode(Protocol.CONFIG_ACK, applied))
    }

    fun sendError(code: String, message: String, requestId: String? = null) {
        val header = JSONObject().put("code", code).put("message", message)
        if (requestId != null) header.put("request_id", requestId)
        send(Protocol.encode(Protocol.ERROR, header))
    }

    private fun send(frame: ByteArray) {
        connection?.enqueue(frame, critical = true)
    }

    private fun setState(newState: State) {
        if (state != newState) {
            state = newState
            handler.onStateChanged(newState)
        }
    }

    // ------------------------------------------------------------ connection

    private inner class Connection(private val socket: Socket) {

        private val input = DataInputStream(socket.getInputStream().buffered(IO_BUFFER))
        private val output = BufferedOutputStream(socket.getOutputStream(), IO_BUFFER)
        private val queue = LinkedBlockingQueue<Item>(MAX_QUEUE)
        private val alive = AtomicBoolean(true)
        private val writer: Thread

        init {
            socket.tcpNoDelay = true
            socket.keepAlive = true
            writer = Thread(::writeLoop, "stopmotion-writer").apply {
                isDaemon = true
                start()
            }
        }

        fun pendingPreviewFrames(): Int = queue.count { !it.critical }

        /** @return true if the frame was accepted onto the writer queue. */
        fun enqueue(frame: ByteArray, critical: Boolean): Boolean {
            if (!alive.get()) return false
            val item = Item(frame, critical)
            return if (critical) {
                try {
                    // Capture results and acks must not be dropped; block briefly
                    // rather than lose them.
                    queue.offer(item, 5, TimeUnit.SECONDS)
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    false
                }
            } else {
                queue.offer(item)
            }
        }

        private fun writeLoop() {
            try {
                while (alive.get()) {
                    val item = queue.poll(500, TimeUnit.MILLISECONDS) ?: continue
                    val frame = item.frame ?: break // poison pill
                    output.write(frame)
                    // Flush per message: the desktop wants preview frames
                    // promptly, and one message is one atomic unit on the wire.
                    output.flush()
                }
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            } catch (e: IOException) {
                if (alive.get()) Log.w(TAG, "write failed: ${e.message}")
                close("write failed: ${e.message}")
            }
        }

        /** @return the reason the connection ended. */
        fun readLoop(): String {
            return try {
                while (alive.get()) {
                    val msg = Protocol.readMessage(input) ?: return "peer closed"
                    dispatch(msg)
                }
                "closed"
            } catch (e: ProtocolException) {
                "protocol error: ${e.message}"
            } catch (e: SocketException) {
                "socket closed: ${e.message}"
            } catch (e: IOException) {
                "io error: ${e.message}"
            } catch (e: Exception) {
                Log.e(TAG, "reader crashed", e)
                "reader crashed: ${e.message}"
            }
        }

        private fun dispatch(msg: Protocol.Message) {
            when (msg.type) {
                Protocol.SET_CONFIG -> handler.onSetConfig(msg.header)
                Protocol.PREVIEW_CTL -> handler.onPreviewCtl(msg.header)
                Protocol.CAPTURE -> handler.onCapture(msg.header)
                Protocol.PING -> enqueue(Protocol.encode(Protocol.PONG, JSONObject()), critical = true)
                Protocol.PONG -> Unit
                else -> {
                    Log.w(TAG, "ignoring ${Protocol.typeName(msg.type)} from desktop")
                    sendError("unexpected_message", "phone does not accept ${Protocol.typeName(msg.type)}")
                }
            }
        }

        fun close(reason: String) {
            if (!alive.compareAndSet(true, false)) return
            Log.d(TAG, "closing connection: $reason")
            queue.clear()
            queue.offer(Item(null, true)) // wake the writer
            try {
                socket.close()
            } catch (_: IOException) {
                // nothing useful to do
            }
            writer.interrupt()
        }
    }

    companion object {
        private const val TAG = "SocketServer"
        const val DEFAULT_PORT = 8099
        private const val LOOPBACK = "127.0.0.1"
        private const val IO_BUFFER = 64 * 1024
        private const val MAX_QUEUE = 8
        private const val MAX_QUEUED_PREVIEW = 2

        private fun sleepQuietly(ms: Long) {
            try {
                Thread.sleep(ms)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
    }
}
