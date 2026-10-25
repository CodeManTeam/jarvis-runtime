package com.jarvis.watch;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
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
 * High-risk permission: show the exact draft, its irreversibility, then ask once.
 *
 * Approving here authorises this message only. The footer states that limit, and
 * a failed request never reports success — see WATCH_UI.md §4.
 */
public class PermissionActivity extends Activity {
    private static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");

    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private SharedPreferences preferences;
    private String pendingId;
    private String eventId = "";
    private TextView stateText;
    private View draftCard;
    private TextView draftTo;
    private TextView draftBody;
    private TextView draftAttachments;
    private TextView evidenceLine;
    private boolean busy;

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(ThemeMode.wrap(base));
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        preferences = getSharedPreferences("jarvis", MODE_PRIVATE);
        setContentView(R.layout.activity_permission);

        stateText = findViewById(R.id.permission_state);
        draftCard = findViewById(R.id.draft_card);
        draftTo = findViewById(R.id.draft_to);
        draftBody = findViewById(R.id.draft_body);
        draftAttachments = findViewById(R.id.draft_attachments);
        evidenceLine = findViewById(R.id.permission_evidence_line);

        bindStatusBar();
        pendingId = getIntent() == null ? "" : getIntent().getStringExtra("pending_id");
        if (pendingId == null) pendingId = "";
        pendingId = pendingId.trim();

        findViewById(R.id.cancel_button).setOnClickListener(v -> submit("reject"));
        findViewById(R.id.confirm_button).setOnClickListener(v -> submit("confirm"));
        findViewById(R.id.permission_why).setOnClickListener(v -> openEvidence());

        if (pendingId.isEmpty()) {
            stateText.setText(R.string.permission_missing_id);
            disableButtons();
            return;
        }
        if (baseUrl().isEmpty()) {
            stateText.setText(R.string.permission_need_config);
            disableButtons();
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
            BatteryManager manager = (BatteryManager) getSystemService(BATTERY_SERVICE);
            int level = manager == null ? -1
                    : manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
            battery.setText(level < 0 ? getString(R.string.status_unavailable) : level + "%");
        }
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
        stateText.setText(R.string.permission_loading);
        Request request = authorized(
                new Request.Builder().url(baseUrl() + "/api/pending-actions").get()).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                runOnUiThread(() -> stateText.setText(R.string.permission_load_failed));
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String body = response.body() == null ? "" : response.body().string();
                response.close();
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> stateText.setText(R.string.permission_load_failed));
                    return;
                }
                try {
                    JSONArray actions = new JSONObject(body).optJSONArray("actions");
                    JSONObject match = null;
                    for (int index = 0; actions != null && index < actions.length(); index++) {
                        JSONObject row = actions.optJSONObject(index);
                        if (row != null && pendingId.equals(row.optString("id", ""))) {
                            match = row;
                            break;
                        }
                    }
                    final JSONObject found = match;
                    runOnUiThread(() -> {
                        if (found == null) {
                            stateText.setText(R.string.permission_not_found);
                            disableButtons();
                        } else {
                            render(found);
                        }
                    });
                } catch (Exception error) {
                    runOnUiThread(() -> stateText.setText(R.string.permission_load_failed));
                }
            }
        });
    }

    private void render(JSONObject action) {
        stateText.setText("");
        JSONObject payload = parsePayload(action.optString("payload", ""));
        eventId = payload.optString("event_id", action.optString("event_id", ""));

        String to = payload.optString("to", payload.optString("recipient", ""));
        String subject = payload.optString("subject", "");
        String body = payload.optString("body", payload.optString("text", ""));
        String attachments = payload.optString("attachments", "");

        if (to.isEmpty() && subject.isEmpty() && body.isEmpty()) {
            // No structured draft: fall back to the recorded title rather than
            // pretending we know the content.
            draftCard.setVisibility(View.GONE);
            stateText.setText(action.optString("title", getString(R.string.permission_not_found)));
        } else {
            draftTo.setText(getString(R.string.permission_draft_to_prefix) + to);
            String quoted = body.isEmpty() ? subject : "“" + body + "”";
            draftBody.setText(quoted.isEmpty() ? "—" : quoted);
            if (attachments.isEmpty()) {
                draftAttachments.setVisibility(View.GONE);
            } else {
                draftAttachments.setVisibility(View.VISIBLE);
                draftAttachments.setText(
                        getString(R.string.permission_draft_attachments_prefix) + attachments);
            }
        }
        evidenceLine.setText(evidenceSummary(payload, action));
    }

    private String evidenceSummary(JSONObject payload, JSONObject action) {
        String confidence = "—";
        if (payload.has("confidence") && !payload.isNull("confidence")) {
            confidence = Math.round(payload.optDouble("confidence", 0) * 100) + "%";
        }
        int memories = payload.optJSONArray("memory_ids") == null
                ? 0 : payload.optJSONArray("memory_ids").length();
        int tools = payload.optJSONArray("tool_ids") == null
                ? 0 : payload.optJSONArray("tool_ids").length();
        if (action.has("memory_ids") && memories == 0 && action.optJSONArray("memory_ids") != null) {
            memories = action.optJSONArray("memory_ids").length();
        }
        return getString(R.string.permission_evidence_format, confidence, memories, tools);
    }

    private JSONObject parsePayload(String raw) {
        try {
            return new JSONObject(raw == null || raw.isEmpty() ? "{}" : raw);
        } catch (Exception error) {
            return new JSONObject();
        }
    }

    private void openEvidence() {
        if (eventId == null || eventId.trim().isEmpty()) return;
        Intent intent = new Intent(this, EvidenceActivity.class);
        intent.putExtra("event_id", eventId);
        startActivity(intent);
    }

    private void submit(String action) {
        if (busy) return;
        busy = true;
        disableButtons();
        stateText.setText(R.string.permission_loading);
        RequestBody body = RequestBody.create("{}", JSON);
        Request request = authorized(new Request.Builder()
                .url(baseUrl() + "/api/pending-actions/" + android.net.Uri.encode(pendingId)
                        + "/" + action)
                .post(body)).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                busy = false;
                runOnUiThread(() -> {
                    stateText.setText(R.string.permission_load_failed);
                    enableButtons();
                    toast(R.string.toast_permission_not_delivered);
                });
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String payload = response.body() == null ? "" : response.body().string();
                response.close();
                busy = false;
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> {
                        stateText.setText(R.string.permission_load_failed);
                        enableButtons();
                        toast(R.string.toast_permission_not_delivered);
                    });
                    return;
                }
                // A confirmed action that carries a 409-style body is not a success.
                if (payload.contains("\"status\": 409") || payload.contains("\"status\":409")) {
                    runOnUiThread(() -> {
                        stateText.setText(R.string.permission_not_found);
                        enableButtons();
                        toast(R.string.toast_permission_not_completed);
                    });
                    return;
                }
                runOnUiThread(() -> {
                    toast("confirm".equals(action)
                            ? R.string.toast_permission_confirmed
                            : R.string.toast_permission_rejected);
                    setResult(RESULT_OK);
                    finish();
                });
            }
        });
    }

    private void toast(int messageRes) {
        Toast.makeText(this, messageRes, Toast.LENGTH_SHORT).show();
    }

    private void disableButtons() {
        findViewById(R.id.cancel_button).setEnabled(false);
        findViewById(R.id.confirm_button).setEnabled(false);
    }

    private void enableButtons() {
        findViewById(R.id.cancel_button).setEnabled(true);
        findViewById(R.id.confirm_button).setEnabled(true);
    }
}
