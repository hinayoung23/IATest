"""Run the native recovery policy on a JVM; device acceptance covers BLE APIs."""

import subprocess
from pathlib import Path


def test_native_ble_recovery_requires_real_reconnect_and_fresh_subscriptions(tmp_path, java_bin):
    harness = tmp_path / "RecoveryAcceptance.java"
    harness.write_text('''package dev.iatest.mobile.hid;
        public class RecoveryAcceptance {
            static void check(boolean value) {if(!value)throw new AssertionError();}
            public static void main(String[] args) {
                BleRecovery r=new BleRecovery("host",0);
                check(r.matchesHost("host")&&!r.matchesHost("other"));
                r.connected("host",10);check(r.phase().equals("disconnect"));
                r.disconnected("other");r.connected("other",20);
                check(r.blocked());
                r.disconnected("host");check(r.phase().equals("connect")&&r.awaitingHost("host")&&!r.awaitingHost("other"));
                r.connected("host",1000);check(r.phase().equals("subscribe")&&!r.awaitingHost("host"));
                r.subscribed("host",1,true);r.subscribed("host",2,true);
                check(r.blocked());
                for(int i=1;i<=3;i++)r.subscribed("other",i,true);
                r.subscribed("host",0,true);r.subscribed("host",4,true);check(r.blocked());
                r.subscribed("host",1,true);r.subscribed("host",2,true);
                r.subscribed("host",2,false);r.subscribed("host",3,true);check(r.blocked());
                r.subscribed("host",2,true);check(!r.blocked());
                r.expire(20000);check(r.phase().equals("ready"));
                BleRecovery fresh=new BleRecovery("host",0);
                for(int i=1;i<=3;i++)fresh.subscribed("host",i,true);
                check(fresh.blocked()); // Persisted/local values before reconnect do not count.
                fresh.disconnected("host");fresh.connected("host",1000);
                for(int i=1;i<=3;i++)fresh.subscribed("other",i,true);
                check(fresh.blocked());
                for(int i=1;i<=3;i++)fresh.subscribed("host",i,true);
                check(!fresh.blocked());
                // Fresh writes on the recovered link must not be discarded or
                // followed by an unnecessary service change that tears HID down.
                BleRecovery first=new BleRecovery("host",0,true);
                check(first.blocked()&&!first.reconnectNeeded(2999));
                for(int i=1;i<=3;i++)first.subscribed("host",i,true);
                check(!first.blocked()&&!first.reconnectNeeded(3000));
                BleRecovery cached=new BleRecovery("host",0,true);
                cached.subscribed("host",1,true);check(cached.blocked());
                check(cached.reconnectNeeded(3000)&&!cached.reconnectNeeded(3001));
                check(cached.phase().equals("disconnect"));
                cached.disconnected("host");check(cached.awaitingHost("host"));
                cached.connected("host",3100);
                for(int i=2;i<=3;i++)cached.subscribed("host",i,true);
                check(cached.blocked()); // The write before reconnect is not fresh for the new link.
                cached.subscribed("host",1,true);check(!cached.blocked());
                BleRecovery late=new BleRecovery("host",0);
                late.disconnected("host");late.connected("host",10000);
                late.expire(12000);check(late.phase().equals("failed")&&late.blocked());
                check(late.matchesHost("host")&&!late.matchesHost("new-pairing"));
                check(!late.reconnectNeeded(20000)); // No retry or service mutation after expiry.
                for(int i=1;i<=3;i++)late.subscribed("host",i,true);
                check(!late.blocked()); // Explicit host rediscovery may recover after timeout.
                BleRecovery lost=new BleRecovery("host",0);
                lost.disconnected("host");lost.connected("host",100);
                lost.disconnected("host");check(lost.phase().equals("failed"));
                check(!lost.reconnectNeeded(5000));
                System.out.println("bounded recovery passed");
            }
        }''')
    source = Path("iatest/mobile/native/android-hid/src/dev/iatest/mobile/hid/BleRecovery.java").resolve()
    subprocess.run([str(java_bin / "javac"), "-d", str(tmp_path), str(source), str(harness)],
                   check=True, capture_output=True, timeout=15)
    result = subprocess.run([str(java_bin / "java"), "-cp", str(tmp_path),
                             "dev.iatest.mobile.hid.RecoveryAcceptance"],
                            check=True, capture_output=True, text=True, timeout=10)
    assert "bounded recovery passed" in result.stdout
