package dev.iatest.mobile.hid;

import android.bluetooth.BluetoothDevice;
import org.json.JSONObject;

/** BLE input endpoint; closing a managed client does not remove its GATT owner. */
interface BleTransport {
    void setActive(boolean enabled);
    void setForeground(boolean foreground);
    boolean ready(BluetoothDevice device);
    default boolean digitizerReady(){return false;}
    boolean send(int id,byte[] data,long budgetMs);
    void requestConnection(BluetoothDevice device);
    void reconnect();
    String recoveryStatus();
    JSONObject diagnostics();
    JSONObject ownerStatus();
    void close();
}
