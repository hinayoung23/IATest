package dev.iatest.mobile.hid;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.os.Binder;
import android.os.IBinder;
import android.os.Parcel;
import android.os.Bundle;
import android.net.Uri;
import android.os.SystemClock;
import android.util.Base64;
import java.io.IOException;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;
import org.json.JSONObject;

/** ADB publishes an authenticated Binder handle through the protected provider. */
final class ManagedBleClient implements BleTransport {
    private final BleOwner owner;
    private final BluetoothAdapter adapter;
    private final String epoch,uiEpoch;
    private final IBinder uiToken=new Binder();
    private final ReentrantLock requests=new ReentrantLock(true);
    private final ScheduledExecutorService polling=Executors.newSingleThreadScheduledExecutor();
    private volatile IBinder remote;
    private volatile boolean active,foreground,closed,attached;
    private volatile JSONObject state=new JSONObject();
    private volatile long observedAt;
    private volatile String error="Waiting for explicit ADB BLE owner";

    ManagedBleClient(BleOwner value,BluetoothAdapter radio,String endpoint,String generation,String ui,boolean enabled,boolean visible) {
        ManagedBleProtocol.binding(endpoint,generation);
        owner=value;adapter=radio;epoch=generation;uiEpoch=ui;active=enabled;foreground=visible;
        polling.scheduleWithFixedDelay(this::poll,0,250,TimeUnit.MILLISECONDS);
    }
    private JSONObject call(String op,JSONObject args,int timeout)throws Exception {
        return call(op,args,timeout,false);
    }
    private JSONObject call(String op,JSONObject args,int timeout,boolean cleanup)throws Exception {
        if(!requests.tryLock(timeout,TimeUnit.MILLISECONDS))throw new IOException("Managed UI request deadline exceeded");
        Parcel input=Parcel.obtain(),output=Parcel.obtain();
        try {
            if(closed&&!cleanup)throw new IOException("Managed UI endpoint closed");
            IBinder target=remote;
            if(target==null||!target.isBinderAlive()) {
                Bundle found=owner.bleContext().getContentResolver().call(Uri.parse("content://"+ManagedBleProvider.AUTHORITY),"get",epoch,null);
                target=found==null?null:found.getBinder("owner");
                if(target==null||!ManagedBleProtocol.VERSION.equals(target.getInterfaceDescriptor()))throw new IOException("Waiting for ADB owner publication");
                remote=target;
            }
            String id=UUID.randomUUID().toString().replace("-","");
            JSONObject request=args.put("op",op).put("ui_epoch",uiEpoch).put("protocol",ManagedBleProtocol.VERSION)
                .put("owner_epoch",epoch).put("request_id",id);
            String encoded=request.toString();
            if(encoded.length()>ManagedBleProtocol.MAX_FRAME)throw new IllegalArgumentException("Managed Binder frame exceeds quota");
            input.writeInterfaceToken(ManagedBleProtocol.VERSION);input.writeString(encoded);input.writeStrongBinder(uiToken);
            if(!target.transact(IBinder.FIRST_CALL_TRANSACTION,input,output,0))throw new IOException("Owner Binder request rejected");
            output.readException();String text=output.readString();
            if(text==null||text.length()>ManagedBleProtocol.MAX_FRAME)throw new IOException("Owner Binder response exceeds quota");
            JSONObject reply=new JSONObject(text);
            if(!ManagedBleProtocol.VERSION.equals(reply.optString("protocol"))||!epoch.equals(reply.optString("owner_epoch"))
                ||!id.equals(reply.optString("request_id")))throw new SecurityException("Managed owner response differs");
            if(!"acknowledged".equals(reply.optString("status")))throw new IllegalStateException(reply.optString("error","Managed BLE rejected"));
            return reply;
        } catch(Exception failure) {
            remote=null;attached=false;state=new JSONObject();observedAt=0;throw failure;
        } finally {input.recycle();output.recycle();requests.unlock();}
    }
    private void publish() {
        if(closed)return;
        JSONObject current=state;BluetoothDevice peer=null;
        if(current.optBoolean("connected"))try{peer=adapter.getRemoteDevice(current.getString("host_address"));}catch(Exception ignored){}
        owner.bleState(this,current.optBoolean("registered"),peer,error);
    }
    private void poll() {
        if(closed)return;
        try {
            if(!attached){call("attach",new JSONObject(),2500);attached=true;}
            JSONObject reply=call("heartbeat",new JSONObject().put("foreground",foreground).put("active",active),2500);
            state=reply.getJSONObject("state");observedAt=SystemClock.elapsedRealtime();error=state.optString("error","");
        } catch(Exception failure) {
            attached=false;state=new JSONObject();observedAt=0;
            String message=String.valueOf(failure.getMessage());
            error="ADB BLE owner unavailable: "+failure.getClass().getSimpleName()+": "+message.substring(0,Math.min(180,message.length()));
        }
        publish();
    }
    public void setForeground(boolean visible){foreground=visible;}
    public void setActive(boolean enabled){active=enabled;publish();}
    public boolean ready(BluetoothDevice peer) {
        JSONObject current=state;
        return !closed&&active&&foreground&&SystemClock.elapsedRealtime()-observedAt<ManagedBleGate.HEARTBEAT_MS
            &&current.optBoolean("input_ready")&&peer!=null&&peer.getAddress().equals(current.optString("host_address"));
    }
    public boolean digitizerReady(){return readyPeer()&&state.optBoolean("digitizer_ready");}
    private boolean readyPeer() {
        try{return ready(adapter.getRemoteDevice(state.optString("host_address")));}catch(Exception ignored){return false;}
    }
    public boolean send(int id,byte[] data,long budgetMs) {
        if(!readyPeer())return false;
        try {
            return call("send",new JSONObject().put("report_id",id).put("data",Base64.encodeToString(data,Base64.NO_WRAP))
                .put("budget_ms",Math.max(1,Math.min(1500,budgetMs))),2500).optBoolean("sent");
        } catch(Exception failure){return false;}
    }
    public void requestConnection(BluetoothDevice device) {
        if(!device.getAddress().equals(state.optString("host_address")))throw new IllegalArgumentException("Managed BLE host differs");
        try{call("connect",new JSONObject(),2500);}catch(Exception failure){throw new IllegalStateException("Managed BLE connect rejected",failure);}
    }
    public void reconnect() {
        try{call("reconnect",new JSONObject(),2500);}catch(Exception failure){throw new IllegalStateException("Managed BLE reconnect rejected",failure);}
    }
    public String recoveryStatus(){return state.optString("ble_recovery","owner_wait");}
    public JSONObject diagnostics(){JSONObject value=state.optJSONObject("ble_diagnostics");return value==null?new JSONObject():value;}
    public JSONObject ownerStatus(){JSONObject value=state.optJSONObject("owner");return value==null?new JSONObject():value;}
    void requireStopped() {
        if(!owner.blePreferences().getBoolean("ble_owner_stopped",false))
            throw new IllegalStateException("Explicitly stop the ADB BLE owner before selecting app lifecycle");
    }
    public void close() {
        if(closed)return;closed=true;foreground=false;active=false;polling.shutdownNow();
        if(remote!=null)try{call("detach",new JSONObject(),1500,true);}catch(Exception ignored){}
        remote=null;state=new JSONObject();
    }
}
