package com.jarvis.watch;

import android.app.Notification;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;

import java.io.IOException;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public class JarvisNotificationListener extends NotificationListenerService {
    private static final MediaType JSON =
            MediaType.parse("application/json; charset=utf-8");
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)
            .writeTimeout(10, TimeUnit.SECONDS)
            .callTimeout(20, TimeUnit.SECONDS)
            .build();

    @Override
    public void onNotificationPosted(StatusBarNotification notification) {
        if (notification.getPackageName().equals(getPackageName())) return;
        try {
            Notification source = notification.getNotification();
            CharSequence title = source.extras.getCharSequence(Notification.EXTRA_TITLE);
            CharSequence text = source.extras.getCharSequence(Notification.EXTRA_TEXT);
            JSONObject content = new JSONObject();
            content.put("title", title == null ? "" : title.toString());
            content.put("text", text == null ? "" : text.toString());
            content.put("package", notification.getPackageName());
            content.put("key", notification.getKey());
            sendEvent(content);
        } catch (Exception ignored) {
        }
    }

    private void sendEvent(JSONObject content) {
        android.content.SharedPreferences prefs =
                getSharedPreferences("jarvis", MODE_PRIVATE);
        String server = prefs.getString("server", "").replaceAll("/+$", "");
        String token = prefs.getString("token", "");
        String deviceId = prefs.getString("device_id", "android-device");
        if (server.isEmpty()) return;
        try {
            JSONObject body = new JSONObject();
            body.put("source", "notification");
            body.put("event_type", "notification");
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
