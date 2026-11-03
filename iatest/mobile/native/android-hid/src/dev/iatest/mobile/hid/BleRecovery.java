package dev.iatest.mobile.hid;

/** One bounded cache recovery, bound to the original host. Never sends input. */
final class BleRecovery {
    private final String host;
    private final long deadline;
    private String phase="disconnect";
    private long discoverUntil;
    private int liveReports;

    BleRecovery(String address,long now) {host=address;deadline=now+12000;}
    BleRecovery(String address,long now,boolean discoverFirst) {
        this(address,now);
        if(discoverFirst) {phase="discover";discoverUntil=now+3000;}
    }
    synchronized boolean reconnectNeeded(long now) {
        expire(now);
        if(!phase.equals("discover")||now<discoverUntil)return false;
        phase="disconnect";liveReports=0;return true;
    }
    synchronized void disconnected(String address) {
        if(!host.equals(address))return;
        liveReports=0;
        if(phase.equals("disconnect")||phase.equals("discover"))phase="connect";
        else if(!phase.equals("ready"))phase="failed";
    }
    synchronized void connected(String address,long now) {
        if(host.equals(address)&&phase.equals("connect"))phase="subscribe";
    }
    synchronized void subscribed(String address,int id,boolean enabled) {
        if(!host.equals(address)||id<1||id>3||(!phase.equals("discover")&&!phase.equals("subscribe")&&!phase.equals("failed")))return;
        if(enabled)liveReports|=1<<(id-1);else liveReports&=~(1<<(id-1));
        if(liveReports==7)phase="ready";
    }
    synchronized void expire(long now) {if(now>=deadline&&!phase.equals("ready"))phase="failed";}
    synchronized void fail() {phase="failed";liveReports=0;}
    synchronized String phase() {return phase;}
    synchronized boolean blocked() {return !phase.equals("ready");}
    synchronized boolean awaitingHost(String address) {return host.equals(address)&&phase.equals("connect");}
    boolean matchesHost(String address) {return host.equals(address);}
}
