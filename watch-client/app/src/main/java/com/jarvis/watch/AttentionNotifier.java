package com.jarvis.watch;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/**
 * Two channels for two very different moments.
 *
 * A high-risk permission request wakes the screen and shows the card, because
 * waiting for the wearer to notice a list is how a pending decision gets lost.
 * Everything else stays a quiet notification plus the in-app dot: interrupting
 * for something that can wait teaches the wearer to dismiss without reading.
 */
final class AttentionNotifier {
    static final String CHANNEL_ID = "jarvis_attention";
    private static final int ID_PERMISSION = 4701;
    private static final int ID_NOTICE = 4702;

    private AttentionNotifier() {
    }

    static void ensureChannel(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "Jarvis 需要你", NotificationManager.IMPORTANCE_HIGH);
        channel.enableVibration(true);
        channel.setVibrationPattern(new long[] {0, 220, 120, 220});
        channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        manager.createNotificationChannel(channel);
    }

    /** Wake the screen with the permission card. */
    static void postPermission(Context context, String pendingId, String title) {
        Intent intent = new Intent(context, PermissionActivity.class);
        intent.putExtra("pending_id", pendingId == null ? "" : pendingId);
        post(context, intent, title, "需要你的许可", ID_PERMISSION);
    }

    /** A high-priority notice that is not a permission request. */
    static void postNotice(Context context, String eventId, String title) {
        Intent intent = new Intent(context, MainActivity.class);
        intent.putExtra("event_id", eventId == null ? "" : eventId);
        post(context, intent, title, "Jarvis", ID_NOTICE);
    }

    static void clear(Context context) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager != null) {
            manager.cancel(ID_PERMISSION);
            manager.cancel(ID_NOTICE);
        }
    }

    private static void post(Context context, Intent intent, String title, String body, int id) {
        ensureChannel(context);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pending = PendingIntent.getActivity(
                context, id, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(context, CHANNEL_ID)
                : new Notification.Builder(context);
        builder.setSmallIcon(R.drawable.ic_notification_bell)
                .setContentTitle(title == null || title.trim().isEmpty() ? "Jarvis" : title)
                .setContentText(body)
                .setContentIntent(pending)
                // The full-screen intent is what actually wakes the watch face.
                .setFullScreenIntent(pending, true)
                .setAutoCancel(true)
                .setDefaults(Notification.DEFAULT_ALL)
                .setPriority(Notification.PRIORITY_HIGH);
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager != null) manager.notify(id, builder.build());
    }
}
