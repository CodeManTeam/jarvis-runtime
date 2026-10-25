package com.jarvis.watch;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.ToggleButton;

import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;

import org.json.JSONObject;

import java.io.IOException;
import java.util.Collections;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public class SettingsActivity extends Activity {
    private static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");
    private static final int RECORD_PERMISSION = 100;
    private static final int OPTIONAL_PERMISSION = 101;

    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private SharedPreferences preferences;
    private EditText serverInput;
    private EditText tokenInput;
    private TextView deviceIdValue;
    private TextView permissionSummary;
    private ToggleButton sensingToggle;
    private ToggleButton ttsToggle;
    private TextView ttsHint;
    private ToggleButton locationToggle;
    private TextView locationHint;
    private TextView backgroundSensingHint;
    private TextView notificationPermissionHint;
    private TextView themeValue;
    private TextView voiceProfileValue;
    private boolean tokenVisible;
    private String deviceId;

    @Override
    protected void attachBaseContext(Context base) {
        super.attachBaseContext(ThemeMode.wrap(base));
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences("jarvis", MODE_PRIVATE);
        deviceId = preferences.getString("device_id", "");
        setContentView(R.layout.activity_settings);
        bindViews();
    }

    private void bindViews() {
        findViewById(R.id.back_button).setOnClickListener(v -> finish());
        themeValue = findViewById(R.id.theme_value);
        updateThemeValue();
        findViewById(R.id.theme_row).setOnClickListener(v -> showThemeChooser());

        voiceProfileValue = findViewById(R.id.voice_profile_value);
        updateVoiceProfileValue();
        findViewById(R.id.voice_profile_row).setOnClickListener(v -> showVoiceProfileChooser());

        serverInput = findViewById(R.id.server_input);
        serverInput.setText(preferences.getString(
                "server", BuildConfig.DEFAULT_SERVER_URL));

        tokenInput = findViewById(R.id.token_input);
        String savedToken = preferences.getString("token", "");
        tokenInput.setText(savedToken.isEmpty()
                ? BuildConfig.DEFAULT_DEVICE_TOKEN : savedToken);
        applyTokenVisibility();
        findViewById(R.id.token_visibility_toggle).setOnClickListener(v -> {
            tokenVisible = !tokenVisible;
            applyTokenVisibility();
        });

        findViewById(R.id.test_connection_button).setOnClickListener(v -> testConnection(false));
        findViewById(R.id.save_config_button).setOnClickListener(v -> saveManualConfiguration());
        findViewById(R.id.scan_qr_button).setOnClickListener(v -> startQrScanner());

        deviceIdValue = findViewById(R.id.device_id_value);
        deviceIdValue.setText(getString(R.string.settings_device_id_prefix) + deviceId);

        sensingToggle = findViewById(R.id.sensing_toggle);
        backgroundSensingHint = findViewById(R.id.background_sensing_hint);
        boolean sensingEnabled = preferences.getBoolean("sensing_enabled", true);
        sensingToggle.setChecked(sensingEnabled);
        updateSensingHint(sensingEnabled);
        sensingToggle.setOnCheckedChangeListener((button, checked) -> {
            setSensingEnabled(checked);
            updateSensingHint(checked);
        });
        ttsToggle = findViewById(R.id.tts_toggle);
        ttsHint = findViewById(R.id.tts_hint);
        boolean ttsEnabled = preferences.getBoolean("tts_enabled", true);
        ttsToggle.setChecked(ttsEnabled);
        updateTtsHint(ttsEnabled);
        ttsToggle.setOnCheckedChangeListener((button, checked) -> {
            preferences.edit().putBoolean("tts_enabled", checked).apply();
            updateTtsHint(checked);
        });
        locationToggle = findViewById(R.id.location_sensing_toggle);
        locationHint = findViewById(R.id.location_sensing_hint);
        boolean locationEnabled = preferences.getBoolean(
                "location_sensing_enabled", false);
        locationToggle.setChecked(locationEnabled);
        updateLocationHint(locationEnabled);
        locationToggle.setOnCheckedChangeListener((button, checked) -> {
            preferences.edit().putBoolean("location_sensing_enabled", checked).apply();
            updateLocationHint(checked);
            if (preferences.getBoolean("sensing_enabled", true)) {
                stopService(new Intent(this, RuntimeService.class));
                startService(new Intent(this, RuntimeService.class));
            }
        });

        notificationPermissionHint = findViewById(R.id.notification_permission_hint);
        updateNotificationPermissionHint();
        findViewById(R.id.notification_permission_action).setOnClickListener(
                v -> showNotificationPermissionDialog());

        findViewById(R.id.microphone_permission_button).setOnClickListener(
                v -> requestMicrophonePermission());
        findViewById(R.id.optional_permission_button).setOnClickListener(
                v -> requestOptionalPermissions());
        permissionSummary = findViewById(R.id.permission_summary);
        permissionSummary.setText(permissionSummaryText());
    }

    private void updateThemeValue() {
        String mode = preferences.getString(ThemeMode.KEY, ThemeMode.SYSTEM);
        themeValue.setText(ThemeMode.DARK.equals(mode) ? R.string.theme_dark
                : ThemeMode.LIGHT.equals(mode) ? R.string.theme_light : R.string.theme_system);
    }

    private void updateVoiceProfileValue() {
        String profile = preferences.getString("voice_sensitivity", "standard");
        voiceProfileValue.setText(
                "sensitive".equals(profile) || "quiet".equals(profile)
                        ? R.string.settings_voice_profile_sensitive
                        : "conservative".equals(profile) || "noisy".equals(profile)
                        ? R.string.settings_voice_profile_conservative
                        : R.string.settings_voice_profile_standard);
    }

    /**
     * Sensitivity is an explicit choice, not something the app guesses: real
     * scenes differ too much for a learned noise floor to be trustworthy.
     */
    private void showVoiceProfileChooser() {
        String[] values = {"sensitive", "standard", "conservative"};
        String[] labels = {
                getString(R.string.settings_voice_profile_sensitive),
                getString(R.string.settings_voice_profile_standard),
                getString(R.string.settings_voice_profile_conservative),
        };
        String current = preferences.getString("voice_sensitivity", "standard");
        int selected = ("sensitive".equals(current) || "quiet".equals(current)) ? 0
                : ("conservative".equals(current) || "noisy".equals(current)) ? 2 : 1;
        new AlertDialog.Builder(this)
                .setTitle(R.string.settings_voice_profile_title)
                .setSingleChoiceItems(labels, selected, (dialog, which) -> {
                    dialog.dismiss();
                    if (!values[which].equals(current)) {
                        preferences.edit().putString("voice_sensitivity", values[which]).apply();
                        updateVoiceProfileValue();
                        // Restart capture so the new threshold applies now.
                        startService(new Intent(this, RuntimeService.class)
                                .setAction(RuntimeService.ACTION_RELOAD_VOICE));
                    }
                })
                .setNegativeButton(R.string.settings_back, null)
                .show();
    }

    private void showThemeChooser() {
        String[] modes = {ThemeMode.SYSTEM, ThemeMode.LIGHT, ThemeMode.DARK};
        String[] labels = {getString(R.string.theme_system), getString(R.string.theme_light),
                getString(R.string.theme_dark)};
        String current = preferences.getString(ThemeMode.KEY, ThemeMode.SYSTEM);
        int selected = ThemeMode.LIGHT.equals(current) ? 1 : ThemeMode.DARK.equals(current) ? 2 : 0;
        new AlertDialog.Builder(this)
                .setTitle(R.string.theme_title)
                .setSingleChoiceItems(labels, selected, (dialog, which) -> {
                    dialog.dismiss();
                    if (!modes[which].equals(current)) {
                        preferences.edit().putString(ThemeMode.KEY, modes[which]).apply();
                        recreate();
                    }
                })
                .setNegativeButton(R.string.settings_back, null)
                .show();
    }

    private void updateTtsHint(boolean enabled) {
        boolean ready = preferences.getBoolean("tts_ready", false);
        if (!enabled) {
            ttsHint.setText(R.string.settings_tts_off);
        } else if (!ready) {
            ttsHint.setText(R.string.settings_tts_unavailable);
        } else {
            ttsHint.setText(R.string.settings_tts_on);
        }
    }

    private void updateLocationHint(boolean enabled) {
        locationHint.setText(enabled
                ? R.string.settings_location_sensing_on
                : R.string.settings_location_sensing_off);
    }

    private void applyTokenVisibility() {
        int cursor = tokenInput.getSelectionStart();
        tokenInput.setInputType(tokenVisible
                ? InputType.TYPE_CLASS_TEXT
                : InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        if (cursor >= 0 && cursor <= tokenInput.length()) {
            tokenInput.setSelection(cursor);
        }
    }

    private void updateSensingHint(boolean enabled) {
        backgroundSensingHint.setText(enabled
                ? R.string.settings_background_sensing_on
                : R.string.settings_background_sensing_off);
    }

    private void updateNotificationPermissionHint() {
        boolean granted = areNotificationsEnabled();
        notificationPermissionHint.setText(granted
                ? R.string.settings_notification_granted
                : R.string.settings_notification_denied);
    }

    private boolean areNotificationsEnabled() {
        android.app.NotificationManager manager =
                (android.app.NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        return manager != null && manager.areNotificationsEnabled();
    }

    private void showNotificationPermissionDialog() {
        Dialog dialog = new Dialog(this);
        dialog.setContentView(R.layout.dialog_notification_permission);
        if (dialog.getWindow() != null) {
            dialog.getWindow().setBackgroundDrawableResource(android.R.color.transparent);
        }
        dialog.findViewById(R.id.notif_dialog_confirm).setOnClickListener(v -> {
            dialog.dismiss();
            openNotificationSettings();
        });
        dialog.findViewById(R.id.notif_dialog_dismiss).setOnClickListener(
                v -> dialog.dismiss());
        dialog.show();
    }

    private void startQrScanner() {
        try {
            IntentIntegrator scanner = new IntentIntegrator(this);
            scanner.setDesiredBarcodeFormats(Collections.singletonList("QR_CODE"));
            scanner.setPrompt("扫描 Jarvis 配置二维码");
            scanner.setBeepEnabled(false);
            scanner.setOrientationLocked(true);
            scanner.initiateScan();
        } catch (Exception error) {
            Toast.makeText(this, R.string.toast_qr_unavailable, Toast.LENGTH_SHORT).show();
        }
    }

    private void applyQrConfig(String raw) {
        try {
            QrConfig config = QrConfig.parse(raw);
            preferences.edit()
                    .putString("server", config.serverUrl)
                    .putString("token", config.deviceToken)
                    .putString("device_name", config.deviceName.isEmpty()
                            ? "Jarvis Watch" : config.deviceName)
                    .apply();
            if (!config.deviceId.isEmpty()) {
                deviceId = config.deviceId;
                preferences.edit().putString("device_id", deviceId).apply();
            }
            serverInput.setText(config.serverUrl);
            tokenInput.setText(config.deviceToken);
            deviceIdValue.setText(getString(R.string.settings_device_id_prefix) + deviceId);
            Toast.makeText(this, R.string.toast_qr_config_loaded, Toast.LENGTH_SHORT).show();
            testConnection(false);
        } catch (IllegalArgumentException error) {
            Toast.makeText(this, error.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private boolean saveManualConfiguration() {
        String serverValue = serverInput.getText().toString().trim();
        String tokenValue = tokenInput.getText().toString().trim();
        Uri uri = Uri.parse(serverValue.replaceAll("/+$", ""));
        if (!(("http".equalsIgnoreCase(uri.getScheme())
                || "https".equalsIgnoreCase(uri.getScheme()))
                && uri.getHost() != null)) {
            Toast.makeText(this, R.string.toast_server_invalid, Toast.LENGTH_SHORT).show();
            return false;
        }
        if (tokenValue.isEmpty()) {
            Toast.makeText(this, R.string.toast_token_empty, Toast.LENGTH_SHORT).show();
            return false;
        }
        preferences.edit()
                .putString("server", serverValue.replaceAll("/+$", ""))
                .putString("token", tokenValue)
                .apply();
        Toast.makeText(this, R.string.toast_config_saved, Toast.LENGTH_SHORT).show();
        return true;
    }

    private void testConnection(boolean connectAfterSuccess) {
        if (!saveManualConfiguration()) return;
        String url = preferences.getString("server", "").replaceAll("/+$", "") + "/health";
        Request.Builder builder = new Request.Builder().url(url).get();
        String token = preferences.getString("token", "").trim();
        if (!token.isEmpty()) builder.header("Authorization", "Bearer " + token);
        http.newCall(builder.build()).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException error) {
                runOnUiThread(() -> Toast.makeText(SettingsActivity.this,
                        R.string.toast_connect_failed, Toast.LENGTH_SHORT).show());
            }

            @Override
            public void onResponse(Call call, Response response) throws IOException {
                response.close();
                runOnUiThread(() -> Toast.makeText(SettingsActivity.this,
                        R.string.toast_service_available, Toast.LENGTH_SHORT).show());
            }
        });
    }

    private void setSensingEnabled(boolean enabled) {
        preferences.edit().putBoolean("sensing_enabled", enabled).apply();
        if (enabled) {
            startService(new Intent(this, RuntimeService.class));
        } else {
            stopService(new Intent(this, RuntimeService.class));
        }
    }

    private String permissionSummaryText() {
        int granted = 0;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED) granted++;
        if (checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
                == PackageManager.PERMISSION_GRANTED) granted++;
        if (checkSelfPermission(Manifest.permission.READ_CALENDAR)
                == PackageManager.PERMISSION_GRANTED) granted++;
        return granted + "/3 权限";
    }

    private void requestOptionalPermissions() {
        String[] permissions = {
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.READ_CALENDAR
        };
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.READ_CALENDAR)
                == PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, R.string.permission_optional_already_on,
                    Toast.LENGTH_SHORT).show();
            return;
        }
        requestPermissionWithExplanation(
                getString(R.string.permission_dialog_optional_title),
                getString(R.string.permission_dialog_optional_message),
                permissions,
                OPTIONAL_PERMISSION,
                "optional_permission_prompted");
    }

    private void requestMicrophonePermission() {
        requestPermissionWithExplanation(
                getString(R.string.permission_dialog_mic_title),
                getString(R.string.permission_dialog_mic_message),
                new String[]{Manifest.permission.RECORD_AUDIO},
                RECORD_PERMISSION,
                "microphone_permission_prompted");
    }

    private void requestPermissionWithExplanation(
            String title, String messageText, String[] permissions,
            int requestCode, String preferenceKey) {
        boolean permanentlyBlocked = preferences.getBoolean(preferenceKey, false)
                && !shouldShowAnyPermissionRationale(permissions);
        AlertDialog.Builder builder = new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(messageText);
        if (permanentlyBlocked) {
            builder.setPositiveButton(R.string.permission_dialog_open_settings,
                    (dialog, which) -> openAppPermissionSettings());
        } else {
            builder.setNegativeButton(R.string.dialog_cancel, null);
            builder.setPositiveButton(R.string.permission_dialog_continue, (dialog, which) -> {
                preferences.edit().putBoolean(preferenceKey, true).apply();
                requestPermissions(permissions, requestCode);
            });
        }
        builder.show();
    }

    private boolean shouldShowAnyPermissionRationale(String[] permissions) {
        for (String permission : permissions) {
            if (shouldShowRequestPermissionRationale(permission)) return true;
        }
        return false;
    }

    private void openAppPermissionSettings() {
        try {
            Intent intent = new Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName()));
            startActivity(intent);
        } catch (Exception ignored) {
        }
    }

    private void openNotificationSettings() {
        try {
            startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateNotificationPermissionHint();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        IntentResult result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
        if (result != null) {
            if (result.getContents() == null) {
                Toast.makeText(this, R.string.toast_qr_cancelled, Toast.LENGTH_SHORT).show();
            } else {
                applyQrConfig(result.getContents());
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
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
            if (grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED
                    && preferences.getBoolean("sensing_enabled", true)) {
                startService(new Intent(this, RuntimeService.class));
            }
        } else if (requestCode == OPTIONAL_PERMISSION) {
            permissionSummary.setText(permissionSummaryText());
        }
    }
}
