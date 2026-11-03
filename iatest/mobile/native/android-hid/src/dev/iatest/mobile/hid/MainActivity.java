package dev.iatest.mobile.hid;

import android.Manifest;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothHidDevice;
import android.bluetooth.BluetoothHidDeviceAppSdpSettings;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.content.Intent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import android.os.SystemClock;
import android.util.Base64;
import org.json.JSONArray;

/** Foreground-only Bluetooth HID sender. User pairing stays in system UI. */
public final class MainActivity extends Activity implements BleOwner {
    private BluetoothAdapter adapter;
    private volatile BluetoothHidDevice hid;
    private volatile BluetoothDevice peer;
    private TextView view;
    private volatile boolean foreground, registered, requested, proxyRequested;
    private String error = "";
    private final String epoch = UUID.randomUUID().toString().replace("-", "");
    static volatile MainActivity current;
    final ExecutorService commands = Executors.newSingleThreadExecutor();
    private String session = "";
    private long sequence;
    private boolean uncertain;
    private volatile byte[] lastAbsolute=new byte[5];
    private volatile byte[] lastDigitizer=new byte[HidDigitizer.LENGTH];
    private boolean digitizerEnabled;
    private byte protocolMode=BluetoothHidDevice.PROTOCOL_REPORT_MODE;
    private BleTransport ble;
    private String bleLifecycle="app",ownerSocket="",ownerEpoch="";
    private boolean configurationValid=true;
    private boolean useBle;
    private volatile boolean bleDiagnostics;
    private int transportGeneration;
    private boolean radioReceiverRegistered;
    private final BroadcastReceiver radioReceiver=new BroadcastReceiver() {
        @Override public void onReceive(Context context,Intent intent) {
            if(!BluetoothAdapter.ACTION_STATE_CHANGED.equals(intent.getAction()))return;
            int state=intent.getIntExtra(BluetoothAdapter.EXTRA_STATE,BluetoothAdapter.ERROR);
            if(state==BluetoothAdapter.STATE_TURNING_OFF||state==BluetoothAdapter.STATE_OFF)resetRadio();
            else if(state==BluetoothAdapter.STATE_ON&&foreground)startHid();
        }
    };
    private final Map<String,JSONObject> ledger = new LinkedHashMap<>();

    private static byte[] hex(String value) {
        byte[] out = new byte[value.length() / 2];
        for (int i = 0; i < out.length; i++) out[i] = (byte)Integer.parseInt(value.substring(i*2,i*2+2),16);
        return out;
    }

    private boolean currentClassic(BluetoothHidDevice source,int generation) {
        return current==this&&!useBle&&hid==source&&transportGeneration==generation;
    }
    private BluetoothHidDevice.Callback classicCallback(BluetoothHidDevice source,int generation) {return new BluetoothHidDevice.Callback() {
        @Override public void onAppStatusChanged(BluetoothDevice device, boolean value) {
            if(!currentClassic(source,generation))return;
            registered = value;
            if (!value) {peer = null; requested=false;}
            status();
        }
        @Override public void onConnectionStateChanged(BluetoothDevice device, int state) {
            if(!currentClassic(source,generation))return;
            if (state == BluetoothProfile.STATE_CONNECTED) peer = device;
            else if (state == BluetoothProfile.STATE_DISCONNECTED && device.equals(peer)) peer = null;
            status();
        }
        @Override public void onGetReport(BluetoothDevice device, byte type, byte id, int size) {
            if (!currentClassic(source,generation)) return;
            if (type == BluetoothHidDevice.REPORT_TYPE_INPUT && (id == 1 || id == 2 || id == 3))
                hid.replyReport(device, type, id, id==2?lastAbsolute.clone():new byte[id == 1 ? 8 : 4]);
            else hid.reportError(device, BluetoothHidDevice.ERROR_RSP_UNSUPPORTED_REQ);
        }
        @Override public void onSetReport(BluetoothDevice device, byte type, byte id, byte[] data) {
            if(!currentClassic(source,generation))return;
            if (hid != null) hid.reportError(device, type == BluetoothHidDevice.REPORT_TYPE_OUTPUT && id == 1
                ? BluetoothHidDevice.ERROR_RSP_SUCCESS : BluetoothHidDevice.ERROR_RSP_UNSUPPORTED_REQ);
        }
        @Override public void onSetProtocol(BluetoothDevice device,byte mode) {if(currentClassic(source,generation)){protocolMode=mode;status();}}
    };}

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        current = this;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        adapter = getSystemService(BluetoothManager.class).getAdapter();
        bleDiagnostics=getIntent().getBooleanExtra("ble_diagnostics",false);
        digitizerEnabled=getIntent().getBooleanExtra("experimental_digitizer",getPreferences(0).getBoolean("experimental_digitizer",false));
        getPreferences(0).edit().putBoolean("experimental_digitizer",digitizerEnabled).apply();
        String transport=getIntent().getStringExtra("transport");
        useBle="ble".equals(transport==null?getPreferences(0).getString("transport","classic"):transport);
        getPreferences(0).edit().putString("transport",useBle?"ble":"classic").apply();
        try {selectOwner(getIntent());}
        catch(IllegalArgumentException failure) {configurationValid=false;error=failure.getMessage();}
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(28,40,28,28);
        view = new TextView(this);
        view.setTextSize(18);
        layout.addView(view);
        Button register = new Button(this);
        register.setText("注册蓝牙键盘与鼠标");
        register.setOnClickListener(v -> startHid());
        layout.addView(register);
        Button discover = new Button(this);
        discover.setText("允许 iPhone 发现（120 秒）");
        discover.setOnClickListener(v -> {
            if (!registered) { error = "Register HID first"; status(); return; }
            if(useBle) {error="BLE 广播已开启。请在 iPhone「辅助触控 → 设备 → 蓝牙设备」配对；无需开启安卓经典蓝牙发现。";status();return;}
            Intent intent = new Intent(BluetoothAdapter.ACTION_REQUEST_DISCOVERABLE);
            intent.putExtra(BluetoothAdapter.EXTRA_DISCOVERABLE_DURATION,120);
            startActivity(intent);
        });
        layout.addView(discover);
        Button stop = new Button(this);
        stop.setText("释放按键并结束控制会话");
        stop.setOnClickListener(v -> commands.execute(()->{
            try {release(); session=""; uncertain=false;}
            catch(Exception failure) {error="Release unconfirmed: "+failure.getClass().getSimpleName();}
            runOnUiThread(()->status());
        }));
        layout.addView(stop);
        setContentView(layout);
        IntentFilter radioChanges=new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED);
        if(Build.VERSION.SDK_INT>=33)registerReceiver(radioReceiver,radioChanges,Context.RECEIVER_EXPORTED);
        else registerReceiver(radioReceiver,radioChanges);
        radioReceiverRegistered=true;
        status();
    }

    private void resetRadio() {
        // The Bluetooth service discards its registrations on radio shutdown.
        // Fence callbacks and never reuse those GATT/profile proxy objects.
        transportGeneration++;
        if(!session.isEmpty())uncertain=true;
        BleTransport previous=ble;ble=null;
        BluetoothHidDevice classic=hid;hid=null;
        registered=false;requested=false;proxyRequested=false;peer=null;
        if(previous!=null)try{previous.close();}catch(Exception ignored){}
        if(classic!=null)try{adapter.closeProfileProxy(BluetoothProfile.HID_DEVICE,classic);}catch(Exception ignored){}
        error="Bluetooth restarted; registrations cleared";status();
    }

    @Override public void onResume() {
        super.onResume(); foreground = true; startHid();if(ble!=null)ble.setForeground(true);
    }
    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent); setIntent(intent);
        if(intent.hasExtra("ble_diagnostics")) {bleDiagnostics=intent.getBooleanExtra("ble_diagnostics",false);status();}
        String transport=intent.getStringExtra("transport");
        boolean selected=transport==null?useBle:"ble".equals(transport);
        boolean selectedDigitizer=intent.getBooleanExtra("experimental_digitizer",digitizerEnabled);
        String lifecycle=intent.getStringExtra("ble_lifecycle");
        String socket=intent.getStringExtra("ble_owner_socket"),generation=intent.getStringExtra("ble_owner_epoch");
        boolean ownerChanged=(lifecycle!=null&&!lifecycle.equals(bleLifecycle))
            ||(socket!=null&&!socket.equals(ownerSocket))||(generation!=null&&!generation.equals(ownerEpoch));
        if(selected!=useBle||ownerChanged||selectedDigitizer!=digitizerEnabled||!configurationValid) {
            if(!session.isEmpty()) {error="End the active HID session before switching transports";status();return;}
            try {validateOwner(intent);}
            catch(IllegalArgumentException failure) {error=failure.getMessage();status();return;}
            if(ownerChanged&&"app".equals(lifecycle)&&ble instanceof ManagedBleClient) {
                try {((ManagedBleClient)ble).requireStopped();}
                catch(IllegalStateException failure){error=failure.getMessage();status();return;}
            }
            transportGeneration++;
            if((ownerChanged||selectedDigitizer!=digitizerEnabled)&&ble!=null) {ble.close();ble=null;}
            if(hid!=null) {hid.unregisterApp();adapter.closeProfileProxy(BluetoothProfile.HID_DEVICE,hid);hid=null;}
            selectOwner(intent);configurationValid=true;
            digitizerEnabled=selectedDigitizer;getPreferences(0).edit().putBoolean("experimental_digitizer",digitizerEnabled).apply();
            useBle=selected;getPreferences(0).edit().putString("transport",useBle?"ble":"classic").apply();
            registered=false;requested=false;proxyRequested=false;peer=null;startHid();
        }
    }
    private void validateOwner(Intent intent) {
        String mode=intent.hasExtra("ble_lifecycle")?intent.getStringExtra("ble_lifecycle"):getPreferences(0).getString("ble_lifecycle","app");
        if(!"app".equals(mode)&&!"adb_managed".equals(mode))throw new IllegalArgumentException("Unknown BLE lifecycle");
        if("app".equals(mode)&&"adb_managed".equals(getPreferences(0).getString("ble_lifecycle","app"))
            &&!getPreferences(0).getBoolean("ble_owner_stopped",false))
            throw new IllegalArgumentException("Explicitly stop the ADB BLE owner before selecting app lifecycle");
        if("adb_managed".equals(mode)) {
            String socket=intent.hasExtra("ble_owner_socket")?intent.getStringExtra("ble_owner_socket"):getPreferences(0).getString("ble_owner_socket","");
            String generation=intent.hasExtra("ble_owner_epoch")?intent.getStringExtra("ble_owner_epoch"):getPreferences(0).getString("ble_owner_epoch","");
            if(socket==null||!socket.matches("iatest_hid_owner_[a-f0-9]{32}")||generation==null||!generation.matches("[a-f0-9]{32}"))
                throw new IllegalArgumentException("Explicit managed BLE owner binding required");
            if(Build.VERSION.SDK_INT<31)throw new IllegalArgumentException("Managed BLE requires Android 12 or newer shell APIs");
        }
    }
    private void selectOwner(Intent intent) {
        validateOwner(intent);
        bleLifecycle=intent.hasExtra("ble_lifecycle")?intent.getStringExtra("ble_lifecycle"):getPreferences(0).getString("ble_lifecycle","app");
        ownerSocket="adb_managed".equals(bleLifecycle)?(intent.hasExtra("ble_owner_socket")?intent.getStringExtra("ble_owner_socket"):getPreferences(0).getString("ble_owner_socket","")):"";
        ownerEpoch="adb_managed".equals(bleLifecycle)?(intent.hasExtra("ble_owner_epoch")?intent.getStringExtra("ble_owner_epoch"):getPreferences(0).getString("ble_owner_epoch","")):"";
        android.content.SharedPreferences.Editor update=getPreferences(0).edit().putString("ble_lifecycle",bleLifecycle).putString("ble_owner_socket",ownerSocket).putString("ble_owner_epoch",ownerEpoch);
        if(intent.hasExtra("ble_lifecycle"))update.putBoolean("ble_owner_stopped",false);
        update.apply();
    }
    @Override public void onPause() {
        foreground = false;if(ble!=null)ble.setForeground(false);status(); super.onPause();
    }
    @Override public void onDestroy() {
        current = null;
        if(radioReceiverRegistered){unregisterReceiver(radioReceiver);radioReceiverRegistered=false;}
        commands.shutdownNow();
        if (hid != null) {
            if (peer != null) {
                try {release();} catch(Exception failure) {android.util.Log.w("IATestHID","Key release unconfirmed on exit");}
                hid.disconnect(peer);
            }
            hid.unregisterApp(); adapter.closeProfileProxy(BluetoothProfile.HID_DEVICE,hid);
        }
        if(ble!=null) {try {release();}catch(Exception ignored) {} ble.close();}
        super.onDestroy();
    }
    @Override public void onRequestPermissionsResult(int code, String[] names, int[] results) {
        super.onRequestPermissionsResult(code,names,results); startHid();
    }

    private void startHid() {
        try {
            if(!configurationValid) {status();return;}
            if("adb_managed".equals(bleLifecycle)&&getPreferences(0).getBoolean("ble_owner_stopped",false)) {
                registered=false;peer=null;error="Explicit ADB BLE owner stopped; start ownership or select app lifecycle";status();return;
            }
            if (Build.VERSION.SDK_INT >= 31 && (checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.BLUETOOTH_ADVERTISE) != PackageManager.PERMISSION_GRANTED)) {
                requestPermissions(new String[]{Manifest.permission.BLUETOOTH_CONNECT,Manifest.permission.BLUETOOTH_ADVERTISE},1);
                return;
            }
            if (adapter == null || !adapter.isEnabled()) { error="Bluetooth is disabled"; status(); return; }
            // Once BLE is used, retain its GATT database for this process even
            // while classic input is selected. Removing it invalidates the
            // bonded host's HID cache and auto-connect registration.
            if(ble==null&&(useBle||getPreferences(0).getBoolean("ble_used",false))) {
                ble="adb_managed".equals(bleLifecycle)?new ManagedBleClient(this,adapter,ownerSocket,ownerEpoch,epoch,useBle,foreground):new BleSender(this,adapter,useBle);
                getPreferences(0).edit().putBoolean("ble_used",true).apply();
            }
            if(useBle) {
                requested=true;ble.setActive(true);
                status();return;
            }
            if(ble!=null)ble.setActive(false);
            if (hid == null && !proxyRequested) {
                final int generation=transportGeneration;
                proxyRequested = adapter.getProfileProxy(this,new BluetoothProfile.ServiceListener() {
                    @Override public void onServiceConnected(int profile,BluetoothProfile proxy) {
                        if(useBle||current!=MainActivity.this||generation!=transportGeneration) {adapter.closeProfileProxy(profile,proxy);return;}
                        hid=(BluetoothHidDevice)proxy; startHid();
                    }
                    @Override public void onServiceDisconnected(int profile) {
                        if(useBle||current!=MainActivity.this||generation!=transportGeneration)return;
                        hid=null; registered=false; requested=false; proxyRequested=false; peer=null; status();
                    }
                },BluetoothProfile.HID_DEVICE);
                if (!proxyRequested) error="HID_DEVICE profile proxy unavailable";
            }
            if (hid != null && !registered && !requested) {
                // Separate keyboard, absolute position and relative mouse reports.
                byte[] descriptor=reportDescriptor();
                requested=hid.registerApp(new BluetoothHidDeviceAppSdpSettings("IATest keyboard and mouse","Owned IATest Bluetooth input","IATest",BluetoothHidDevice.SUBCLASS1_COMBO,descriptor),null,null,getMainExecutor(),classicCallback(hid,transportGeneration));
                if (!requested) error="HID registration request rejected";
            }
            status();
        } catch (Exception failure) { error=failure.getClass().getSimpleName()+": "+failure.getMessage(); status(); }
    }

    static byte[] reportDescriptor() {
        // Keyboard, absolute position, then relative mouse buttons/wheel.
        return hex("05010906a1018501050719e029e71500250175019508810295017508810395057501050819012905910295017503910395067508150026a400050719002aa4008100c0"
            +"05010902a10185020901a1000509190129031500250175019503810275059501810105010930093116000026ff7f751095028102c0c0"
            +"05010902a10185030901a1000509190129031500250195037501810275059501810305010930093109381581257f750895038106c0c0");
    }
    public android.content.Context bleContext() {return this;}
    public android.content.SharedPreferences blePreferences() {return getPreferences(0);}
    public boolean acceptBlePeer(BluetoothDevice device) {return true;}
    public void bleState(BleTransport source,boolean enabled,BluetoothDevice device,String message) {
        runOnUiThread(()->{if(!useBle||ble!=source)return;registered=enabled;peer=device;error=message;status();});
    }
    public boolean bleDiagnosticsEnabled() {return bleDiagnostics;}
    public boolean bleDigitizerEnabled(){return digitizerEnabled;}
    void managedOwnerStopped(String generation) {
        if(!generation.equals(ownerEpoch)||!"adb_managed".equals(bleLifecycle))return;
        runOnUiThread(()->{
            if(ble!=null){ble.close();ble=null;}
            registered=false;peer=null;session="";uncertain=false;
            error="Explicit ADB BLE owner stopped";status();
        });
    }
    private boolean send(int id,byte[] data) {return send(id,data,1500);}
    private boolean send(int id,byte[] data,long budgetMs) {return useBle?ble!=null&&ble.send(id,data,budgetMs):hid!=null&&peer!=null&&hid.sendReport(peer,id,data);}

    private void status() {
        if(current!=this)return;
        try {
            JSONObject value=new JSONObject();
            value.put("epoch",epoch); value.put("sdk",Build.VERSION.SDK_INT);
            value.put("foreground",foreground); value.put("proxy_available",useBle?ble!=null:hid!=null);
            value.put("transport",useBle?"ble":"classic");
            value.put("experimental_digitizer",digitizerEnabled).put("digitizer_ready",useBle&&ble!=null&&ble.digitizerReady());
            value.put("ble_lifecycle",bleLifecycle).put("managed_ble_protocol","iatest-managed-ble/1");
            value.put("session_active",!session.isEmpty()).put("delivery_uncertain",uncertain);
            value.put("pid",android.os.Process.myPid());
            value.put("registration_requested",requested); value.put("registered",registered);
            value.put("connected",peer!=null); value.put("error",error);
            value.put("input_ready",foreground&&registered&&peer!=null&&(!useBle||ble!=null&&ble.ready(peer)));
            if(useBle&&ble!=null)value.put("ble_recovery",ble.recoveryStatus());
            if("adb_managed".equals(bleLifecycle)&&ble!=null)value.put("ble_owner",ble.ownerStatus());
            if("adb_managed".equals(bleLifecycle))value.put("ble_owner_epoch",ownerEpoch);
            value.put("protocol_mode",protocolMode);
            if(bleDiagnostics&&ble!=null)value.put("ble_diagnostics",ble.diagnostics());
            if (peer != null) value.put("host_address",peer.getAddress());
            JSONArray hosts=new JSONArray();
            if (adapter!=null && (Build.VERSION.SDK_INT<31 || checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT)==PackageManager.PERMISSION_GRANTED))
                for(BluetoothDevice host:adapter.getBondedDevices()) {
                    JSONObject item=new JSONObject(); item.put("address",host.getAddress()); item.put("name",host.getName()); hosts.put(item);
                }
            value.put("paired_hosts",hosts);
            byte[] data=value.toString().getBytes(StandardCharsets.UTF_8);
            File temporary=new File(getFilesDir(),"status.tmp");
            try(FileOutputStream stream=new FileOutputStream(temporary)) {stream.write(data);}
            if (!temporary.renameTo(new File(getFilesDir(),"status.json"))) throw new IllegalStateException("Status replace failed");
            String ownership="ADB 托管（界面退出后保留服务）";
            if("adb_managed".equals(bleLifecycle)&&ble!=null) {
                JSONObject owner=ble.ownerStatus();
                ownership+="\n托管剩余："+owner.optLong("remaining_seconds",0)+" 秒；请从 Mac 显式停止托管。";
                if("waiting".equals(owner.optString("pairing_state")))
                    ownership+="\n配对窗口："+owner.optLong("pairing_remaining_seconds",0)+" 秒；身份确认前禁止输入。";
            }
            view.setText("IATest Bluetooth HID\n模式："+(useBle?"BLE HID":"经典蓝牙 HID")
                +"\n服务生命周期："+("adb_managed".equals(bleLifecycle)?ownership:"随 App 进程结束")
                +"\n输入时请保持此 App 在前台。\n\n系统 HID 接口："+((hid!=null||ble!=null)?"已连接":"等待")
                +"\n键鼠注册回调："+(registered?"成功":"未完成")+"\niPhone 连接："+(peer!=null?"已连接":"等待配对")+"\n\n"+error);
        } catch(Exception failure) {android.util.Log.e("IATestHID","Status unavailable",failure);}
    }

    private void ready(String address) {
        if (!foreground || !registered || (useBle?(ble==null||!ble.ready(peer)):hid==null) || peer == null || !peer.getAddress().equals(address))
            throw new IllegalStateException("Foreground HID registration and exact paired host required");
    }
    private void release() {
        if (peer != null) {
            boolean keyboard=send(1,new byte[8],300);
            boolean mouse=send(3,new byte[4],300);
            byte[] absolute=lastAbsolute.clone(); absolute[0]=0;
            boolean pointer=send(2,absolute,300);
            boolean touch=HidDigitizer.released(lastDigitizer)||send(4,HidDigitizer.release(lastDigitizer),300);
            if (!keyboard || !mouse || !pointer || !touch) throw new IllegalStateException("Bluetooth release unconfirmed");
            lastAbsolute=absolute;
            lastDigitizer=HidDigitizer.release(lastDigitizer);
        } else throw new IllegalStateException("Bluetooth host disconnected before release");
    }
    JSONObject command(JSONObject request) throws Exception {
        String operation=request.getString("operation_id");
        if (!operation.matches("[a-f0-9]{32}")) throw new IllegalArgumentException("Invalid operation ID");
        if (ledger.containsKey(operation)) return ledger.get(operation);
        JSONObject reply=new JSONObject();
        for (String key:new String[]{"protocol","epoch","session","sequence","operation_id","host_address","transport"})
            reply.put(key,request.get(key));
        boolean dispatched=false;
        try {
            if (!"iatest-bluetooth-hid/1".equals(request.getString("protocol")) || !epoch.equals(request.getString("epoch")))
                throw new IllegalArgumentException("Protocol or application generation differs");
            if(!request.getString("transport").equals(useBle?"ble":"classic"))throw new IllegalArgumentException("HID transport differs");
            String owner=request.getString("session"), address=request.getString("host_address"), op=request.getString("op");
            long number=request.getLong("sequence");
            if (!owner.matches("[a-f0-9]{32}") || !address.matches("(?:[0-9A-F]{2}:){5}[0-9A-F]{2}"))
                throw new IllegalArgumentException("Invalid session or host binding");
            if (op.equals("prepare")) {
                if(number!=0||!session.isEmpty()||!foreground||!registered||!useBle)
                    throw new IllegalStateException("Idle foreground BLE preparation required");
                BluetoothDevice target=adapter.getRemoteDevice(address);
                if(!"adb_managed".equals(bleLifecycle)&&!adapter.getBondedDevices().contains(target))throw new IllegalStateException("Host must be explicitly paired first");
                ble.requestConnection(target);
                reply.put("connection_requested",true);
            } else if (op.equals("hello")) {
                if (!session.isEmpty() && (!session.equals(owner) || uncertain))
                    throw new IllegalStateException("Existing or uncertain session requires explicit release");
                if (number!=1) throw new IllegalArgumentException("Handshake sequence differs");
                if(peer==null && foreground && registered && hid!=null) {
                    BluetoothDevice target=adapter.getRemoteDevice(address);
                    if(!adapter.getBondedDevices().contains(target)) throw new IllegalStateException("Host must be explicitly paired first");
                    if(!hid.connect(target)) throw new IllegalStateException("Bluetooth connection request rejected");
                    long until=SystemClock.elapsedRealtime()+5000;
                    while(peer==null && SystemClock.elapsedRealtime()<until) Thread.sleep(50);
                }
                if(useBle) {
                    long until=SystemClock.elapsedRealtime()+5000;
                    while((peer==null || !ble.ready(peer)) && foreground && SystemClock.elapsedRealtime()<until) Thread.sleep(50);
                }
                ready(address); release(); session=owner; sequence=number; uncertain=false;
                reply.put("capabilities",new JSONArray().put("keyboard").put("relative_mouse").put("absolute_mouse"));
                if(useBle&&ble.digitizerReady())reply.getJSONArray("capabilities").put("digitizer");
                reply.put("max_touch_points",useBle&&ble.digitizerReady()?HidDigitizer.CONTACTS:0);
                reply.put("controlled_reconnect",useBle);
            } else {
                if (!session.equals(owner) || number!=sequence+1) throw new IllegalStateException("Session or sequence differs");
                sequence=number;
                if (op.equals("close")) {
                    ready(address); release(); session=""; uncertain=false;
                } else if (op.equals("reconnect")) {
                    if(!useBle||uncertain)throw new IllegalStateException("Confirmed BLE session required for reconnect");
                    ready(address);release();session="";ble.reconnect();
                } else if (op.equals("execute")) {
                    if (uncertain) throw new IllegalStateException("Previous input delivery uncertain");
                    ready(address);
                    JSONArray reports=request.getJSONArray("reports");
                    if (reports.length()<1 || reports.length()>64) throw new IllegalArgumentException("Report quota exceeded");
                    int budget=request.getInt("deadline_ms");
                    if(budget<1 || budget>5000) throw new IllegalArgumentException("Invalid input deadline");
                    long previous=-1;
                    Map<Integer,byte[]> last=new LinkedHashMap<>();
                    for(int i=0;i<reports.length();i++) {
                        JSONObject item=reports.getJSONObject(i); int id=item.getInt("id");
                        long stamp=item.getLong("t_ms"); byte[] data=Base64.decode(item.getString("data"),Base64.NO_WRAP);
                        if (stamp<0 || stamp<=previous || stamp>5000 || (i==0 && stamp!=0)) throw new IllegalArgumentException("Invalid report timeline");
                        if ((id<1||id>4) || data.length!=(id==1?8:id==2?5:id==3?4:HidDigitizer.LENGTH)) throw new IllegalArgumentException("Report ID or length differs");
                        if(id==4&&(!useBle||!ble.digitizerReady()||!HidDigitizer.valid(data)))throw new IllegalArgumentException("Digitizer endpoint not negotiated or report invalid");
                        if (id==1) {
                            if(data[1]!=0) throw new IllegalArgumentException("Invalid keyboard reserved byte");
                            for(int k=2;k<8;k++) if((data[k]&255)>164) throw new IllegalArgumentException("Invalid keyboard usage");
                        } else if(id!=4&&(data[0]&255)>7) throw new IllegalArgumentException("Invalid mouse buttons");
                        if(id==2 && ((data[2]&128)!=0 || (data[4]&128)!=0)) throw new IllegalArgumentException("Absolute coordinates exceed 32767");
                        previous=stamp; last.put(id,data);
                    }
                    for(Map.Entry<Integer,byte[]> entry:last.entrySet()) {
                        byte[] data=entry.getValue();
                        if(entry.getKey()==1) {for(byte v:data) if(v!=0) throw new IllegalArgumentException("Keyboard release required");}
                        else if(entry.getKey()==4) {if(!HidDigitizer.released(data))throw new IllegalArgumentException("All touch contacts must be released");}
                        else if(data[0]!=0) throw new IllegalArgumentException("Mouse release required");
                    }
                    long start=SystemClock.elapsedRealtime(); int count=0;
                    for(int i=0;i<reports.length();i++) {
                        JSONObject item=reports.getJSONObject(i);
                        long wait=start+item.getLong("t_ms")-SystemClock.elapsedRealtime();
                        if(wait>0) Thread.sleep(wait);
                        ready(address);
                        long left=start+budget-SystemClock.elapsedRealtime();
                        if(left<=0)throw new IllegalStateException("Input deadline expired");
                        dispatched=true;
                        if(item.getInt("id")==4)lastDigitizer=Base64.decode(item.getString("data"),Base64.NO_WRAP);
                        if(!send(item.getInt("id"),Base64.decode(item.getString("data"),Base64.NO_WRAP),left))
                            throw new IllegalStateException("Bluetooth report not acknowledged");
                        if(item.getInt("id")==2) lastAbsolute=Base64.decode(item.getString("data"),Base64.NO_WRAP);
                        count++;
                    }
                    reply.put("reports_queued",count);
                } else throw new IllegalArgumentException("Unknown command");
            }
            reply.put("status","acknowledged");
        } catch(Exception failure) {
            uncertain|=dispatched;
            reply.put("status",dispatched?"unknown":"rejected");
            reply.put("error",failure.getClass().getSimpleName()+": "+failure.getMessage());
            if(dispatched) try {release();} catch(Exception ignored) {}
        }
        if(ledger.size()>=256) ledger.remove(ledger.keySet().iterator().next());
        ledger.put(operation,reply);
        return reply;
    }
    void writeReply(JSONObject reply) throws Exception {
        File directory=new File(getFilesDir(),"replies"); directory.mkdir();
        File[] old=directory.listFiles();
        if(old!=null) for(File file:old) if(System.currentTimeMillis()-file.lastModified()>60000) file.delete();
        File temporary=new File(directory,reply.getString("operation_id")+".tmp");
        try(FileOutputStream stream=new FileOutputStream(temporary)) {stream.write(reply.toString().getBytes(StandardCharsets.UTF_8));}
        if(!temporary.renameTo(new File(directory,reply.getString("operation_id")+".json"))) throw new IllegalStateException("Reply replace failed");
        if(bleDiagnostics)runOnUiThread(()->status());
    }
}
