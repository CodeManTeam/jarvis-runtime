package com.jarvis.watch;

import android.net.Uri;

import org.json.JSONObject;

final class QrConfig {
    static final int CURRENT_VERSION = 1;

    final String serverUrl;
    final String deviceToken;
    final String deviceName;
    final String deviceId;

    private QrConfig(
            String serverUrl, String deviceToken, String deviceName, String deviceId) {
        this.serverUrl = serverUrl;
        this.deviceToken = deviceToken;
        this.deviceName = deviceName;
        this.deviceId = deviceId;
    }

    static QrConfig parse(String raw) {
        if (raw == null || raw.trim().isEmpty()) {
            throw new IllegalArgumentException("二维码内容为空");
        }
        try {
            JSONObject json = new JSONObject(raw.trim());
            if (json.optInt("config_version", -1) != CURRENT_VERSION) {
                throw new IllegalArgumentException("配置版本不支持");
            }
            String serverUrl = json.optString("server_url", "").trim()
                    .replaceAll("/+$", "");
            String deviceToken = json.optString("device_token", "").trim();
            String deviceName = json.optString("device_name", "").trim();
            String deviceId = json.optString("device_id", "").trim();
            Uri uri = Uri.parse(serverUrl);
            if (!(("http".equalsIgnoreCase(uri.getScheme())
                    || "https".equalsIgnoreCase(uri.getScheme()))
                    && uri.getHost() != null)) {
                throw new IllegalArgumentException("服务器地址无效");
            }
            if (deviceToken.isEmpty()) {
                throw new IllegalArgumentException("设备 Token 为空");
            }
            if (deviceName.isEmpty() && deviceId.isEmpty()) {
                throw new IllegalArgumentException("缺少设备名称或设备 ID");
            }
            return new QrConfig(serverUrl, deviceToken, deviceName, deviceId);
        } catch (IllegalArgumentException error) {
            throw error;
        } catch (Exception error) {
            throw new IllegalArgumentException("二维码格式无效");
        }
    }
}
