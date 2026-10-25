package com.jarvis.watch;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.BatteryManager;
import android.os.Bundle;
import android.view.View;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * Habit suggestion: shows one ai_candidate at a time and lets the user apply it
 * for today only or ignore it with a trace. It never writes a long-term rule and
 * never reports success on a failed request — see WATCH_UI.md §6.
 */
public class SuggestionActivity extends Activity {
    private static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");

    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private final ArrayList<JSONObject> candidates = new ArrayList<>();
    private SharedPreferences preferences;
    private TextView stateText;
    private TextView claimText;
    private View evidenceCard;
    private TextView evidenceDetailText;
    private TextView evidenceLineText;
    private View buttonRow;
    private TextView ignoreButton;
    private TextView applyTodayButton;
    private TextView moreText;
    private int currentIndex;
    private boolean busy;

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(ThemeMode.wrap(base));
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        preferences = getSharedPreferences("jarvis", MODE_PRIVATE);
        setContentView(R.layout.activity_suggestion);

        findViewById(R.id.back_button).setOnClickListener(v -> finish());
        stateText = findViewById(R.id.suggestion_state);
        claimText = findViewById(R.id.suggestion_claim);
        evidenceCard = findViewById(R.id.suggestion_evidence_card);
        evidenceDetailText = findViewById(R.id.suggestion_evidence_detail);
        evidenceLineText = findViewById(R.id.suggestion_evidence_line);
        buttonRow = findViewById(R.id.suggestion_buttons);
        ignoreButton = findViewById(R.id.ignore_button);
        applyTodayButton = findViewById(R.id.apply_today_button);
        moreText = findViewById(R.id.suggestion_more);

        ignoreButton.setOnClickListener(v -> submit("ignore"));
        applyTodayButton.setOnClickListener(v -> submit("apply_today"));
        moreText.setOnClickListener(v -> showNext());

        bindStatusBar();
        if (baseUrl().isEmpty()) {
            stateText.setText(R.string.suggestion_need_config);
            hideButtons();
            return;
        }
        fetch();
    }

    private void bindStatusBar() {
        TextView clock = findViewById(R.id.status_clock);
        TextView battery = findViewById(R.id.status_battery);
        if (clock != null) {
            clock.setText(new SimpleDateFormat("HH:mm", Locale.getDefault()).format(new Date()));
        }
        if (battery != null) {
            int level = batteryLevel();
            battery.setText(level < 0 ? getString(R.string.status_unavailable) : level + "%");
        }
    }

    private int batteryLevel() {
        BatteryManager manager = (BatteryManager) getSystemService(BATTERY_SERVICE);
        if (manager == null) return -1;
        return manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
    }

    private String baseUrl() {
        return preferences.getString("server", "").trim().replaceAll("/+$", "");
    }

    private Request.Builder authorized(Request.Builder builder) {
        String token = preferences.getString("token", "").trim();
        if (!token.isEmpty()) builder.header("Authorization", "Bearer " + token);
        return builder;
    }

    private void fetch() {
        stateText.setText(R.string.suggestion_loading);
        Request request = authorized(
                new Request.Builder().url(baseUrl() + "/api/memories/candidates").get()).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                runOnUiThread(() -> {
                    stateText.setText(R.string.suggestion_load_failed);
                    hideButtons();
                });
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String body = response.body() == null ? "" : response.body().string();
                response.close();
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> {
                        stateText.setText(R.string.suggestion_load_failed);
                        hideButtons();
                    });
                    return;
                }
                try {
                    JSONArray items = new JSONObject(body).optJSONArray("memories");
                    if (items == null) items = new JSONArray();
                    JSONArray result = items;
                    runOnUiThread(() -> {
                        candidates.clear();
                        for (int index = 0; index < result.length(); index++) {
                            JSONObject item = result.optJSONObject(index);
                            if (item != null) candidates.add(item);
                        }
                        currentIndex = 0;
                        render();
                    });
                } catch (Exception error) {
                    runOnUiThread(() -> {
                        stateText.setText(R.string.suggestion_load_failed);
                        hideButtons();
                    });
                }
            }
        });
    }

    private void render() {
        if (candidates.isEmpty()) {
            stateText.setText(R.string.suggestion_empty);
            claimText.setText("—");
            evidenceCard.setVisibility(View.GONE);
            hideButtons();
            moreText.setVisibility(View.GONE);
            return;
        }
        stateText.setText("");
        showButtons();
        JSONObject candidate = candidates.get(currentIndex);
        String content = candidate.optString("content", "");
        claimText.setText(content.isEmpty() ? "—" : content);

        // The spec's "依据卡" supplements the claim with additional detail; this backend
        // has no separate supplement field, so the card only renders when distinct
        // supplementary text exists rather than repeating the claim.
        String detail = candidate.optString("detail", candidate.optString("reason", ""));
        if (detail.isEmpty() || detail.equals(content)) {
            evidenceCard.setVisibility(View.GONE);
        } else {
            evidenceCard.setVisibility(View.VISIBLE);
            evidenceDetailText.setText(detail);
        }
        evidenceLineText.setText(confidenceLine(candidate));

        int remaining = candidates.size() - 1 - currentIndex;
        if (remaining > 0) {
            moreText.setVisibility(View.VISIBLE);
            moreText.setText(getString(R.string.suggestion_more_suffix, remaining));
        } else {
            moreText.setVisibility(View.GONE);
        }
    }

    private String confidenceLine(JSONObject candidate) {
        if (!candidate.has("confidence") || candidate.isNull("confidence")) {
            return getString(R.string.suggestion_evidence_missing);
        }
        int percent = (int) Math.round(candidate.optDouble("confidence", 0) * 100);
        return getString(R.string.suggestion_evidence_format, percent);
    }

    private void showNext() {
        if (candidates.isEmpty()) return;
        currentIndex = (currentIndex + 1) % candidates.size();
        render();
    }

    private void submit(String action) {
        if (busy || candidates.isEmpty()) return;
        JSONObject candidate = candidates.get(currentIndex);
        String id = candidate.optString("id", "");
        if (id.isEmpty()) {
            toast(R.string.suggestion_load_failed);
            return;
        }
        busy = true;
        disableButtons();
        String path = "apply_today".equals(action)
                ? "/api/memories/" + android.net.Uri.encode(id) + "/apply-today"
                : "/api/memories/candidates/" + android.net.Uri.encode(id) + "/ignore";
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + path)
                .post(RequestBody.create("{}", JSON))).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                busy = false;
                runOnUiThread(() -> {
                    enableButtons();
                    toast(R.string.toast_suggestion_not_delivered);
                });
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                response.close();
                busy = false;
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> {
                        enableButtons();
                        toast(R.string.toast_suggestion_not_delivered);
                    });
                    return;
                }
                runOnUiThread(() -> {
                    toast("apply_today".equals(action)
                            ? R.string.toast_suggestion_applied_today
                            : R.string.toast_suggestion_ignored);
                    finish();
                });
            }
        });
    }

    private void toast(int messageRes) {
        Toast.makeText(this, messageRes, Toast.LENGTH_SHORT).show();
    }

    private void hideButtons() {
        buttonRow.setVisibility(View.GONE);
    }

    private void showButtons() {
        buttonRow.setVisibility(View.VISIBLE);
        enableButtons();
    }

    private void disableButtons() {
        ignoreButton.setEnabled(false);
        applyTodayButton.setEnabled(false);
    }

    private void enableButtons() {
        ignoreButton.setEnabled(true);
        applyTodayButton.setEnabled(true);
    }
}
