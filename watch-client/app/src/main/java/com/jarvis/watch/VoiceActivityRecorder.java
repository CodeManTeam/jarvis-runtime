package com.jarvis.watch;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;

/**
 * Continuously samples the microphone locally, but only emits segments that
 * contain voice-level energy. Silence never leaves the device.
 *
 * Sensitivity adapts to the room on its own, without the failure mode of a
 * simple moving average (speech raises the mean, so the gate slowly goes deaf):
 *
 * <ul>
 *   <li>the floor is a low percentile (20th) of recent frame energy, so the
 *       few loud frames that are actual speech do not move it — only sustained
 *       ambient sound does;</li>
 *   <li>onset and sustain use two thresholds with hysteresis, so one sentence
 *       is not chopped into fragments by a breath;</li>
 *   <li>adaptation is asymmetric: a louder room is followed immediately, a
 *       quieter one is followed slowly, which prevents a loud burst from
 *       opening the gate on the noise that follows it.</li>
 * </ul>
 *
 * The only user-facing control is a trim (sensitive / standard / conservative),
 * because at the margins the wearer knows better than any estimator.
 */
final class VoiceActivityRecorder {
    private static final int RATE = 16000;
    private static final int BYTES_PER_SAMPLE = 2;
    private static final int PRE_ROLL_BYTES = RATE * BYTES_PER_SAMPLE * 700 / 1000;
    private static final int MIN_SEGMENT_BYTES = RATE * BYTES_PER_SAMPLE * 500 / 1000;
    private static final int MAX_SEGMENT_BYTES = RATE * BYTES_PER_SAMPLE * 20;


    enum Trim {
        SENSITIVE(0.72),
        STANDARD(1.0),
        CONSERVATIVE(1.45);

        final double factor;

        Trim(double factor) {
            this.factor = factor;
        }

        static Trim from(String name) {
            if ("sensitive".equalsIgnoreCase(name) || "quiet".equalsIgnoreCase(name)) {
                return SENSITIVE;
            }
            if ("conservative".equalsIgnoreCase(name) || "noisy".equalsIgnoreCase(name)) {
                return CONSERVATIVE;
            }
            return STANDARD;
        }
    }

    private final File directory;
    private final SegmentListener listener;
    private final ActivityListener activityListener;
    private final Trim trim;
    private final int silenceFramesToEnd;
    private final int bufferSize;

    private final AdaptiveGate gate;

    private volatile boolean running;
    private AudioRecord audioRecord;
    private Thread worker;

    VoiceActivityRecorder(
            File directory,
            SegmentListener listener,
            ActivityListener activityListener,
            Trim trim,
            int patienceMs) {
        this.directory = directory;
        this.listener = listener;
        this.activityListener = activityListener;
        this.trim = trim == null ? Trim.STANDARD : trim;
        int minimum = AudioRecord.getMinBufferSize(
                RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) {
            throw new IllegalStateException("设备不支持 16kHz 麦克风录音");
        }
        bufferSize = Math.max(2048, minimum);
        this.gate = new AdaptiveGate(this.trim.factor);
        double frameMs = (bufferSize / (double) BYTES_PER_SAMPLE) / RATE * 1000.0;
        this.silenceFramesToEnd = (int) Math.max(12, Math.round(patienceMs / frameMs));
    }

    void start() {
        audioRecord = new AudioRecord(
                MediaRecorder.AudioSource.MIC,
                RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferSize);
        if (audioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
            audioRecord.release();
            audioRecord = null;
            throw new IllegalStateException("麦克风初始化失败");
        }
        audioRecord.startRecording();
        if (audioRecord.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
            audioRecord.release();
            audioRecord = null;
            throw new IllegalStateException("麦克风无法开始录音");
        }
        running = true;
        worker = new Thread(this::capture, "jarvis-vad");
        worker.start();
    }

    void stop() {
        running = false;
        if (audioRecord != null) {
            try {
                audioRecord.stop();
            } catch (IllegalStateException ignored) {
            }
            audioRecord.release();
            audioRecord = null;
        }
        if (worker != null) {
            try {
                worker.join(1500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            worker = null;
        }
    }

    private void capture() {
        ByteArrayOutputStream preRoll = new ByteArrayOutputStream(PRE_ROLL_BYTES);
        ByteArrayOutputStream segment = null;
        int voicedFrames = 0;
        int silentFrames = 0;
        double frameMs = (bufferSize / (double) BYTES_PER_SAMPLE) / RATE * 1000.0;
        try {
            byte[] buffer = new byte[bufferSize];
            while (running) {
                int count = audioRecord.read(buffer, 0, buffer.length);
                if (count <= 0) continue;
                double amplitude = averageAbsAmplitude(buffer, count);
                if (activityListener != null) activityListener.onAmplitude(amplitude);
                observe(amplitude);
                double onset = onsetThreshold();
                double sustain = sustainThreshold();
                boolean inTake = segment != null;
                boolean voiced = amplitude >= (inTake ? sustain : onset);

                if (!inTake) {
                    appendBounded(preRoll, buffer, count, PRE_ROLL_BYTES);
                    voicedFrames = voiced ? voicedFrames + 1 : 0;
                    if (voicedFrames < 3) continue;
                    segment = new ByteArrayOutputStream(MAX_SEGMENT_BYTES);
                    byte[] previous = preRoll.toByteArray();
                    segment.write(previous, 0, previous.length);
                    preRoll.reset();
                    silentFrames = 0;
                    if (activityListener != null) activityListener.onVoiceActivity(true);
                }
                segment.write(buffer, 0, count);

                if (voiced) {
                    silentFrames = 0;
                    if (activityListener != null) activityListener.onGrace(-1);
                } else {
                    silentFrames++;
                    int graceAt = Math.max(1, (int) Math.round(silenceFramesToEnd * 0.55));
                    if (silentFrames >= graceAt && activityListener != null) {
                        int remaining = (int) Math.round(
                                (silenceFramesToEnd - silentFrames) * frameMs);
                        activityListener.onGrace(Math.max(0, remaining));
                    }
                }
                if (segment.size() >= MAX_SEGMENT_BYTES
                        || silentFrames >= silenceFramesToEnd) {
                    emit(segment);
                    if (activityListener != null) {
                        activityListener.onVoiceActivity(false);
                        activityListener.onGrace(-1);
                    }
                    segment = null;
                    voicedFrames = 0;
                    silentFrames = 0;
                    preRoll.reset();
                }
            }
            if (segment != null && segment.size() >= MIN_SEGMENT_BYTES) {
                emit(segment);
            }
            if (segment != null && activityListener != null) {
                activityListener.onVoiceActivity(false);
            }
        } finally {
            if (segment != null) segment.reset();
        }
    }

    private void observe(double amplitude) {
        gate.observe(amplitude);
    }

    private double onsetThreshold() {
        return gate.onset();
    }

    private double sustainThreshold() {
        return gate.sustain();
    }

    private void emit(ByteArrayOutputStream pcm) {
        if (pcm.size() < MIN_SEGMENT_BYTES) return;
        if (!directory.exists() && !directory.mkdirs()) return;
        File output = new File(directory, "jarvis-vad-" + System.currentTimeMillis() + ".wav");
        try (FileOutputStream file = new FileOutputStream(output)) {
            writeHeader(file, pcm.size());
            pcm.writeTo(file);
            if (listener != null) listener.onSegment(output);
        } catch (IOException ignored) {
            output.delete();
        }
    }

    private double averageAbsAmplitude(byte[] data, int length) {
        long total = 0;
        int samples = length / BYTES_PER_SAMPLE;
        for (int i = 0; i < samples; i++) {
            int value = (short) ((data[i * 2] & 0xff) | (data[i * 2 + 1] << 8));
            total += Math.abs(value);
        }
        return samples == 0 ? 0 : (double) total / samples;
    }

    private void appendBounded(
            ByteArrayOutputStream target, byte[] data, int length, int maxBytes) {
        if (length >= maxBytes) {
            target.reset();
            target.write(data, length - maxBytes, maxBytes);
            return;
        }
        int overflow = target.size() + length - maxBytes;
        if (overflow > 0) {
            byte[] existing = target.toByteArray();
            target.reset();
            target.write(existing, overflow, existing.length - overflow);
        }
        target.write(data, 0, length);
    }

    interface SegmentListener {
        void onSegment(File file);
    }

    interface ActivityListener {
        void onVoiceActivity(boolean active);
        void onAmplitude(double amplitude);
        /** Remaining milliseconds before submit, or -1 when no grace is pending. */
        default void onGrace(int remainingMs) {
        }
    }

    private void writeHeader(FileOutputStream file, int dataLength) throws IOException {
        int byteRate = RATE * BYTES_PER_SAMPLE;
        file.write(new byte[] {'R', 'I', 'F', 'F'});
        writeInt(file, 36 + dataLength);
        file.write(new byte[] {'W', 'A', 'V', 'E', 'f', 'm', 't', ' '});
        writeInt(file, 16);
        writeShort(file, 1);
        writeShort(file, 1);
        writeInt(file, RATE);
        writeInt(file, byteRate);
        writeShort(file, BYTES_PER_SAMPLE);
        writeShort(file, 16);
        file.write(new byte[] {'d', 'a', 't', 'a'});
        writeInt(file, dataLength);
    }

    private void writeInt(FileOutputStream file, int value) throws IOException {
        file.write(value & 255);
        file.write((value >> 8) & 255);
        file.write((value >> 16) & 255);
        file.write((value >> 24) & 255);
    }

    private void writeShort(FileOutputStream file, int value) throws IOException {
        file.write(value & 255);
        file.write((value >> 8) & 255);
    }
}
