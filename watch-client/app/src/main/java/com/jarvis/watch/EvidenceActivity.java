package com.jarvis.watch;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.BatteryManager;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/**
 * Four-layer evidence: what happened, why, what it cited, what raw input gated it.
 *
 * The screen only renders checkable references. It never invents a confidence
 * value and never shows the model's private reasoning — see WATCH_UI.md §3.
 */
public class EvidenceActivity extends Activity {
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private SharedPreferences preferences;
    private TextView stateText;
    private TextView conclusionText;
    private TextView judgmentText;
    private TextView confidenceRiskText;
    private LinearLayout refsContainer;
    private LinearLayout rawList;
    private TextView rawToggle;
    private TextView footerText;
    private boolean rawExpanded;
    private JSONObject evidence = new JSONObject();
    private JSONArray rawItems = new JSONArray();

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(ThemeMode.wrap(base));
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        preferences = getSharedPreferences("jarvis", MODE_PRIVATE);
        setContentView(R.layout.activity_evidence);

        findViewById(R.id.back_button).setOnClickListener(v -> finish());
        stateText = findViewById(R.id.evidence_state);
        conclusionText = findViewById(R.id.evidence_conclusion);
        judgmentText = findViewById(R.id.evidence_judgment);
        confidenceRiskText = findViewById(R.id.evidence_confidence_risk);
        refsContainer = findViewById(R.id.evidence_refs);
        rawList = findViewById(R.id.evidence_raw_list);
        rawToggle = findViewById(R.id.evidence_raw_toggle);
        footerText = findViewById(R.id.evidence_footer);
        findViewById(R.id.evidence_raw_header).setOnClickListener(v -> toggleRaw());

        bindStatusBar();
        String eventId = eventIdFromIntent(getIntent());
        if (eventId.isEmpty()) {
            stateText.setText(R.string.evidence_missing_id);
            return;
        }
        if (baseUrl().isEmpty()) {
            stateText.setText(R.string.evidence_need_config);
            return;
        }
        fetch(eventId);
    }

    private String eventIdFromIntent(Intent intent) {
        if (intent == null) return "";
        String value = intent.getStringExtra("event_id");
        if (value == null || value.trim().isEmpty()) value = intent.getStringExtra("event");
        if (value == null) return "";
        value = value.trim();
        return value.startsWith("event:") ? value.substring(6) : value;
    }

    private String baseUrl() {
        return preferences.getString("server", "").trim().replaceAll("/+$", "");
    }

    private void bindStatusBar() {
        TextView clock = findViewById(R.id.status_clock);
        TextView battery = findViewById(R.id.status_battery);
        if (clock != null) {
            clock.setText(new SimpleDateFormat("HH:mm", Locale.getDefault()).format(new Date()));
        }
        if (battery != null) {
            battery.setText(batteryLevel() < 0
                    ? getString(R.string.status_unavailable)
                    : batteryLevel() + "%");
        }
    }

    private int batteryLevel() {
        BatteryManager manager = (BatteryManager) getSystemService(BATTERY_SERVICE);
        if (manager == null) return -1;
        int level = manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        return level < 0 ? -1 : level;
    }

    private void fetch(String eventId) {
        stateText.setText(R.string.evidence_loading);
        String url = baseUrl() + "/api/activities/" + android.net.Uri.encode("event:" + eventId);
        Request.Builder builder = new Request.Builder().url(url).get();
        String token = preferences.getString("token", "").trim();
        if (!token.isEmpty()) builder.header("Authorization", "Bearer " + token);
        http.newCall(builder.build()).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                runOnUiThread(() -> stateText.setText(R.string.evidence_load_failed));
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String body = response.body() == null ? "" : response.body().string();
                response.close();
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> stateText.setText(R.string.evidence_load_failed));
                    return;
                }
                try {
                    final JSONObject item = new JSONObject(body);
                    runOnUiThread(() -> render(item));
                } catch (Exception error) {
                    runOnUiThread(() -> stateText.setText(R.string.evidence_load_failed));
                }
            }
        });
    }

    private void render(JSONObject item) {
        stateText.setText("");
        evidence = item.optJSONObject("evidence");
        if (evidence == null) evidence = new JSONObject();
        JSONObject judgment = evidence.optJSONObject("judgment");
        if (judgment == null) judgment = new JSONObject();

        String conclusion = evidence.optString("conclusion", "");
        if (conclusion.isEmpty()) conclusion = item.optString("title", "");
        conclusionText.setText(conclusion.isEmpty() ? "—" : conclusion);

        String reasoning = judgment.optString("text", "");
        if (reasoning.isEmpty()) {
            JSONObject detail = item.optJSONObject("detail");
            reasoning = detail == null ? "" : detail.optString("reason", "");
        }
        judgmentText.setText(reasoning.isEmpty() ? "—" : reasoning);
        confidenceRiskText.setText(confidenceLine(item, judgment));

        renderRefs(evidence.optJSONObject("refs"));
        rawItems = evidence.optJSONArray("raw");
        if (rawItems == null) rawItems = new JSONArray();
        renderRaw();
        footerText.setText(getString(
                R.string.evidence_footer_format,
                count(evidence.optJSONObject("refs"), "memories"),
                count(evidence.optJSONObject("refs"), "tools")));
    }

    private String confidenceLine(JSONObject item, JSONObject judgment) {
        double confidence = judgment.has("confidence") && !judgment.isNull("confidence")
                ? judgment.optDouble("confidence", Double.NaN)
                : (item.has("confidence") && !item.isNull("confidence")
                        ? item.optDouble("confidence", Double.NaN) : Double.NaN);
        String risk = judgment.optString("risk", item.optString("risk", ""));
        String riskLabel = riskLabel(risk);
        if (Double.isNaN(confidence)) return getString(R.string.evidence_confidence_missing);
        int percent = (int) Math.round(confidence * 100);
        return getString(R.string.evidence_confidence_format, percent, riskLabel);
    }

    private String riskLabel(String risk) {
        if ("high".equals(risk)) return getString(R.string.evidence_risk_high);
        if ("medium".equals(risk)) return getString(R.string.evidence_risk_medium);
        if ("low".equals(risk)) return getString(R.string.evidence_risk_low);
        return getString(R.string.status_unavailable);
    }

    private void renderRefs(JSONObject refs) {
        refsContainer.removeAllViews();
        if (refs == null) {
            addEmptyRefLine();
            return;
        }
        int rendered = 0;
        rendered += addRefGroup(refs.optJSONArray("memories"), R.string.evidence_ref_memory_prefix, "id", "title", "source");
        rendered += addRefGroup(refs.optJSONArray("events"), R.string.evidence_ref_event_prefix, "id", "title", "source");
        rendered += addRefGroup(refs.optJSONArray("tools"), R.string.evidence_ref_tool_prefix, "id", "name", "result");
        if (rendered == 0) addEmptyRefLine();
    }

    private int addRefGroup(JSONArray group, int prefixRes, String idKey, String titleKey, String metaKey) {
        if (group == null) return 0;
        LayoutInflater inflater = LayoutInflater.from(this);
        int added = 0;
        for (int index = 0; index < group.length(); index++) {
            JSONObject row = group.optJSONObject(index);
            if (row == null) continue;
            View view = inflater.inflate(R.layout.item_evidence_ref, refsContainer, false);
            TextView label = view.findViewById(R.id.ref_label);
            TextView title = view.findViewById(R.id.ref_title);
            String id = row.optString(idKey, "—");
            label.setText(getString(prefixRes) + id);
            String titleText = row.optString(titleKey, "");
            String meta = row.optString(metaKey, "");
            StringBuilder line = new StringBuilder();
            if (!titleText.isEmpty()) line.append(titleText);
            if (!meta.isEmpty()) line.append(line.length() > 0 ? " · " : "").append(meta);
            if (!row.optBoolean("available", true)) line.append(getString(R.string.evidence_ref_cleared));
            title.setText(line.length() == 0 ? "—" : line.toString());
            refsContainer.addView(view);
            added++;
        }
        return added;
    }

    private void addEmptyRefLine() {
        TextView empty = new TextView(this);
        empty.setText("—");
        empty.setTextSize(12);
        empty.setTextColor(getColor(R.color.color_text_tertiary));
        refsContainer.addView(empty);
    }

    private void renderRaw() {
        rawList.removeAllViews();
        if (rawItems.length() == 0) {
            TextView empty = new TextView(this);
            empty.setText(R.string.evidence_raw_empty);
            empty.setTextSize(12);
            empty.setTextColor(getColor(R.color.color_text_tertiary));
            rawList.addView(empty);
            return;
        }
        for (int index = 0; index < rawItems.length(); index++) {
            JSONObject row = rawItems.optJSONObject(index);
            if (row == null) continue;
            TextView line = new TextView(this);
            String kind = row.optString("kind", "");
            String id = row.optString("id", "—");
            line.setText(getString(R.string.evidence_raw_item_format,
                    kind.isEmpty() ? id : kind + " " + id));
            line.setTextSize(12);
            line.setTextColor(getColor(R.color.color_text_secondary));
            line.setPadding(0, dp(4), 0, dp(4));
            rawList.addView(line);
        }
    }

    private void toggleRaw() {
        rawExpanded = !rawExpanded;
        rawList.setVisibility(rawExpanded ? View.VISIBLE : View.GONE);
        rawToggle.setText(rawExpanded
                ? R.string.evidence_raw_collapse : R.string.evidence_raw_expand);
    }

    private int count(JSONObject refs, String key) {
        if (refs == null) return 0;
        JSONArray group = refs.optJSONArray(key);
        return group == null ? 0 : group.length();
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density);
    }
}
