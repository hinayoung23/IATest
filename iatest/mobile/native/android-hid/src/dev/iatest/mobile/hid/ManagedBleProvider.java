package dev.iatest.mobile.hid;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.IBinder;

/** Only ADB publishes a handle; only this APK's UID can retrieve it. */
public final class ManagedBleProvider extends ContentProvider {
    static final String AUTHORITY="dev.iatest.mobile.hid.managed";
    private static IBinder owner;
    private static String epoch="";
    public boolean onCreate(){return true;}
    @Override public synchronized Bundle call(String method,String argument,Bundle extras) {
        int uid=Binder.getCallingUid();
        Bundle result=new Bundle();
        if("publish".equals(method)) {
            if(uid!=2000&&uid!=0)throw new SecurityException("Only ADB may publish a BLE owner");
            String expected=getContext().getSharedPreferences("MainActivity",0).getString("ble_owner_epoch","");
            if(argument==null||!argument.matches("[a-f0-9]{32}")||!argument.equals(expected)||extras==null)
                throw new SecurityException("BLE owner publication binding differs");
            IBinder value=extras.getBinder("owner");
            try {
                if(value==null||!ManagedBleProtocol.VERSION.equals(value.getInterfaceDescriptor()))throw new SecurityException("BLE owner interface differs");
            }catch(android.os.RemoteException failure){throw new IllegalStateException("BLE owner died before publication",failure);}
            owner=value;epoch=argument;result.putBoolean("published",true);return result;
        }
        if(!"get".equals(method)||uid!=getContext().getApplicationInfo().uid)throw new SecurityException("Only the bridge UI may retrieve its owner");
        if(argument!=null&&argument.equals(epoch)&&owner!=null&&owner.isBinderAlive())result.putBinder("owner",owner);
        return result;
    }
    public Cursor query(Uri uri,String[] projection,String selection,String[] args,String sort){throw new UnsupportedOperationException();}
    public String getType(Uri uri){return null;}
    public Uri insert(Uri uri,ContentValues values){throw new UnsupportedOperationException();}
    public int delete(Uri uri,String selection,String[] args){throw new UnsupportedOperationException();}
    public int update(Uri uri,ContentValues values,String selection,String[] args){throw new UnsupportedOperationException();}
}
