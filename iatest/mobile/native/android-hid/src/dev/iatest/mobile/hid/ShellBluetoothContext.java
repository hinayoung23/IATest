package dev.iatest.mobile.hid;

import android.bluetooth.BluetoothManager;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.os.Process;

/** Explicit ADB shell bootstrap. Capability failure never falls back to app input. */
final class ShellBluetoothContext extends ContextWrapper {
    private BluetoothManager bluetooth;
    private ShellBluetoothContext(Context base){super(base);}
    static ShellBluetoothContext create()throws Exception {
        if(Process.myUid()!=2000||android.os.Build.VERSION.SDK_INT<31)
            throw new SecurityException("Managed BLE requires authorized Android 12+ ADB shell");
        Object thread=Class.forName("android.app.ActivityThread").getMethod("systemMain").invoke(null);
        Context base=(Context)thread.getClass().getMethod("getSystemContext").invoke(thread);
        Class<?> initializer=Class.forName("android.bluetooth.BluetoothFrameworkInitializer");
        Class<?> serviceManager=Class.forName("android.os.BluetoothServiceManager");
        if(initializer.getMethod("getBluetoothServiceManager").invoke(null)==null)
            initializer.getMethod("setBluetoothServiceManager",serviceManager).invoke(null,serviceManager.getConstructor().newInstance());
        ShellBluetoothContext context=new ShellBluetoothContext(base);
        context.bluetooth=BluetoothManager.class.getConstructor(Context.class).newInstance(context);
        return context;
    }
    @Override public String getPackageName(){return "com.android.shell";}
    @Override public String getOpPackageName(){return "com.android.shell";}
    @Override public AttributionSource getAttributionSource(){return new AttributionSource.Builder(Process.myUid()).setPackageName("com.android.shell").build();}
    @Override public Context getApplicationContext(){return this;}
    @Override public Context createDeviceContext(int id){if(id!=0)throw new IllegalArgumentException("Default device context required");return this;}
    @Override public Object getSystemService(String name){return Context.BLUETOOTH_SERVICE.equals(name)?bluetooth:super.getSystemService(name);}
}
