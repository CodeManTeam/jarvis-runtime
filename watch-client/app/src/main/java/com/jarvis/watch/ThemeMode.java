package com.jarvis.watch;

import android.content.Context;
import android.content.res.Configuration;

final class ThemeMode {
    static final String KEY = "theme_mode";
    static final String SYSTEM = "system";
    static final String LIGHT = "light";
    static final String DARK = "dark";

    private ThemeMode() {}

    static Context wrap(Context base) {
        String mode = base.getSharedPreferences("jarvis", Context.MODE_PRIVATE)
                .getString(KEY, SYSTEM);
        if (SYSTEM.equals(mode)) return base;
        Configuration config = new Configuration(base.getResources().getConfiguration());
        config.uiMode = (config.uiMode & ~Configuration.UI_MODE_NIGHT_MASK)
                | (DARK.equals(mode) ? Configuration.UI_MODE_NIGHT_YES
                        : Configuration.UI_MODE_NIGHT_NO);
        return base.createConfigurationContext(config);
    }
}
