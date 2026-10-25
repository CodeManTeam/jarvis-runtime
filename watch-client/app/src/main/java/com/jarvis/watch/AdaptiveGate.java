package com.jarvis.watch;

/**
 * Pure room-noise estimator for voice gating, kept free of Android types so it
 * can be unit tested on the JVM.
 *
 * Model:
 * <ul>
 *   <li>floor = 20th percentile of recent frame energy. Speech occupies far less
 *       than 80% of a window, so it cannot drag the floor up — that is the exact
 *       failure mode of a moving average;</li>
 *   <li>spread = p90 − floor, used to widen thresholds in a room that is not
 *       merely loud but uneven;</li>
 *   <li>asymmetric adaptation: rising is immediate, falling is capped, so the
 *       quiet gap after a shout cannot open the gate on the following noise.</li>
 * </ul>
 */
final class AdaptiveGate {
    private static final int WINDOW_FRAMES = 600;
    private static final int REFRESH_FRAMES = 16;
    private static final double MIN_ONSET_ABOVE_FLOOR = 180.0;
    private static final double MIN_SUSTAIN_ABOVE_FLOOR = 110.0;
    private static final double FALL_CAP = 0.85;

    private final double trim;
    private final double[] window = new double[WINDOW_FRAMES];
    private int count;
    private int cursor;
    private int sinceRefresh;
    private double floor;
    private double spread;

    AdaptiveGate(double trim) {
        this.trim = trim <= 0 ? 1.0 : trim;
    }

    void observe(double amplitude) {
        window[cursor] = amplitude;
        cursor = (cursor + 1) % WINDOW_FRAMES;
        if (count < WINDOW_FRAMES) count++;
        if (++sinceRefresh < REFRESH_FRAMES) return;
        sinceRefresh = 0;
        if (count < REFRESH_FRAMES) return;

        double[] sample = new double[count];
        System.arraycopy(window, 0, sample, 0, count);
        java.util.Arrays.sort(sample);
        double next = percentile(sample, 0.20);
        double high = percentile(sample, 0.90);
        if (floor > 0 && next < floor) {
            next = Math.max(next, floor * FALL_CAP);
        }
        floor = next;
        spread = Math.max(0.0, high - floor);
    }

    double floor() {
        return floor;
    }

    /**
     * The trim scales the whole margin, not just the spread term. Scaling only
     * the spread would make the setting do nothing in a room with steady noise,
     * which is exactly where someone reaches for it.
     */
    double onset() {
        return floor + trim * Math.max(MIN_ONSET_ABOVE_FLOOR, spread * 0.9 + 120.0);
    }

    double sustain() {
        return floor + trim * Math.max(MIN_SUSTAIN_ABOVE_FLOOR, spread * 0.5 + 60.0);
    }

    private static double percentile(double[] sorted, double fraction) {
        if (sorted.length == 0) return 0;
        int index = (int) Math.round((sorted.length - 1) * fraction);
        return sorted[Math.max(0, Math.min(sorted.length - 1, index))];
    }
}
