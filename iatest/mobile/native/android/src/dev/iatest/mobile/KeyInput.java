package dev.iatest.mobile;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;

/** Timestamped key pairs; a failed or interrupted press releases with cancellation. */
final class KeyInput {
    interface Injector { boolean send(KeyEvent event) throws Exception; }
    private final Injector injector;
    private int held=-1;
    private long down;
    KeyInput(Injector injector){this.injector=injector;}
    private KeyEvent event(int action,int flags){
        return new KeyEvent(down,SystemClock.uptimeMillis(),action,held,0,0,
            KeyCharacterMap.VIRTUAL_KEYBOARD,0,flags,InputDevice.SOURCE_KEYBOARD);
    }
    boolean press(int key,long deadline) throws Exception {
        if(key<0||key>300)throw new IllegalArgumentException("Invalid keycode");
        ControlServer.check(deadline);down=SystemClock.uptimeMillis();held=key;
        try {
            if(!injector.send(event(KeyEvent.ACTION_DOWN,0)))return false;
            ControlServer.check(deadline);
            boolean accepted=injector.send(event(KeyEvent.ACTION_UP,0));
            if(accepted)held=-1;
            return accepted;
        }finally{release();}
    }
    void release(){
        if(held<0)return;
        try{injector.send(event(KeyEvent.ACTION_UP,KeyEvent.FLAG_CANCELED));}
        catch(Exception ignored){}finally{held=-1;}
    }
}
