package dev.iatest.mobile.hid;

import android.bluetooth.*;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.system.Os;
import android.util.Base64;
import java.io.*;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import org.json.JSONObject;

/** Explicit shell-owned, finite-lived GATT server. No network listener. */
public final class ManagedBleMain implements BleOwner {
    static final String PACKAGE="dev.iatest.mobile.hid";
    static final File REGISTRY=new File("/data/local/tmp/iatest-managed-ble.json");
    private final JSONObject config;
    private final String socket,epoch,host,apkHash;
    private final int appUid;
    private final long until;
    private long lastRadioCheck;
    private final ManagedBleGate gate;
    private final ManagedBlePairing pairing;
    private final SharedPreferences preferences=new OwnerPreferences();
    private final Object inputLock=new Object();
    private final Map<String,JSONObject> ledger=new LinkedHashMap<>();
    private final Map<String,String> fingerprints=new HashMap<>();
    private final ThreadPoolExecutor clients=new ThreadPoolExecutor(3,3,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<>(16));
    private final Handler handler=new Handler(Looper.getMainLooper());
    private volatile BluetoothDevice peer;
    private volatile BluetoothDevice boundDevice;
    private volatile boolean registered,stopping;
    private volatile String error="";
    private byte[] absolute=new byte[5];
    private byte[] digitizer=new byte[HidDigitizer.LENGTH];
    private ShellBluetoothContext context;
    private BluetoothAdapter adapter;
    private BleSender sender;
    private LocalServerSocket listener;
    private android.os.Binder ownerBinder;
    private android.os.IBinder uiToken;
    private android.os.IBinder.DeathRecipient uiDeath;

    private ManagedBleMain(JSONObject value)throws Exception {
        config=value;socket=value.getString("socket");epoch=value.getString("owner_epoch");
        host=value.getString("host_address");apkHash=value.getString("apk_sha256");appUid=value.getInt("app_uid");
        until=SystemClock.elapsedRealtime()+value.getLong("lifetime_seconds")*1000;gate=new ManagedBleGate(appUid);ownerBinder=createBinder();
        pairing=new ManagedBlePairing(host,SystemClock.elapsedRealtime(),value.optInt("pairing_window_seconds",0));
    }
    static String sha(File path)throws Exception {
        MessageDigest digest=MessageDigest.getInstance("SHA-256");
        try(InputStream input=new FileInputStream(path)) {byte[] data=new byte[16384];int count;while((count=input.read(data))!=-1)digest.update(data,0,count);}
        StringBuilder result=new StringBuilder();for(byte b:digest.digest())result.append(String.format(java.util.Locale.ROOT,"%02x",b&255));return result.toString();
    }
    private static JSONObject configuration(String encoded)throws Exception {
        if(encoded.length()>8192||!encoded.matches("[A-Za-z0-9+/=]+"))throw new IllegalArgumentException("Invalid owner configuration");
        JSONObject value=new JSONObject(new String(Base64.decode(encoded,Base64.NO_WRAP),StandardCharsets.UTF_8));
        if(value.has("experimental_digitizer")&&!(value.get("experimental_digitizer") instanceof Boolean))throw new IllegalArgumentException("Digitizer selection must be boolean");
        ManagedBleProtocol.binding(value.getString("socket"),value.getString("owner_epoch"));
        if(!value.getString("host_address").matches("(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
            ||!value.getString("apk_sha256").matches("[a-f0-9]{64}")||!value.getString("jar_sha256").matches("[a-f0-9]{64}")
            ||!value.getString("jar_path").matches("/data/local/tmp/iatest-managed-ble-[a-f0-9]{16}\\.jar")
            ||value.getInt("app_uid")<10000||value.getLong("lifetime_seconds")<5||value.getLong("lifetime_seconds")>7200
            ||value.optInt("pairing_window_seconds",0)<0||value.optInt("pairing_window_seconds",0)>300
            ||(value.optInt("pairing_window_seconds",0)>0&&value.optInt("pairing_window_seconds",0)<5))
            throw new IllegalArgumentException("Managed BLE configuration exceeds bounds");
        return value;
    }
    public static void main(String[] args) {
        try {
            if(Process.myUid()!=2000)throw new SecurityException("Authorized ADB shell required");
            if(args.length==4&&args[0].equals("rpc")) {
                if(!Arrays.asList("status","stop").contains(args[3]))throw new IllegalArgumentException("Management operation required");
                System.out.println(ManagedBleProtocol.request(args[1],args[2],new JSONObject().put("op",args[3]),3000));return;
            }
            if(args.length!=2)throw new IllegalArgumentException("start or serve with bounded configuration required");
            JSONObject value=configuration(args[1]);
            if(args[0].equals("start")) {start(value,args[1]);return;}
            if(!args[0].equals("serve"))throw new IllegalArgumentException("Unknown managed BLE action");
            Os.setsid();
            Looper.prepareMainLooper();
            File lockPath=new File("/data/local/tmp/iatest-managed-ble.lock");
            if(!lockPath.getCanonicalPath().equals(lockPath.getAbsolutePath()))throw new SecurityException("Owner lock must not be a symlink");
            try(RandomAccessFile lockFile=new RandomAccessFile(lockPath,"rw");FileChannel channel=lockFile.getChannel();FileLock lock=channel.tryLock()) {
                if(lock==null)throw new IllegalStateException("A GATT owner already exists on this phone");
                Os.chmod(lockPath.getPath(),0600);
                ManagedBleMain owner=new ManagedBleMain(value);
                try {owner.run();Looper.loop();}finally {owner.stop();}
            }
        } catch(Throwable failure) {System.err.println(failure.getClass().getSimpleName()+": "+String.valueOf(failure.getMessage()));System.exit(2);}
    }
    private static void start(JSONObject value,String encoded)throws Exception {
        File jar=new File(value.getString("jar_path"));
        if(!sha(jar).equals(value.getString("jar_sha256")))throw new SecurityException("Managed BLE artifact hash differs");
        File directory=new File("/data/local/tmp/iatest-managed-ble-"+value.getString("owner_epoch"));
        if(!directory.mkdir())throw new IllegalStateException("Owner generation directory already exists");
        Os.chmod(directory.getPath(),0700);
        File log=new File(directory,"owner.log");if(!log.createNewFile())throw new IOException("Owner log already exists");Os.chmod(log.getPath(),0600);
        ProcessBuilder builder=new ProcessBuilder("/system/bin/app_process","/",ManagedBleMain.class.getName(),"serve",encoded);
        builder.environment().put("CLASSPATH",jar.getPath());builder.redirectInput(new File("/dev/null"));builder.redirectOutput(log);builder.redirectErrorStream(true);
        java.lang.Process process=builder.start();long deadline=SystemClock.elapsedRealtime()+10000;
        while(SystemClock.elapsedRealtime()<deadline) {
            if(!process.isAlive())throw new IllegalStateException("Owner bootstrap failed; inspect its private log");
            try {
                JSONObject state=new JSONObject(readFile(REGISTRY));
                if(value.getString("owner_epoch").equals(state.optString("owner_epoch"))) {System.out.println(state);return;}
            } catch(Exception ignored) {}
            Thread.sleep(50);
        }
        process.destroy();throw new IllegalStateException("Owner bootstrap deadline exceeded");
    }
    private static String readFile(File file)throws Exception {
        if(file.length()>16384)throw new IllegalArgumentException("Owner state exceeds quota");
        ByteArrayOutputStream output=new ByteArrayOutputStream();
        try(InputStream input=new FileInputStream(file)){byte[] bytes=new byte[4096];int n;while((n=input.read(bytes))!=-1){output.write(bytes,0,n);if(output.size()>16384)throw new IOException("Owner state exceeds quota");}}
        return new String(output.toByteArray(),StandardCharsets.UTF_8);
    }
    private void verifyApp()throws Exception {
        ApplicationInfo app=context.getPackageManager().getApplicationInfo(PACKAGE,0);
        if(app.uid!=appUid||!sha(new File(app.sourceDir)).equals(apkHash))throw new SecurityException("Bridge APK identity differs from explicit owner configuration");
    }
    private void run()throws Exception {
        context=ShellBluetoothContext.create();verifyApp();
        terminateAt(until+3000);
        adapter=context.getSystemService(BluetoothManager.class).getAdapter();
        if(adapter==null||!adapter.isEnabled())throw new IllegalStateException("Bluetooth is required");
        if(pairing.enabled()&&(android.os.Build.VERSION.SDK_INT<36
            ||context.checkSelfPermission("android.permission.BLUETOOTH_PRIVILEGED")!=android.content.pm.PackageManager.PERMISSION_GRANTED))
            throw new IllegalStateException("Pair-before-input preparation requires Android 16+ shell identity access");
        boundDevice=findPairedHost();
        if(pairing.enabled()&&boundDevice!=null)
            throw new IllegalStateException("Clear the previous target pairing before preparing a new pairing window");
        if(!pairing.enabled()&&boundDevice==null)
            throw new IllegalStateException("The explicitly paired host is required");
        if(boundDevice!=null)acceptBlePeer(boundDevice);
        listener=new LocalServerSocket(socket);
        sender=new BleSender(this,adapter,true);
        JSONObject record=new JSONObject(config.toString()).put("protocol",ManagedBleProtocol.VERSION).put("pid",Process.myPid());
        record.put("boot_id",readFile(new File("/proc/sys/kernel/random/boot_id")).trim());
        File temporary=new File(REGISTRY.getPath()+"."+epoch+".tmp");
        try(FileOutputStream stream=new FileOutputStream(temporary)){stream.write(record.toString().getBytes(StandardCharsets.UTF_8));}
        Os.chmod(temporary.getPath(),0600);if(!temporary.renameTo(REGISTRY))throw new IOException("Owner registry replace failed");
        Thread accepting=new Thread(()->{
            while(!stopping)try {
                LocalSocket socket=listener.accept();
                Credentials credentials=socket.getPeerCredentials();
                if(credentials.getUid()!=0&&credentials.getUid()!=2000&&credentials.getUid()!=appUid){socket.close();continue;}
                try {clients.execute(()->handle(socket,credentials));}
                catch(RejectedExecutionException full){socket.close();}
            }catch(IOException failure){if(!stopping)handler.post(this::stop);}
        },"iatest-managed-ble-ipc");accepting.setDaemon(true);accepting.start();
        Runtime.getRuntime().addShutdownHook(new Thread(this::stop));
        handler.post(this::tick);
    }
    private void tick() {
        if(stopping)return;
        long now=SystemClock.elapsedRealtime();
        if(now>=until||pairing.expired(now)){stop();return;}
        if(now-lastRadioCheck>=1000) {
            lastRadioCheck=now;
            try{if(!adapter.isEnabled()){stop();return;}}
            catch(RuntimeException unavailable){stop();return;}
        }
        synchronized(inputLock){if(gate.takeRelease(SystemClock.elapsedRealtime()))release();}
        handler.postDelayed(this::tick,100);
    }
    private JSONObject state()throws Exception {
        long now=SystemClock.elapsedRealtime();BluetoothDevice current=peer;boolean verified=verifiedPeer(current);
        return new JSONObject().put("registered",registered).put("connected",verified)
            .put("experimental_digitizer",bleDigitizerEnabled()).put("digitizer_ready",sender!=null&&sender.digitizerReady())
            .put("host_address",host).put("input_ready",verified&&!stopping&&now<until&&gate.ready(now)&&sender!=null&&sender.ready(current))
            .put("ble_recovery",sender==null?"owner_wait":sender.recoveryStatus()).put("error",error)
            .put("ble_diagnostics",sender==null?new JSONObject():sender.diagnostics())
            .put("owner",new JSONObject().put("running",!stopping&&now<until).put("pid",Process.myPid()).put("epoch",epoch)
                .put("ui_lease_ready",gate.ready(now)).put("ui_pid",gate.uiPid())
                .put("pairing_state",pairing.phase(now)).put("pairing_remaining_seconds",pairing.remaining(now))
                .put("remaining_seconds",Math.max(0,(until-now)/1000)).put("heartbeat_limit_ms",ManagedBleGate.HEARTBEAT_MS));
    }
    private android.os.Binder createBinder() {
        return new android.os.Binder() {
            {attachInterface(null,ManagedBleProtocol.VERSION);}
            @Override protected boolean onTransact(int code,android.os.Parcel data,android.os.Parcel reply,int flags)throws android.os.RemoteException {
                if(code!=android.os.IBinder.FIRST_CALL_TRANSACTION)return super.onTransact(code,data,reply,flags);
                data.enforceInterface(ManagedBleProtocol.VERSION);
                int uid=android.os.Binder.getCallingUid(),pid=android.os.Binder.getCallingPid();
                try {
                    String text=data.readString();
                    if(text==null||text.length()>ManagedBleProtocol.MAX_FRAME)throw new IllegalArgumentException("Binder frame exceeds quota");
                    JSONObject request=new JSONObject(text);android.os.IBinder token=data.readStrongBinder();
                    JSONObject response;long identity=android.os.Binder.clearCallingIdentity();
                    try {
                        response=dispatch(request,uid,pid);
                        if("attach".equals(request.optString("op"))&&"acknowledged".equals(response.optString("status")))
                            bindUiDeath(token,uid,pid,request.getString("ui_epoch"));
                    } finally {android.os.Binder.restoreCallingIdentity(identity);}
                    String encoded=response.toString();
                    if(encoded.length()>ManagedBleProtocol.MAX_FRAME)throw new IllegalArgumentException("Binder response exceeds quota");
                    reply.writeNoException();reply.writeString(encoded);
                }catch(Exception failure){reply.writeException(new IllegalStateException("Managed Binder request failed: "+failure.getClass().getSimpleName()));}
                return true;
            }
        };
    }
    private void bindUiDeath(android.os.IBinder token,int uid,int pid,String generation)throws Exception {
        if(token==null)throw new SecurityException("UI lifetime token required");
        synchronized(inputLock) {
            if(!gate.matches(uid,pid,generation))throw new SecurityException("UI changed during attach");
            if(uiToken==token)return;
            if(uiToken!=null&&uiDeath!=null)uiToken.unlinkToDeath(uiDeath,0);
            uiToken=token;
            uiDeath=()->{synchronized(inputLock){try{gate.detach(uid,pid,generation);release();}catch(SecurityException ignored){}}};
            token.linkToDeath(uiDeath,0);
        }
    }
    private void publishBinder()throws Exception {
        android.os.Bundle extras=new android.os.Bundle();extras.putBinder("owner",ownerBinder);
        Object manager=Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
        Class<?> activityManager=Class.forName("android.app.IActivityManager");
        android.os.IBinder token=new android.os.Binder();
        Object holder=activityManager.getMethod("getContentProviderExternal",String.class,int.class,android.os.IBinder.class,String.class)
            .invoke(manager,ManagedBleProvider.AUTHORITY,0,token,"iatest-managed-ble");
        if(holder==null)throw new IllegalStateException("Protected bridge provider unavailable");
        try {
            Object provider=holder.getClass().getField("provider").get(holder);
            android.content.AttributionSource source=new android.content.AttributionSource.Builder(Process.myUid()).setPackageName("com.android.shell").build();
            android.os.Bundle result=(android.os.Bundle)Class.forName("android.content.IContentProvider")
                .getMethod("call",android.content.AttributionSource.class,String.class,String.class,String.class,android.os.Bundle.class)
                .invoke(provider,source,ManagedBleProvider.AUTHORITY,"publish",epoch,extras);
            if(result==null||!result.getBoolean("published"))throw new IllegalStateException("Owner publication not confirmed");
        } finally {
            activityManager.getMethod("removeContentProviderExternalAsUser",String.class,android.os.IBinder.class,int.class)
                .invoke(manager,ManagedBleProvider.AUTHORITY,token,0);
        }
    }
    private void handle(LocalSocket socket,Credentials credentials) {
        try(LocalSocket channel=socket) {
            channel.setSoTimeout(2500);
            JSONObject request=ManagedBleProtocol.read(new DataInputStream(channel.getInputStream()));
            ManagedBleProtocol.write(new DataOutputStream(channel.getOutputStream()),dispatch(request,credentials.getUid(),credentials.getPid()));
        }catch(Exception ignored){}
    }
    private JSONObject dispatch(JSONObject request,int uid,int pid)throws Exception {
            JSONObject reply=new JSONObject().put("protocol",ManagedBleProtocol.VERSION).put("owner_epoch",epoch).put("request_id",request.optString("request_id"));
            try {
                if(!ManagedBleProtocol.VERSION.equals(request.optString("protocol"))||!epoch.equals(request.optString("owner_epoch"))
                    ||!request.optString("request_id").matches("[a-f0-9]{32}"))throw new SecurityException("Owner request binding differs");
                String op=request.getString("op");
                if(op.equals("status")) {reply.put("state",state());}
                else if(op.equals("publish")) {
                    if(uid!=2000&&uid!=0)throw new SecurityException("Only ADB may publish ownership");
                    publishBinder();reply.put("published",true).put("state",state());
                }
                else if(op.equals("stop")) {
                    if(uid!=2000&&uid!=0)throw new SecurityException("Only the ADB controller can stop ownership");
                    reply.put("stopping",true);handler.postDelayed(this::stop,100);
                } else {
                    if(uid!=appUid)throw new SecurityException("Only the pinned bridge UI can request input");
                    if(stopping||SystemClock.elapsedRealtime()>=until)throw new IllegalStateException("Managed BLE ownership expired");
                    String generation=request.getString("ui_epoch");long now=SystemClock.elapsedRealtime();
                    if(op.equals("attach")) {
                        verifyApp();synchronized(inputLock){gate.attach(uid,pid,generation,now);if(gate.takeRelease(now))release();}
                    } else if(op.equals("heartbeat"))gate.heartbeat(uid,pid,generation,request.getBoolean("foreground"),request.getBoolean("active"),now);
                    else if(op.equals("detach")) {synchronized(inputLock){gate.detach(uid,pid,generation);release();}}
                    else synchronized(inputLock) {
                        if(gate.takeRelease(SystemClock.elapsedRealtime()))release();gate.requireInput(uid,pid,generation,SystemClock.elapsedRealtime());
                        if(op.equals("send")) {
                            if(!verifiedPeer(peer))throw new SecurityException("Bonded target identity required before input");
                            int id=request.getInt("report_id"),budget=request.getInt("budget_ms");byte[] data=Base64.decode(request.getString("data"),Base64.NO_WRAP);
                            if((id<1||id>4)||data.length!=(id==1?8:id==2?5:id==3?4:HidDigitizer.LENGTH)||budget<1||budget>1500)throw new IllegalArgumentException("Invalid HID report quota");
                            if(id==4&&(!bleDigitizerEnabled()||!sender.digitizerReady()||!HidDigitizer.valid(data)))throw new IllegalArgumentException("Digitizer endpoint not negotiated or report invalid");
                            String operation=request.getString("request_id"),fingerprint=id+":"+request.getString("data");
                            if(ledger.containsKey(operation)) {
                                if(!fingerprint.equals(fingerprints.get(operation)))throw new SecurityException("Report operation changed");
                                reply.put("sent",ledger.get(operation).getBoolean("sent"));
                            } else {
                                if(ledger.size()>=8192)throw new IllegalStateException("Owner report ledger exhausted; explicitly end ownership");
                                ledger.put(operation,new JSONObject().put("sent",false));fingerprints.put(operation,fingerprint);
                                int remaining=budget-(int)(SystemClock.elapsedRealtime()-now);
                                if(remaining<1)throw new IllegalStateException("Managed BLE dispatch deadline exceeded");
                                if(id==4)digitizer=data.clone();
                                boolean sent=sender.send(id,data,remaining);if(id==2)absolute=data.clone();
                                ledger.put(operation,new JSONObject().put("sent",sent));reply.put("sent",sent);
                            }
                        } else if(op.equals("connect")) {
                            BluetoothDevice target=findPairedHost();
                            if(target==null)throw new SecurityException("Pair the configured target before connecting");
                            sender.requestConnection(target);
                        }
                        else if(op.equals("reconnect")){release();sender.reconnect();}
                        else throw new IllegalArgumentException("Unknown managed BLE operation");
                    }
                    reply.put("state",state());
                }
                reply.put("status","acknowledged");
            } catch(Exception failure){reply.put("status","rejected").put("error",failure.getClass().getSimpleName()+": "+String.valueOf(failure.getMessage()));}

        return reply;
    }
    private void release() {
        if(sender==null||!verifiedPeer(peer)||!sender.ready(peer))return;
        byte[] position=absolute.clone();position[0]=0;
        boolean keyboard=sender.send(1,new byte[8],300),mouse=sender.send(3,new byte[4],300),pointer=sender.send(2,position,300);
        boolean touch=HidDigitizer.released(digitizer)||sender.send(4,HidDigitizer.release(digitizer),300);
        if(touch)digitizer=HidDigitizer.release(digitizer);
        if(!keyboard||!mouse||!pointer||!touch)error="Managed BLE key release unconfirmed";
    }
    private synchronized void stop() {
        if(stopping)return;stopping=true;
        // Expiry and explicit stop remain finite even if a Bluetooth Binder
        // call or cleanup gets stuck. This can terminate only this owner PID.
        terminateAt(SystemClock.elapsedRealtime()+3000);
        handler.removeCallbacksAndMessages(null);
        synchronized(inputLock){release();if(sender!=null)sender.close();}
        try {if(listener!=null)listener.close();}catch(IOException ignored){}
        clients.shutdownNow();
        try {
            JSONObject request=new JSONObject().put("protocol",ManagedBleProtocol.VERSION).put("op","managed_owner_stopped").put("owner_epoch",epoch);
            new ProcessBuilder("/system/bin/am","broadcast","--include-stopped-packages","-n",PACKAGE+"/.ControlReceiver","--es","managed_owner_stopped",
                Base64.encodeToString(request.toString().getBytes(StandardCharsets.UTF_8),Base64.NO_WRAP))
                .redirectOutput(new File("/dev/null")).redirectError(new File("/dev/null")).start().waitFor(2,TimeUnit.SECONDS);
        }catch(Exception ignored){}
        try {if(epoch.equals(new JSONObject(readFile(REGISTRY)).optString("owner_epoch")))REGISTRY.delete();}catch(Exception ignored){}
        handler.postDelayed(()->System.exit(0),50);
    }
    private void terminateAt(long deadline) {
        Thread watchdog=new Thread(()->{
            long remaining;
            while((remaining=deadline-SystemClock.elapsedRealtime())>0) {
                try{Thread.sleep(remaining);}catch(InterruptedException ignored){}
            }
            Process.killProcess(Process.myPid());
        },"iatest-managed-ble-expiry");watchdog.setDaemon(true);watchdog.start();
    }
    public Context bleContext(){return context;}
    public SharedPreferences blePreferences(){return preferences;}
    public boolean bleDiagnosticsEnabled(){return true;}
    public boolean bleDigitizerEnabled(){return config.optBoolean("experimental_digitizer",false);}
    private String identityOf(BluetoothDevice device) {
        if(android.os.Build.VERSION.SDK_INT<36
            ||context.checkSelfPermission("android.permission.BLUETOOTH_PRIVILEGED")!=android.content.pm.PackageManager.PERMISSION_GRANTED)return null;
        BluetoothDevice.BluetoothAddress identity=device.getIdentityAddressWithType();
        // Some stacks retain the identity address across a radio restart but
        // lose its address-type metadata. Match the privileged address value;
        // UNKNOWN type alone does not mean the returned address is absent.
        return identity.getAddress();
    }
    private BluetoothDevice findPairedHost() {
        for(BluetoothDevice device:adapter.getBondedDevices())
            if(pairing.matches(device.getAddress(),true,identityOf(device)))return device;
        return null;
    }
    private boolean verifiedPeer(BluetoothDevice device) {
        return device!=null&&pairing.verified(device.getAddress(),device.getBondState()==BluetoothDevice.BOND_BONDED);
    }
    public boolean acceptBlePeer(BluetoothDevice device) {
        long identity=android.os.Binder.clearCallingIdentity();
        try {
            long now=SystemClock.elapsedRealtime();boolean bonded=device.getBondState()==BluetoothDevice.BOND_BONDED;
            boolean accepted=pairing.accept(device.getAddress(),bonded,bonded?identityOf(device):null,now,gate.ready(now));
            if(accepted&&bonded)boundDevice=device;
            return accepted;
        } finally {android.os.Binder.restoreCallingIdentity(identity);}
    }
    public void bleState(BleTransport source,boolean enabled,BluetoothDevice device,String message){registered=enabled;peer=device;error=message;}
}
