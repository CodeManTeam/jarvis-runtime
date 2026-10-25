package com.jarvis.watch;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.util.AttributeSet;
import android.view.View;

/** Draws a five-bar sound wave whose height follows microphone amplitude. */
final class SoundWaveView extends View {
    private static final float[] BAR_GAIN = {0.72f, 0.92f, 1.12f, 0.92f, 0.72f};
    private static final float IDLE_LEVEL = 0.12f;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final float[] currentLevels = {
            IDLE_LEVEL, IDLE_LEVEL, IDLE_LEVEL, IDLE_LEVEL, IDLE_LEVEL
    };
    private boolean active;

    public SoundWaveView(Context context) {
        this(context, null);
    }

    public SoundWaveView(Context context, AttributeSet attrs) {
        super(context, attrs);
        paint.setColor(getResources().getColor(R.color.color_accent));
        paint.setStyle(Paint.Style.FILL);
    }

    void setActive(boolean value) {
        active = value;
        if (!active) setLevel(0f);
    }

    void setLevel(float level) {
        float normalized = Math.max(0f, Math.min(1f, level));
        for (int i = 0; i < currentLevels.length; i++) {
            float target = normalized * BAR_GAIN[i];
            currentLevels[i] = Math.max(IDLE_LEVEL, Math.min(1f, target));
        }
        invalidate();
    }

    @Override
    protected void onDetachedFromWindow() {
        super.onDetachedFromWindow();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        int width = getWidth();
        int height = getHeight();
        if (width <= 0 || height <= 0) return;
        int barCount = currentLevels.length;
        float gap = width / (float) (barCount * 2 - 1);
        float barWidth = gap;
        float radius = barWidth / 2f;
        for (int i = 0; i < barCount; i++) {
            float barHeight = height * currentLevels[i];
            float left = i * gap * 2;
            float top = (height - barHeight) / 2f;
            canvas.drawRoundRect(
                    left, top, left + barWidth, top + barHeight, radius, radius, paint);
        }
    }
}
