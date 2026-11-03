package dev.iatest.mobile.hid;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** Explicit shell-only receiver; manifest enforces the signature DUMP permission. */
public final class ControlReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        MainActivity activity=MainActivity.current;
        String stopped=intent.getStringExtra("managed_owner_stopped");
        if(stopped!=null&&stopped.length()<2048) {
            try {
                JSONObject notification=new JSONObject(new String(Base64.decode(stopped,Base64.NO_WRAP),StandardCharsets.UTF_8));
                android.content.SharedPreferences preferences=context.getSharedPreferences("MainActivity",0);
                String generation=notification.getString("owner_epoch");
                if(ManagedBleProtocol.VERSION.equals(notification.getString("protocol"))&&"managed_owner_stopped".equals(notification.getString("op"))
                    &&generation.matches("[a-f0-9]{32}")&&generation.equals(preferences.getString("ble_owner_epoch",""))) {
                    preferences.edit().putBoolean("ble_owner_stopped",true).apply();
                    if(activity!=null)activity.managedOwnerStopped(generation);
                }
            }catch(Exception ignored){}
            return;
        }
        String encoded=intent.getStringExtra("payload");
        if(activity==null || encoded==null || encoded.length()>65536) return;
        final PendingResult pending=goAsync();
        activity.commands.execute(()->{
            try {
                byte[] data=Base64.decode(encoded,Base64.NO_WRAP);
                if(data.length>49152) return;
                JSONObject request=new JSONObject(new String(data,StandardCharsets.UTF_8));
                activity.writeReply(activity.command(request));
            } catch(Exception failure) {android.util.Log.e("IATestHID","Command rejected",failure);}
            finally {pending.finish();}
        });
    }
}
