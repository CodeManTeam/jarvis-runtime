package com.jarvis.watch;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;

final class PcmRecorder {
    private static final int RATE = 16000;
    private final File output;
    private final ChunkListener listener;
    private final int bufferSize;
    private volatile boolean recording;
    private AudioRecord audioRecord;
    private Thread worker;

    PcmRecorder(File output, ChunkListener listener) {
        this.output = output;
        this.listener = listener;
        int minimum = AudioRecord.getMinBufferSize(
                RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) {
            throw new IllegalStateException("设备不支持 16kHz 麦克风录音");
        }
        bufferSize = Math.max(2048, minimum);
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
        recording = true;
        worker = new Thread(this::record);
        worker.start();
    }

    File stop() {
        recording = false;
        if (audioRecord != null) {
            try {
                audioRecord.stop();
            } catch (IllegalStateException ignored) {
            }
            audioRecord.release();
        }
        if (worker != null) {
            try {
                worker.join(1000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        return output;
    }

    private void record() {
        try (RandomAccessFile file = new RandomAccessFile(output, "rw")) {
            file.write(new byte[44]);
            byte[] buffer = new byte[bufferSize];
            int total = 0;
            while (recording) {
                int count = audioRecord.read(buffer, 0, buffer.length);
                if (count > 0) {
                    file.write(buffer, 0, count);
                    total += count;
                    if (listener != null) {
                        byte[] chunk = new byte[count];
                        System.arraycopy(buffer, 0, chunk, 0, count);
                        listener.onChunk(chunk);
                    }
                }
            }
            writeHeader(file, total);
        } catch (IOException ignored) {
        }
    }

    interface ChunkListener {
        void onChunk(byte[] data);
    }

    private void writeHeader(RandomAccessFile file, int dataLength) throws IOException {
        file.seek(0);
        int byteRate = RATE * 2;
        file.writeBytes("RIFF");
        writeInt(file, 36 + dataLength);
        file.writeBytes("WAVEfmt ");
        writeInt(file, 16);
        writeShort(file, 1);
        writeShort(file, 1);
        writeInt(file, RATE);
        writeInt(file, byteRate);
        writeShort(file, 2);
        writeShort(file, 16);
        file.writeBytes("data");
        writeInt(file, dataLength);
    }

    private void writeInt(RandomAccessFile file, int value) throws IOException {
        file.write(value & 255);
        file.write((value >> 8) & 255);
        file.write((value >> 16) & 255);
        file.write((value >> 24) & 255);
    }

    private void writeShort(RandomAccessFile file, int value) throws IOException {
        file.write(value & 255);
        file.write((value >> 8) & 255);
    }
}
