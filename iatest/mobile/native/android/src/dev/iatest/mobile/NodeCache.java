package dev.iatest.mobile;

import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONObject;
import java.util.*;

/** Bounded persistent handles for advisory regional observations, never uniqueness proof. */
final class NodeCache {
    private static final class Entry {
        long created;int window;String region;List<AccessibilityNodeInfo> nodes=new ArrayList<>();
        void close(){for(AccessibilityNodeInfo n:nodes)n.recycle();nodes.clear();}
    }
    private final LinkedHashMap<String,Entry> entries=new LinkedHashMap<>();
    long hits,invalidations;
    private String key(JSONObject target) throws Exception {JSONObject copy=new JSONObject(target.toString());copy.remove("require_unique");return copy.toString();}
    synchronized void store(JSONObject target,List<AccessibilityNodeInfo> nodes,int window) throws Exception {
        if(nodes.size()>8||target.has("anchor"))return;
        String key=key(target);Entry old=entries.remove(key);if(old!=null)old.close();
        while(entries.size()>=16){String first=entries.keySet().iterator().next();entries.remove(first).close();}
        Entry entry=new Entry();entry.created=SystemClock.uptimeMillis();entry.window=window;entry.region=target.optString("container_identifier","");
        for(AccessibilityNodeInfo n:nodes)entry.nodes.add(AccessibilityNodeInfo.obtain(n));entries.put(key,entry);
    }
    synchronized List<AccessibilityNodeInfo> get(JSONObject target,int window) throws Exception {
        if(target.optBoolean("require_unique",true)||target.has("anchor"))return null;
        String key=key(target);Entry entry=entries.get(key);if(entry==null)return null;
        boolean valid=entry.window==window&&SystemClock.uptimeMillis()-entry.created<=500;
        ArrayList<AccessibilityNodeInfo> result=new ArrayList<>();
        for(AccessibilityNodeInfo node:entry.nodes){
            if(!valid||!node.refresh()||node.getWindowId()!=window){valid=false;break;}
            if(!entry.region.isEmpty()){
                AccessibilityNodeInfo cursor=AccessibilityNodeInfo.obtain(node);boolean inside=false;
                try{for(int i=0;cursor!=null&&i<24;i++){if(entry.region.equals(cursor.getViewIdResourceName())){inside=true;break;}AccessibilityNodeInfo next=cursor.getParent();cursor.recycle();cursor=next;}}
                finally{if(cursor!=null)cursor.recycle();}if(!inside){valid=false;break;}
            }
            result.add(AccessibilityNodeInfo.obtain(node));
        }
        if(!valid){for(AccessibilityNodeInfo n:result)n.recycle();entries.remove(key).close();invalidations++;return null;}
        hits++;return result;
    }
    synchronized void event(AccessibilityEvent event){
        // Event callbacks must not fetch remote nodes/ancestors while holding the
        // cache lock: a destroyed window can block the platform RPC for seconds.
        // Invalidate the event's whole window conservatively using local metadata.
        int window=event.getWindowId(),type=event.getEventType();
        boolean all=window<0||type==AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED||type==AccessibilityEvent.TYPE_WINDOWS_CHANGED;
        Iterator<Entry> iterator=entries.values().iterator();
        while(iterator.hasNext()){Entry e=iterator.next();if(all||e.window==window){iterator.remove();e.close();invalidations++;}}
    }
    synchronized int size(){return entries.size();}
    synchronized void clear(){for(Entry e:entries.values())e.close();entries.clear();}
}
