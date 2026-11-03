package dev.iatest.mobile.hid;

/** Authenticated UI generation and expiring foreground lease. No Android APIs. */
final class ManagedBleGate {
    static final long HEARTBEAT_MS=3000;
    private final int uid;
    private int pid;
    private String epoch="";
    private long heartbeat;
    private boolean foreground,active,releasePending;

    ManagedBleGate(int appUid) {if(appUid<10000)throw new IllegalArgumentException("Application UID required");uid=appUid;}
    private void caller(int callerUid,int callerPid,String generation) {
        if(callerUid!=uid||callerPid!=pid||!epoch.equals(generation))throw new SecurityException("UI owner generation differs");
    }
    synchronized void attach(int callerUid,int callerPid,String generation,long now) {
        if(callerUid!=uid||callerPid<1||generation==null||!generation.matches("[a-f0-9]{32}"))
            throw new SecurityException("Invalid UI owner");
        if(pid==callerPid&&epoch.equals(generation))return;
        if(foreground&&active&&now-heartbeat<HEARTBEAT_MS)throw new IllegalStateException("Current UI lease is still active");
        releasePending|=foreground&&active;
        pid=callerPid;epoch=generation;foreground=false;active=false;heartbeat=now;
    }
    synchronized void heartbeat(int callerUid,int callerPid,String generation,boolean visible,boolean enabled,long now) {
        caller(callerUid,callerPid,generation);
        if(foreground&&active&&(!visible||!enabled))releasePending=true;
        foreground=visible;active=enabled;heartbeat=now;
    }
    synchronized void detach(int callerUid,int callerPid,String generation) {
        caller(callerUid,callerPid,generation);
        releasePending|=foreground&&active;foreground=false;active=false;pid=0;epoch="";
    }
    synchronized boolean ready(long now) {return foreground&&active&&now>=heartbeat&&now-heartbeat<HEARTBEAT_MS;}
    synchronized int uiPid() {return pid;}
    synchronized boolean matches(int callerUid,int callerPid,String generation) {return callerUid==uid&&callerPid==pid&&epoch.equals(generation);}
    synchronized void requireInput(int callerUid,int callerPid,String generation,long now) {
        caller(callerUid,callerPid,generation);
        if(!ready(now))throw new IllegalStateException("Foreground UI heartbeat required");
    }
    synchronized boolean takeRelease(long now) {
        if(foreground&&active&&!ready(now)) {foreground=false;active=false;releasePending=true;}
        boolean value=releasePending;releasePending=false;return value;
    }
}
