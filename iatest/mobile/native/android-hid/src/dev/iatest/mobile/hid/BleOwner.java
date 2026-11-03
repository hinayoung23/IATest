package dev.iatest.mobile.hid;

import android.bluetooth.BluetoothDevice;
import android.content.Context;
import android.content.SharedPreferences;

/** State and context supplied by either the Activity or the explicit ADB owner. */
interface BleOwner {
    Context bleContext();
    SharedPreferences blePreferences();
    boolean bleDiagnosticsEnabled();
    default boolean bleDigitizerEnabled(){return false;}
    boolean acceptBlePeer(BluetoothDevice device);
    void bleState(BleTransport source,boolean registered,BluetoothDevice device,String error);
}
