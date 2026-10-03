package com.usbdisplay.receiver;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.content.res.Configuration;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Minimal UDSP/1 MJPEG receiver (Phase 2.5).
 * Connects to 127.0.0.1:PORT (via `adb forward`), parses, CRC-checks,
 * decodes JPEG on the worker thread, renders aspect-fit on a SurfaceView.
 * No MediaCodec/H.264/USB/Wi-Fi/audio/touch. UI stays off the hot path.
 */
public class MainActivity extends Activity {
    private static final String TAG = "USBDISP";
    static final String HOST = "127.0.0.1";
    static final int PORT = 18958;
    /**
     * Diagnostic overlay switch. ON for measurement runs; set false (or tap
     * the video) for a pure full-screen display. Single constant on purpose.
     */
    private static final boolean DEBUG_OVERLAY = true;

    private TextView statusView;
    private SurfaceView surfaceView;
    private Button connectButton;
    private TextView titleView;
    private LinearLayout rootLayout;
    private LinearLayout overlayLayout;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean cleanupStarted = new AtomicBoolean();
    private volatile Worker worker;
    private int streamWidth;
    private int streamHeight;
    private int originalRequestedOrientation;
    private boolean orientationLocked;
    /** Set by the SurfaceHolder callback; H.264 configure waits for a valid surface. */
    volatile boolean surfaceReady;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        hideSystemUi();
        streamWidth = Math.max(2, getIntent().getIntExtra("stream_width", 800));
        streamHeight = Math.max(2, getIntent().getIntExtra("stream_height", 600));
        lockCurrentOrientation();

        // Full-screen video (MATCH_PARENT in a FrameLayout) with a tap-toggle
        // stats overlay (Phase 3.3 display contract: maximum tablet area; the
        // overlay floats above the video and never changes its geometry, so edge
        // verification runs with the overlay hidden).
        android.widget.FrameLayout root = new android.widget.FrameLayout(this);
        root.setBackgroundColor(0xFF000000);

        surfaceView = new SurfaceView(this);
        android.widget.FrameLayout.LayoutParams slp =
                new android.widget.FrameLayout.LayoutParams(
                        android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
                        android.widget.FrameLayout.LayoutParams.MATCH_PARENT);
        surfaceView.setLayoutParams(slp);
        root.addView(surfaceView);

        LinearLayout overlay = new LinearLayout(this);
        overlay.setOrientation(LinearLayout.VERTICAL);
        overlay.setBackgroundColor(0x99000000); // corner chip, translucent
        overlay.setPadding(8, 6, 8, 6);
        overlayLayout = overlay;
        if (!DEBUG_OVERLAY) overlay.setVisibility(android.view.View.GONE);

        TextView title = new TextView(this);
        title.setText("USB Display v0.95-Beta");
        title.setTextSize(12);
        title.setTextColor(0xFFFFFFFF);
        overlay.addView(title);
        titleView = title;

        statusView = new TextView(this);
        statusView.setText("Status: idle — press Connect (adb reverse tcp:" + PORT + " tcp:" + PORT + ")");
        statusView.setTextSize(11);
        statusView.setTextColor(0xFFFFFFFF);
        statusView.setMaxLines(7);
        statusView.setMaxWidth(600);
        overlay.addView(statusView);

        connectButton = new Button(this);
        connectButton.setText("Connect");
        connectButton.setMinHeight(0);
        connectButton.setMinimumHeight(0);
        connectButton.setOnClickListener(v -> toggle());
        overlay.addView(connectButton);

        android.widget.FrameLayout.LayoutParams olp =
                new android.widget.FrameLayout.LayoutParams(
                        android.widget.FrameLayout.LayoutParams.WRAP_CONTENT,
                        android.widget.FrameLayout.LayoutParams.WRAP_CONTENT);
        olp.gravity = android.view.Gravity.TOP | android.view.Gravity.START;
        olp.setMargins(8, 8, 0, 0);
        overlay.setLayoutParams(olp);
        root.addView(overlay);
        surfaceView.setOnClickListener(v -> {
            overlayLayout.setVisibility(
                    overlayLayout.getVisibility() == android.view.View.GONE
                            ? android.view.View.VISIBLE : android.view.View.GONE);
        });
        surfaceView.getHolder().addCallback(new SurfaceHolder.Callback() {
            @Override
            public void surfaceCreated(SurfaceHolder h) {
                surfaceReady = true;
                Log.i(TAG, "surface created");
            }

            @Override
            public void surfaceChanged(SurfaceHolder h, int f, int w, int ht) {
                // Keep the immutable session aspect ratio even when a vendor ROM
                // ignores the Activity orientation lock and rotates the root view.
                android.view.ViewParent rp = surfaceView.getParent();
                int rw = w, rh = ht;
                if (rp instanceof android.view.View) {
                    rw = ((android.view.View) rp).getWidth();
                    rh = ((android.view.View) rp).getHeight();
                }
                if (rw <= 0) rw = w;
                if (rh <= 0) rh = ht;

                int targetW = rw;
                int targetH = rh;
                if ((long) rw * streamHeight <= (long) rh * streamWidth) {
                    targetH = Math.max(1, (int) ((long) rw * streamHeight / streamWidth));
                } else {
                    targetW = Math.max(1, (int) ((long) rh * streamWidth / streamHeight));
                }

                android.widget.FrameLayout.LayoutParams cur =
                        (android.widget.FrameLayout.LayoutParams) surfaceView.getLayoutParams();
                if (cur == null
                        || cur.width != targetW
                        || cur.height != targetH
                        || cur.gravity != android.view.Gravity.CENTER) {
                    android.widget.FrameLayout.LayoutParams nlp =
                            new android.widget.FrameLayout.LayoutParams(
                                    targetW, targetH);
                    nlp.gravity = android.view.Gravity.CENTER;
                    surfaceView.setLayoutParams(nlp);
                }
                Log.i(TAG, "surface aspect-fit stream=" + streamWidth + "x" + streamHeight
                        + " root=" + rw + "x" + rh
                        + " target=" + targetW + "x" + targetH);
            }

            @Override
            public void surfaceDestroyed(SurfaceHolder h) {
                surfaceReady = false;
                Log.i(TAG, "surface destroyed");
            }
        });
        android.widget.FrameLayout.LayoutParams lp = new android.widget.FrameLayout.LayoutParams(
                android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
                android.widget.FrameLayout.LayoutParams.MATCH_PARENT);
        surfaceView.setLayoutParams(lp);

        setContentView(root);

        // Test hook for automated runs: `am start ... --ez autoconnect true`
        // connects without a UI tap (button position shifts with status length,
        // making fixed-coordinate taps fragile). Not used by normal launches.
        if (getIntent() != null && getIntent().getBooleanExtra("autoconnect", false)) {
            connectButton.postDelayed(() -> {
                if (worker == null) toggle();
            }, 3000);
        }
    }

    private void lockCurrentOrientation() {
        originalRequestedOrientation = getRequestedOrientation();
        int hostRotation = getIntent().getIntExtra("session_rotation", -1);
        int displayRotation = getWindowManager().getDefaultDisplay().getRotation();
        int configurationOrientation = getResources().getConfiguration().orientation;
        int requestedOrientation = orientationForRotation(
                displayRotation, configurationOrientation);
        setRequestedOrientation(requestedOrientation);
        orientationLocked = true;
        Log.i(TAG, "orientation locked for session hostRotation=" + hostRotation
                + " displayRotation=" + displayRotation
                + " configurationOrientation=" + configurationOrientation
                + " requestedOrientation=" + requestedOrientation);
    }

    /**
     * Convert the session-start display rotation into an explicit Activity
     * orientation. SCREEN_ORIENTATION_LOCKED is not reliable on every vendor
     * Android build; an explicit orientation prevents the decoder Surface from
     * following the sensor and stretching an immutable portrait/landscape
     * stream after the tablet is physically rotated.
     *
     * The configuration is considered as well as the rotation so this works
     * on devices whose natural panel orientation is landscape.
     */
    static int orientationForRotation(int rotation, int configurationOrientation) {
        boolean portrait = configurationOrientation == Configuration.ORIENTATION_PORTRAIT;
        switch (rotation) {
            case Surface.ROTATION_0:
                return portrait
                        ? ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
                        : ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE;
            case Surface.ROTATION_90:
                return portrait
                        ? ActivityInfo.SCREEN_ORIENTATION_REVERSE_PORTRAIT
                        : ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE;
            case Surface.ROTATION_180:
                return portrait
                        ? ActivityInfo.SCREEN_ORIENTATION_REVERSE_PORTRAIT
                        : ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE;
            case Surface.ROTATION_270:
                return portrait
                        ? ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
                        : ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE;
            default:
                return portrait
                        ? ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
                        : ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE;
        }
    }

    private void restoreOrientationPolicy() {
        if (!orientationLocked) return;
        orientationLocked = false;
        setRequestedOrientation(originalRequestedOrientation);
        Log.i(TAG, "orientation policy restored");
    }

    /** Immersive sticky fullscreen: no status/nav bars; video keeps the area. */
    private void hideSystemUi() {
        android.view.View decor = getWindow().getDecorView();
        int flags = android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                | android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN;
        decor.setSystemUiVisibility(flags);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemUi();
    }

    private synchronized void toggle() {
        if (worker != null) {
            shutdownSession("Disconnect", true);
        } else if (!cleanupStarted.get()) {
            worker = new Worker();
            worker.start();
            connectButton.setText("Disconnect");
        }
    }

    private void shutdownSession(String reason, boolean finishActivity) {
        if (!cleanupStarted.compareAndSet(false, true)) return;
        Log.i(TAG, "session cleanup reason=" + reason);
        Worker active = worker;
        worker = null;
        if (active != null) active.shutdown();
        restoreOrientationPolicy();
        if (finishActivity && !isFinishing()) finishAndRemoveTask();
    }

    private void workerTerminated(Worker completed, String reason) {
        if (worker != completed) return;
        worker = null;
        shutdownSession(reason, true);
    }

    @Override
    protected void onDestroy() {
        shutdownSession("Activity destroyed", false);
        super.onDestroy();
    }

    private void setStatus(final String s) {
        ui.post(() -> statusView.setText(s));
    }

    private final class Worker extends Thread {
        volatile boolean stop;
        Socket socket;
        long lastCanvasLogMs;

        void shutdown() {
            stop = true;
            try {
                if (socket != null) socket.close();
            } catch (Exception ignored) {
            }
        }

        @Override
        public void run() {
            UdspParser parser = new UdspParser();
            BitmapFactory.Options opts = new BitmapFactory.Options();
            opts.inPreferredConfig = Bitmap.Config.RGB_565; // half the bytes vs ARGB_8888; decode+flinger cheaper
            long rx = 0, rendered = 0, gaps = 0, decodeErr = 0, bytes = 0, droppedQueue = 0;
            long prevSeq = -1;
            int lastW = 0, lastH = 0, lastCodec = -1;
            long startNs = System.nanoTime();
            byte[] tmp = new byte[65536];
            Log.i(TAG, "connecting to " + HOST + ":" + PORT);
            setStatus("Status: connecting to " + HOST + ":" + PORT + " …");
            H264Session h264 = null; // created on first 0x02 frame; released in finally
            String endReason = "host disconnected";
            try {
                socket = new Socket();
                socket.connect(new InetSocketAddress(HOST, PORT), 5000);
                socket.setTcpNoDelay(true);
                Log.i(TAG, "connection established");
                InputStream in = socket.getInputStream();
                boolean firstLogged = false;
                int n;
                while (!stop && (n = in.read(tmp)) != -1) {
                    bytes += n;
                    List<UdspParser.Frame> frames = parser.feed(tmp, 0, n);
                    for (UdspParser.Frame f : frames) {
                        rx++;
                        if (prevSeq >= 0 && (int) (prevSeq + 1) != f.seq) gaps++;
                        prevSeq = f.seq & 0xFFFFFFFFL;
                        lastCodec = f.codec;
                        if (f.codec == UdspParser.CODEC_H264) {
                            if (h264 == null) {
                                h264 = new H264Session();
                                Log.i(TAG, "H264 session, first AU seq=" + f.seq + " len=" + f.payload.length);
                            }
                            // Bounded queue: if decoded output trails parsed input by
                            // more than ~1.5 s (45 frames), flush the codec and rejoin
                            // at the next IDR. Prevents unbounded end-to-end lag after
                            // startup bursts (measured: 61-frame queue sticking at 2 s).
                            // Dropped backlog is counted explicitly, never as rendered.
                            if (rx - rendered - droppedQueue > 45 && h264.configured) {
                                long drop = rx - rendered - droppedQueue;
                                Log.i(TAG, "input queue depth " + (rx - rendered)
                                        + ", flushing codec, dropping " + drop);
                                try {
                                    h264.codec.flush();
                                } catch (Exception e) {
                                    Log.i(TAG, "flush failed: " + e);
                                }
                                h264.seenIdr = false;
                                droppedQueue += drop;
                            }
                            int rc = h264.feed(f);
                            if (rc < 0) decodeErr++;
                            else rendered += rc;
                            lastW = h264.width;
                            lastH = h264.height;
                        } else {
                            if (h264 != null) {
                                decodeErr++; // mixed-codec session: ignore non-matching frames
                                continue;
                            }
                            Bitmap bmp = BitmapFactory.decodeByteArray(f.payload, 0, f.payload.length, opts);
                            if (bmp == null) {
                                decodeErr++;
                                continue;
                            }
                            lastW = bmp.getWidth();
                            lastH = bmp.getHeight();
                            if (drawCentered(bmp)) rendered++;
                            bmp.recycle();
                        }
                        if (!firstLogged) {
                            firstLogged = true;
                            Log.i(TAG, "first frame received seq=" + f.seq + " codec=" + f.codec
                                    + " len=" + f.payload.length);
                            Log.i(TAG, "first frame rendered " + lastW + "x" + lastH);
                        }
                        // MARK every 300th received frame: seq + sender PTS + device
                        // wall clock, for transport-latency correlation against
                        // live-serve mark lines (see docs/H264_LIVE_PIPELINE.md).
                        if (rx % 300 == 1) {
                            Log.i(TAG, "MARK rx=" + rx + " seq=" + f.seq + " pts_ms=" + f.ptsMs
                                    + " recv_wall_ms=" + System.currentTimeMillis());
                        }
                        if (rendered % 60 == 0 && rendered > 0) logSummary(rx, rendered, gaps, parser, decodeErr, droppedQueue, bytes, startNs, lastW, lastH);
                        if (rx % 30 == 0) updateStatus(rx, rendered, gaps, parser, decodeErr, droppedQueue, bytes, startNs, lastW, lastH, lastCodec, false);
                    }
                }
                if (h264 != null) {
                    rendered += h264.finish(); // EOS + drain remainder
                }
                int tail = parser.buffered();
                endReason = stop ? "receiver stopped" : "host disconnected";
                Log.i(TAG, "EOF after " + rx + " frames, truncated_tail=" + tail);
                if (h264 != null) {
                    Log.i(TAG, h264.pacingReport());
                }
                updateStatus(rx, rendered, gaps, parser, decodeErr, droppedQueue, bytes, startNs, lastW, lastH, lastCodec, true);
            } catch (Exception e) {
                if (stop) {
                    endReason = "receiver stopped";
                    Log.i(TAG, "receiver socket closed for session teardown");
                } else {
                    endReason = "connection failure";
                    Log.i(TAG, "connection error: " + e);
                    setStatus("Status: disconnected (" + e.getClass().getSimpleName() + ": " + e.getMessage() + ")");
                }
            } finally {
                try {
                    if (socket != null) socket.close();
                } catch (Exception ignored) {
                }
                if (h264 != null) {
                    h264.release(); // never leak OMX instances on disconnect
                    h264 = null;
                }
                Log.i(TAG, "session end reason=" + endReason + " rx=" + rx
                        + " rendered=" + rendered + " gaps=" + gaps
                        + " crc=" + parser.crcMismatch + " badlen=" + parser.invalidLength
                        + " badcodec=" + parser.invalidCodec + " decode_errors=" + decodeErr
                        + " dropped=" + droppedQueue);
                final String reason = endReason;
                ui.post(() -> workerTerminated(this, reason));
            }
        }

        /**
         * H.264 session: Annex-B AUs -> MediaCodec ("video/avc") -> holder Surface.
         * First IDR's SPS/PPS become csd-0/csd-1; every AU is converted
         * Annex-B -> length-prefixed (MP4-style) before queueInputBuffer.
         * feed() returns rendered-output count delta, or -1 on fatal codec error.
         */
        private final class H264Session {
            MediaCodec codec;
            int width;
            int height;
            boolean configured;
            /** First IDR seen (decoder must never be fed a P-frame first). */
            boolean seenIdr;
            /** AUs skipped while waiting for params/surface/first-IDR (not errors). */
            long skippedPreIdr;
            /** Wall ms of the last dequeued output; stall watchdog baseline. */
            long lastOutputWallMs;
            /** Inputs queued since last output (stall detector). */
            long inputsSinceOutput;
            /** Consecutive feed() exceptions (error-state detector). */
            long feedErrorStreak;
            /** Output pacing stats (dequeue wall-clock gaps). */
            long outCount;
            long outFirstWallMs;
            long outPrevWallMs;
            long outMinGapMs = Long.MAX_VALUE;
            long outMaxGapMs;
            long outGapSumMs;

            /** First 16 payload bytes as hex (diagnostics for fed-AU forensics). */
            private String hex16(byte[] b) {
                StringBuilder sb = new StringBuilder();
                for (int i = 0; i < Math.min(16, b.length); i++) {
                    String h = Integer.toHexString(b[i] & 0xFF);
                    if (h.length() < 2) sb.append('0');
                    sb.append(h);
                }
                return sb.toString();
            }

            /** Split Annex-B payload into raw NALs (no start codes). Returns {types, bodies}. */            private void splitNals(byte[] au, List<Integer> types, List<byte[]> bodies) {
                List<Integer> starts = new ArrayList<Integer>();
                for (int i = 0; i + 3 < au.length; i++) {
                    if (au[i] == 0 && au[i + 1] == 0
                            && (au[i + 2] == 1 || (au[i + 2] == 0 && i + 3 < au.length && au[i + 3] == 1))) {
                        starts.add(i);
                    }
                }
                for (int k = 0; k < starts.size(); k++) {
                    int s = starts.get(k);
                    int sc = (au[s + 2] == 1) ? 3 : 4;
                    int e = (k + 1 < starts.size()) ? starts.get(k + 1) : au.length;
                    if (s + sc >= e) continue;
                    types.add(au[s + sc] & 0x1F);
                    byte[] body = new byte[e - s - sc];
                    System.arraycopy(au, s + sc, body, 0, body.length);
                    bodies.add(body);
                }
            }

            private byte[] findParam(List<Integer> types, List<byte[]> bodies, int want) {
                for (int i = 0; i < types.size(); i++) {
                    if (types.get(i) == want) return bodies.get(i);
                }
                return null;
            }

            /** True if the Annex-B AU contains a NAL unit of the given type. */
            private boolean auHasNalType(byte[] au, int want) {
                for (int i = 0; i + 3 < au.length; i++) {
                    int sc;
                    if (au[i] == 0 && au[i + 1] == 0 && au[i + 2] == 0 && au[i + 3] == 1) sc = 4;
                    else if (au[i] == 0 && au[i + 1] == 0 && au[i + 2] == 1) sc = 3;
                    else continue;
                    if (i + sc < au.length && (au[i + sc] & 0x1F) == want) return true;
                    i += sc;
                }
                return false;
            }

            int feed(UdspParser.Frame f) {
                try {
                    if (!configured) {
                        List<Integer> types = new ArrayList<Integer>();
                        List<byte[]> bodies = new ArrayList<byte[]>();
                        splitNals(f.payload, types, bodies);
                        byte[] sps = findParam(types, bodies, 7);
                        byte[] pps = findParam(types, bodies, 8);
                        if (sps == null || pps == null) return 0; // wait for IDR carrying params
                        if (!MainActivity.this.surfaceReady) return 0; // surface not up: skip quietly
                        try {
                            android.view.Surface s = surfaceView.getHolder().getSurface();
                            Log.i(TAG, "H264 pre-configure surface valid=" + (s != null && s.isValid()));
                            MediaFormat fmt = MediaFormat.createVideoFormat("video/avc", streamWidth, streamHeight);
                            fmt.setByteBuffer("csd-0", ByteBuffer.wrap(sps));
                            fmt.setByteBuffer("csd-1", ByteBuffer.wrap(pps));
                            MediaCodec c = MediaCodec.createDecoderByType("video/avc");
                            c.configure(fmt, s, null, 0);
                            c.start();
                            codec = c;
                        } catch (Exception e) {
                            Log.i(TAG, "H264 configure failed: " + e);
                            try {
                                if (codec != null) {
                                    codec.release();
                                }
                            } catch (Exception ignored) {
                            }
                            codec = null;
                            return -1;
                        }
                        configured = true;
                        width = streamWidth;
                        height = streamHeight;
                        Log.i(TAG, "MediaCodec configured: " + codec.getName()
                                + " sps=" + sps.length + "B pps=" + pps.length + "B"
                                + " spshex=" + hex16(sps) + " ppshex=" + hex16(pps));
                    }
                    // Never feed a P-frame first: a fresh decoder must start from an
                    // IDR, otherwise OMX.qcom wedges permanently (mid-stream joins
                    // otherwise receive SPS/PPS grouped with a P-slice). Skips are
                    // counted, not errors; worst wait is one keyint (<= ~1 s).
                    if (!seenIdr) {
                        if (!auHasNalType(f.payload, 5)) {
                            skippedPreIdr++;
                            return 0;
                        }
                        seenIdr = true;
                        Log.i(TAG, "first IDR AU seq=" + f.seq + " len=" + f.payload.length
                                + " head=" + hex16(f.payload));
                    }
                    // Stall watchdog: a healthy decoder emits within ~1 frame of input.
                    // If 90+ inputs pass with no output for 3+ s (and we have seen
                    // output before, so this isn't startup), or 10 consecutive feed
                    // exceptions occur (codec in error state), the OMX output path is
                    // wedged ("can not return buffer to native window"): tear down and
                    // rejoin at the next IDR instead of erroring forever.
                    if (configured && lastOutputWallMs != 0
                            && ((inputsSinceOutput >= 90
                                    && System.currentTimeMillis() - lastOutputWallMs > 3000)
                                || feedErrorStreak >= 10)) {
                        Log.i(TAG, "decoder stall suspected (silent=" + inputsSinceOutput
                                + " errStreak=" + feedErrorStreak + "); restarting codec");
                        release();
                        configured = false;
                        seenIdr = false;
                        inputsSinceOutput = 0;
                        feedErrorStreak = 0;
                    }
                    // Queue Annex-B directly: OMX.qcom accepts byte-stream input when
                    // configured with csd-0/csd-1 (length-prefixed conversion tried
                    // first and stalled the decoder after 1 frame — see MEASUREMENTS).
                    byte[] mp4 = f.payload;
                    int idx = codec.dequeueInputBuffer(1_000_000);
                    if (idx < 0) {
                        Log.i(TAG, "no input buffer, dropping AU seq=" + f.seq);
                        return 0;
                    }
                    ByteBuffer ib = codec.getInputBuffer(idx);
                    ib.clear();
                    ib.put(mp4);
                    codec.queueInputBuffer(idx, 0, mp4.length, f.ptsMs * 1000, 0);
                    inputsSinceOutput++;
                    feedErrorStreak = 0;
                    return drain(false);
                } catch (Exception e) {
                    Log.i(TAG, "H264 feed error: " + e);
                    feedErrorStreak++;
                    return -1;
                }
            }

            /** Drain available outputs (render=true). Returns newly rendered count. */
            int drain(boolean eos) {
                if (codec == null) return 0;
                int rendered = 0;
                MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
                try {
                    for (;;) {
                        int idx = codec.dequeueOutputBuffer(info, eos ? 100_000 : 0);
                        if (idx >= 0) {
                            codec.releaseOutputBuffer(idx, true);
                            rendered++;
                            long nowMs = System.currentTimeMillis();
                            if (outCount == 0) {
                                outFirstWallMs = nowMs;
                            } else {
                                long gap = nowMs - outPrevWallMs;
                                if (gap < outMinGapMs) outMinGapMs = gap;
                                if (gap > outMaxGapMs) outMaxGapMs = gap;
                                outGapSumMs += gap;
                            }
                            outPrevWallMs = nowMs;
                            outCount++;
                            lastOutputWallMs = nowMs;
                            inputsSinceOutput = 0;
                            feedErrorStreak = 0;
                            if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break;
                        } else if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                            MediaFormat of = codec.getOutputFormat();
                            width = of.getInteger(MediaFormat.KEY_WIDTH);
                            height = of.getInteger(MediaFormat.KEY_HEIGHT);
                            StringBuilder keys = new StringBuilder();
                            for (String k : new String[]{"crop-left", "crop-right", "crop-top",
                                    "crop-bottom", "stride", "slice-height"}) {
                                try {
                                    if (of.containsKey(k)) keys.append(' ').append(k).append('=').append(of.getInteger(k));
                                } catch (Exception ignored) {
                                }
                            }
                            android.graphics.Rect sf = surfaceView.getHolder().getSurfaceFrame();
                            Log.i(TAG, "decoder output format: " + width + "x" + height + keys
                                    + " surface=" + sf.width() + "x" + sf.height());
                        } else {
                            break;
                        }
                    }
                } catch (Exception e) {
                    Log.i(TAG, "H264 drain error: " + e);
                }
                return rendered;
            }

            /** One-line pacing summary (call at stream end). */
            String pacingReport() {
                if (outCount < 2) return "pacing: n=" + outCount + " (too few outputs)";
                double avg = (double) outGapSumMs / (outCount - 1);
                return String.format("pacing: n=%d avg=%.1fms min=%dms max=%dms",
                        outCount, avg, outMinGapMs, outMaxGapMs);
            }

            /** Signal EOS and drain the remainder. Returns additionally rendered count. */
            int finish() {
                int n = 0;
                if (codec != null) {
                    try {
                        int idx = codec.dequeueInputBuffer(1_000_000);
                        if (idx >= 0) {
                            codec.queueInputBuffer(idx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                        }
                        n = drain(true);
                    } catch (Exception e) {
                        Log.i(TAG, "H264 finish error: " + e);
                    }
                }
                release();
                return n;
            }

            /** Always release the codec (prevents OMX instance leaks on stop/disconnect). */
            void release() {
                if (codec != null) {
                    try {
                        codec.stop();
                    } catch (Exception ignored) {
                    }
                    try {
                        codec.release();
                    } catch (Exception ignored) {
                    }
                    codec = null;
                    Log.i(TAG, "MediaCodec released");
                }
                configured = false;
            }
        }
        /** Draw bitmap aspect-fit centered on black (never crop, never stretch).
         * The 4:3 view can be smaller than the 800x600 source (e.g. 692x519);
         * 1:1 drawing would center-crop, so scale uniformly to fit instead. */
        private boolean drawCentered(Bitmap bmp) {
            SurfaceHolder h = surfaceView.getHolder();
            Canvas c = null;
            try {
                c = h.lockCanvas();
                if (c == null) return false;
                c.drawColor(Color.BLACK);
                if (System.currentTimeMillis() - lastCanvasLogMs > 5000) {
                    lastCanvasLogMs = System.currentTimeMillis();
                    Log.i(TAG, "mjpeg canvas " + c.getWidth() + "x" + c.getHeight()
                            + " view " + surfaceView.getWidth() + "x" + surfaceView.getHeight()
                            + " bmp " + bmp.getWidth() + "x" + bmp.getHeight());
                }
                float s = Math.min((float) c.getWidth() / bmp.getWidth(),
                        (float) c.getHeight() / bmp.getHeight());
                float dw = bmp.getWidth() * s, dh = bmp.getHeight() * s;
                float x = (c.getWidth() - dw) / 2f, y = (c.getHeight() - dh) / 2f;
                android.graphics.RectF dst = new android.graphics.RectF(x, y, x + dw, y + dh);
                c.drawBitmap(bmp, null, dst, null);
                return true;
            } catch (Exception e) {
                return false;
            } finally {
                if (c != null) {
                    try {
                        h.unlockCanvasAndPost(c);
                    } catch (Exception ignored) {
                    }
                }
            }
        }

        private void logSummary(long rx, long rendered, long gaps, UdspParser p,
                                long decodeErr, long dropped, long bytes, long startNs, int w, int h) {
            double secs = (System.nanoTime() - startNs) / 1e9;
            double fps = secs > 0 ? rendered / secs : 0;
            Log.i(TAG, String.format("RX: frames=%d rendered=%d gaps=%d crc=%d badlen=%d badcodec=%d decode_errors=%d dropped=%d fps=%.1f %dx%d",
                    rx, rendered, gaps, p.crcMismatch, p.invalidLength, p.invalidCodec, decodeErr, dropped, fps, w, h));
        }

        private void updateStatus(long rx, long rendered, long gaps, UdspParser p,
                                  long decodeErr, long dropped, long bytes, long startNs, int w, int h, int codec, boolean done) {
            double secs = (System.nanoTime() - startNs) / 1e9;
            double fps = secs > 0 ? rendered / secs : 0;
            double kbps = secs > 0 ? bytes * 8 / secs / 1000 : 0;
            final String s = String.format(
                    "Status: %s\nResolution: %dx%d\nCodec: %s\nFPS: %.1f\nFrames: %d rx / %d rendered\nErrors: gaps=%d crc=%d badlen=%d badcodec=%d decode=%d dropped=%d\nBitrate: %.0f kbps",
                    done ? "stream ended" : "connected", w, h,
                    codec == 1 ? "MJPEG" : (codec == 2 ? "H264" : ("codec " + codec)),
                    fps, rx, rendered, gaps, p.crcMismatch, p.invalidLength, p.invalidCodec, decodeErr, dropped, kbps);
            setStatus(s);
        }
    }
}
