"""Run actual Java trajectory dispatch with explicit platform boundary stubs."""

import subprocess
from pathlib import Path


def test_java_gesture_dispatch_and_cancel_after_rejection(tmp_path, java_bin):
    java = java_bin
    sources = {
        "android/os/SystemClock.java": """package android.os; public class SystemClock {
            public static long now;public static long uptimeMillis(){return now;}
            public static void sleep(long n){now+=n;}}""",
        "android/view/InputDevice.java": "package android.view;public class InputDevice {public static final int SOURCE_TOUCHSCREEN=4098;}",
        "android/view/MotionEvent.java": """package android.view;public class MotionEvent {
            public static final int ACTION_DOWN=0,ACTION_UP=1,ACTION_MOVE=2,ACTION_CANCEL=3,ACTION_POINTER_DOWN=5,ACTION_POINTER_UP=6,TOOL_TYPE_FINGER=1;
            public static int retained;public int action;public int[] ids;
            public static class PointerProperties {public int id,toolType;}
            public static class PointerCoords {public float x,y,pressure,size;}
            public static MotionEvent obtain(long d,long t,int a,int n,PointerProperties[] p,PointerCoords[] c,int m,int b,float xp,float yp,int device,int edge,int source,int flags){
                MotionEvent e=new MotionEvent();e.action=a;e.ids=new int[n];for(int i=0;i<n;i++)e.ids[i]=p[i].id;retained++;return e;}
            public void recycle(){retained--;}}""",
        "org/json/JSONObject.java": """package org.json;import java.util.*;public class JSONObject {
            Map<String,Object> v=new LinkedHashMap<>();public JSONObject put(String k,Object x){v.put(k,x);return this;}
            public int getInt(String k){return ((Number)v.get(k)).intValue();}public double getDouble(String k){return ((Number)v.get(k)).doubleValue();}
            public JSONObject getJSONObject(String k){return (JSONObject)v.get(k);}public JSONArray getJSONArray(String k){return (JSONArray)v.get(k);}
            public String toString(){return v.toString();}}""",
        "org/json/JSONArray.java": """package org.json;import java.util.*;public class JSONArray {
            List<Object> v=new ArrayList<>();public JSONArray put(Object x){v.add(x);return this;}public int length(){return v.size();}
            public JSONObject getJSONObject(int i){return (JSONObject)v.get(i);}public double getDouble(int i){return ((Number)v.get(i)).doubleValue();}
            public String toString(){return v.toString();}}""",
        "dev/iatest/mobile/InputMain.java": """package dev.iatest.mobile;import org.json.*;public class InputMain {
            static JSONObject display(){return new JSONObject().put("id",0).put("width",400).put("height",800).put("rotation",0);}}""",
        "dev/iatest/mobile/ControlServer.java": """package dev.iatest.mobile;import android.os.*;public class ControlServer {
            static long cancelAt=Long.MAX_VALUE;static void check(long deadline){if(SystemClock.now>=deadline||SystemClock.now>=cancelAt)throw new IllegalStateException("cancelled");}}""",
        "dev/iatest/mobile/GestureAcceptance.java": """package dev.iatest.mobile;import java.util.*;import org.json.*;import android.os.*;import android.view.*;
            public class GestureAcceptance {
            static void yes(boolean b){if(!b)throw new AssertionError();}
            static JSONObject point(int t,double x){return new JSONObject().put("t_ms",t).put("x",x).put("y",0.5);}
            static JSONObject contact(int id,int start,int end){return new JSONObject().put("id",id).put("points",new JSONArray().put(point(start,0.2)).put(point(end,0.8)));}
            static JSONObject args(JSONArray c){return new JSONObject().put("display",InputMain.display()).put("bounds",new JSONArray().put(10).put(10).put(200).put(200)).put("contacts",c);}
            public static void main(String[] unused)throws Exception {
                JSONArray c=new JSONArray().put(contact(7,0,100)).put(contact(2,50,150)).put(contact(9,100,200));
                List<String> boundaries=new ArrayList<>();GestureInput g=new GestureInput(e->{
                    int a=e.action&255;if(a!=2)boundaries.add(a+":"+e.ids[e.action>>8]);return true;});
                g.perform(args(c),10000);yes(boundaries.equals(Arrays.asList("0:7","5:2","6:7","5:9","6:2","1:9")));yes(MotionEvent.retained==0);
                List<Integer> actions=new ArrayList<>();SystemClock.now=0;
                GestureInput reject=new GestureInput(e->{actions.add(e.action&255);return (e.action&255)!=5;});
                try{reject.perform(args(c),10000);throw new AssertionError();}catch(IllegalStateException expected){}
                yes(actions.get(actions.size()-1)==3);yes(MotionEvent.retained==0);
                actions.clear();SystemClock.now=0;ControlServer.cancelAt=30;
                GestureInput cancel=new GestureInput(e->{actions.add(e.action&255);return true;});
                try{cancel.perform(args(c),10000);throw new AssertionError();}catch(IllegalStateException expected){}
                yes(actions.get(actions.size()-1)==3);yes(MotionEvent.retained==0);ControlServer.cancelAt=Long.MAX_VALUE;
                actions.clear();SystemClock.now=0;
                try{cancel.perform(args(new JSONArray().put(contact(1,0,20)).put(contact(1,0,30))),10000);throw new AssertionError();}catch(IllegalArgumentException expected){}
                yes(actions.isEmpty());System.out.println("native gesture policy passed; platform injection is stubbed");
            }}""",
    }
    paths = []
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        paths.append(path)
    source = Path(
        "iatest/mobile/native/android/src/dev/iatest/mobile/GestureInput.java"
    ).resolve()
    subprocess.run(
        [str(java / "javac"), "-d", str(tmp_path), *map(str, paths), str(source)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    result = subprocess.run(
        [
            str(java / "java"),
            "-cp",
            str(tmp_path),
            "dev.iatest.mobile.GestureAcceptance",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "native gesture policy passed" in result.stdout


def test_java_keys_use_fresh_times_and_cancel_release_on_rejection(tmp_path, java_bin):
    java = java_bin
    sources = {
        "android/os/SystemClock.java": """package android.os; public class SystemClock {
            public static long now=123456789;public static long uptimeMillis(){return now;}}""",
        "android/view/InputDevice.java": "package android.view;public class InputDevice {public static final int SOURCE_KEYBOARD=257;}",
        "android/view/KeyCharacterMap.java": "package android.view;public class KeyCharacterMap {public static final int VIRTUAL_KEYBOARD=-1;}",
        "android/view/KeyEvent.java": """package android.view;public class KeyEvent {
            public static final int ACTION_DOWN=0,ACTION_UP=1,FLAG_CANCELED=32;
            public long down,time;public int action,key,device,flags,source;
            public KeyEvent(long d,long t,int a,int k,int repeat,int meta,int dev,int scan,int f,int s){down=d;time=t;action=a;key=k;device=dev;flags=f;source=s;}}""",
        "dev/iatest/mobile/ControlServer.java": """package dev.iatest.mobile;public class ControlServer {
            static boolean cancelled;static void check(long deadline){if(cancelled)throw new IllegalStateException("cancelled");}}""",
        "dev/iatest/mobile/KeyAcceptance.java": """package dev.iatest.mobile;
            import java.util.*;import android.os.*;import android.view.*;
            public class KeyAcceptance {
            static void yes(boolean b){if(!b)throw new AssertionError();}
            public static void main(String[] unused)throws Exception {
                List<KeyEvent> events=new ArrayList<>();KeyInput good=new KeyInput(e->{events.add(e);SystemClock.now+=10;return true;});
                yes(good.press(3,Long.MAX_VALUE));yes(events.size()==2);
                yes(events.get(0).action==0&&events.get(1).action==1);
                yes(events.get(0).down==123456789&&events.get(0).time==123456789&&events.get(1).time==123456799);
                for(KeyEvent e:events)yes(e.source==257&&e.device==-1&&e.key==3&&e.flags==0&&e.down==123456789);
                good.release();yes(events.size()==2);
                events.clear();KeyInput reject=new KeyInput(e->{events.add(e);return e.action==0||e.flags==32;});
                yes(!reject.press(4,Long.MAX_VALUE));yes(events.size()==3&&events.get(2).flags==32);
                yes(events.stream().filter(e->e.action==0).count()==1);reject.release();yes(events.size()==3);
                events.clear();KeyInput cancel=new KeyInput(e->{events.add(e);ControlServer.cancelled=true;return true;});
                try{cancel.press(66,Long.MAX_VALUE);throw new AssertionError();}catch(IllegalStateException expected){}
                yes(events.size()==2&&events.get(1).action==1&&events.get(1).flags==32);
                events.clear();ControlServer.cancelled=false;
                try{good.press(301,Long.MAX_VALUE);throw new AssertionError();}catch(IllegalArgumentException expected){}
                yes(events.isEmpty());System.out.println("native key timing and cancelled release passed; injection is stubbed");
            }}""",
    }
    paths = []
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        paths.append(path)
    source = Path(
        "iatest/mobile/native/android/src/dev/iatest/mobile/KeyInput.java"
    ).resolve()
    subprocess.run(
        [str(java / "javac"), "-d", str(tmp_path), *map(str, paths), str(source)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    result = subprocess.run(
        [str(java / "java"), "-cp", str(tmp_path), "dev.iatest.mobile.KeyAcceptance"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "native key timing and cancelled release passed" in result.stdout
