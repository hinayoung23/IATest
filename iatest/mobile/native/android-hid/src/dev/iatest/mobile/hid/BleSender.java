package dev.iatest.mobile.hid;

import android.bluetooth.*;
import android.bluetooth.le.*;
import android.os.Build;
import android.os.ParcelUuid;
import android.os.SystemClock;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import org.json.JSONObject;
import org.json.JSONArray;

/** Standard encrypted HID over GATT. No network service or input on connect. */
final class BleSender implements BleTransport {
    private final BleOwner owner;
    private final BluetoothAdapter adapter;
    private volatile BluetoothGattServer server;
    private volatile BluetoothDevice peer;
    private volatile boolean advertising, suspended, reconnecting;
    private volatile boolean active;
    private volatile boolean serviceReady;
    private volatile BleRecovery recovery;
    private volatile boolean recoveryAttempted;
    private final android.os.Handler handler=new android.os.Handler(android.os.Looper.getMainLooper());
    private final Map<Integer,BluetoothGattCharacteristic> reports=new HashMap<>();
    private final Map<BluetoothGattCharacteristic,byte[]> values=new ConcurrentHashMap<>();
    private final Set<Integer> subscribed=Collections.synchronizedSet(new HashSet<>());
    private final Object sent=new Object();
    private boolean pending;
    private int sentStatus;
    private int pendingReport;
    private final Object diagnosticLock=new Object();
    private final ArrayDeque<JSONObject> events=new ArrayDeque<>();
    private final Map<String,Integer> counts=new LinkedHashMap<>();
    private long lastEvent;
    private final ArrayDeque<BluetoothGattService> registrationQueue=new ArrayDeque<>();
    private BluetoothGattCharacteristic batteryLevel;
    private static UUID uuid(String shortId) {return UUID.fromString("0000"+shortId+"-0000-1000-8000-00805f9b34fb");}
    private static final UUID CCC=uuid("2902"),REFERENCE=uuid("2908");

    BleSender(BleOwner activity,BluetoothAdapter value,boolean enabled) {
        owner=activity; adapter=value;active=enabled;
        BluetoothManager manager=activity.bleContext().getSystemService(BluetoothManager.class);
        server=manager.openGattServer(activity.bleContext(),callback);
        if(server==null) throw new IllegalStateException("BLE GATT server unavailable");
        BluetoothGattService hid=new BluetoothGattService(uuid("1812"),BluetoothGattService.SERVICE_TYPE_PRIMARY);
        add(hid,"2a4a",BluetoothGattCharacteristic.PROPERTY_READ,new byte[]{0x11,1,0,2},false);
        byte[] base=MainActivity.reportDescriptor(),extra=owner.bleDigitizerEnabled()?HidDigitizer.descriptor():new byte[0];
        byte[] map=Arrays.copyOf(base,base.length+extra.length);System.arraycopy(extra,0,map,base.length,extra.length);
        if(map.length>512)throw new IllegalStateException("HOGP Report Map exceeds GATT attribute limit");
        add(hid,"2a4b",BluetoothGattCharacteristic.PROPERTY_READ,map,false);
        add(hid,"2a4c",BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,new byte[]{0},true);
        for(int id=1;id<=(owner.bleDigitizerEnabled()?4:3);id++) {
            BluetoothGattCharacteristic report=add(hid,"2a4d",BluetoothGattCharacteristic.PROPERTY_READ|BluetoothGattCharacteristic.PROPERTY_NOTIFY,new byte[id==1?8:id==2?5:id==3?4:HidDigitizer.LENGTH],false);
            BluetoothGattDescriptor ref=new BluetoothGattDescriptor(REFERENCE,BluetoothGattDescriptor.PERMISSION_READ);
            ref.setValue(new byte[]{(byte)id,1}); report.addDescriptor(ref);
            BluetoothGattDescriptor ccc=new BluetoothGattDescriptor(CCC,BluetoothGattDescriptor.PERMISSION_READ_ENCRYPTED|BluetoothGattDescriptor.PERMISSION_WRITE_ENCRYPTED);
            ccc.setValue(new byte[]{0,0}); report.addDescriptor(ccc); reports.put(id,report);
        }
        if(owner.bleDigitizerEnabled()) {
            BluetoothGattCharacteristic feature=add(hid,"2a4d",BluetoothGattCharacteristic.PROPERTY_READ,new byte[]{HidDigitizer.CONTACTS},false);
            BluetoothGattDescriptor ref=new BluetoothGattDescriptor(REFERENCE,BluetoothGattDescriptor.PERMISSION_READ);
            ref.setValue(new byte[]{5,3});feature.addDescriptor(ref);
        }
        BluetoothGattCharacteristic leds=add(hid,"2a4d",BluetoothGattCharacteristic.PROPERTY_READ|BluetoothGattCharacteristic.PROPERTY_WRITE|BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,new byte[]{0},true);
        BluetoothGattDescriptor ledRef=new BluetoothGattDescriptor(REFERENCE,BluetoothGattDescriptor.PERMISSION_READ);
        ledRef.setValue(new byte[]{1,2}); leds.addDescriptor(ledRef);
        BluetoothGattService information=new BluetoothGattService(uuid("180a"),BluetoothGattService.SERVICE_TYPE_PRIMARY);
        add(information,"2a29",BluetoothGattCharacteristic.PROPERTY_READ,"IATest".getBytes(java.nio.charset.StandardCharsets.UTF_8),false);
        add(information,"2a26",BluetoothGattCharacteristic.PROPERTY_READ,(owner.bleDigitizerEnabled()?"hogp3-key1-abs2-rel3-touch4-max5":"hogp2-key1-abs2-rel3").getBytes(java.nio.charset.StandardCharsets.UTF_8),false);
        // Reserved unknown-vendor identity for the development bridge, not an
        // assigned manufacturer ID or another vendor's product identity.
        add(information,"2a50",BluetoothGattCharacteristic.PROPERTY_READ,new byte[]{1,(byte)0xff,(byte)0xff,1,0,0,1},false);
        BluetoothGattService battery=new BluetoothGattService(uuid("180f"),BluetoothGattService.SERVICE_TYPE_PRIMARY);
        // Read only: the profile does not promise battery notifications.
        batteryLevel=add(battery,"2a19",BluetoothGattCharacteristic.PROPERTY_READ,new byte[0],false);
        registrationQueue.add(information);registrationQueue.add(battery);registrationQueue.add(hid);
        if(!server.addService(registrationQueue.removeFirst())) {
            server.close();server=null;
            throw new IllegalStateException("BLE profile registration rejected");
        }
    }
    private BluetoothGattCharacteristic add(BluetoothGattService service,String id,int properties,byte[] value,boolean writable) {
        BluetoothGattCharacteristic c=new BluetoothGattCharacteristic(uuid(id),properties,BluetoothGattCharacteristic.PERMISSION_READ_ENCRYPTED|(writable?BluetoothGattCharacteristic.PERMISSION_WRITE_ENCRYPTED:0));
        c.setValue(value);service.addCharacteristic(c);values.put(c,value);return c;
    }
    private void state(String error) {owner.bleState(this,advertising,peer,error);}
    private int reportId(BluetoothGattCharacteristic value) {
        for(Map.Entry<Integer,BluetoothGattCharacteristic> item:reports.entrySet())
            if(item.getValue()==value)return item.getKey();
        return -1;
    }
    private void trace(String event,int status,int id,int handle,int detail) {
        if(!owner.bleDiagnosticsEnabled())return;
        synchronized(diagnosticLock) {
            try {
                int count=counts.containsKey(event)?counts.get(event)+1:1;counts.put(event,count);
                lastEvent=SystemClock.elapsedRealtime();
                if(event.startsWith("notify_")&&count>3&&status==BluetoothGatt.GATT_SUCCESS)return;
                JSONObject row=new JSONObject().put("event",event).put("uptime_ms",lastEvent)
                    .put("status",status).put("report_id",id).put("handle",handle).put("detail",detail);
                if(events.size()==48)events.removeFirst();events.addLast(row);
            } catch(Exception ignored) {}
        }
    }
    public JSONObject diagnostics() {
        Set<Integer> current;
        synchronized(subscribed) {current=new TreeSet<>(subscribed);}
        synchronized(diagnosticLock) {
            JSONObject result=new JSONObject(),handles=new JSONObject();
            try {
                for(Map.Entry<Integer,BluetoothGattCharacteristic> item:reports.entrySet())handles.put(String.valueOf(item.getKey()),item.getValue().getInstanceId());
                JSONArray services=new JSONArray();BluetoothGattServer diagnosticServer=server;
                if(diagnosticServer!=null)for(BluetoothGattService service:diagnosticServer.getServices())services.put(service.getUuid().toString());
                result.put("profile_version",2).put("services",services)
                    .put("report_handles",handles).put("counts",new JSONObject(counts)).put("last_event_uptime_ms",lastEvent)
                    .put("events",new JSONArray(events)).put("subscribed_reports",new JSONArray(current))
                    .put("suspended",suspended).put("advertising",advertising);
            } catch(Exception ignored) {}
            return result;
        }
    }
    private void startAdvertising() {
        if(server==null||!active)return;
        BluetoothLeAdvertiser radio=adapter.getBluetoothLeAdvertiser();
        if(radio==null) {state("BLE peripheral advertising unavailable");return;}
        radio.stopAdvertising(advertise);advertising=false;
        AdvertiseSettings settings=new AdvertiseSettings.Builder().setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY).setConnectable(true).setTimeout(0).build();
        AdvertiseData data=new AdvertiseData.Builder().addServiceUuid(new ParcelUuid(uuid("1812"))).addServiceUuid(new ParcelUuid(uuid("180f"))).build();
        AdvertiseData scan=new AdvertiseData.Builder().setIncludeDeviceName(true).build();
        radio.startAdvertising(settings,data,scan,advertise);
    }
    public synchronized void reconnect() {
        if(server==null||peer==null)throw new IllegalStateException("Connected BLE host required");
        reconnecting=true;state("");
        // Incoming links have no app-held reference. Claim one before releasing
        // it so Android actually considers closing the idle LE connection.
        if(!server.connect(peer,false)) {
            reconnecting=false;state("BLE connection reference rejected");
            throw new IllegalStateException("BLE connection reference rejected");
        }
        trace("reconnect_requested",0,-1,0,0);
        server.cancelConnection(peer);
    }
    public synchronized void requestConnection(BluetoothDevice device) {
        if(server==null||!active||!serviceReady||device.getBondState()!=BluetoothDevice.BOND_BONDED)
            throw new IllegalStateException("Registered BLE service and paired host required");
        if(peer!=null&&!peer.equals(device))throw new IllegalStateException("Another BLE host is connected");
        if(!server.connect(device,false))throw new IllegalStateException("BLE connection request rejected");
        trace("bound_connection_requested",0,-1,0,0);
    }
    public String recoveryStatus() {BleRecovery current=recovery;return current==null?"none":current.phase();}
    private synchronized void restoreSubscriptions(BluetoothDevice device) {
        if(!active||!serviceReady||device.getBondState()!=BluetoothDevice.BOND_BONDED)return;
        for(Map.Entry<Integer,BluetoothGattCharacteristic> entry:reports.entrySet())
            if(entry.getKey()!=4&&owner.blePreferences().getBoolean(subscriptionKey(device,entry.getKey()),false)) {
                subscribed.add(entry.getKey());
                trace("cccd_restored",0,entry.getKey(),entry.getValue().getInstanceId(),1);
                entry.getValue().getDescriptor(CCC).setValue(new byte[]{1,0});
            }
        if(!recoveryAttempted&&subscribed.containsAll(Arrays.asList(1,2,3))) {
            recoveryAttempted=true;
            recovery=new BleRecovery(device.getAddress(),SystemClock.elapsedRealtime(),true);
            trace("recovery_started",0,-1,0,0);
            handler.post(this::recover);
        }
    }
    private synchronized void recover() {
        BleRecovery current=recovery;
        if(server==null||!active||current==null)return;
        if(current.reconnectNeeded(SystemClock.elapsedRealtime())) {
            try {reconnect();}
            catch(Exception failure) {current.fail();state("BLE startup connection recovery failed");return;}
        }
        current.expire(SystemClock.elapsedRealtime());
        if(current.phase().equals("failed")) {state("BLE startup recovery timed out; reconnect the host Bluetooth radio");return;}
        state("");
        if(current.blocked())handler.postDelayed(this::recover,150);
    }
    private final AdvertiseCallback advertise=new AdvertiseCallback() {
        @Override public void onStartSuccess(AdvertiseSettings settings) {advertising=true;trace("advertising",0,-1,0,1);state("");}
        @Override public void onStartFailure(int code) {advertising=false;trace("advertising",code,-1,0,0);state("BLE advertising failed: "+code);}
    };
    private final BluetoothGattServerCallback callback=new BluetoothGattServerCallback() {
        @Override public void onMtuChanged(BluetoothDevice device,int mtu) {
            if(owner.acceptBlePeer(device))trace("mtu",0,-1,0,mtu);
        }
        @Override public void onServiceAdded(int status,BluetoothGattService service) {
            if(server==null)return;
            trace("service_added",status,-1,service.getInstanceId(),service.getCharacteristics().size());
            if(status!=BluetoothGatt.GATT_SUCCESS) {state("BLE service registration failed: "+status);return;}
            synchronized(BleSender.this) {
                if(!registrationQueue.isEmpty()) {
                    if(!server.addService(registrationQueue.removeFirst()))state("BLE profile service registration rejected");
                    return;
                }
                serviceReady=true;
                if(peer!=null)restoreSubscriptions(peer);
                startAdvertising();
            }
        }
        @Override public void onConnectionStateChange(BluetoothDevice device,int status,int newState) {
            if(server==null)return;
            if(!owner.acceptBlePeer(device)) {server.cancelConnection(device);return;}
            trace("connection",status,-1,0,newState);
            synchronized(BleSender.this) {
            if(newState==BluetoothProfile.STATE_CONNECTED && status==BluetoothGatt.GATT_SUCCESS) {
                if(peer!=null && !peer.equals(device)) {server.cancelConnection(device);return;}
                if(recovery!=null&&!recovery.matchesHost(device.getAddress())) {
                    recovery=null;recoveryAttempted=false;reconnecting=false;
                }
                peer=device; subscribed.clear();state("");
                if(recovery!=null)recovery.connected(device.getAddress(),SystemClock.elapsedRealtime());
                restoreSubscriptions(device);state("");
            } else if(device.equals(peer)) {
                boolean requestedReconnect=reconnecting;
                if(recovery!=null)recovery.disconnected(device.getAddress());
                peer=null;reconnecting=false;subscribed.clear();state("");
                synchronized(sent) {pending=false;sentStatus=BluetoothGatt.GATT_FAILURE;sent.notifyAll();}
                startAdvertising();
                // cancelConnection releases this app's reference; another GATT
                // user may keep the physical link alive. Reclaim our reference
                // so the following ATT requests have a current connected peer.
                if(requestedReconnect)handler.postDelayed(()->{
                    synchronized(BleSender.this) {
                        if(server==null||peer!=null)return;
                        try {requestConnection(device);}
                        catch(Exception failure) {
                            if(recovery!=null)recovery.fail();
                            state("BLE connection reference recovery failed");
                        }
                    }
                },150);
            }
            }
        }
        @Override public void onCharacteristicReadRequest(BluetoothDevice device,int request,int offset,BluetoothGattCharacteristic characteristic) {
            if(server==null)return;
            if(!owner.acceptBlePeer(device)) {server.sendResponse(device,request,BluetoothGatt.GATT_READ_NOT_PERMITTED,offset,null);return;}
            confirmAttConnection(device);
            trace("read_"+characteristic.getUuid().toString().substring(4,8),0,reportId(characteristic),characteristic.getInstanceId(),values.containsKey(characteristic)?offset:-1);
            if(characteristic==batteryLevel) {
                android.os.BatteryManager battery=owner.bleContext().getSystemService(android.os.BatteryManager.class);
                int level=battery==null?-1:battery.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY);
                if(level<0||level>100)server.sendResponse(device,request,BluetoothGatt.GATT_FAILURE,offset,null);
                else read(device,request,offset,new byte[]{(byte)level});
            } else read(device,request,offset,values.get(characteristic));
        }
        @Override public void onDescriptorReadRequest(BluetoothDevice device,int request,int offset,BluetoothGattDescriptor descriptor) {
            if(server==null)return;
            if(!owner.acceptBlePeer(device)) {server.sendResponse(device,request,BluetoothGatt.GATT_READ_NOT_PERMITTED,offset,null);return;}
            confirmAttConnection(device);
            trace("descriptor_read_"+descriptor.getUuid().toString().substring(4,8),0,reportId(descriptor.getCharacteristic()),descriptor.getCharacteristic().getInstanceId(),offset);
            read(device,request,offset,descriptor.getValue());
        }
        @Override public void onCharacteristicWriteRequest(BluetoothDevice device,int request,BluetoothGattCharacteristic c,boolean prepared,boolean response,int offset,byte[] value) {
            if(server==null)return;
            if(!owner.acceptBlePeer(device)) {if(response)server.sendResponse(device,request,BluetoothGatt.GATT_WRITE_NOT_PERMITTED,offset,null);return;}
            trace("write_"+c.getUuid().toString().substring(4,8),0,reportId(c),c.getInstanceId(),value.length==1?value[0]&255:-1);
            boolean valid=!prepared && offset==0 && value.length==1;
            if(c.getUuid().equals(uuid("2a4e"))) valid&=value.length==1 && value[0]==1;
            if(c.getUuid().equals(uuid("2a4c"))) {
                valid&=value.length==1 && (value[0]==0||value[0]==1);
                if(valid)suspended=value[0]==0;
            }
            if(valid)values.put(c,value.clone());
            if(response)server.sendResponse(device,request,valid?BluetoothGatt.GATT_SUCCESS:BluetoothGatt.GATT_REQUEST_NOT_SUPPORTED,offset,null);
        }
        @Override public void onDescriptorWriteRequest(BluetoothDevice device,int request,BluetoothGattDescriptor d,boolean prepared,boolean response,int offset,byte[] value) {
            if(server==null)return;
            if(!owner.acceptBlePeer(device)) {if(response)server.sendResponse(device,request,BluetoothGatt.GATT_WRITE_NOT_PERMITTED,offset,null);return;}
            confirmAttConnection(device);
            trace("descriptor_write_"+d.getUuid().toString().substring(4,8),0,reportId(d.getCharacteristic()),d.getCharacteristic().getInstanceId(),value.length==2?value[0]&255:-1);
            boolean valid=device.equals(peer)&&!prepared && offset==0 && d.getUuid().equals(CCC) && value.length==2 && value[1]==0 && (value[0]==0||value[0]==1);
            if(valid) {
                d.setValue(value.clone());
                for(Map.Entry<Integer,BluetoothGattCharacteristic> entry:reports.entrySet()) if(entry.getValue()==d.getCharacteristic()) {
                    if(value[0]==1)subscribed.add(entry.getKey());else subscribed.remove(entry.getKey());
                    if(recovery!=null)recovery.subscribed(device.getAddress(),entry.getKey(),value[0]==1);
                    owner.blePreferences().edit().putBoolean(subscriptionKey(device,entry.getKey()),value[0]==1).apply();
                }
                if(recovery==null&&subscribed.containsAll(Arrays.asList(1,2,3)))recoveryAttempted=true;
                state("");
            }
            if(response)server.sendResponse(device,request,valid?BluetoothGatt.GATT_SUCCESS:BluetoothGatt.GATT_REQUEST_NOT_SUPPORTED,offset,null);
        }
        @Override public void onNotificationSent(BluetoothDevice device,int status) {
            synchronized(sent) {if(pending && device.equals(peer)) {trace("notify_"+pendingReport,status,pendingReport,reports.get(pendingReport).getInstanceId(),0);sentStatus=status;pending=false;sent.notifyAll();}}
        }
    };
    private String subscriptionKey(BluetoothDevice device,int id) {return "hogp2-key1-abs2-rel3:"+device.getAddress()+":"+id;}
    private synchronized void confirmAttConnection(BluetoothDevice device) {
        // Releasing the app connection reference can yield DISCONNECTED while
        // the bonded host still uses the encrypted physical ATT link. A fresh
        // request from the exact recovering host confirms that link directly.
        if(server==null||!active||!serviceReady||peer!=null||recovery==null
            ||device.getBondState()!=BluetoothDevice.BOND_BONDED||!recovery.awaitingHost(device.getAddress()))return;
        peer=device;reconnecting=false;
        recovery.connected(device.getAddress(),SystemClock.elapsedRealtime());
        trace("att_connection_confirmed",0,-1,0,0);
        restoreSubscriptions(device);state("");
    }
    private void read(BluetoothDevice device,int request,int offset,byte[] value) {
        int status=value==null||offset<0||offset>value.length?BluetoothGatt.GATT_INVALID_OFFSET:BluetoothGatt.GATT_SUCCESS;
        byte[] data=status==BluetoothGatt.GATT_SUCCESS?Arrays.copyOfRange(value,offset,value.length):null;
        boolean accepted=server.sendResponse(device,request,status,offset,data);
        trace("read_response",accepted?status:-1,-1,offset,data==null?0:data.length);
    }
    public synchronized void setActive(boolean enabled) {
        if(active==enabled) {state("");return;}
        active=enabled;
        if(!enabled) {
            handler.removeCallbacksAndMessages(null);
            BluetoothLeAdvertiser radio=adapter.getBluetoothLeAdvertiser();
            if(radio!=null)radio.stopAdvertising(advertise);
            advertising=false;state("");return;
        }
        if(recovery!=null&&recovery.blocked()) {recovery=null;recoveryAttempted=false;}
        if(peer!=null)restoreSubscriptions(peer);
        if(serviceReady)startAdvertising();state("");
    }
    public void setForeground(boolean foreground) {}
    public JSONObject ownerStatus() {return new JSONObject();}
    public boolean ready(BluetoothDevice device) {return active && serviceReady && advertising && !suspended && !reconnecting && (recovery==null||!recovery.blocked()) && device!=null && device.equals(peer) && subscribed.containsAll(Arrays.asList(1,2,3));}
    public boolean digitizerReady(){return reports.containsKey(4)&&subscribed.contains(4)&&ready(peer);}
    public boolean send(int id,byte[] data,long budgetMs) {
        if(!ready(peer))return false;
        if(id==4&&(!digitizerReady()||!HidDigitizer.valid(data)))return false;
        BluetoothGattCharacteristic report=reports.get(id);
        if(report==null)return false;
        synchronized(sent) {
            if(pending)return false;
            pending=true;pendingReport=id;sentStatus=BluetoothGatt.GATT_FAILURE;values.put(report,data.clone());
            boolean accepted;
            if(Build.VERSION.SDK_INT>=33)accepted=server.notifyCharacteristicChanged(peer,report,false,data)==BluetoothStatusCodes.SUCCESS;
            else {report.setValue(data);accepted=server.notifyCharacteristicChanged(peer,report,false);}
            if(!accepted) {pending=false;return false;}
            long until=SystemClock.elapsedRealtime()+Math.max(1,Math.min(1500,budgetMs));
            while(pending && SystemClock.elapsedRealtime()<until)try {sent.wait(Math.max(1,until-SystemClock.elapsedRealtime()));}catch(InterruptedException failure) {Thread.currentThread().interrupt();return false;}
            return !pending && sentStatus==BluetoothGatt.GATT_SUCCESS;
        }
    }
    public void close() {
        active=false;
        handler.removeCallbacksAndMessages(null);
        BluetoothGattServer closing=server;server=null;
        BluetoothLeAdvertiser radio=adapter.getBluetoothLeAdvertiser();
        if(radio!=null)radio.stopAdvertising(advertise);
        if(closing!=null) {if(peer!=null)closing.cancelConnection(peer);closing.close();}
        peer=null;advertising=false;subscribed.clear();
    }
}
