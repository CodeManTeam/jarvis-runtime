package com.jarvis.watch;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.util.ArrayList;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

public class LogActivity extends Activity {
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private final ArrayList<JSONObject> activities = new ArrayList<>();
    private SharedPreferences preferences;
    private LinearLayout logList;
    private TextView logState;
    private TextView loadMoreButton;
    private String cursor;
    private boolean loading;

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(ThemeMode.wrap(base));
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences("jarvis", MODE_PRIVATE);
        setContentView(R.layout.activity_log);
        findViewById(R.id.back_button).setOnClickListener(v -> finish());
        findViewById(R.id.refresh_button).setOnClickListener(v -> fetchPage(null, true));
        logList = findViewById(R.id.log_list);
        logState = findViewById(R.id.log_state);
        loadMoreButton = findViewById(R.id.load_more_button);
        loadMoreButton.setOnClickListener(v -> fetchPage(cursor, false));
        fetchPage(null, true);
    }

    private String baseUrl() {
        return preferences.getString("server", "").trim().replaceAll("/+$", "");
    }

    private Request.Builder authorized(Request.Builder builder) {
        String token = preferences.getString("token", "").trim();
        if (!token.isEmpty()) builder.header("Authorization", "Bearer " + token);
        return builder;
    }

    private void fetchPage(String nextCursor, boolean refresh) {
        if (loading || baseUrl().isEmpty()) {
            logState.setText(R.string.log_need_config);
            return;
        }
        loading = true;
        logState.setText(R.string.log_loading);
        String url = baseUrl() + "/api/activities?limit=20";
        if (nextCursor != null && !nextCursor.isEmpty()) {
            url += "&cursor=" + Uri.encode(nextCursor);
        }
        Request request = authorized(new Request.Builder().url(url).get()).build();
        http.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                loading = false;
                runOnUiThread(() -> logState.setText(R.string.log_load_failed));
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                String body = response.body() == null ? "" : response.body().string();
                response.close();
                loading = false;
                if (!response.isSuccessful()) {
                    runOnUiThread(() -> logState.setText(R.string.log_load_failed));
                    return;
                }
                try {
                    JSONObject result = new JSONObject(body);
                    JSONArray items = result.optJSONArray("items");
                    String next = result.optString("next_cursor", "");
                    if (items == null) items = new JSONArray();
                    JSONArray pageItems = items;
                    runOnUiThread(() -> {
                        if (refresh) activities.clear();
                        for (int index = 0; index < pageItems.length(); index++) {
                            JSONObject item = pageItems.optJSONObject(index);
                            if (item != null) activities.add(item);
                        }
                        cursor = next.isEmpty() ? null : next;
                        render();
                    });
                } catch (Exception error) {
                    runOnUiThread(() -> logState.setText(R.string.log_parse_failed));
                }
            }
        });
    }

    private void render() {
        logList.removeAllViews();
        LayoutInflater inflater = LayoutInflater.from(this);
        if (activities.isEmpty()) {
            TextView empty = new TextView(this);
            empty.setText(R.string.activity_empty);
            logList.addView(empty);
        } else {
            for (JSONObject item : activities) {
                android.view.View row = inflater.inflate(
                        R.layout.item_activity_log, logList, false);
                TextView meta = row.findViewById(R.id.item_meta);
                TextView title = row.findViewById(R.id.item_title);
                meta.setText(activityTime(item) + "  ·  " + statusLabel(item));
                title.setText(item.optString("title", "收到一条活动"));
                row.setOnClickListener(v -> showActivityDetails(item));
                logList.addView(row);

                android.view.View divider = new android.view.View(this);
                divider.setLayoutParams(new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT, (int) (1 * getResources()
                        .getDisplayMetrics().density)));
                divider.setBackgroundResource(R.drawable.divider_line);
                logList.addView(divider);
            }
        }
        logState.setText(activities.size() + getString(R.string.log_count_suffix));
        loadMoreButton.setVisibility(cursor == null
                ? android.view.View.GONE : android.view.View.VISIBLE);
        loadMoreButton.setEnabled(!loading);
    }

    private String activityTime(JSONObject activity) {
        String value = activity.optString("created_at", "");
        if (value.length() >= 16) return value.substring(5, 16).replace("T", " ");
        return value.isEmpty() ? getString(R.string.activity_now) : value;
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

    private String actionLabel(String action) {
        if ("accept".equals(action)) return getString(R.string.action_accept);
        if ("reject".equals(action)) return getString(R.string.action_reject);
        if ("modify".equals(action)) return getString(R.string.action_modify);
        if ("delete".equals(action)) return getString(R.string.action_delete);
        if ("undo".equals(action)) return getString(R.string.action_undo);
        return action;
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
}
