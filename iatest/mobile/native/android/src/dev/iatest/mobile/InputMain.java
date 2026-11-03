package dev.iatest.mobile;

import android.os.Looper;
import android.os.SystemClock;
import android.view.InputEvent;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import org.json.JSONObject;
import org.json.JSONArray;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.Map;

/** Input implementation informed by scrcpy v5.0.1; see THIRD_PARTY_NOTICES.md. */
public final class InputMain implements ControlServer.Backend {
    private Object manager;
    private Method inject;
    private boolean touching;
    private long downTime;
    private float lastX,lastY;
    private int pinchCount;
    private float pinchRadius;
    private final Map<Integer,Long> textHeldKeys=new HashMap<>();
    private final GestureInput gestures=new GestureInput(this::send);
    private final KeyInput keys=new KeyInput(this::send);
    static int displayId=0;
    private android.content.ClipboardManager clipboard;
    private android.content.ClipData originalClip;
    private String ownedClipHash;
    private boolean clipChanged;

    private android.content.ClipboardManager clipboard() throws Exception {
        if(clipboard==null){
            Object thread=Class.forName("android.app.ActivityThread").getMethod("systemMain").invoke(null);
            android.content.Context system=(android.content.Context)thread.getClass().getMethod("getSystemContext").invoke(thread);
            android.content.Context context=new android.content.ContextWrapper(system){
                @Override public String getPackageName(){return "com.android.shell";}
                @Override public String getOpPackageName(){return "com.android.shell";}
                @Override public String getAttributionTag(){return null;}
            };
            // A package context created from systemMain retains android as its
            // operation package; construct the service with the actual shell UID's
            // package identity instead. No target-App identity is impersonated.
            clipboard=android.content.ClipboardManager.class.getConstructor(android.content.Context.class,android.os.Handler.class)
                .newInstance(context,new android.os.Handler(Looper.getMainLooper()));
        }
        return clipboard;
    }
    private static String sha(String text)throws Exception{byte[] value=java.security.MessageDigest.getInstance("SHA-256").digest(text.getBytes("UTF-8"));StringBuilder out=new StringBuilder();for(byte b:value)out.append(String.format(java.util.Locale.ROOT,"%02x",b&255));return out.toString();}
    private String clipText(android.content.ClipData data){if(data==null)return "";if(data.getItemCount()!=1||data.getItemAt(0).getText()==null||data.getItemAt(0).getText().length()>16384)throw new IllegalStateException("Clipboard is not a bounded plain-text value");return data.getItemAt(0).getText().toString();}
    private boolean restoreClip()throws Exception {
        if(!clipChanged)return true;
        if(!sha(clipText(clipboard().getPrimaryClip())).equals(ownedClipHash))return false;
        if(originalClip==null){if(android.os.Build.VERSION.SDK_INT<28)return false;clipboard().clearPrimaryClip();}else clipboard().setPrimaryClip(originalClip);
        boolean restored=sha(clipText(clipboard().getPrimaryClip())).equals(sha(clipText(originalClip)));
        if(restored){clipChanged=false;originalClip=null;ownedClipHash=null;}return restored;
    }

    public static void main(String[] args) throws Exception {
        if(args.length!=4&&args.length!=5)throw new IllegalArgumentException("socket session token app [display] required");
        if(args.length==5){displayId=Integer.parseInt(args[4]);if(displayId<0)throw new IllegalArgumentException("Invalid display");}
        Looper.prepareMainLooper();
        InputMain backend=new InputMain();
        new ControlServer(backend,args[1],args[2],args[3]).serve(args[0]);
    }
    InputMain() throws Exception {
        Class<?> cls;
        try{cls=Class.forName("android.hardware.input.InputManagerGlobal");}
        catch(ClassNotFoundException e){cls=Class.forName("android.hardware.input.InputManager");}
        manager=cls.getMethod("getInstance").invoke(null);
        inject=cls.getMethod("injectInputEvent",InputEvent.class,int.class);
    }
    public JSONObject capabilities() throws Exception {
        return new JSONObject().put("component","android-input").put("actions",new JSONArray(new String[]{"tap","swipe","pinch","key","text","gesture","clipboard_set","clipboard_expect","clipboard_restore"}))
            .put("max_touch_points",10).put("gesture_types",new JSONArray(new String[]{"tap","swipe","pinch","trajectory"})).put("ime_composition",false)
            .put("input_modes",new JSONArray(new String[]{"sdk","key_events"})).put("display",display())
            .put("limitations",new JSONArray(new String[]{"Requires shell input permission; key_events does not provide arbitrary Unicode or IME composition"}));
    }
    static JSONObject display() throws Exception {
        Class<?> cls=Class.forName("android.hardware.display.DisplayManagerGlobal");
        Object dm=cls.getMethod("getInstance").invoke(null);
        Object info=cls.getMethod("getDisplayInfo",int.class).invoke(dm,displayId);
        if(info==null)throw new IllegalArgumentException("Selected display does not exist");
        return new JSONObject().put("id",displayId).put("width",info.getClass().getField("logicalWidth").getInt(info))
            .put("height",info.getClass().getField("logicalHeight").getInt(info)).put("rotation",info.getClass().getField("rotation").getInt(info));
    }
    private boolean send(InputEvent event) throws Exception {
        if(android.os.Build.VERSION.SDK_INT>=29)InputEvent.class.getMethod("setDisplayId",int.class).invoke(event,displayId);
        return (Boolean)inject.invoke(manager,event,1); // WAIT_FOR_RESULT: accepted by dispatcher, not business completion.
    }
    private boolean touch(int action,float x,float y) throws Exception {
        lastX=x;lastY=y;
        MotionEvent.PointerProperties pointer=new MotionEvent.PointerProperties();
        pointer.id=0;pointer.toolType=MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords coordinates=new MotionEvent.PointerCoords();
        coordinates.x=x;coordinates.y=y;coordinates.size=1;
        coordinates.pressure=(action==MotionEvent.ACTION_UP||action==MotionEvent.ACTION_CANCEL)?0:1;
        MotionEvent event=MotionEvent.obtain(downTime,SystemClock.uptimeMillis(),action,1,
            new MotionEvent.PointerProperties[]{pointer},new MotionEvent.PointerCoords[]{coordinates},
            0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try{return send(event);}finally{event.recycle();}
    }
    private boolean multiTouch(int action,int count,float x,float y,float radius) throws Exception {
        lastX=x;lastY=y;pinchRadius=radius;
        MotionEvent.PointerProperties[] properties=new MotionEvent.PointerProperties[count];
        MotionEvent.PointerCoords[] coordinates=new MotionEvent.PointerCoords[count];
        for(int i=0;i<count;i++){
            properties[i]=new MotionEvent.PointerProperties();properties[i].id=i;properties[i].toolType=MotionEvent.TOOL_TYPE_FINGER;
            coordinates[i]=new MotionEvent.PointerCoords();coordinates[i].x=x+(i==0?-radius:radius);coordinates[i].y=y;coordinates[i].pressure=1;coordinates[i].size=1;
        }
        MotionEvent event=MotionEvent.obtain(downTime,SystemClock.uptimeMillis(),action,count,properties,coordinates,0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try{return send(event);}finally{event.recycle();}
    }
    public JSONObject execute(String op,JSONObject args,long deadline) throws Exception {
        ControlServer.check(deadline);
        if("state".equals(op))return new JSONObject().put("display",display());
        if("clipboard_set".equals(op)){
            String value=args.getString("value");if(value.getBytes("UTF-8").length>16384)throw new IllegalArgumentException("Clipboard limit");
            if(!clipChanged){originalClip=clipboard().getPrimaryClip();clipText(originalClip);if(originalClip==null&&android.os.Build.VERSION.SDK_INT<28)throw new IllegalStateException("Cannot restore an empty clipboard on this API");}
            clipboard().setPrimaryClip(android.content.ClipData.newPlainText("IATest",value));clipChanged=true;ownedClipHash=sha(value);
            if(!ownedClipHash.equals(sha(clipText(clipboard().getPrimaryClip()))))throw new IllegalStateException("Clipboard set not verified");
            return new JSONObject().put("status","injection_call_returned").put("accepted",true).put("sha256",ownedClipHash).put("bytes",value.getBytes("UTF-8").length);
        }
        if("clipboard_expect".equals(op))return new JSONObject().put("matched",sha(clipText(clipboard().getPrimaryClip())).equals(args.getString("expected_sha256"))).put("postcondition_verified",true);
        if("clipboard_restore".equals(op))return new JSONObject().put("restored",restoreClip());
        boolean accepted=true;
        try {
            if("gesture".equals(op))return gestures.perform(args,deadline);
            else if("pinch".equals(op)){
                JSONObject expected=args.getJSONObject("display"),actual=display();
                for(String key:new String[]{"id","width","height","rotation"})if(expected.getInt(key)!=actual.getInt(key))throw new IllegalStateException("Stale display geometry");
                float x=(float)args.getDouble("x"),y=(float)args.getDouble("y"),radius=(float)args.getDouble("radius"),scale=(float)args.getDouble("scale");
                int duration=args.optInt("duration_ms",300);float maximum=radius*Math.max(1,scale);
                if(!Float.isFinite(x)||!Float.isFinite(y)||!Float.isFinite(radius)||!Float.isFinite(scale)||scale<0.1||scale>5||radius<=0||duration<50||duration>2000||x-maximum<0||x+maximum>=actual.getInt("width")||y<0||y>=actual.getInt("height"))throw new IllegalArgumentException("Invalid pinch geometry");
                downTime=SystemClock.uptimeMillis();pinchCount=1;accepted=multiTouch(MotionEvent.ACTION_DOWN,1,x,y,radius);
                if(!accepted)throw new IllegalStateException("First pinch touch rejected");
                pinchCount=2;accepted=multiTouch(MotionEvent.ACTION_POINTER_DOWN|(1<<MotionEvent.ACTION_POINTER_INDEX_SHIFT),2,x,y,radius);
                int moves=Math.max(2,duration/16);
                for(int i=1;i<=moves&&accepted;i++){ControlServer.check(deadline);SystemClock.sleep(duration/moves);accepted=multiTouch(MotionEvent.ACTION_MOVE,2,x,y,radius*(1+(scale-1)*i/moves));}
                boolean up=multiTouch(MotionEvent.ACTION_POINTER_UP|(1<<MotionEvent.ACTION_POINTER_INDEX_SHIFT),2,x,y,radius*scale);accepted&=up;if(up)pinchCount=1;else throw new IllegalStateException("Pinch pointer release unconfirmed");
                up=multiTouch(MotionEvent.ACTION_UP,1,x,y,radius*scale);accepted&=up;if(up)pinchCount=0;
            }else if("tap".equals(op)||"swipe".equals(op)){
                JSONObject expected=args.getJSONObject("display"), actual=display();
                for(String key:new String[]{"id","width","height","rotation"})
                    if(expected.getInt(key)!=actual.getInt(key))throw new IllegalStateException("Stale display geometry");
                float x=(float)args.getDouble("x"), y=(float)args.getDouble("y");
                if(!Float.isFinite(x)||!Float.isFinite(y)||x<0||y<0||x>=actual.getInt("width")||y>=actual.getInt("height"))
                    throw new IllegalArgumentException("Coordinates outside display");
                float endX=(float)args.optDouble("end_x",x),endY=(float)args.optDouble("end_y",y);
                if(endX<0||endY<0||endX>=actual.getInt("width")||endY>=actual.getInt("height"))throw new IllegalArgumentException("Invalid gesture endpoint");
                int duration=args.optInt("duration_ms",300);
                if(duration<50||duration>2000)throw new IllegalArgumentException("Gesture duration out of range");
                downTime=SystemClock.uptimeMillis();touching=true;accepted=touch(MotionEvent.ACTION_DOWN,x,y);
                if("swipe".equals(op)){
                    int moves=Math.max(2,duration/16);
                    for(int i=1;i<=moves && accepted;i++){
                        ControlServer.check(deadline);SystemClock.sleep(duration/moves);
                        accepted=touch(MotionEvent.ACTION_MOVE,x+(endX-x)*i/moves,y+(endY-y)*i/moves);
                    }
                }
                boolean upAccepted=touch(MotionEvent.ACTION_UP,endX,endY);accepted=upAccepted&&accepted;touching=!upAccepted;
            }else if("key".equals(op)){
                accepted=keys.press(args.getInt("keycode"),deadline);
            }else if("text".equals(op)){
                String text=args.getString("value");
                if(!"key_events".equals(args.optString("mode")) || text.getBytes("UTF-8").length>16384)
                    throw new IllegalArgumentException("Unsupported input mode or text limit");
                KeyEvent[] events=KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD).getEvents(text.toCharArray());
                if(events==null)throw new IllegalArgumentException("Text cannot be represented by this key map; select semantic mode explicitly");
                for(KeyEvent event:events){
                    ControlServer.check(deadline);
                    if(event.getAction()==KeyEvent.ACTION_DOWN)textHeldKeys.put(event.getKeyCode(),event.getDownTime());
                    boolean eventAccepted=send(event);accepted=eventAccepted&&accepted;
                    if(event.getAction()==KeyEvent.ACTION_UP&&eventAccepted)textHeldKeys.remove(event.getKeyCode());
                }
            }else throw new IllegalArgumentException("Unsupported input action: "+op);
            return new JSONObject().put("status",accepted?"injection_call_returned":"failed").put("accepted",accepted)
                .put("injection_mode","WAIT_FOR_RESULT").put("postcondition_verified",false);
        } finally {release();}
    }
    public void release(){
        gestures.release();keys.release();
        try{if(pinchCount>0)multiTouch(MotionEvent.ACTION_CANCEL,pinchCount,lastX,lastY,pinchRadius);}catch(Exception ignored){}finally{pinchCount=0;}
        try{if(touching)touch(MotionEvent.ACTION_CANCEL,lastX,lastY);}catch(Exception ignored){}finally{touching=false;}
        for(Map.Entry<Integer,Long> key:textHeldKeys.entrySet()){try{send(new KeyEvent(key.getValue(),SystemClock.uptimeMillis(),KeyEvent.ACTION_UP,key.getKey(),0,0,KeyCharacterMap.VIRTUAL_KEYBOARD,0,KeyEvent.FLAG_CANCELED,InputDevice.SOURCE_KEYBOARD));}catch(Exception ignored){}}
        textHeldKeys.clear();
    }
    public void sessionClosed()throws Exception{try{if(!restoreClip())throw new IllegalStateException("Clipboard changed externally; reconcile cleanup");}finally{release();}}
}
