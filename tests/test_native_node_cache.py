"""Run actual native cache ownership/expiry policy on a JVM with explicit Android stubs."""

import subprocess
from pathlib import Path


def test_android_node_cache_expiry_invalidation_bounds_and_advisory_gate(tmp_path, java_bin):
    java = java_bin
    sources = {
        "android/os/SystemClock.java": "package android.os; public class SystemClock {public static long now;public static long uptimeMillis(){return now;}}",
        "android/view/accessibility/AccessibilityNodeInfo.java": """package android.view.accessibility;
            public class AccessibilityNodeInfo {public static int retained;public String id; public int window=1;public boolean valid=true;public AccessibilityNodeInfo parent;boolean owned;
            public AccessibilityNodeInfo(String id){this.id=id;}public static AccessibilityNodeInfo obtain(AccessibilityNodeInfo n){AccessibilityNodeInfo c=new AccessibilityNodeInfo(n.id);c.window=n.window;c.parent=n.parent;c.valid=n.valid;c.owned=true;retained++;return c;}
            public boolean refresh(){return valid;}public int getWindowId(){return window;}public String getViewIdResourceName(){return id;}public AccessibilityNodeInfo getParent(){return parent==null?null:obtain(parent);}public void recycle(){if(owned){owned=false;retained--;}}} """,
        "android/view/accessibility/AccessibilityEvent.java": """package android.view.accessibility;public class AccessibilityEvent {
            public static final int TYPE_WINDOW_STATE_CHANGED=1,TYPE_WINDOWS_CHANGED=2;int type;AccessibilityNodeInfo source;
            public AccessibilityEvent(int t,AccessibilityNodeInfo n){type=t;source=n;}public int getEventType(){return type;}public int getWindowId(){return source==null?-1:source.window;}public AccessibilityNodeInfo getSource(){throw new AssertionError("Event invalidation must not fetch remote nodes");}}""",
        "org/json/JSONObject.java": """package org.json;import java.util.*;public class JSONObject {
            Map<String,String> values=new TreeMap<>();public JSONObject(){}public JSONObject(String s){for(String p:s.split(";")){String[] a=p.split("=",2);if(a.length==2)values.put(a[0],a[1]);}}
            public JSONObject put(String k,Object v){values.put(k,v.toString());return this;}public void remove(String k){values.remove(k);}public boolean has(String k){return values.containsKey(k);}public String optString(String k,String d){return values.getOrDefault(k,d);}public boolean optBoolean(String k,boolean d){return values.containsKey(k)?Boolean.parseBoolean(values.get(k)):d;}public String toString(){StringBuilder s=new StringBuilder();values.forEach((k,v)->s.append(k).append('=').append(v).append(';'));return s.toString();}}""",
        "dev/iatest/mobile/CacheAcceptance.java": """package dev.iatest.mobile;import java.util.*;import org.json.*;import android.os.*;import android.view.accessibility.*;
            public class CacheAcceptance {static void yes(boolean b){if(!b)throw new AssertionError();}public static void main(String[] args)throws Exception {
            NodeCache c=new NodeCache();AccessibilityNodeInfo region=new AccessibilityNodeInfo("region"),node=new AccessibilityNodeInfo("save");node.parent=region;
            JSONObject t=new JSONObject().put("identifier","save").put("container_identifier","region").put("require_unique",false);
            c.store(t,Arrays.asList(node),1);List<AccessibilityNodeInfo> hit=c.get(t,1);yes(hit.size()==1);hit.forEach(AccessibilityNodeInfo::recycle);
            t.put("require_unique",true);yes(c.get(t,1)==null);t.put("require_unique",false);
            AccessibilityNodeInfo other=new AccessibilityNodeInfo("other-region");other.window=2;
            c.event(new AccessibilityEvent(3,other));yes(c.size()==1);
            c.event(new AccessibilityEvent(3,node));yes(c.size()==0&&AccessibilityNodeInfo.retained==0);
            c.store(t,Arrays.asList(node),1);SystemClock.now=501;yes(c.get(t,1)==null&&AccessibilityNodeInfo.retained==0);
            for(int i=0;i<100;i++)c.store(new JSONObject().put("identifier","x"+i),Arrays.asList(node),1);yes(c.size()==16&&AccessibilityNodeInfo.retained==16);
            c.event(new AccessibilityEvent(AccessibilityEvent.TYPE_WINDOWS_CHANGED,null));yes(c.size()==0&&AccessibilityNodeInfo.retained==0);System.out.println("cache policy passed; Android API boundaries are stubs");}}
        """,
    }
    paths = []
    for name, content in sources.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        paths.append(p)
    source = Path(
        "iatest/mobile/native/android/src/dev/iatest/mobile/NodeCache.java"
    ).resolve()
    subprocess.run(
        [str(java / "javac"), "-d", str(tmp_path), *map(str, paths), str(source)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    value = subprocess.run(
        [str(java / "java"), "-cp", str(tmp_path), "dev.iatest.mobile.CacheAcceptance"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "cache policy passed" in value.stdout
