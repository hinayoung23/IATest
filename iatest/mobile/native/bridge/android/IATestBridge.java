package dev.iatest.bridge;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.util.AtomicFile;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/** Include only in src/debug. Publishes hints; exposes no business action API. */
public final class IATestBridge {
    private static final AtomicLong sequence=new AtomicLong();
    private IATestBridge() {}
    public static void publish(Context context,String versionCode,Map<String,Boolean> ready,Map<String,String> identifiers) throws Exception {
        if((context.getApplicationInfo().flags&ApplicationInfo.FLAG_DEBUGGABLE)==0)throw new IllegalStateException("Bridge disabled for non-debuggable App");
        if(ready.size()>20||identifiers.size()>20)throw new IllegalArgumentException("Bridge key quota");
        for(String key:ready.keySet())check(key);
        for(Map.Entry<String,String> item:identifiers.entrySet()){check(item.getKey());if(item.getValue()==null||item.getValue().isEmpty()||item.getValue().length()>500)throw new IllegalArgumentException("Identifier quota");}
        JSONObject value=new JSONObject().put("contract_version",1).put("test_build",true).put("app_id",context.getPackageName()).put("build",versionCode)
            .put("process_id",android.os.Process.myPid()).put("sequence",sequence.incrementAndGet()).put("timestamp_ms",System.currentTimeMillis())
            .put("ready",new JSONObject(ready)).put("identifiers",new JSONObject(identifiers));
        JSONObject performance=new JSONObject();
        android.content.Intent battery=context.registerReceiver(null,new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED));
        if(battery!=null){
            int level=battery.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL,-1),scale=battery.getIntExtra(android.os.BatteryManager.EXTRA_SCALE,-1);
            if(level>=0&&scale>0)performance.put("device_battery_level_percent",100.0*level/scale);
            if(battery.hasExtra(android.os.BatteryManager.EXTRA_TEMPERATURE))performance.put("device_battery_temperature_celsius",battery.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE,0)/10.0);
            android.os.BatteryManager manager=(android.os.BatteryManager)context.getSystemService(Context.BATTERY_SERVICE);
            int voltage=battery.getIntExtra(android.os.BatteryManager.EXTRA_VOLTAGE,-1);
            if(manager!=null&&voltage>0){int current=manager.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CURRENT_NOW);if(current!=Integer.MIN_VALUE)performance.put("device_battery_power_watts",Math.abs((double)current)*voltage/1.0e9);}
        }
        if(android.os.Build.VERSION.SDK_INT>=29){android.os.PowerManager power=(android.os.PowerManager)context.getSystemService(Context.POWER_SERVICE);if(power!=null)performance.put("device_thermal_status",power.getCurrentThermalStatus());}
        int uid=android.os.Process.myUid();long rx=android.net.TrafficStats.getUidRxBytes(uid),tx=android.net.TrafficStats.getUidTxBytes(uid);
        if(rx>=0)performance.put("app_network_rx_bytes",rx);if(tx>=0)performance.put("app_network_tx_bytes",tx);
        value.put("performance",performance);
        byte[] data=value.toString().getBytes(StandardCharsets.UTF_8);if(data.length>16384)throw new IllegalArgumentException("Bridge byte quota");
        AtomicFile file=new AtomicFile(new File(context.getFilesDir(),"iatest-bridge.json"));FileOutputStream output=file.startWrite();
        try{output.write(data);file.finishWrite(output);}catch(Exception error){file.failWrite(output);throw error;}
    }
    private static void check(String key){
        if(key==null||!key.matches("[A-Za-z][A-Za-z0-9_]{0,79}")||key.toLowerCase(java.util.Locale.ROOT).matches(".*(token|password|secret|cookie|authorization|apikey|api_key).*"))throw new IllegalArgumentException("Only non-secret observation keys are allowed");
    }
}
