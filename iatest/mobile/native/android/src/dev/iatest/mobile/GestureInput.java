package dev.iatest.mobile;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import org.json.*;
import java.util.*;

/** Complete, finite pointer trajectories; validates before any input and cancels on exit. */
final class GestureInput {
    interface Injector {boolean send(MotionEvent event) throws Exception;}
    private final Injector injector;
    private final LinkedHashMap<Integer,Contact> active=new LinkedHashMap<>();
    private long down;
    private int currentTime;
    GestureInput(Injector injector){this.injector=injector;}
    static final class Contact {
        int id;int[] times;float[] xs,ys;
        float at(float[] coordinates,int time){
            for(int i=1;i<times.length;i++)if(time<=times[i]){float k=Math.max(0,(time-times[i-1])/(float)(times[i]-times[i-1]));return coordinates[i-1]+(coordinates[i]-coordinates[i-1])*k;}
            return coordinates[coordinates.length-1];
        }
    }
    private boolean send(int action,int time) throws Exception {
        MotionEvent.PointerProperties[] properties=new MotionEvent.PointerProperties[active.size()];
        MotionEvent.PointerCoords[] coordinates=new MotionEvent.PointerCoords[active.size()];int i=0;
        for(Contact contact:active.values()){
            properties[i]=new MotionEvent.PointerProperties();properties[i].id=contact.id;properties[i].toolType=MotionEvent.TOOL_TYPE_FINGER;
            coordinates[i]=new MotionEvent.PointerCoords();coordinates[i].x=contact.at(contact.xs,time);coordinates[i].y=contact.at(contact.ys,time);coordinates[i].pressure=1;coordinates[i].size=1;i++;
        }
        MotionEvent event=MotionEvent.obtain(down,SystemClock.uptimeMillis(),action,active.size(),properties,coordinates,0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try{return injector.send(event);}finally{event.recycle();}
    }
    private void accepted(int action,int time) throws Exception {if(!send(action,time))throw new IllegalStateException("Gesture input rejected; cleanup required");}
    JSONObject perform(JSONObject args,long deadline) throws Exception {
        JSONArray bounds=args.getJSONArray("bounds"),values=args.getJSONArray("contacts");JSONObject display=InputMain.display(),expected=args.getJSONObject("display");
        for(String key:new String[]{"id","width","height","rotation"})if(display.getInt(key)!=expected.getInt(key))throw new IllegalStateException("Stale gesture display");
        double left=bounds.getDouble(0),top=bounds.getDouble(1),width=bounds.getDouble(2)-left,height=bounds.getDouble(3)-top;
        if(!Double.isFinite(left)||!Double.isFinite(top)||!Double.isFinite(width)||!Double.isFinite(height)||left<0||top<0||width<=1||height<=1||left+width>display.getInt("width")||top+height>display.getInt("height"))throw new IllegalArgumentException("Invalid gesture region");
        if(values.length()<1||values.length()>10||values.toString().getBytes("UTF-8").length>40000)throw new IllegalArgumentException("Gesture contact/wire budget exceeded");
        ArrayList<Contact> contacts=new ArrayList<>();Set<Integer> ids=new HashSet<>();TreeSet<Integer> schedule=new TreeSet<>();int end=0,start=5001,total=0;
        for(int i=0;i<values.length();i++){
            JSONObject value=values.getJSONObject(i);Contact c=new Contact();c.id=value.getInt("id");JSONArray points=value.getJSONArray("points");
            if(c.id<0||c.id>31||!ids.add(c.id)||points.length()<2||points.length()>128)throw new IllegalArgumentException("Invalid contact ID/path");
            total+=points.length();if(total>256)throw new IllegalArgumentException("Gesture point budget exceeded");
            c.times=new int[points.length()];c.xs=new float[points.length()];c.ys=new float[points.length()];
            for(int j=0;j<points.length();j++){
                JSONObject p=points.getJSONObject(j);int t=p.getInt("t_ms");double x=p.getDouble("x"),y=p.getDouble("y");
                if(t<0||t>5000||(j>0&&t<=c.times[j-1])||!Double.isFinite(x)||!Double.isFinite(y)||x<0||x>1||y<0||y>1)throw new IllegalArgumentException("Invalid normalized gesture point");
                c.times[j]=t;c.xs[j]=(float)(left+x*(width-0.5));c.ys[j]=(float)(top+y*(height-0.5));schedule.add(t);
            }
            start=Math.min(start,c.times[0]);end=Math.max(end,c.times[c.times.length-1]);contacts.add(c);
        }
        if(start!=0||SystemClock.uptimeMillis()+end>=deadline)throw new IllegalArgumentException("Gesture start/deadline is invalid");
        for(int t=0;t<=end;t+=16)schedule.add(t);down=SystemClock.uptimeMillis();int events=0;
        try{
            for(int t:schedule){long delay=down+t-SystemClock.uptimeMillis();if(delay>0)SystemClock.sleep(delay);ControlServer.check(deadline);currentTime=t;
                if(!active.isEmpty()){accepted(MotionEvent.ACTION_MOVE,t);events++;}
                for(Contact c:contacts)if(c.times[c.times.length-1]==t){
                    int index=new ArrayList<>(active.keySet()).indexOf(c.id);if(index<0)throw new IllegalStateException("Gesture contact state differs");
                    accepted(active.size()==1?MotionEvent.ACTION_UP:MotionEvent.ACTION_POINTER_UP|(index<<8),t);active.remove(c.id);events++;
                }
                for(Contact c:contacts)if(c.times[0]==t){active.put(c.id,c);accepted(active.size()==1?MotionEvent.ACTION_DOWN:MotionEvent.ACTION_POINTER_DOWN|((active.size()-1)<<8),t);events++;}
            }
            return new JSONObject().put("status","injection_call_returned").put("accepted",true).put("contacts",contacts.size()).put("events",events).put("postcondition_verified",false);
        }finally{release();}
    }
    void release(){try{if(!active.isEmpty())send(MotionEvent.ACTION_CANCEL,currentTime);}catch(Exception ignored){}finally{active.clear();}}
}
