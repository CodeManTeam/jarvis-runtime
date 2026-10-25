package com.jarvis.watch;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.MediaPlayer;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.TypedValue;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import android.util.Base64;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

public class MainActivity extends Activity {
    static final int RECORD_PERMISSION = 100;
    static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");

    final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .callTimeout(150, TimeUnit.SECONDS)
            .build();

    SharedPreferences preferences;
    private String appliedThemeMode;
    private TextView asrState;
    private TextView perceptionText;
    private TextView headerStatus;
    private SoundWaveView soundWave;
    private TextView usageStatus;
    private TextView voiceTimeline;
    private TextView voiceReplay;
    private TextView voiceSpeak;
    private String lastSttText = "";
    private TextView idleClock;
    private TextView idleDate;
    private View idleDetails;
    private TextView idleDetailsToggle;
    private TextView idleNetwork;
    private TextView idleBattery;
    private TextView idleRadio;
    private TextView idleSync;
    private TextView statusClock;
    private TextView statusBattery;
    private LinearLayout attentionStream;
    private View voiceOverlay;
    private TextView voiceClock;
    private TextView voiceBattery;
    private TextView voiceHeaderStatus;
    private TextView voiceTranscript;
    private SoundWaveView voiceSoundWave;
    private TextView voiceCurrentStage;
    private TextView voiceResponseStatus;
    private TextView voiceRetry;
    private TextView voicePermissionAction;
    private TextView serverConfigNotice;
    private boolean voiceOverlayOpen;
    private long voiceResponseStartedAt;
    private String voiceResponseText;
    private boolean voiceResponseFailed;
    private String pendingPermissionId;
    private String pendingPermissionTitle;
    private final Runnable voiceResponseTicker = new Runnable() {
        @Override
        public void run() {
            if (voiceResponseStartedAt == 0 || !voiceOverlayOpen) return;
            long seconds = (System.currentTimeMillis() - voiceResponseStartedAt) / 1000;
            if (seconds >= 45) {
                showVoiceFailure("超时未响应");
                recordVoiceFailure("超时未响应");
                applyRemotePerceptionState("error", "超时未响应");
                return;
            }
            if (voiceResponseStatus != null) {
                voiceResponseStatus.setText(seconds > 20
                ? getString(R.string.voice_response_slow, seconds)
                        : getString(R.string.voice_response_processing, seconds));
                voiceResponseStatus.setVisibility(View.VISIBLE);
            }
            socketHandler.postDelayed(this, 1000);
        }
    };
    private String lastTranscript = "";
    private String remotePerceptionState;
    private long remotePerceptionStateUntil;
    private JSONObject activeActivity;
    private boolean activitiesLoading;
    private long lastActivityAt;
    private boolean activityActionInFlight;
    private TextView serverCardAction;
    private TextView interventionHint;
    private android.widget.ToggleButton interventionToggle;
    private String deviceId;
    private String deviceName;
    private final Handler socketHandler = new Handler(Looper.getMainLooper());
    private boolean manualSocketClose;
    private boolean reconnectScheduled;
    private int reconnectAttempt;
    private boolean socketReady;
    private long sequence;
    private WebSocket socket;
    private PcmRecorder recorder;
    private boolean streamingAudio;
    private String audioSessionId;
    private long audioSequence;
    private TextToSpeech textToSpeech;
    private boolean ttsReady;
    private String ttsInitError;
    private String activeTtsRunId;
    private boolean ttsEndReceived;
    private int ttsPendingUtterances;
    private long ttsChunkSequence;
    private WebSocket ttsSocket;
    private final ArrayDeque<TtsSegment> pendingTtsSegments = new ArrayDeque<>();
    private final ArrayDeque<AudioSegment> pendingAudioSegments = new ArrayDeque<>();
    private MediaPlayer audioPlayer;
    private final Map<String, String> ttsUtteranceRuns = new HashMap<>();
    private final Runnable perceptionPoll = new Runnable() {
        private long lastUsageAt;
        @Override
        public void run() {
            if (activeTtsRunId == null
                    && (remotePerceptionState == null
                    || System.currentTimeMillis() >= remotePerceptionStateUntil)) {
                String state = preferences.getString("perception_state",
                        preferences.getBoolean("sensing_enabled", true)
                                ? "listening" : "idle");
                long stateAt = preferences.getLong("perception_state_at", 0L);
                if ("error".equals(state)
                        && System.currentTimeMillis() - stateAt > 30000L) {
                    state = preferences.getBoolean("sensing_enabled", true)
                            ? "listening" : "idle";
                    preferences.edit()
                            .putString("perception_state", state)
                            .remove("perception_detail")
                            .putLong("perception_state_at", System.currentTimeMillis())
                            .apply();
                }
                String detail = preferences.getString("perception_detail", null);
                applyPerceptionState(state, detail);
            }
            if (System.currentTimeMillis() - lastUsageAt > 15000) {
                lastUsageAt = System.currentTimeMillis();
                fetchUsage();
                updateStatusBarClockAndBattery();
            }
            if (System.currentTimeMillis() - lastActivityAt > 5000) {
                lastActivityAt = System.currentTimeMillis();
                fetchActivities(false);
            }
            float amplitude = preferences.getFloat("voice_amplitude", 0f);
            if (soundWave != null) {
                soundWave.setLevel(amplitude);
            }
            applyVoiceGrace(preferences.getInt("voice_grace_ms", -1));
            socketHandler.postDelayed(this, 100);
        }
    };

    private final Runnable ttsInitTimeout = () -> {
        if (!ttsReady && activeTtsRunId != null) {
            failTts(activeTtsRunId, ttsInitError == null
                    ? "tts_initialization_timeout" : ttsInitError);
        }
    };
    private final Runnable heartbeat = new Runnable() {
        @Override
        public void run() {
            if (socket != null) {
                try {
                    JSONObject payload = new JSONObject();
                    payload.put("type", "heartbeat");
                    payload.put("request_id", "watch-" + System.currentTimeMillis());
                    payload.put("sequence", nextSequence());
                    payload.put("battery_level", getBatteryLevel());
                    payload.put("network_type", getNetworkType());
                    payload.put("app_version", BuildConfig.VERSION_NAME);
                    payload.put("storage_free_mb", getFreeStorageMb());
                    payload.put("charging", isCharging());
                    payload.put("tts_enabled",
                            preferences.getBoolean("tts_enabled", true));
                    payload.put("tts_ready", ttsReady);
                    if (!ttsReady && ttsInitError != null) {
                        payload.put("tts_error", ttsInitError);
                    }
                    socket.send(payload.toString());
                } catch (Exception ignored) {
                }
                socketHandler.postDelayed(this, 25000);
            }
        }
    };

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(ThemeMode.wrap(base));
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences("jarvis", MODE_PRIVATE);
        appliedThemeMode = preferences.getString(ThemeMode.KEY, ThemeMode.SYSTEM);
        String configuredServer = preferences.getString("server", "").trim();
        String configuredToken = preferences.getString("token", "").trim();
        SharedPreferences.Editor configDefaults = preferences.edit();
        boolean hasConfigDefaults = false;
        if (configuredServer.isEmpty()) {
            configDefaults.putString("server", BuildConfig.DEFAULT_SERVER_URL);
            hasConfigDefaults = true;
        }
        if (configuredToken.isEmpty() && !BuildConfig.DEFAULT_DEVICE_TOKEN.isEmpty()) {
            configDefaults.putString("token", BuildConfig.DEFAULT_DEVICE_TOKEN);
            hasConfigDefaults = true;
        }
        if (hasConfigDefaults) configDefaults.apply();
        deviceId = preferences.getString("device_id", null);
        deviceName = preferences.getString("device_name", "Jarvis Watch");
        sequence = preferences.getLong("sequence", 0L);
        if (deviceId == null) {
            deviceId = "watch-" + UUID.randomUUID().toString().substring(0, 8);
            preferences.edit().putString("device_id", deviceId).apply();
        }
        setContentView(R.layout.activity_main);
        bindViews();
        socketHandler.post(perceptionPoll);
        initializeTextToSpeech();
        if (preferences.getBoolean("sensing_enabled", true)) {
            startService(new Intent(this, RuntimeService.class));
        }
    }

    private void bindViews() {
        serverCardAction = findViewById(R.id.server_card_action);
        serverCardAction.setOnClickListener(v -> openSettings());

        interventionHint = findViewById(R.id.intervention_hint);
        interventionToggle = findViewById(R.id.intervention_toggle);
        boolean aiEnabled = preferences.getBoolean("ai_activity_enabled", true);
        interventionToggle.setChecked(aiEnabled);
        updateInterventionHint(aiEnabled);
        interventionToggle.setOnCheckedChangeListener((button, checked) -> {
            preferences.edit().putBoolean("ai_activity_enabled", checked).apply();
            updateInterventionHint(checked);
        });

        asrState = findViewById(R.id.asr_state);
        perceptionText = findViewById(R.id.perception_text);
        headerStatus = findViewById(R.id.header_status);
        serverConfigNotice = findViewById(R.id.server_config_notice);
        if (serverConfigNotice != null) {
            serverConfigNotice.setOnClickListener(v -> openSettings());
        }
        soundWave = findViewById(R.id.sound_wave);
        View perceptionArea = findViewById(R.id.perception_area);
        perceptionArea.setOnClickListener(v -> toggleRecording());
        perceptionArea.setOnLongClickListener(v -> {
            openVoiceOverlay();
            return true;
        });

        bindVoiceOverlay();

        statusClock = findViewById(R.id.status_clock);
        idleClock = findViewById(R.id.idle_clock);
        voiceTimeline = findViewById(R.id.voice_timeline);
        voiceReplay = findViewById(R.id.voice_replay);
        voiceSpeak = findViewById(R.id.voice_speak);
        if (voiceReplay != null) {
            voiceReplay.setOnClickListener(v -> runOnUiThread(() -> voiceTranscript.setText(lastSttText)));
        }
        if (voiceSpeak != null) {
            voiceSpeak.setOnClickListener(v -> speak(lastSttText));
        }
        idleDate = findViewById(R.id.idle_date);
        idleDetails = findViewById(R.id.idle_details);
        idleDetailsToggle = findViewById(R.id.idle_details_toggle);
        idleNetwork = findViewById(R.id.idle_network);
        idleBattery = findViewById(R.id.idle_battery);
        idleRadio = findViewById(R.id.idle_radio);
        idleSync = findViewById(R.id.idle_sync);
        idleDetailsToggle.setOnClickListener(v -> toggleIdleDetails());
        applyIdleDetailsState(preferences.getBoolean("idle_details_open", true));
        statusBattery = findViewById(R.id.status_battery);
        attentionStream = findViewById(R.id.attention_stream);
        updateStatusBarClockAndBattery();
        updateHeaderStatus(null);
        updateServerConfigNotice();

        findViewById(R.id.view_log_button).setOnClickListener(
                v -> startActivity(new Intent(this, LogActivity.class)));

        String token = preferences.getString("token", "");
        if (!token.trim().isEmpty()) {
            connectSocket();
        }
    }

    @SuppressWarnings("ClickableViewAccessibility")
    private void bindVoiceOverlay() {
        voiceOverlay = findViewById(R.id.voice_overlay);
        voiceClock = findViewById(R.id.voice_clock);
        voiceBattery = findViewById(R.id.voice_battery);
        voiceHeaderStatus = findViewById(R.id.voice_header_status);
        voiceTranscript = findViewById(R.id.voice_transcript);
        voiceSoundWave = findViewById(R.id.voice_sound_wave);
        voiceCurrentStage = findViewById(R.id.voice_current_stage);
        voiceResponseStatus = findViewById(R.id.voice_response_status);
        voiceRetry = findViewById(R.id.voice_retry);
        voicePermissionAction = findViewById(R.id.voice_permission_action);
        voiceRetry.setOnClickListener(v -> {
            if (!lastSttText.isEmpty()) {
                startVoiceResponseTimer();
                sendTextMessage(lastSttText);
            }
        });
        voicePermissionAction.setOnClickListener(v -> openPendingPermission());

        findViewById(R.id.voice_close).setOnClickListener(v -> closeVoiceOverlay(false));
        findViewById(R.id.voice_use_text).setOnClickListener(v -> {
            closeVoiceOverlay(true);
            openTextInput();
        });

        View holdButton = findViewById(R.id.voice_hold_button);
        holdButton.setOnTouchListener((v, event) -> {
            switch (event.getAction()) {
                case android.view.MotionEvent.ACTION_DOWN:
                    startRecording();
                    return true;
                case android.view.MotionEvent.ACTION_UP:
                case android.view.MotionEvent.ACTION_CANCEL:
                    stopRecording();
                    return true;
                default:
                    return false;
            }
        });
    }

    /** Opens the voice input overlay (WATCH_UI.md §5). Recording only starts once the
     * hold button is actually pressed, never just by opening the overlay. */
    private void openVoiceOverlay() {
        if (voiceOverlay == null) return;
        voiceOverlayOpen = true;
        voiceOverlay.setVisibility(View.VISIBLE);
        renderVoiceResponse();
        renderPermissionAction();
        if (voiceResponseStartedAt != 0) socketHandler.post(voiceResponseTicker);
        voiceTranscript.setText(lastTranscript.isEmpty()
                ? getString(R.string.voice_transcript_placeholder)
                : "“" + lastTranscript + "”");
        updateVoiceOverlayStatus(
                recorder != null ? "recording" : remotePerceptionState, null);
        updateStatusBarClockAndBattery();
    }

    /** Hides the overlay without submitting anything; stops any in-flight capture
     * so a swipe-out or close never leaves a dangling recording (WATCH_UI.md §5 规则). */
    private void closeVoiceOverlay(boolean keepRecordingForTextSwitch) {
        voiceOverlayOpen = false;
        stopVoiceResponseTimer();
        if (voiceOverlay != null) voiceOverlay.setVisibility(View.GONE);
        if (!keepRecordingForTextSwitch && recorder != null) {
            stopRecording();
        }
    }

    private void startVoiceResponseTimer() {
        stopVoiceResponseTimer();
        voiceResponseText = null;
        voiceResponseFailed = false;
        voiceResponseStartedAt = System.currentTimeMillis();
        renderVoiceResponse();
        if (voiceOverlayOpen) socketHandler.post(voiceResponseTicker);
    }

    private void stopVoiceResponseTimer() {
        socketHandler.removeCallbacks(voiceResponseTicker);
        voiceResponseStartedAt = 0;
    }

    private void renderVoiceResponse() {
        if (voiceResponseStatus == null || !voiceOverlayOpen) return;
        if (voiceResponseText != null) {
            voiceResponseStatus.setText(voiceResponseText);
            voiceResponseStatus.setVisibility(View.VISIBLE);
            voiceRetry.setVisibility(voiceResponseFailed ? View.VISIBLE : View.GONE);
        } else if (voiceResponseStartedAt != 0) {
            long seconds = (System.currentTimeMillis() - voiceResponseStartedAt) / 1000;
            voiceResponseStatus.setText(seconds > 20 ? getString(R.string.voice_response_slow, seconds)
                    : getString(R.string.voice_response_processing, seconds));
            voiceResponseStatus.setVisibility(View.VISIBLE);
            voiceRetry.setVisibility(View.GONE);
        } else {
            voiceResponseStatus.setVisibility(View.GONE);
            voiceRetry.setVisibility(View.GONE);
        }
    }

    private void renderPermissionAction() {
        if (voicePermissionAction == null) return;
        voicePermissionAction.setVisibility(voiceOverlayOpen && pendingPermissionId != null
                ? View.VISIBLE : View.GONE);
    }

    private void openPendingPermission() {
        if (pendingPermissionId == null) return;
        Intent intent = new Intent(this, PermissionActivity.class);
        intent.putExtra("pending_id", pendingPermissionId);
        startActivityForResult(intent, 204);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == 204 && resultCode == RESULT_OK) {
            fetchActivities(false);
        }
    }

    /** Mirrors the real perception state into the overlay's four-stage pipeline.
     * Only highlighting changes; the screen is never rebuilt (task 完成标准). */
    private void updateVoiceOverlayStatus(String state, String detail) {
        if (voiceOverlay == null || !voiceOverlayOpen) return;
        String safeState = state == null ? "idle" : state;
        if (voiceClock != null) {
            voiceClock.setText(new java.text.SimpleDateFormat(
                    "HH:mm", Locale.getDefault()).format(new java.util.Date()));
        }
        if (voiceBattery != null) {
            int level = getBatteryLevel();
            voiceBattery.setText(level < 0 ? "--%" : level + "%");
        }
        if (voiceHeaderStatus != null) {
            voiceHeaderStatus.setText(R.string.voice_status_listening);
        }
        if (voiceTranscript != null) {
            voiceTranscript.setText(lastTranscript.isEmpty()
                    ? getString(R.string.voice_transcript_placeholder)
                    : "“" + lastTranscript + "”");
        }
        if (voiceSoundWave != null) {
            voiceSoundWave.setActive("recording".equals(safeState)
                    || "voice_detected".equals(safeState)
                    || "asr".equals(safeState));
        }

        boolean failed = "error".equals(safeState);
        if (voiceCurrentStage != null) {
            int stageText = failed ? R.string.voice_stage_failed
                    : ("recording".equals(safeState) || "voice_detected".equals(safeState)
                            ? R.string.voice_stage_listening
                            : "asr".equals(safeState) ? R.string.voice_stage_asr
                            : "processing".equals(safeState) ? R.string.voice_stage_processing
                            : "completed".equals(safeState) || "playing".equals(safeState)
                            ? R.string.voice_stage_done : R.string.voice_stage_listening);
            voiceCurrentStage.setText(stageText);
            voiceCurrentStage.setTextColor(getColor(failed
                    ? R.color.color_error_text : R.color.color_text_primary));
        }
    }

    private void fetchUsage() {
        if (baseUrl().isEmpty()) return;
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/usage")
                .get()).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                try {
                    JSONObject body = new JSONObject(response.body().string());
                    JSONObject usage = body.optJSONObject("usage");
                    if (usage == null) return;
                    double seconds = usage.optDouble("asr_seconds", 0);
                    int tokens = usage.optInt("total_tokens", 0);
                    int limit = usage.optInt("token_limit", 0);
                    String asr = seconds >= 60
                            ? String.format(Locale.getDefault(), "%.1f 分钟", seconds / 60)
                            : String.format(Locale.getDefault(), "%.0f 秒", seconds);
                    runOnUiThread(() -> {
                        if (usageStatus != null) {
                            usageStatus.setText(getString(
                                    R.string.perception_usage_format, asr, tokens, limit));
                        }
                    });
                } catch (Exception ignored) {
                } finally {
                    response.close();
                }
            }
        });
    }

    private void updateInterventionHint(boolean enabled) {
        interventionHint.setText(enabled
                ? getString(R.string.card_intervention_hint_on)
                : getString(R.string.card_intervention_hint_off));
    }

    private void openSettings() {
        startActivity(new Intent(this, SettingsActivity.class));
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!appliedThemeMode.equals(preferences.getString(ThemeMode.KEY, ThemeMode.SYSTEM))) {
            recreate();
            return;
        }
        updateServerConfigNotice();
        fetchActivities(false);
    }

    private void updateServerConfigNotice() {
        if (serverConfigNotice == null) return;
        boolean configured = !preferences.getString("token", "").trim().isEmpty()
                && !baseUrl().isEmpty();
        serverConfigNotice.setVisibility(configured && socketReady ? View.GONE : View.VISIBLE);
    }

    private void fetchActivities(boolean showToast) {
        if (activitiesLoading || baseUrl().isEmpty()) {
            if (showToast) {
                Toast.makeText(this, R.string.log_need_config, Toast.LENGTH_SHORT).show();
            }
            return;
        }
        activitiesLoading = true;
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/activities?limit=20")
                .get()).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                activitiesLoading = false;
                runOnUiThread(() -> {
                    if (showToast) {
                        Toast.makeText(MainActivity.this,
                                R.string.toast_activities_unavailable,
                                Toast.LENGTH_SHORT).show();
                    }
                });
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String body = response.body() == null ? "" : response.body().string();
                response.close();
                activitiesLoading = false;
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> {
                        if (showToast) {
                            Toast.makeText(MainActivity.this,
                                    R.string.toast_activities_failed,
                                    Toast.LENGTH_SHORT).show();
                        }
                    });
                    return;
                }
                try {
                    JSONArray items = new JSONObject(body).optJSONArray("items");
                    if (items == null) items = new JSONArray();
                    JSONArray result = items;
                    runOnUiThread(() -> renderActivities(result));
                } catch (Exception error) {
                    runOnUiThread(() -> {
                        if (showToast) {
                            Toast.makeText(MainActivity.this,
                                    R.string.toast_activities_format_invalid,
                                    Toast.LENGTH_SHORT).show();
                        }
                    });
                }
            }
        });
    }

    private static final int ATTENTION_CARD_LIMIT = 6;
    private JSONArray lastActivityItems = new JSONArray();
    private JSONArray lastSuggestionCandidates = new JSONArray();

    private void renderActivities(JSONArray items) {
        lastActivityItems = items == null ? new JSONArray() : items;
        preferences.edit().putLong("last_sync_at", System.currentTimeMillis()).apply();
        updateHeaderStatusFromActivities(lastActivityItems);
        renderAttentionStream(lastActivityItems);
        fetchSuggestionCandidates();
    }

    /** M2e home entry: a suggestion card only exists when the backend has a
     * candidate to show, and never when a status card already occupies the slot
     * (WATCH_UI.md §6 规则 / 状态卡与建议卡互斥). */
    private void fetchSuggestionCandidates() {
        if (baseUrl().isEmpty()) return;
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/memories/candidates")
                .get()).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String body = response.body() == null ? "" : response.body().string();
                response.close();
                if (!response.isSuccessful()) return;
                try {
                    JSONArray items = new JSONObject(body).optJSONArray("memories");
                    if (items == null) items = new JSONArray();
                    JSONArray result = items;
                    runOnUiThread(() -> {
                        lastSuggestionCandidates = result;
                        renderAttentionStream(lastActivityItems);
                    });
                } catch (Exception ignored) {
                }
            }
        });
    }

    /** Re-renders the attention stream from the last known activity list, e.g. after an action. */
    private void renderPrimaryActivity(JSONObject activity) {
        activeActivity = activity;
        renderAttentionStream(lastActivityItems);
    }

    private void updateAttentionFooter() {
        TextView footer = findViewById(R.id.attention_footer);
        if (footer == null) return;
        long at = preferences.getLong("last_sync_at", 0L);
        if (at <= 0) {
            footer.setVisibility(android.view.View.GONE);
            return;
        }
        footer.setVisibility(android.view.View.VISIBLE);
        footer.setText(getString(R.string.attention_footer_format,
                new java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
                        .format(new java.util.Date(at))));
    }

    private void renderAttentionStream(JSONArray items) {
        if (attentionStream == null) return;
        updateAttentionFooter();
        if (refreshStatusCard()) {
            attentionStream.setVisibility(View.GONE);
            attentionStream.removeAllViews();
            return;
        }
        attentionStream.setVisibility(View.VISIBLE);
        attentionStream.removeAllViews();
        if (lastSuggestionCandidates != null && lastSuggestionCandidates.length() > 0) {
            attentionStream.addView(buildSuggestionEntryCard(lastSuggestionCandidates));
        }
        if (items == null || items.length() == 0) {
            if (attentionStream.getChildCount() > 0) return;
            TextView empty = new TextView(this);
            empty.setText(R.string.attention_empty);
            empty.setTextSize(12);
            empty.setTextColor(getColor(R.color.color_text_tertiary));
            attentionStream.addView(empty);
            return;
        }
        java.util.List<JSONObject> sorted = sortByAttention(items);
        int shown = Math.min(sorted.size(), ATTENTION_CARD_LIMIT);
        for (int index = 0; index < shown; index++) {
            attentionStream.addView(buildAttentionCard(sorted.get(index)));
        }
        if (sorted.size() > shown) {
            TextView more = new TextView(this);
            more.setText(getString(R.string.attention_more_suffix, sorted.size() - shown));
            more.setTextSize(11);
            more.setTextColor(getColor(R.color.color_text_tertiary));
            LinearLayout.LayoutParams params = wrapParams();
            params.topMargin = dp(8);
            attentionStream.addView(more, params);
        }
    }

    private boolean isLowBatteryDim() {
        int level = getBatteryLevel();
        return level >= 0 && level <= 15 && !isCharging();
    }

    /** `degradation[]` of the most recent activity, or null when empty/absent (WATCH_UI.md §8). */
    private JSONArray latestDegradation(JSONArray items) {
        if (items == null || items.length() == 0) return null;
        JSONObject latest = items.optJSONObject(0);
        if (latest == null) return null;
        JSONArray degradation = latest.optJSONArray("degradation");
        return degradation != null && degradation.length() > 0 ? degradation : null;
    }

    /**
     * Mutually-exclusive home status card (M2c), highest priority first:
     * low-battery dim > offline > degraded. Returns true and fills
     * {@code status_card} when one applies; the caller must then hide the
     * attention stream instead of overlaying it (WATCH_UI.md 规则: 状态卡与注意力流
     * 同时只能出现一个).
     */
    private boolean refreshStatusCard() {
        View card = findViewById(R.id.status_card);
        if (!(card instanceof LinearLayout)) return false;
        LinearLayout container = (LinearLayout) card;
        container.removeAllViews();
        boolean lowBattery = isLowBatteryDim();
        applyDimBackground(lowBattery);
        boolean configured = !preferences.getString("token", "").trim().isEmpty()
                && !baseUrl().isEmpty();
        if (!configured) {
            container.setVisibility(View.GONE);
            return false;
        }
        if (lowBattery) {
            container.addView(buildLowBatteryCard());
        } else if (!socketReady) {
            container.addView(buildOfflineCard());
        } else {
            JSONArray degradation = latestDegradation(lastActivityItems);
            if (degradation != null) {
                container.addView(buildDegradedCard(degradation));
            } else {
                container.setVisibility(View.GONE);
                return false;
            }
        }
        container.setVisibility(View.VISIBLE);
        return true;
    }

    /** Low-battery dim only changes overall brightness, never semantic-color legibility
     * (WATCH_UI.md §9 / DESIGN_SYSTEM_V2.md §8). */
    private void applyDimBackground(boolean dim) {
        View rootScroll = findViewById(R.id.root_scroll);
        View rootContainer = findViewById(R.id.root_container);
        int color = getColor(dim ? R.color.color_bg_deep : R.color.color_background);
        if (rootScroll != null) rootScroll.setBackgroundColor(color);
        if (rootContainer != null) rootContainer.setBackgroundColor(color);
    }

    /** attention 缺失时按 auto_done 处理；排序：high_risk_waiting > suggest_once > auto_done > ignored，同类按时间倒序。 */
    private java.util.List<JSONObject> sortByAttention(JSONArray items) {
        java.util.List<JSONObject> list = new java.util.ArrayList<>();
        for (int index = 0; index < items.length(); index++) {
            JSONObject item = items.optJSONObject(index);
            if (item != null) list.add(item);
        }
        Collections.sort(list, (a, b) -> {
            int rankA = attentionRank(a.optString("attention", "auto_done"));
            int rankB = attentionRank(b.optString("attention", "auto_done"));
            if (rankA != rankB) return rankA - rankB;
            return b.optString("created_at", "").compareTo(a.optString("created_at", ""));
        });
        return list;
    }

    private int attentionRank(String attention) {
        if ("high_risk_waiting".equals(attention)) return 0;
        if ("suggest_once".equals(attention)) return 1;
        if ("ignored".equals(attention)) return 3;
        return 2; // auto_done, also the default for missing/unknown values
    }

    /** M2e home entry card: one amber card linking to the first habit-suggestion
     * candidate. Title text is the candidate's content, not invented copy (WATCH_UI.md §6). */
    private View buildSuggestionEntryCard(JSONArray candidates) {
        JSONObject first = candidates.optJSONObject(0);
        String content = first == null ? "" : first.optString("content", "");

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams cardParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        card.setLayoutParams(cardParams);
        card.setBackgroundResource(R.drawable.bg_card_warning);
        card.setPadding(dp(12), dp(12), dp(12), dp(12));
        card.setClickable(true);
        card.setFocusable(true);
        card.setOnClickListener(v -> startActivity(new Intent(this, SuggestionActivity.class)));

        TextView pill = new TextView(this);
        pill.setText(R.string.attention_pill_suggest_entry);
        pill.setTextSize(11);
        pill.setPadding(dp(10), dp(2), dp(10), dp(2));
        pill.setMinHeight(dp(20));
        pill.setGravity(android.view.Gravity.CENTER_VERTICAL);
        pill.setTextColor(getColor(R.color.color_warning_text));
        pill.setBackgroundResource(R.drawable.bg_pill_warning);
        card.addView(pill, wrapParams());

        TextView title = new TextView(this);
        title.setText(content.isEmpty() ? getString(R.string.status_unavailable) : content);
        title.setTextSize(16);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        title.setTextColor(getColor(R.color.color_text_primary));
        title.setMaxLines(2);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams titleParams = wrapParams();
        titleParams.topMargin = dp(6);
        card.addView(title, titleParams);

        TextView action = new TextView(this);
        action.setText(R.string.attention_action_suggest);
        action.setTextSize(12);
        action.setTextColor(getColor(R.color.color_warning_text));
        LinearLayout.LayoutParams actionParams = wrapParams();
        actionParams.topMargin = dp(7);
        card.addView(action, actionParams);

        return card;
    }

    private View buildAttentionCard(JSONObject activity) {
        String attention = activity.optString("attention", "auto_done");
        boolean highRisk = "high_risk_waiting".equals(attention);

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams cardParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        cardParams.topMargin = dp(10);
        card.setLayoutParams(cardParams);
        card.setBackgroundResource(highRisk ? R.drawable.bg_card_high_risk : R.drawable.bg_card);

        // "已忽略" 左竖条无色（DESIGN_SYSTEM_V2.md §6.2 / WATCH_UI.md §2 表格"左竖条色"列为"无"）
        if (!"ignored".equals(attention)) {
            View stripe = new View(this);
            stripe.setLayoutParams(new LinearLayout.LayoutParams(dp(2),
                    LinearLayout.LayoutParams.MATCH_PARENT));
            stripe.setBackgroundColor(getColor(attentionStripeColor(attention)));
            card.addView(stripe);
        }

        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams bodyParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        body.setLayoutParams(bodyParams);
        body.setPadding(dp(12), dp(12), dp(12), dp(12));

        LinearLayout topRow = new LinearLayout(this);
        topRow.setOrientation(LinearLayout.HORIZONTAL);
        topRow.setGravity(android.view.Gravity.CENTER_VERTICAL);
        topRow.addView(attentionPill(attention), wrapParams());
        TextView why = new TextView(this);
        why.setText(R.string.attention_why);
        why.setTextSize(11);
        why.setTextColor(getColor(R.color.color_text_tertiary));
        why.setGravity(android.view.Gravity.END);
        LinearLayout.LayoutParams whyParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        why.setClickable(true);
        why.setFocusable(true);
        why.setOnClickListener(v -> openEvidence(activity));
        topRow.addView(why, whyParams);
        body.addView(topRow);

        TextView title = new TextView(this);
        title.setText(activity.optString("title", "收到一条活动"));
        title.setTextSize(16);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        title.setTextColor(getColor(R.color.color_text_primary));
        title.setMaxLines(2);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams titleParams = wrapParams();
        titleParams.topMargin = dp(6);
        body.addView(title, titleParams);

        LinearLayout actionRow = buildActionRow(activity, attention);
        LinearLayout.LayoutParams actionParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        actionParams.topMargin = dp(7);
        body.addView(actionRow, actionParams);

        card.addView(body);
        return card;
    }

    private TextView attentionPill(String attention) {
        TextView pill = new TextView(this);
        pill.setTextSize(11);
        pill.setPadding(dp(10), dp(2), dp(10), dp(2));
        pill.setMinHeight(dp(20));
        pill.setGravity(android.view.Gravity.CENTER_VERTICAL);
        if ("high_risk_waiting".equals(attention)) {
            pill.setText(R.string.attention_pill_high_risk);
            pill.setTextColor(getColor(R.color.color_error_text));
            pill.setBackgroundResource(R.drawable.bg_pill_error);
        } else if ("suggest_once".equals(attention)) {
            pill.setText(R.string.attention_pill_suggest);
            pill.setTextColor(getColor(R.color.color_warning_text));
            pill.setBackgroundResource(R.drawable.bg_pill_warning);
        } else if ("ignored".equals(attention)) {
            pill.setText(R.string.attention_pill_ignored);
            pill.setTextColor(getColor(R.color.color_text_tertiary));
            pill.setBackgroundResource(R.drawable.bg_pill_outline);
        } else {
            pill.setText(R.string.attention_pill_auto);
            pill.setTextColor(getColor(R.color.color_success_text));
            pill.setBackgroundResource(R.drawable.bg_pill_success);
        }
        return pill;
    }

    private int attentionStripeColor(String attention) {
        if ("high_risk_waiting".equals(attention)) return R.color.color_error_text;
        if ("suggest_once".equals(attention)) return R.color.color_warning_text;
        return R.color.color_success_text;
    }

    /** 动作行：有 available_actions 时渲染主/次按钮，否则渲染语义色文字链接。 */
    private LinearLayout buildActionRow(JSONObject activity, String attention) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        JSONArray actions = activity.optJSONArray("available_actions");
        boolean hasActions = actions != null && actions.length() > 0;
        if (hasActions) {
            for (int index = 0; index < actions.length(); index++) {
                String action = actions.optString(index, "");
                if (action.isEmpty()) continue;
                TextView chip = buildActionChip(activity, action);
                LinearLayout.LayoutParams chipParams = wrapParams();
                if (index > 0) chipParams.leftMargin = dp(8);
                row.addView(chip, chipParams);
            }
        } else {
            TextView link = new TextView(this);
            link.setText(actionLinkLabel(attention));
            link.setTextSize(12);
            link.setTextColor(getColor(attentionLinkColor(attention)));
            link.setClickable(true);
            link.setFocusable(true);
            link.setOnClickListener(v -> showActivityDetails(activity));
            row.addView(link, wrapParams());
        }
        return row;
    }

    private String actionLinkLabel(String attention) {
        if ("high_risk_waiting".equals(attention)) return getString(R.string.attention_action_review);
        if ("suggest_once".equals(attention)) return getString(R.string.attention_action_suggest);
        if ("ignored".equals(attention)) return getString(R.string.attention_action_ignored);
        return getString(R.string.attention_action_undo);
    }

    private int attentionLinkColor(String attention) {
        if ("high_risk_waiting".equals(attention)) return R.color.color_error_text;
        if ("suggest_once".equals(attention)) return R.color.color_warning_text;
        if ("ignored".equals(attention)) return R.color.color_text_tertiary;
        return R.color.color_success_text;
    }

    private TextView buildActionChip(JSONObject activity, String action) {
        boolean primary = "accept".equals(action);
        TextView chip = pillButton(actionLabel(action), primary);
        chip.setEnabled(!activityActionInFlight);
        chip.setOnClickListener(v -> {
            if ("modify".equals(action)) {
                showModifyDialog(activity);
            } else if ("accept".equals(action) && needsPermissionScreen(activity)) {
                // Approving a high-risk action must show the draft and the
                // irreversibility notice first (WATCH_UI.md §4).
                openPermission(activity);
            } else {
                performActivityAction(activity, action, null);
            }
        });
        return chip;
    }

    private boolean needsPermissionScreen(JSONObject activity) {
        return "high_risk_waiting".equals(activity.optString("attention", ""))
                && !activity.optString("pending_action_id", "").isEmpty();
    }

    private void openEvidence(JSONObject activity) {
        String eventId = activity.optString("event_id", "");
        if (eventId.isEmpty()) {
            showActivityDetails(activity);
            return;
        }
        Intent intent = new Intent(this, EvidenceActivity.class);
        intent.putExtra("event_id", eventId);
        startActivity(intent);
    }

    private void openPermission(JSONObject activity) {
        Intent intent = new Intent(this, PermissionActivity.class);
        intent.putExtra("pending_id", activity.optString("pending_action_id", ""));
        startActivity(intent);
    }

    /**
     * Low-battery dim card (WATCH_UI.md §9). Dims the root container's visual weight but
     * keeps voice and text entry live, and never touches the high-risk permission flow —
     * that screen is reached the same way regardless of battery state.
     */
    private View buildLowBatteryCard() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackgroundResource(R.drawable.bg_card_dim);
        card.setPadding(dp(14), dp(14), dp(14), dp(14));

        TextView headline = new TextView(this);
        headline.setText(getString(R.string.lowbatt_headline, getBatteryLevel()));
        headline.setTextSize(TypedValue.COMPLEX_UNIT_PX, getResources()
                .getDimension(R.dimen.text_xl));
        headline.setTypeface(headline.getTypeface(), android.graphics.Typeface.BOLD);
        headline.setTextColor(getColor(R.color.color_warning_text));
        card.addView(headline);

        TextView claim = new TextView(this);
        claim.setText(R.string.lowbatt_claim);
        claim.setTextSize(14);
        claim.setTextColor(getColor(R.color.color_text_secondary));
        LinearLayout.LayoutParams claimParams = wrapParams();
        claimParams.topMargin = dp(6);
        card.addView(claim, claimParams);

        TextView notice = new TextView(this);
        notice.setText(R.string.lowbatt_notice);
        notice.setTextSize(11);
        notice.setTextColor(getColor(R.color.color_text_secondary));
        LinearLayout.LayoutParams noticeParams = wrapParams();
        noticeParams.topMargin = dp(10);
        card.addView(notice, noticeParams);

        LinearLayout buttonRow = new LinearLayout(this);
        buttonRow.setOrientation(LinearLayout.HORIZONTAL);
        TextView voiceButton = pillButton(getString(R.string.lowbatt_action_voice), false);
        voiceButton.setOnClickListener(v -> openVoiceOverlay());
        TextView textButton = pillButton(getString(R.string.lowbatt_action_text), true);
        textButton.setOnClickListener(v -> openTextInput());
        LinearLayout.LayoutParams rowParams = wrapParams();
        rowParams.topMargin = dp(12);
        buttonRow.addView(voiceButton, wrapParams());
        LinearLayout.LayoutParams textParams = wrapParams();
        textParams.leftMargin = dp(8);
        buttonRow.addView(textButton, textParams);
        card.addView(buttonRow, rowParams);

        return card;
    }

    /** Offline card (WATCH_UI.md §7). No retry retransmit queue exists yet, so the backlog
     * count is always shown as "—", never 0 or an estimate. */
    private View buildOfflineCard() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackgroundResource(R.drawable.bg_card_high_risk);
        card.setPadding(dp(14), dp(14), dp(14), dp(14));

        TextView title = new TextView(this);
        title.setText(R.string.offline_title);
        title.setTextSize(16);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        title.setTextColor(getColor(R.color.color_text_primary));
        card.addView(title);

        TextView claim = new TextView(this);
        claim.setText(R.string.offline_claim);
        claim.setTextSize(14);
        claim.setTextColor(getColor(R.color.color_text_primary));
        LinearLayout.LayoutParams claimParams = wrapParams();
        claimParams.topMargin = dp(6);
        card.addView(claim, claimParams);

        TextView networkLine = new TextView(this);
        int battery = getBatteryLevel();
        String batteryText = battery < 0 ? getString(R.string.status_unavailable)
                : getString(R.string.idle_battery_format, battery);
        networkLine.setText(networkLabel() + " · " + batteryText);
        networkLine.setTextSize(12);
        networkLine.setTextColor(getColor(R.color.color_text_secondary));
        LinearLayout.LayoutParams networkParams = wrapParams();
        networkParams.topMargin = dp(10);
        card.addView(networkLine, networkParams);

        TextView queueLine = new TextView(this);
        queueLine.setText(getString(R.string.offline_queue_format,
                getString(R.string.status_unavailable)) + " · " +
                getString(R.string.offline_queue_not_wired));
        queueLine.setTextSize(12);
        queueLine.setTextColor(getColor(R.color.color_error_text));
        queueLine.setText(getString(R.string.offline_queue_format,
                getString(R.string.status_unavailable)));
        LinearLayout.LayoutParams queueParams = wrapParams();
        queueParams.topMargin = dp(10);
        card.addView(queueLine, queueParams);

        TextView notice = new TextView(this);
        notice.setText(R.string.offline_notice);
        notice.setTextSize(11);
        notice.setTextColor(getColor(R.color.color_text_secondary));
        LinearLayout.LayoutParams noticeParams = wrapParams();
        noticeParams.topMargin = dp(8);
        card.addView(notice, noticeParams);

        LinearLayout buttonRow = new LinearLayout(this);
        buttonRow.setOrientation(LinearLayout.HORIZONTAL);
        TextView checkButton = pillButton(getString(R.string.offline_action_check_network), false);
        checkButton.setOnClickListener(v -> connectSocket());
        TextView cacheButton = pillButton(getString(R.string.offline_action_view_cache), false);
        cacheButton.setOnClickListener(v -> startActivity(new Intent(this, LogActivity.class)));
        LinearLayout.LayoutParams rowParams = wrapParams();
        rowParams.topMargin = dp(12);
        buttonRow.addView(checkButton, wrapParams());
        LinearLayout.LayoutParams cacheParams = wrapParams();
        cacheParams.leftMargin = dp(8);
        buttonRow.addView(cacheButton, cacheParams);
        card.addView(buttonRow, rowParams);

        return card;
    }

    /** Degraded-service card (WATCH_UI.md §8). The chain must come verbatim from the
     * server's degradation[]; the client never invents or estimates a step, and no retry
     * button is rendered because this screen has no retry action to wire up. */
    private View buildDegradedCard(JSONArray degradation) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackgroundResource(R.drawable.bg_card_high_risk);
        card.setPadding(dp(14), dp(14), dp(14), dp(14));

        TextView title = new TextView(this);
        title.setText(R.string.degraded_title);
        title.setTextSize(16);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        title.setTextColor(getColor(R.color.color_text_primary));
        card.addView(title);

        TextView claim = new TextView(this);
        claim.setText(R.string.degraded_claim);
        claim.setTextSize(14);
        claim.setTextColor(getColor(R.color.color_text_primary));
        LinearLayout.LayoutParams claimParams = wrapParams();
        claimParams.topMargin = dp(6);
        card.addView(claim, claimParams);

        int shown = Math.min(degradation.length(), 4);
        for (int index = 0; index < shown; index++) {
            JSONObject stage = degradation.optJSONObject(index);
            if (stage == null) continue;
            JSONArray lossArray = stage.optJSONArray("capability_loss");
            StringBuilder loss = new StringBuilder();
            if (lossArray != null) {
                for (int lossIndex = 0; lossIndex < lossArray.length(); lossIndex++) {
                    if (loss.length() > 0) loss.append("，");
                    loss.append(lossArray.optString(lossIndex, ""));
                }
            }
            TextView line = new TextView(this);
            line.setText(getString(R.string.degraded_stage_format,
                    stage.optString("stage", getString(R.string.status_unavailable)),
                    stage.optString("from", getString(R.string.status_unavailable)),
                    stage.optString("to", getString(R.string.status_unavailable)),
                    loss));
            line.setTextSize(12);
            line.setTypeface(android.graphics.Typeface.MONOSPACE);
            line.setTextColor(getColor(R.color.color_text_secondary));
            LinearLayout.LayoutParams lineParams = wrapParams();
            lineParams.topMargin = dp(8);
            card.addView(line, lineParams);
        }
        if (degradation.length() > shown) {
            TextView more = new TextView(this);
            more.setText(getString(R.string.degraded_more_suffix, degradation.length() - shown));
            more.setTextSize(11);
            more.setTextColor(getColor(R.color.color_text_tertiary));
            LinearLayout.LayoutParams moreParams = wrapParams();
            moreParams.topMargin = dp(6);
            card.addView(more, moreParams);
        }

        TextView notice = new TextView(this);
        notice.setText(R.string.degraded_notice);
        notice.setTextSize(11);
        notice.setTextColor(getColor(R.color.color_text_secondary));
        LinearLayout.LayoutParams noticeParams = wrapParams();
        noticeParams.topMargin = dp(10);
        card.addView(notice, noticeParams);

        return card;
    }

    private TextView pillButton(String label, boolean primary) {
        TextView view = new TextView(this);
        view.setText(label);
        view.setTextSize(11);
        view.setMinHeight(dp(30));
        view.setGravity(android.view.Gravity.CENTER);
        view.setPadding(dp(12), 0, dp(12), 0);
        if (primary) {
            view.setTextColor(getColor(R.color.color_text_on_accent));
            view.setBackgroundResource(R.drawable.bg_pill_accent);
        } else {
            view.setTextColor(getColor(R.color.color_text_primary));
            view.setBackgroundResource(R.drawable.bg_pill_outline);
        }
        return view;
    }

    private LinearLayout.LayoutParams wrapParams() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
    }

    /**
     * Shows how long is left before this take is submitted. A pause is usually
     * the user still thinking, so ending it silently is the wrong behaviour.
     */
    private void applyVoiceGrace(int remainingMs) {
        TextView grace = findViewById(R.id.voice_grace);
        if (grace == null) return;
        if (remainingMs < 0) {
            grace.setVisibility(android.view.View.GONE);
            return;
        }
        grace.setVisibility(android.view.View.VISIBLE);
        grace.setText(getString(R.string.voice_grace_format, (remainingMs + 999) / 1000));
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }


    private String actionLabel(String action) {
        if ("accept".equals(action)) return getString(R.string.action_accept);
        if ("reject".equals(action)) return getString(R.string.action_reject);
        if ("modify".equals(action)) return getString(R.string.action_modify);
        if ("delete".equals(action)) return getString(R.string.action_delete);
        if ("undo".equals(action)) return getString(R.string.action_undo);
        return action;
    }

    private void showModifyDialog(JSONObject activity) {
        EditText editor = new EditText(this);
        editor.setHint(R.string.modify_dialog_hint);
        editor.setText(activity.optString("content", ""));
        editor.setSelection(editor.length());
        new AlertDialog.Builder(this)
                .setTitle(R.string.modify_dialog_title)
                .setView(editor)
                .setNegativeButton(R.string.dialog_cancel, null)
                .setPositiveButton(R.string.dialog_save, (dialog, which) ->
                        performActivityAction(activity, "modify",
                                editor.getText().toString().trim()))
                .show();
    }

    /** Text entry point kept alive under low-battery dim (WATCH_UI.md §9): sends straight
     * to /api/message, the same channel voice transcripts use once recognized. */
    private void openTextInput() {
        EditText editor = new EditText(this);
        editor.setHint(R.string.modify_dialog_hint);
        new AlertDialog.Builder(this)
                .setTitle(R.string.lowbatt_action_text)
                .setView(editor)
                .setNegativeButton(R.string.dialog_cancel, null)
                .setPositiveButton(R.string.dialog_save, (dialog, which) ->
                        sendTextMessage(editor.getText().toString().trim()))
                .show();
    }

    private void sendTextMessage(String text) {
        if (text.isEmpty() || baseUrl().isEmpty()) return;
        JSONObject payload = new JSONObject();
        try {
            payload.put("text", text);
            payload.put("device_id", deviceId);
        } catch (Exception error) {
            return;
        }
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/message")
                .post(RequestBody.create(payload.toString(), JSON))).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        R.string.toast_action_not_delivered, Toast.LENGTH_SHORT).show());
            }

            @Override
            public void onResponse(Call call, Response response) {
                response.close();
                runOnUiThread(() -> fetchActivities(false));
            }
        });
    }

    private void performActivityAction(JSONObject activity, String action, String content) {
        if (activityActionInFlight) return;
        String eventId = activity.optString("event_id", "");
        if (eventId.isEmpty()) {
            Toast.makeText(this, R.string.toast_activity_missing_id, Toast.LENGTH_SHORT).show();
            return;
        }
        if ("modify".equals(action) && (content == null || content.isEmpty())) {
            Toast.makeText(this, R.string.toast_content_empty, Toast.LENGTH_SHORT).show();
            return;
        }
        activityActionInFlight = true;
        renderPrimaryActivity(activity);
        JSONObject payload = new JSONObject();
        try {
            payload.put("action", action);
            payload.put("activity_id", activity.optString("id", ""));
            payload.put("event_id", eventId);
            payload.put("revision", activity.optInt("revision", 0));
            payload.put("idempotency_key", action + "-" + UUID.randomUUID());
            if (content != null) payload.put("content", content);
        } catch (Exception error) {
            activityActionInFlight = false;
            renderPrimaryActivity(activity);
            Toast.makeText(this, R.string.toast_action_invalid, Toast.LENGTH_SHORT).show();
            return;
        }
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/activities/" + Uri.encode(eventId) + "/actions")
                .post(RequestBody.create(payload.toString(), JSON))).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                runOnUiThread(() -> finishActivityAction(
                        activity, getString(R.string.toast_action_not_delivered)));
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String responseBody = response.body() == null ? "" : response.body().string();
                response.close();
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> finishActivityAction(
                            activity, getString(R.string.toast_action_not_completed)));
                    return;
                }
                try {
                    JSONObject result = new JSONObject(responseBody).optJSONObject("activity");
                    runOnUiThread(() -> {
                        activityActionInFlight = false;
                        if (result != null) renderPrimaryActivity(result);
                        fetchActivities(false);
                        Toast.makeText(MainActivity.this,
                                R.string.toast_action_updated, Toast.LENGTH_SHORT).show();
                    });
                } catch (Exception error) {
                    runOnUiThread(() -> finishActivityAction(
                            activity, getString(R.string.toast_action_response_invalid)));
                }
            }
        });
    }

    private void finishActivityAction(JSONObject activity, String message) {
        activityActionInFlight = false;
        renderPrimaryActivity(activity);
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    private void showActivityDetails(JSONObject activity) {
        JSONObject detail = activity.optJSONObject("detail");
        StringBuilder message = new StringBuilder();
        message.append(getString(R.string.detail_status_prefix))
                .append(statusLabel(activity)).append("\n");
        message.append(getString(R.string.detail_source_prefix))
                .append(activity.optString("source", getString(R.string.detail_unknown_source)))
                .append("\n");
        String reason = detail == null ? "" : detail.optString("reason", "");
        String reply = detail == null ? "" : detail.optString("reply", "");
        if (!reason.isEmpty()) {
            message.append(getString(R.string.detail_reason_prefix)).append(reason).append("\n");
        }
        if (!reply.isEmpty()) {
            message.append(getString(R.string.detail_reply_prefix)).append(reply).append("\n");
        }
        JSONArray history = detail == null ? null : detail.optJSONArray("history");
        if (history != null && history.length() > 0) {
            message.append(getString(R.string.detail_history_prefix));
            for (int index = 0; index < history.length(); index++) {
                JSONObject record = history.optJSONObject(index);
                if (record != null) {
                    message.append("\n· ").append(record.optString("action", "已记录"));
                }
            }
            message.append("\n");
        }
        JSONArray availableActions = activity.optJSONArray("available_actions");
        if (availableActions != null && availableActions.length() > 0) {
            message.append(getString(R.string.detail_actions_prefix));
            for (int index = 0; index < availableActions.length(); index++) {
                message.append(index == 0 ? " " : "、")
                        .append(actionLabel(availableActions.optString(index)));
            }
            message.append("\n");
        }
        message.append(getString(R.string.detail_event_id_prefix))
                .append(activity.optString("event_id", ""));
        new AlertDialog.Builder(this)
                .setTitle(activity.optString("title", getString(R.string.detail_default_title)))
                .setMessage(message.toString())
                .setPositiveButton(R.string.detail_close, null)
                .show();
    }

    private String statusLabel(JSONObject activity) {
        String status = activity.optString("status", "");
        JSONObject detail = activity.optJSONObject("detail");
        if ("waiting_user".equals(status)
                && detail != null
                && "input".equals(detail.optString("waiting_for", ""))) {
            return "等待输入";
        }
        if ("observed".equals(status)) return "已观察";
        if ("processing".equals(status)) return "处理中";
        if ("waiting_user".equals(status)) return "等待决定";
        if ("accepted".equals(status)) return "已接受";
        if ("rejected".equals(status)) return "已暂不";
        if ("completed".equals(status)) return "已完成";
        if ("cancelled".equals(status)) return "已取消";
        if ("failed".equals(status)) return "失败";
        if ("expired".equals(status)) return "已过期";
        if ("ignored".equals(status)) return "已忽略";
        return status.isEmpty() ? "未知状态" : status;
    }

    private String baseUrl() {
        return preferences.getString("server", "").trim().replaceAll("/+$", "");
    }

    private Request.Builder authorized(Request.Builder builder) {
        String value = preferences.getString("token", "").trim();
        if (!value.isEmpty()) builder.header("Authorization", "Bearer " + value);
        return builder;
    }

    private void toggleRecording() {
        if (preferences.getBoolean("sensing_enabled", true)) {
            Toast.makeText(this, "后台持续感知已开启，检测到声音后会自动识别",
                    Toast.LENGTH_SHORT).show();
            return;
        }
        if (recorder == null) {
            startRecording();
        } else {
            stopRecording();
        }
    }

    /** Press-and-hold start path, shared by the home tap-to-toggle flow and the
     * voice overlay's hold button (WATCH_UI.md §5: 松手前不发起任何工具调用). */
    private boolean startRecording() {
        if (recorder != null) return true;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.RECORD_AUDIO}, RECORD_PERMISSION);
            return false;
        }
        File file = new File(getCacheDir(), "jarvis-" + System.currentTimeMillis() + ".wav");
        streamingAudio = socketReady;
        audioSessionId = "speech-" + UUID.randomUUID();
        audioSequence = 0L;
        try {
            if (streamingAudio) sendAudioStart();
            PcmRecorder nextRecorder = new PcmRecorder(file, this::sendAudioChunk);
            nextRecorder.start();
            recorder = nextRecorder;
        } catch (RuntimeException error) {
            streamingAudio = false;
            audioSessionId = null;
            file.delete();
            Toast.makeText(this,
                    error.getMessage() == null ? "无法使用麦克风" : error.getMessage(),
                    Toast.LENGTH_LONG).show();
            return false;
        }
        applyPerceptionState("recording", null);
        soundWave.setActive(true);
        return true;
    }

    private void stopRecording() {
        if (recorder == null) return;
        File file = recorder.stop();
        recorder = null;
        asrState.setText(R.string.perception_asr_idle);
        perceptionText.setText(R.string.perception_placeholder);
        soundWave.setActive(false);
        if (streamingAudio) {
            if (socketReady && socket != null) {
                sendAudioEnd();
                file.delete();
            } else {
                // The realtime socket may have dropped while recording.
                // Keep the captured WAV and fall back to HTTP upload.
                uploadAudio(file);
            }
            streamingAudio = false;
        } else {
            uploadAudio(file);
        }
    }

    private void sendAudioStart() {
        if (socket == null || audioSessionId == null) return;
        try {
            JSONObject payload = new JSONObject();
            payload.put("type", "audio_start");
            payload.put("session_id", audioSessionId);
            payload.put("sample_rate", 16000);
            payload.put("channels", 1);
            payload.put("sample_width", 2);
            payload.put("request_id", "audio-" + System.currentTimeMillis());
            socket.send(payload.toString());
        } catch (Exception ignored) {
        }
    }

    private void sendAudioChunk(byte[] data) {
        WebSocket target = socket;
        if (!streamingAudio || target == null || audioSessionId == null) return;
        try {
            JSONObject payload = new JSONObject();
            payload.put("type", "audio_chunk");
            payload.put("session_id", audioSessionId);
            payload.put("sequence", audioSequence++);
            payload.put("data", Base64.encodeToString(data, Base64.NO_WRAP));
            target.send(payload.toString());
        } catch (Exception ignored) {
        }
    }

    private void sendAudioEnd() {
        WebSocket target = socket;
        if (target == null || audioSessionId == null) return;
        try {
            JSONObject payload = new JSONObject();
            payload.put("type", "audio_end");
            payload.put("session_id", audioSessionId);
            payload.put("sequence", audioSequence);
            target.send(payload.toString());
        } catch (Exception ignored) {
        }
    }

    private void uploadAudio(File file) {
        okhttp3.RequestBody audioBody = RequestBody.create(file, MediaType.parse("audio/wav"));
        okhttp3.RequestBody body = new okhttp3.MultipartBody.Builder()
                .setType(okhttp3.MultipartBody.FORM)
                .addFormDataPart("device_id", deviceId)
                .addFormDataPart("audio", file.getName(), audioBody)
                .build();
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/message/audio")
                .header("X-Device-ID", deviceId)
                .post(body)).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                String detail = error instanceof java.net.SocketTimeoutException
                        ? "语音上传超时，请稍后重试"
                        : "语音发送失败：" + error.getMessage();
                runOnUiThread(() -> {
                    showVoiceFailure(detail);
                    recordVoiceFailure(detail);
                    applyRemotePerceptionState("error", detail);
                });
                file.delete();
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                if (!response.isSuccessful()) {
                    String detail = "语音发送失败（HTTP " + response.code() + "）";
                    runOnUiThread(() -> {
                        showVoiceFailure(detail);
                        recordVoiceFailure(detail);
                        applyRemotePerceptionState("error", detail);
                    });
                }
                response.close();
                file.delete();
            }
        });
    }

    private void connectSocket() {
        if (baseUrl().isEmpty()) return;
        manualSocketClose = false;
        reconnectScheduled = false;
        WebSocket oldSocket = socket;
        socket = null;
        if (oldSocket != null) oldSocket.close(1000, "reconnect");
        registerDevice();
        String wsBase = baseUrl().replaceFirst("^http", "ws");
        String url = wsBase + "/ws/device/" + deviceId
                + "?token=" + preferences.getString("token", "").trim();
        Request request;
        try {
            request = new Request.Builder().url(url).build();
        } catch (IllegalArgumentException error) {
            return;
        }
        WebSocket newSocket = http.newWebSocket(
                request, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                if (socket != webSocket) return;
                socket = webSocket;
                socketReady = true;
                reconnectScheduled = false;
                reconnectAttempt = 0;
                socketHandler.removeCallbacks(heartbeat);
                socketHandler.post(heartbeat);
                runOnUiThread(() -> {
                    updateServerConfigNotice();
                    updateHeaderStatus(null);
                    fetchActivities(false);
                });
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                try {
                    JSONObject json = new JSONObject(text);
                    String type = json.optString("type");
                    if (isDuplicatePush(json)) return;
                    if ("task_due".equals(type) || "timer.fired".equals(type)) {
                        String value = json.optString("text",
                                json.optString("title", "有一条提醒"));
                        updateActivity(value, true);
                        runOnUiThread(() -> {
                            vibrate();
                            fetchActivities(false);
                        });
                    } else if ("command".equals(type)) {
                        runOnUiThread(() -> executeLocalCommand(json));
                    } else if ("tts_start".equals(type) || "tts_chunk".equals(type)
                            || "tts_end".equals(type) || "tts_cancel".equals(type)) {
                        runOnUiThread(() -> handleTtsMessage(webSocket, json));
                    } else if ("agent_event".equals(type)) {
                        handleAgentEvent(json);
                    } else if ("attention_required".equals(type)) {
            handleAttentionRequired(json);
        } else if ("speech_partial".equals(type)) {
                        String partial = json.optString("text", "");
                        runOnUiThread(() -> {
                            if (!partial.isEmpty()) {
                                lastTranscript = partial;
                                applyPerceptionState("asr", "正在识别：“" + partial + "”");
                            }
                        });
                    } else if ("speech_final".equals(type)) {
                        String transcript = json.optString("text", "");
                        JSONArray utterances = json.optJSONArray("utterances");
                        if (!transcript.isEmpty()) {
                            runOnUiThread(() -> {
                                lastTranscript = transcript;
                                lastSttText = transcript;
                                startVoiceResponseTimer();
                                showTranscript(transcript, utterances);
                                applyRemotePerceptionState(
                                        json.optString("state", "processing"), null);
                            });
                        }
                    } else if ("perception_state".equals(type)) {
                        String state = json.optString("state", "idle");
                        String detail = json.optString("text", "");
                        runOnUiThread(() -> applyRemotePerceptionState(state, detail));
                    } else if ("tts_unavailable".equals(type)) {
                        String detail = json.optString("text", "TTS 不可用，已保留文字回复");
                        runOnUiThread(() -> applyRemotePerceptionState("error", detail));
                    } else if ("speech_error".equals(type)) {
                        String error = json.optString("error", "语音识别失败");
                        runOnUiThread(() -> {
                            showVoiceFailure(error);
                            recordVoiceFailure(error);
                            applyRemotePerceptionState("error", error);
                            Toast.makeText(MainActivity.this, error, Toast.LENGTH_LONG).show();
                        });
                    }
                } catch (Exception ignored) {
                }
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable error, Response response) {
                if (socket != webSocket) return;
                socket = null;
                socketReady = false;
                socketHandler.removeCallbacks(heartbeat);
                String detail = error instanceof java.net.SocketTimeoutException
                        ? "WebSocket 响应超时，正在重连"
                        : "连接服务器失败：" + error.getMessage();
                runOnUiThread(() -> applyRemotePerceptionState("offline", detail));
                scheduleReconnect();
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                if (socket != webSocket) return;
                socket = null;
                socketReady = false;
                socketHandler.removeCallbacks(heartbeat);
                if (!manualSocketClose) {
                    runOnUiThread(() -> applyRemotePerceptionState(
                            "offline", "服务器连接已断开，正在重连"));
                    scheduleReconnect();
                }
            }
        });
        socket = newSocket;
    }

    private void initializeTextToSpeech() {
        try {
            textToSpeech = new TextToSpeech(this, status -> {
                if (status != TextToSpeech.SUCCESS) {
                    ttsInitError = "tts_engine_unavailable_" + status;
                    ttsReady = false;
                    preferences.edit().putBoolean("tts_ready", false)
                            .putString("tts_error", ttsInitError).apply();
                    socketHandler.post(heartbeat);
                    return;
                }
                int languageStatus = textToSpeech.setLanguage(Locale.getDefault());
                if (languageStatus == TextToSpeech.LANG_MISSING_DATA
                        || languageStatus == TextToSpeech.LANG_NOT_SUPPORTED) {
                    ttsInitError = "tts_language_unavailable";
                    ttsReady = false;
                    preferences.edit().putBoolean("tts_ready", false)
                            .putString("tts_error", ttsInitError).apply();
                    socketHandler.post(heartbeat);
                    return;
                }
                ttsReady = true;
                ttsInitError = null;
                preferences.edit()
                        .putBoolean("tts_ready", true)
                        .remove("tts_error")
                        .apply();
                socketHandler.post(heartbeat);
                textToSpeech.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override
                    public void onStart(String utteranceId) {
                    }

                    @Override
                    public void onDone(String utteranceId) {
                        runOnUiThread(() -> onTtsUtteranceFinished(utteranceId, null));
                    }

                    @Override
                    public void onError(String utteranceId) {
                        runOnUiThread(() -> onTtsUtteranceFinished(
                                utteranceId, "playback_error"));
                    }
                });
                flushPendingTtsSegments();
            });
        } catch (RuntimeException error) {
            ttsInitError = "tts_service_missing";
            ttsReady = false;
            preferences.edit().putBoolean("tts_ready", false)
                    .putString("tts_error", ttsInitError).apply();
            socketHandler.post(heartbeat);
        }
    }

    private void handleTtsMessage(WebSocket webSocket, JSONObject json) {
        String type = json.optString("type");
        String runId = json.optString("run_id", "");
        if ("tts_start".equals(type)) {
            stopTtsPlayback(false);
            preferences.edit().putBoolean("tts_playback_active", true).apply();
            applyPerceptionState("playing", "正在播放回复");
            activeTtsRunId = runId.isEmpty() ? "tts-" + System.currentTimeMillis() : runId;
            ttsSocket = webSocket;
            ttsEndReceived = false;
            ttsChunkSequence = 0L;
            socketHandler.removeCallbacks(ttsInitTimeout);
            socketHandler.postDelayed(ttsInitTimeout, 8000);
            enqueueTtsSegment(activeTtsRunId, json.optString("text", ""));
        } else if ("tts_chunk".equals(type)) {
            if (activeTtsRunId == null) {
                activeTtsRunId = runId.isEmpty() ? "tts-" + System.currentTimeMillis() : runId;
                ttsSocket = webSocket;
                ttsEndReceived = false;
            }
            if (runId.isEmpty() || activeTtsRunId.equals(runId)) {
                String audio = json.optString("audio_base64", "");
                if (!audio.isEmpty()) {
                    enqueueAudioSegment(activeTtsRunId, audio, json.optInt("sequence", 0));
                } else {
                    enqueueTtsSegment(activeTtsRunId, json.optString("text", ""));
                }
            }
        } else if ("tts_end".equals(type)) {
            if (activeTtsRunId != null && (runId.isEmpty() || activeTtsRunId.equals(runId))) {
                ttsEndReceived = true;
                finishTtsIfReady();
            }
        } else if ("tts_cancel".equals(type)) {
            if (activeTtsRunId != null && (runId.isEmpty() || activeTtsRunId.equals(runId))) {
                String canceledRunId = activeTtsRunId;
                stopTtsPlayback(true);
                sendTtsReceipt(webSocket, "tts_canceled", canceledRunId, null);
            }
        }
    }

    private void enqueueTtsSegment(String runId, String text) {
        if (text == null || text.trim().isEmpty()) return;
        String utteranceId = runId + "-" + (++ttsChunkSequence);
        TtsSegment segment = new TtsSegment(runId, text, utteranceId);
        if (!ttsReady) {
            pendingTtsSegments.add(segment);
            return;
        }
        speakTtsSegment(segment);
    }

    private void flushPendingTtsSegments() {
        while (!pendingTtsSegments.isEmpty()) {
            TtsSegment segment = pendingTtsSegments.removeFirst();
            if (activeTtsRunId != null && activeTtsRunId.equals(segment.runId)) {
                speakTtsSegment(segment);
            }
        }
        finishTtsIfReady();
    }

    private void speakTtsSegment(TtsSegment segment) {
        if (textToSpeech == null || !ttsReady || !segment.runId.equals(activeTtsRunId)) {
            return;
        }
        ttsPendingUtterances += 1;
        ttsUtteranceRuns.put(segment.utteranceId, segment.runId);
        int result = textToSpeech.speak(
                segment.text, TextToSpeech.QUEUE_ADD, null, segment.utteranceId);
        if (result == TextToSpeech.ERROR) {
            ttsUtteranceRuns.remove(segment.utteranceId);
            ttsPendingUtterances = Math.max(0, ttsPendingUtterances - 1);
            failTts(segment.runId, "speak_failed");
        }
    }

    private void enqueueAudioSegment(String runId, String encoded, int sequence) {
        if (!runId.equals(activeTtsRunId)) return;
        try {
            byte[] data = Base64.decode(encoded, Base64.DEFAULT);
            File file = new File(getCacheDir(),
                    "jarvis-tts-" + System.currentTimeMillis() + "-" + sequence + ".mp3");
            try (FileOutputStream output = new FileOutputStream(file)) {
                output.write(data);
            }
            pendingAudioSegments.add(new AudioSegment(runId, file));
            pumpAudio();
        } catch (Exception error) {
            failTts(runId, "audio_decode_failed");
        }
    }

    private void pumpAudio() {
        if (audioPlayer != null || pendingAudioSegments.isEmpty()) {
            finishTtsIfReady();
            return;
        }
        AudioSegment segment = pendingAudioSegments.removeFirst();
        if (!segment.runId.equals(activeTtsRunId)) {
            segment.file.delete();
            pumpAudio();
            return;
        }
        try {
            audioPlayer = new MediaPlayer();
            audioPlayer.setDataSource(segment.file.getAbsolutePath());
            audioPlayer.setOnCompletionListener(player -> {
                player.release();
                audioPlayer = null;
                segment.file.delete();
                pumpAudio();
            });
            audioPlayer.setOnErrorListener((player, what, extra) -> {
                player.reset();
                player.release();
                audioPlayer = null;
                segment.file.delete();
                failTts(segment.runId, "audio_playback_failed");
                return true;
            });
            audioPlayer.prepare();
            audioPlayer.start();
        } catch (Exception error) {
            if (audioPlayer != null) {
                audioPlayer.release();
                audioPlayer = null;
            }
            segment.file.delete();
            failTts(segment.runId, "audio_prepare_failed");
        }
    }

    private void onTtsUtteranceFinished(String utteranceId, String error) {
        String runId = ttsUtteranceRuns.remove(utteranceId);
        if (runId == null) return;
        ttsPendingUtterances = Math.max(0, ttsPendingUtterances - 1);
        if (error != null && runId.equals(activeTtsRunId)) {
            failTts(runId, error);
            return;
        }
        pumpAudio();
        finishTtsIfReady();
    }

    private void failTts(String runId, String error) {
        if (runId == null || !runId.equals(activeTtsRunId)) return;
        WebSocket failedSocket = ttsSocket;
        stopTtsPlayback(true);
        preferences.edit().putBoolean("tts_playback_active", false).apply();
        applyPerceptionState("error", "播放失败：" + error);
        sendTtsReceipt(failedSocket, "tts_error", runId, error);
    }

    private void finishTtsIfReady() {
        if (!ttsEndReceived || ttsPendingUtterances != 0
                || !pendingTtsSegments.isEmpty() || !pendingAudioSegments.isEmpty()
                || audioPlayer != null || activeTtsRunId == null) {
            return;
        }
        String completedRunId = activeTtsRunId;
        WebSocket completedSocket = ttsSocket;
        activeTtsRunId = null;
        ttsEndReceived = false;
        ttsSocket = null;
        socketHandler.removeCallbacks(ttsInitTimeout);
        preferences.edit().putBoolean("tts_playback_active", false).apply();
        applyPerceptionState("listening", null);
        sendTtsReceipt(completedSocket, "tts_completed", completedRunId, null);
    }

    private void stopTtsPlayback(boolean clearActiveRun) {
        if (textToSpeech != null) textToSpeech.stop();
        if (audioPlayer != null) {
            audioPlayer.stop();
            audioPlayer.release();
            audioPlayer = null;
        }
        for (AudioSegment segment : pendingAudioSegments) {
            segment.file.delete();
        }
        pendingAudioSegments.clear();
        pendingTtsSegments.clear();
        ttsUtteranceRuns.clear();
        ttsPendingUtterances = 0;
        ttsEndReceived = false;
        socketHandler.removeCallbacks(ttsInitTimeout);
        if (clearActiveRun) {
            activeTtsRunId = null;
            ttsSocket = null;
            preferences.edit().putBoolean("tts_playback_active", false).apply();
        }
    }

    private void applyRemotePerceptionState(String state, String detail) {
        remotePerceptionState = state;
        remotePerceptionStateUntil = System.currentTimeMillis() + 10000;
        if (detail != null && !detail.isEmpty()) {
            preferences.edit().putString("perception_detail", detail).apply();
        } else if (!"error".equals(state)) {
            preferences.edit().remove("perception_detail").apply();
        }
        applyPerceptionState(state, detail);
    }

    private void applyPerceptionState(String state, String detail) {
        if (asrState == null || perceptionText == null) return;
        String safeState = state == null ? "idle" : state;
        updateHeaderStatus(safeState);
        updateVoiceOverlayStatus(safeState, detail);
        if ("playing".equals(safeState)) {
            asrState.setText(R.string.perception_state_playing);
            if (detail == null || detail.isEmpty()) {
                perceptionText.setText(R.string.perception_playing);
            } else {
                perceptionText.setText(detail);
            }
        } else if ("voice_detected".equals(safeState)) {
            asrState.setText(R.string.perception_state_voice);
            perceptionText.setText(R.string.perception_voice_detected);
        } else if ("recording".equals(safeState)) {
            asrState.setText(R.string.header_status_recording);
            perceptionText.setText("正在录音，松开后发送识别");
        } else if ("asr".equals(safeState)) {
            asrState.setText(R.string.perception_state_asr);
            if (detail == null || detail.isEmpty()) {
                perceptionText.setText(R.string.perception_asr_processing);
            } else {
                perceptionText.setText(detail);
            }
        } else if ("processing".equals(safeState)) {
            asrState.setText(R.string.perception_state_processing);
            if (detail != null && !detail.isEmpty()) {
                perceptionText.setText(detail);
            } else if (lastTranscript.isEmpty()) {
                perceptionText.setText(R.string.perception_processing);
            } else {
                perceptionText.setText("“" + lastTranscript + "”");
            }
        } else if ("waiting_user".equals(safeState)) {
            asrState.setText("等待输入");
            perceptionText.setText(detail == null || detail.isEmpty()
                    ? "请继续说话"
                    : detail);
        } else if ("error".equals(safeState)) {
            asrState.setText(R.string.perception_state_error);
            if (detail == null || detail.isEmpty()) {
                perceptionText.setText(R.string.perception_error);
            } else {
                perceptionText.setText(detail);
            }
        } else if ("listening".equals(safeState)) {
            asrState.setText(R.string.perception_state_listening);
            perceptionText.setText(R.string.perception_listening_idle);
        } else {
            asrState.setText(R.string.perception_asr_idle);
            perceptionText.setText(R.string.perception_placeholder);
        }
        soundWave.setActive("recording".equals(safeState)
                || "voice_detected".equals(safeState)
                || "asr".equals(safeState)
                || "processing".equals(safeState)
                || "playing".equals(safeState));
        if (!"recording".equals(safeState)
                && !"voice_detected".equals(safeState) && !"asr".equals(safeState)) {
            soundWave.setLevel(0f);
        }
    }

    private void updateStatusBarClockAndBattery() {
        String clock = new java.text.SimpleDateFormat(
                "HH:mm", Locale.getDefault()).format(new java.util.Date());
        if (statusClock != null) statusClock.setText(clock);
        if (idleClock != null) idleClock.setText(clock);
        if (idleDate != null) idleDate.setText(idleDateText());
        if (statusBattery != null) {
            int level = getBatteryLevel();
            statusBattery.setText(level < 0 ? "--%" : level + "%");
        }
        updateIdleFacts();
        // Deliberately does not re-render the attention stream: the clock ticks
        // every 15s and rebuilding the card list threw away the scroll position.
    }

    /** 抬腕详情 collapses so the stream gets the screen when you scroll. */
    private void toggleIdleDetails() {
        boolean open = idleDetails == null || idleDetails.getVisibility() != android.view.View.VISIBLE;
        preferences.edit().putBoolean("idle_details_open", open).apply();
        applyIdleDetailsState(open);
    }

    private void applyIdleDetailsState(boolean open) {
        if (idleDetails == null || idleDetailsToggle == null) return;
        idleDetails.setVisibility(open ? android.view.View.VISIBLE : android.view.View.GONE);
        idleDetailsToggle.setText(
                open ? R.string.idle_details_open : R.string.idle_details_closed);
    }

    private void updateIdleFacts() {
        if (idleNetwork != null) idleNetwork.setText(networkLabel());
        if (idleBattery != null) {
            int level = getBatteryLevel();
            idleBattery.setText(level < 0
                    ? getString(R.string.idle_battery_unknown)
                    : getString(R.string.idle_battery_format, level));
        }
        if (idleRadio != null) idleRadio.setText(radioLabel());
        if (idleSync != null) {
            long at = preferences.getLong("last_sync_at", 0L);
            idleSync.setText(at <= 0
                    ? getString(R.string.idle_sync_never)
                    : getString(R.string.idle_sync_format, new java.text.SimpleDateFormat(
                            "HH:mm:ss", Locale.getDefault()).format(new java.util.Date(at))));
        }
    }

    private String networkLabel() {
        try {
            android.net.ConnectivityManager manager =
                    (android.net.ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (manager == null) return getString(R.string.idle_network_unknown);
            android.net.NetworkInfo info = manager.getActiveNetworkInfo();
            if (info == null || !info.isConnected()) return getString(R.string.idle_network_none);
            return info.getType() == android.net.ConnectivityManager.TYPE_WIFI
                    ? getString(R.string.idle_network_wifi)
                    : getString(R.string.idle_network_cellular);
        } catch (RuntimeException error) {
            return getString(R.string.idle_network_unknown);
        }
    }

    private String radioLabel() {
        try {
            android.bluetooth.BluetoothAdapter adapter =
                    android.bluetooth.BluetoothAdapter.getDefaultAdapter();
            if (adapter == null) return getString(R.string.idle_radio_none);
            return adapter.isEnabled()
                    ? getString(R.string.idle_radio_ready)
                    : getString(R.string.idle_radio_off);
        } catch (RuntimeException error) {
            return getString(R.string.idle_radio_none);
        }
    }

    private String idleDateText() {
        java.util.Calendar now = java.util.Calendar.getInstance();
        String[] weekdays = {"周日", "周一", "周二", "周三", "周四", "周五", "周六"};
        return (now.get(java.util.Calendar.MONTH) + 1) + " 月 "
                + now.get(java.util.Calendar.DAY_OF_MONTH) + " 日 · "
                + weekdays[now.get(java.util.Calendar.DAY_OF_WEEK) - 1];
    }

    private int waitingUserCount;

    /**
     * Status word is strictly one of the four WATCH_UI.md §总则·2 phrases: it reflects
     * connection + waiting-permission state, not the transient per-utterance perception
     * state (recording/ASR/playing/...), which only drives the perception line below it.
     */
    private void updateHeaderStatus(String state) {
        if (headerStatus == null) return;
        String token = preferences.getString("token", "").trim();
        if (token.isEmpty() || baseUrl().isEmpty()) {
            headerStatus.setText(R.string.statusword_unconfigured);
            headerStatus.setTextColor(getColor(R.color.color_text_tertiary));
            return;
        }
        if (isLowBatteryDim()) {
            headerStatus.setText(R.string.statusword_low_battery);
            headerStatus.setTextColor(getColor(R.color.color_warning_text));
            return;
        }
        if (!socketReady || "offline".equals(state)) {
            headerStatus.setText(R.string.statusword_offline);
            headerStatus.setTextColor(getColor(R.color.color_error_text));
            return;
        }
        if (latestDegradation(lastActivityItems) != null) {
            headerStatus.setText(R.string.statusword_degraded);
            headerStatus.setTextColor(getColor(R.color.color_error_text));
            return;
        }
        if (waitingUserCount > 0) {
            headerStatus.setText(getString(R.string.statusword_online_waiting, waitingUserCount));
            headerStatus.setTextColor(getColor(R.color.color_warning_text));
        } else {
            headerStatus.setText(R.string.statusword_online_idle);
            headerStatus.setTextColor(getColor(R.color.color_success_text));
        }
    }

    private void updateHeaderStatusFromActivities(JSONArray items) {
        int waiting = 0;
        if (items != null) {
            for (int index = 0; index < items.length(); index++) {
                JSONObject item = items.optJSONObject(index);
                if (item != null && "waiting_user".equals(item.optString("status", ""))) {
                    waiting++;
                }
            }
        }
        waitingUserCount = waiting;
        // The dot is the quiet channel: it stays lit while anything waits on you.
        showAttentionDot(waiting > 0);
        updateHeaderStatus(remotePerceptionState);
    }

    private void sendTtsReceipt(WebSocket target, String type, String runId, String error) {
        if (target == null || runId == null || runId.isEmpty()) return;
        try {
            JSONObject receipt = new JSONObject();
            receipt.put("type", type);
            receipt.put("run_id", runId);
            receipt.put("request_id", "tts-" + System.currentTimeMillis());
            if (error != null) receipt.put("error", error);
            target.send(receipt.toString());
        } catch (Exception ignored) {
        }
    }

    /**
     * Permission requests wake the screen; everything else only buzzes and lights
     * the dot, so the wearer is not trained to dismiss without reading.
     */
    private void handleAttentionRequired(JSONObject json) {
        String level = json.optString("level", "low");
        String kind = json.optString("kind", "notice");
        String title = json.optString("title", "");
        String pendingId = json.optString("pending_action_id", "");
        String eventId = json.optString("event_id", "");
        if ("permission".equals(kind) && !pendingId.isEmpty()) {
            pendingPermissionId = pendingId;
            pendingPermissionTitle = title;
            runOnUiThread(this::renderPermissionAction);
        }
        vibrate();
        showAttentionDot(true);
        if ("permission".equals(kind) && !pendingId.isEmpty()) {
            AttentionNotifier.postPermission(this, pendingId, title);
        } else if ("high".equals(level)) {
            AttentionNotifier.postNotice(this, eventId, title);
        }
    }

    private void showAttentionDot(boolean visible) {
        View dot = findViewById(R.id.attention_dot);
        if (dot != null) dot.setVisibility(visible ? View.VISIBLE : View.GONE);
    }

    private void handleAgentEvent(JSONObject json) {
        String text = json.optString("text",
                json.optString("message", json.optString("event", "Agent 事件")));
        JSONObject result = json.optJSONObject("result");
        boolean failed = result != null
                && "reply".equals(result.optString("action"))
                && result.optBoolean("failed", false);
        String reason = result == null ? "" : result.optString("reason", "");
        String summary = result == null ? text : result.optString("text",
                result.optString("reply", text));
        if (failed) {
            String failureReason = reason.isEmpty() ? text : reason;
            runOnUiThread(() -> {
                showVoiceFailure(failureReason);
                recordVoiceFailure(failureReason);
            });
        } else if (!summary.isEmpty()) {
            String shown = summary.length() > 60 ? summary.substring(0, 60) + "…" : summary;
            runOnUiThread(() -> {
                stopVoiceResponseTimer();
                voiceResponseText = shown;
                renderVoiceResponse();
            });
        }
        if (!text.isEmpty()) {
            preferences.edit().putString("last_reply", text).apply();
            runOnUiThread(() -> applyRemotePerceptionState("processing", text));
        }
        if (!preferences.getBoolean("ai_activity_enabled", true)) return;
        boolean needsAttention = json.optBoolean("requires_attention", false)
                || json.optBoolean("notify", false);
        updateActivity(text, needsAttention);
        runOnUiThread(() -> {
            fetchActivities(false);
            if (needsAttention) vibrate();
        });
    }

    private void showVoiceFailure(String reason) {
        stopVoiceResponseTimer();
        reason = shortFailureReason(reason);
        String safeReason = reason == null || reason.trim().isEmpty()
                ? "请再试一次" : reason.trim();
        if (safeReason.length() > 20) safeReason = safeReason.substring(0, 20);
        voiceResponseText = getString(R.string.voice_response_failed, safeReason);
        voiceResponseFailed = true;
        renderVoiceResponse();
        if (voiceCurrentStage != null) {
            voiceCurrentStage.setText(R.string.voice_stage_failed);
            voiceCurrentStage.setTextColor(getColor(R.color.color_error_text));
        }
    }

    private String shortFailureReason(String reason) {
        String safeReason = reason == null || reason.trim().isEmpty()
                ? getString(R.string.voice_failure_unknown) : reason.trim();
        return safeReason.length() > 20 ? safeReason.substring(0, 20) : safeReason;
    }

    private void recordVoiceFailure(String reason) {
        updateActivity(getString(R.string.voice_failure_activity_format,
                shortFailureReason(reason)), true);
        fetchActivities(false);
    }

    /**
     * Text first: keep the transcript and its timeline on screen instead of
     * racing to speak. Speaking is a deliberate tap, not a reflex.
     */
    private void showTranscript(String text, JSONArray utterances) {
        if (voiceTranscript != null) {
            voiceTranscript.setText(text);
        }
        if (voiceTimeline != null) {
            if (utterances != null && utterances.length() > 0) {
                JSONObject last = utterances.optJSONObject(utterances.length() - 1);
                double seconds = last == null ? 0 : last.optDouble("end_ms", 0) / 1000.0;
                voiceTimeline.setText(getString(R.string.voice_timeline_format,
                        utterances.length(), seconds));
                voiceTimeline.setVisibility(View.VISIBLE);
            } else {
                voiceTimeline.setVisibility(View.GONE);
            }
        }
        if (voiceReplay != null) voiceReplay.setVisibility(View.VISIBLE);
        if (voiceSpeak != null) voiceSpeak.setVisibility(View.VISIBLE);
    }

    private void speak(String text) {
        if (text == null || text.isEmpty()) return;
        if (!ttsReady || textToSpeech == null) {
            // Speaking is optional; if the engine is not ready, say so in the
            // transcript area rather than pretending it played.
            if (voiceTimeline != null) {
                voiceTimeline.setVisibility(View.VISIBLE);
                voiceTimeline.setText("朗读引擎未就绪");
            }
            return;
        }
        String runId = "replay:" + System.currentTimeMillis();
        try {
            activeTtsRunId = runId;
            textToSpeech.speak(text, TextToSpeech.QUEUE_FLUSH, null, runId);
        } catch (RuntimeException error) {
            failTts(runId, error.getClass().getSimpleName());
        }
    }

    private boolean isDuplicatePush(JSONObject json) {
        String type = json.optString("type", "");
        if (!"task_due".equals(type) && !"timer.fired".equals(type)
                && !"agent_event".equals(type)) {
            return false;
        }
        String identity = json.optString("message_id",
                json.optString("dedupe_key",
                        json.optString("run_id", json.optString("event_id", ""))));
        if (identity.isEmpty()) return false;
        String key = type + ":" + identity;
        Set<String> stored = preferences.getStringSet("push_seen_keys", Collections.emptySet());
        Set<String> seen = new HashSet<>(stored);
        if (seen.contains(key)) return true;
        seen.add(key);
        while (seen.size() > 32) {
            seen.remove(seen.iterator().next());
        }
        preferences.edit().putStringSet("push_seen_keys", seen).apply();
        return false;
    }

    private void updateActivity(String value, boolean needsDecision) {
        if (value != null && !value.isEmpty()) {
            preferences.edit().putString("activity_text", value).apply();
        }
        String timestamp = new java.text.SimpleDateFormat(
                "HH:mm", Locale.getDefault()).format(new java.util.Date());
        preferences.edit().putString("activity_time", timestamp).apply();
    }

    private long nextSequence() {
        sequence += 1;
        preferences.edit().putLong("sequence", sequence).apply();
        return sequence;
    }

    private void executeLocalCommand(JSONObject command) {
        String action = command.optString("action", "");
        String commandId = command.optString("command_id", "");
        JSONObject payload = command.optJSONObject("payload");
        if (payload == null) payload = new JSONObject();
        boolean success = true;
        String error = null;
        JSONObject result = new JSONObject();
        try {
            if ("vibrate".equals(action)) {
                vibrate();
            } else if ("show_notification".equals(action)) {
                Toast.makeText(this, payload.optString("text", "Jarvis"),
                        Toast.LENGTH_SHORT).show();
            } else if ("open_url".equals(action)) {
                String url = payload.optString("url", "");
                if (url.isEmpty()) throw new IllegalArgumentException("url is empty");
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            } else if ("plugin".equals(action)) {
                success = false;
                error = "设备插件能力未启用";
            } else if ("run_script".equals(action)) {
                success = false;
                error = "设备脚本执行已禁用";
            } else {
                success = false;
                error = "unknown action";
            }
            if ("run_script".equals(action)
                    && result.optBoolean("ok", false) == false) {
                success = false;
                error = result.optString("error", "设备脚本执行失败");
            }
        } catch (Exception exception) {
            success = false;
            error = exception.getMessage();
        }
        if (socket != null && !commandId.isEmpty()) {
            try {
                JSONObject ack = new JSONObject();
                ack.put("type", "command_ack");
                ack.put("request_id", "ack-" + System.currentTimeMillis());
                ack.put("command_id", commandId);
                ack.put("success", success);
                if (error != null) ack.put("error", error);
                if (result.length() > 0) ack.put("result", result);
                socket.send(ack.toString());
            } catch (Exception ignored) {
            }
        }
    }

    private long getFreeStorageMb() {
        return getFilesDir().getUsableSpace() / (1024 * 1024);
    }

    private int getBatteryLevel() {
        BatteryManager manager = (BatteryManager) getSystemService(BATTERY_SERVICE);
        return manager == null ? -1
                : manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
    }

    private boolean isCharging() {
        Intent battery = registerReceiver(
                null, new android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (battery == null) return false;
        int status = battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
        return status == BatteryManager.BATTERY_STATUS_CHARGING
                || status == BatteryManager.BATTERY_STATUS_FULL;
    }

    private String getNetworkType() {
        ConnectivityManager manager =
                (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        if (manager == null) return "unknown";
        NetworkCapabilities capabilities =
                manager.getNetworkCapabilities(manager.getActiveNetwork());
        if (capabilities == null) return "offline";
        if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "wifi";
        if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) return "cellular";
        return "other";
    }

    private void registerDevice() {
        JSONObject payload = new JSONObject();
        try {
            payload.put("device_id", deviceId);
            payload.put("name", deviceName == null ? "Jarvis Watch" : deviceName);
            payload.put("platform", "android-watch");
        } catch (Exception ignored) {
        }
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/device/register")
                .post(RequestBody.create(payload.toString(), JSON))).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                response.close();
            }
        });
    }

    private void scheduleReconnect() {
        if (manualSocketClose || reconnectScheduled) return;
        reconnectScheduled = true;
        long delay = Math.min(30000L, 2000L * (1L << Math.min(reconnectAttempt, 4)));
        reconnectAttempt++;
        socketHandler.postDelayed(this::connectSocket, delay);
    }

    @Override
    protected void onPause() {
        stopVoiceResponseTimer();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        manualSocketClose = true;
        stopVoiceResponseTimer();
        socketHandler.removeCallbacksAndMessages(null);
        if (socket != null) socket.close(1000, "shutdown");
        if (recorder != null) {
            recorder.stop();
            recorder = null;
        }
        stopTtsPlayback(true);
        if (textToSpeech != null) {
            textToSpeech.shutdown();
            textToSpeech = null;
        }
        preferences.edit().putBoolean("tts_playback_active", false).apply();
        super.onDestroy();
    }

    private void vibrate() {
        Vibrator vibrator = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
        if (vibrator == null) return;
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            vibrator.vibrate(VibrationEffect.createOneShot(
                    500, VibrationEffect.DEFAULT_AMPLITUDE));
        } else {
            vibrator.vibrate(500);
        }
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == RECORD_PERMISSION) {
            int messageRes = (grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED)
                    ? R.string.toast_permission_mic_granted
                    : R.string.toast_permission_mic_denied;
            Toast.makeText(this, messageRes, Toast.LENGTH_SHORT).show();
        }
    }

    private static final class TtsSegment {
        private final String runId;
        private final String text;
        private final String utteranceId;

        private TtsSegment(String runId, String text, String utteranceId) {
            this.runId = runId;
            this.text = text;
            this.utteranceId = utteranceId;
        }
    }

    private static final class AudioSegment {
        private final String runId;
        private final File file;

        private AudioSegment(String runId, File file) {
            this.runId = runId;
            this.file = file;
        }
    }
}
