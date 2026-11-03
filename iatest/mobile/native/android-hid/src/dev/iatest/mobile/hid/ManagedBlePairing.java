package dev.iatest.mobile.hid;

/** Pairing metadata may be exposed briefly; input always requires a bonded target. */
final class ManagedBlePairing {
    private final String host;
    private final boolean enabled;
    private final long until;
    private String address="";

    ManagedBlePairing(String expected,long now,int seconds) {
        if(expected==null||!expected.matches("(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
            ||seconds<0||seconds>300||(seconds>0&&seconds<5))
            throw new IllegalArgumentException("Bounded target pairing window required");
        host=expected;enabled=seconds>0;until=now+seconds*1000L;
    }
    boolean enabled(){return enabled;}
    synchronized boolean matches(String peer,boolean bonded,String identity) {
        return bonded&&(host.equals(peer)||host.equals(identity));
    }
    synchronized boolean accept(String peer,boolean bonded,String identity,long now,boolean foreground) {
        if(matches(peer,bonded,identity)) {
            if(expired(now))return false;
            address=peer;return true;
        }
        return !bonded&&enabled&&address.isEmpty()&&now<until&&foreground;
    }
    synchronized boolean verified(String peer,boolean bonded){return bonded&&!address.isEmpty()&&address.equals(peer);}
    synchronized boolean expired(long now){return enabled&&address.isEmpty()&&now>=until;}
    synchronized String phase(long now){return !address.isEmpty()?"bound":!enabled?"paired_host_required":expired(now)?"expired":"waiting";}
    synchronized long remaining(long now){return enabled&&address.isEmpty()?Math.max(0,(until-now)/1000):0;}
}
