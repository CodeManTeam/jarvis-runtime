package com.jarvis.watch;

import org.junit.Test;

import static org.junit.Assert.assertTrue;

public class AdaptiveGateTest {

    private static void feed(AdaptiveGate gate, double amplitude, int frames) {
        for (int i = 0; i < frames; i++) {
            gate.observe(amplitude);
        }
    }

    @Test
    public void quietRoomKeepsAFloorNearTheAmbientLevel() {
        AdaptiveGate gate = new AdaptiveGate(1.0);
        feed(gate, 40, 400);
        assertTrue("floor should follow a quiet room", gate.floor() < 70);
        assertTrue("speech must clear the onset threshold", gate.onset() > 180);
    }

    @Test
    public void speechDoesNotRaiseTheFloor() {
        AdaptiveGate gate = new AdaptiveGate(1.0);
        // A 40s window where 12% of frames are loud speech.
        for (int i = 0; i < 640; i++) {
            gate.observe(i % 8 == 0 ? 1500 : 60);
        }
        // A moving average would sit far higher; a low percentile ignores speech.
        assertTrue("floor must stay near ambient, was " + gate.floor(), gate.floor() < 120);
    }

    @Test
    public void aLoudRoomRaisesTheFloorImmediately() {
        AdaptiveGate gate = new AdaptiveGate(1.0);
        feed(gate, 50, 320);
        double quietFloor = gate.floor();
        feed(gate, 1200, 640);
        assertTrue("floor must rise in a loud room", gate.floor() > quietFloor * 4);
    }

    @Test
    public void aFallingFloorIsCappedSoAGapDoesNotOpenTheGate() {
        AdaptiveGate gate = new AdaptiveGate(1.0);
        feed(gate, 1200, 640);
        double loudFloor = gate.floor();
        // Only one refresh worth of quiet frames: it must not collapse to ~50.
        feed(gate, 50, 16);
        assertTrue(
                "floor fell too fast: " + gate.floor() + " from " + loudFloor,
                gate.floor() >= loudFloor * 0.8);
    }

    @Test
    public void sustainThresholdIsAlwaysBelowOnset() {
        AdaptiveGate gate = new AdaptiveGate(1.0);
        feed(gate, 300, 200);
        assertTrue(gate.sustain() < gate.onset());
    }

    @Test
    public void conservativeTrimRaisesThresholdsAndSensitiveLowersThem() {
        AdaptiveGate sensitive = new AdaptiveGate(0.72);
        AdaptiveGate standard = new AdaptiveGate(1.0);
        AdaptiveGate conservative = new AdaptiveGate(1.45);
        feed(sensitive, 200, 320);
        feed(standard, 200, 320);
        feed(conservative, 200, 320);
        assertTrue(sensitive.onset() < standard.onset());
        assertTrue(standard.onset() < conservative.onset());
    }

    @Test
    public void onsetNeverSitsBelowTheFloor() {
        AdaptiveGate gate = new AdaptiveGate(1.0);
        feed(gate, 900, 400);
        assertTrue(gate.onset() > gate.floor());
        assertTrue(gate.sustain() > gate.floor());
    }
}
