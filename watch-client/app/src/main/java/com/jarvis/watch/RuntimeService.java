package com.jarvis.watch;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.provider.CalendarContract;
import android.os.IBinder;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

import java.io.IOException;
import java.io.File;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public class RuntimeService extends Service {
    private static final String CHANNEL_ID = "jarvis_runtime";
    private static final MediaType JSON =
            MediaType.parse("application/json; charset=utf-8");
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .callTimeout(150, TimeUnit.SECONDS)
            .build();
    private LocationManager locationManager;
    static final String ACTION_RELOAD_VOICE = "com.jarvis.watch.RELOAD_VOICE";

    private VoiceActivityRecorder voiceRecorder;
    private Location lastSentLocation;
    private long lastLocationSentAt;
    private long lastAmplitudeUpdateAt;
    private boolean voiceUploadInFlight;
    private File pendingVoiceSegment;
    private final LocationListener locationListener = new LocationListener() {
        @Override
        public void onLocationChanged(Location location) {
            sendLocationEvent(location);
        }
    };
    private final Runnable locationPoll = new Runnable() {
        @Override
        public void run() {
            sendLastKnownLocation();
            handler.postDelayed(this, 300000);
        }
    };
    private final Runnable calendarPoll = new Runnable() {
        @Override
        public void run() {
            syncCalendar();
            handler.postDelayed(this, 300000);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        SharedPreferences prefs = getSharedPreferences("jarvis", MODE_PRIVATE);
        if (!prefs.getBoolean("sensing_enabled", true)) {
            stopSelf();
            return;
        }
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(
                CHANNEL_ID, "Jarvis Runtime", NotificationManager.IMPORTANCE_LOW));
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Jarvis Runtime")
                .setContentText("设备运行时已保持在线")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setOngoing(true)
                .build();
        startForeground(1001, notification);
        startVoiceActivityCapture();
        handler.post(calendarPoll);
        if (prefs.getBoolean("location_sensing_enabled", false)) {
            startLocationUpdates();
            handler.post(locationPoll);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_RELOAD_VOICE.equals(intent.getAction())) {
            restartVoiceActivityCapture();
        }
        return START_NOT_STICKY;
    }

    /** Applies a changed voice profile without waiting for a reboot. */
    private void restartVoiceActivityCapture() {
        if (voiceRecorder != null) {
            voiceRecorder.stop();
            voiceRecorder = null;
        }
        startVoiceActivityCapture();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (voiceRecorder != null) {
            voiceRecorder.stop();
            voiceRecorder = null;
        }
        synchronized (this) {
            if (pendingVoiceSegment != null) {
                pendingVoiceSegment.delete();
                pendingVoiceSegment = null;
            }
        }
        if (locationManager != null) {
            locationManager.removeUpdates(locationListener);
        }
        super.onDestroy();
    }

    private void startVoiceActivityCapture() {
        if (checkSelfPermission("android.permission.RECORD_AUDIO")
                != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        try {
            SharedPreferences voicePrefs = getSharedPreferences("jarvis", MODE_PRIVATE);
            VoiceActivityRecorder.Trim voiceTrim =
                    VoiceActivityRecorder.Trim.from(
                            voicePrefs.getString("voice_sensitivity", "standard"));
            int patienceMs = voicePrefs.getInt("voice_patience_ms", 2200);
            voiceRecorder = new VoiceActivityRecorder(
                    new File(getCacheDir(), "voice-segments"),
                    this::uploadVoiceSegment,
                    new VoiceActivityRecorder.ActivityListener() {
                        @Override
                        public void onVoiceActivity(boolean active) {
                            setVoiceActivity(active);
                            if (!active) setVoiceGrace(-1);
                        }

                        @Override
                        public void onAmplitude(double amplitude) {
                            setVoiceAmplitude(amplitude);
                        }

                        @Override
                        public void onGrace(int remainingMs) {
                            setVoiceGrace(remainingMs);
                        }
                    },
                    voiceTrim,
                    patienceMs);
            voiceRecorder.start();
            setPerceptionState("listening");
        } catch (RuntimeException ignored) {
            voiceRecorder = null;
            setPerceptionState("error");
        }
    }

    /**
     * Publishes the remaining grace before submit so the watch can show a
     * countdown instead of ending the sentence behind the user's back.
     */
    private void setVoiceGrace(int remainingMs) {
        getSharedPreferences("jarvis", MODE_PRIVATE)
                .edit()
                .putInt("voice_grace_ms", Math.max(-1, remainingMs))
                .apply();
    }

    private void setVoiceActivity(boolean active) {
        setPerceptionState(active ? "recording" : "listening");
    }

    private void setVoiceAmplitude(double amplitude) {
        long now = System.currentTimeMillis();
        if (now - lastAmplitudeUpdateAt < 60) return;
        lastAmplitudeUpdateAt = now;
        double normalized = Math.max(0d, Math.min(1d, amplitude / 5000d));
        getSharedPreferences("jarvis", MODE_PRIVATE)
                .edit()
                .putFloat("voice_amplitude", (float) normalized)
                .apply();
    }

    private void setPerceptionState(String state) {
        getSharedPreferences("jarvis", MODE_PRIVATE)
                .edit()
                .putString("perception_state", state)
                .putLong("perception_state_at", System.currentTimeMillis())
                .apply();
    }

    private void uploadVoiceSegment(File file) {
        synchronized (this) {
            if (voiceUploadInFlight) {
                if (pendingVoiceSegment != null) pendingVoiceSegment.delete();
                pendingVoiceSegment = file;
                setPerceptionState("processing");
                return;
            }
            voiceUploadInFlight = true;
        }
        uploadVoiceSegmentNow(file);
    }

    private void uploadVoiceSegmentNow(File file) {
        SharedPreferences prefs = getSharedPreferences("jarvis", MODE_PRIVATE);
        String server = prefs.getString("server", "").replaceAll("/+$", "");
        String token = prefs.getString("token", "");
        String deviceId = prefs.getString("device_id", "android-device");
        if (server.isEmpty() || prefs.getBoolean("tts_playback_active", false)) {
            finishVoiceUpload(file);
            file.delete();
            return;
        }
        setPerceptionState("asr");
        RequestBody audioBody = RequestBody.create(file, MediaType.parse("audio/wav"));
        RequestBody body = new okhttp3.MultipartBody.Builder()
                .setType(okhttp3.MultipartBody.FORM)
                .addFormDataPart("device_id", deviceId)
                .addFormDataPart("audio", file.getName(), audioBody)
                .build();
        Request request = new Request.Builder()
                .url(server + "/api/message/audio")
                .header("Authorization", "Bearer " + token)
                .header("X-Device-ID", deviceId)
                .post(body)
                .build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                String detail = error instanceof java.net.SocketTimeoutException
                        ? "服务器响应超时（ASR/AI 处理时间较长）"
                        : "连接服务器失败：" + error.getMessage();
                setPerceptionError(detail);
                finishVoiceUpload(file);
                file.delete();
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                try {
                    if (!response.isSuccessful()) {
                        String detail = response.code() == 401
                                ? "设备 Token 无效（HTTP 401），请重新导入配置"
                                : "语音识别请求失败（HTTP " + response.code() + "）";
                        try {
                            String responseBody = response.body() == null
                                    ? null : response.body().string();
                            if (responseBody != null && !responseBody.isEmpty()) {
                                JSONObject json = new JSONObject(responseBody);
                                String message = json.optString("detail", "");
                                if (!message.isEmpty()) detail += "：" + message;
                            }
                        } catch (Exception ignored) {
                            // Preserve the HTTP status when the response cannot be read.
                        }
                        setPerceptionError(detail);
                    }
                } finally {
                    response.close();
                    finishVoiceUpload(file);
                    file.delete();
                }
            }
        });
    }

    private void finishVoiceUpload(File completedFile) {
        File next = null;
        synchronized (this) {
            voiceUploadInFlight = false;
            if (pendingVoiceSegment != null) {
                next = pendingVoiceSegment;
                pendingVoiceSegment = null;
                voiceUploadInFlight = true;
            }
        }
        if (next != null) {
            uploadVoiceSegmentNow(next);
        }
    }

    private void setPerceptionError(String detail) {
        getSharedPreferences("jarvis", MODE_PRIVATE)
                .edit()
                .putString("perception_state", "error")
                .putString("perception_detail", detail)
                .putLong("perception_state_at", System.currentTimeMillis())
                .apply();
    }

    private void syncCalendar() {
        if (checkSelfPermission("android.permission.READ_CALENDAR")
                != PackageManager.PERMISSION_GRANTED) return;
        SharedPreferences prefs = getSharedPreferences("jarvis", MODE_PRIVATE);
        String server = prefs.getString("server", "").replaceAll("/+$", "");
        String token = prefs.getString("token", "");
        String deviceId = prefs.getString("device_id", "android-device");
        if (server.isEmpty()) return;
        long now = System.currentTimeMillis();
        String[] projection = {
                CalendarContract.Instances.EVENT_ID,
                CalendarContract.Instances.TITLE,
                CalendarContract.Instances.BEGIN,
                CalendarContract.Instances.END,
                CalendarContract.Instances.EVENT_LOCATION
        };
        Cursor cursor = CalendarContract.Instances.query(
                getContentResolver(),
                projection,
                now,
                now + 24 * 60 * 60 * 1000
        );
        if (cursor == null) return;
        try {
            while (cursor.moveToNext()) {
                String eventId = cursor.getString(0);
                long begin = cursor.getLong(2);
                String marker = eventId + ":" + begin;
                if (prefs.getBoolean("calendar_sent_" + marker, false)) continue;
                try {
                    JSONObject content = new JSONObject();
                    content.put("event_id", eventId);
                    content.put("title", cursor.getString(1));
                    content.put("start", begin);
                    content.put("end", cursor.getLong(3));
                    content.put("location", cursor.getString(4));
                    sendCalendarEvent(server, token, deviceId, content);
                    prefs.edit().putBoolean("calendar_sent_" + marker, true).apply();
                } catch (Exception ignored) {
                }
            }
        } finally {
            cursor.close();
        }
    }

    private void startLocationUpdates() {
        if (checkSelfPermission("android.permission.ACCESS_FINE_LOCATION")
                != PackageManager.PERMISSION_GRANTED
                && checkSelfPermission("android.permission.ACCESS_COARSE_LOCATION")
                != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        locationManager = (LocationManager) getSystemService(LOCATION_SERVICE);
        if (locationManager == null) return;
        try {
            if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                sendLastKnownLocation(LocationManager.NETWORK_PROVIDER);
                locationManager.requestLocationUpdates(
                        LocationManager.NETWORK_PROVIDER,
                        60000L,
                        50f,
                        locationListener
                );
            }
            if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                sendLastKnownLocation(LocationManager.GPS_PROVIDER);
                locationManager.requestLocationUpdates(
                        LocationManager.GPS_PROVIDER,
                        60000L,
                        50f,
                        locationListener
                );
            }
        } catch (SecurityException ignored) {
        }
    }

    private void sendLastKnownLocation() {
        if (locationManager == null) return;
        try {
            if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                sendLastKnownLocation(LocationManager.NETWORK_PROVIDER);
            }
            if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                sendLastKnownLocation(LocationManager.GPS_PROVIDER);
            }
        } catch (SecurityException ignored) {
        }
    }

    private void sendLastKnownLocation(String provider) {
        try {
            Location location = locationManager.getLastKnownLocation(provider);
            if (location != null) sendLocationEvent(location);
        } catch (SecurityException ignored) {
        }
    }

    private void sendLocationEvent(Location location) {
        long now = System.currentTimeMillis();
        if (lastSentLocation != null
                && lastSentLocation.distanceTo(location) < 100f
                && now - lastLocationSentAt < 600000L) {
            return;
        }
        lastSentLocation = new Location(location);
        lastLocationSentAt = now;
        SharedPreferences prefs = getSharedPreferences("jarvis", MODE_PRIVATE);
        String server = prefs.getString("server", "").replaceAll("/+$", "");
        String token = prefs.getString("token", "");
        String deviceId = prefs.getString("device_id", "android-device");
        if (server.isEmpty()) return;
        try {
            JSONObject content = new JSONObject();
            content.put("latitude", location.getLatitude());
            content.put("longitude", location.getLongitude());
            content.put("accuracy", location.getAccuracy());
            content.put("provider", location.getProvider());
            content.put("timestamp", location.getTime());
            JSONObject body = new JSONObject();
            body.put("source", "location");
            body.put("event_type", "location_change");
            body.put("content", content);
            JSONObject context = new JSONObject();
            context.put("device_id", deviceId);
            body.put("context", context);
            body.put("confidence", 1.0);
            Request request = new Request.Builder()
                    .url(server + "/api/events")
                    .header("Authorization", "Bearer " + token)
                    .post(RequestBody.create(body.toString(), JSON))
                    .build();
            http.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, IOException error) {
                }

                @Override
                public void onResponse(Call call, Response response) throws IOException {
                    response.close();
                }
            });
        } catch (Exception ignored) {
        }
    }

    private void sendCalendarEvent(
            String server,
            String token,
            String deviceId,
            JSONObject content
    ) {
        try {
            JSONObject body = new JSONObject();
            body.put("source", "calendar");
            body.put("event_type", "calendar_change");
            body.put("content", content);
            JSONObject context = new JSONObject();
            context.put("device_id", deviceId);
            body.put("context", context);
            body.put("confidence", 1.0);
            Request request = new Request.Builder()
                    .url(server + "/api/events")
                    .header("Authorization", "Bearer " + token)
                    .post(RequestBody.create(body.toString(), JSON))
                    .build();
            http.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, IOException error) {
                }

                @Override
                public void onResponse(Call call, Response response) throws IOException {
                    response.close();
                }
            });
        } catch (Exception ignored) {
        }
    }
}
