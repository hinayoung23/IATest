package dev.iatest.mobile;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.graphics.Point;
import android.hardware.display.DisplayManager;
import android.view.Display;
import android.view.MotionEvent;
import android.view.InputDevice;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONObject;
import org.json.JSONArray;
import java.io.ByteArrayOutputStream;
import java.util.*;
import java.util.concurrent.atomic.AtomicLong;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;

public final class Probe extends Instrumentation implements ControlServer.Backend {
    private Bundle options;
    private UiAutomation automation;
    private final AtomicLong version=new AtomicLong();
    private final Map<String,AccessibilityNodeInfo> refs=new LinkedHashMap<>();
    private final Map<String,String> fingerprints=new HashMap<>();
    private byte[] image;
    private String app;
    private ControlServer mediaServer;
    private Thread mediaThread;
    private String mediaFormat="";
    private final Map<String,TargetRecipe> recipes=new LinkedHashMap<>();
    private long recipeHits;
    private final NodeCache nodeCache=new NodeCache();
    @Override public void onCreate(Bundle arguments){options=arguments;start();}
    @Override public void onStart(){
        Bundle result=new Bundle();
        try {
            app=options.getString("app");automation=getUiAutomation();InputMain.displayId=Integer.parseInt(options.getString("display_id","0"));
            AccessibilityServiceInfo info=automation.getServiceInfo();
            info.flags|=AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS|AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            automation.setServiceInfo(info);
            automation.setOnAccessibilityEventListener(event->{version.incrementAndGet();nodeCache.event(event);});
            new ControlServer(this,options.getString("session"),options.getString("token"),app).serve(options.getString("socket"));
        }catch(Exception e){result.putString("error",e.toString());}
        finally{release();finish(0,result);}
    }
    public JSONObject capabilities() throws Exception {
        return new JSONObject().put("component","android-semantic").put("actions",new JSONArray(new String[]{"query","outline","tap","text","screenshot","state","fragment","ime","reset_observation"})).put("tap_backends",new JSONArray(new String[]{"accessibility"}))
            .put("ime_composition",true).put("ime_backends",new JSONArray(new String[]{"input_connection"}))
            .put("ime_selected",Arrays.asList("dev.iatest.mobile/.IATestIme","dev.iatest.mobile/dev.iatest.mobile.IATestIme").contains(android.provider.Settings.Secure.getString(getContext().getContentResolver(),android.provider.Settings.Secure.DEFAULT_INPUT_METHOD)))
            .put("fragment_max_steps",8).put("fragment_deadline_ms",5000).put("fragment_input_modes",new JSONArray(new String[]{"semantic"}))
            .put("separate_png_media",true).put("media_max_fps",2).put("media_queue_frames",1)
            .put("query_plan_cache_entries",32).put("reuses_cached_node_state",false)
            .put("query_snapshot_attempts",3).put("query_deadline_ms",250)
            .put("query_max_deadline_ms",1000)
            .put("lifecycle_geometry_checks",true)
            .put("pointer_target_validation",true)
            .put("window_animation_sync",android.os.Build.VERSION.SDK_INT>=29)
            .put("selected_display_capture",android.os.Build.VERSION.SDK_INT>=29)
            .put("persistent_node_cache",true).put("node_cache_entries",16).put("node_cache_ttl_ms",500).put("cached_observations_prove_uniqueness",false)
            .put("input_modes",new JSONArray(new String[]{"semantic"})).put("continuous_video",true).put("video_codecs",new JSONArray(new String[]{"h264-annexb"}))
            .put("limitations",new JSONArray(new String[]{"Selected-display semantics need Android 11+; secondary-display pixels require the system mirror API and shell capture permission; bounded local traversal may be incomplete; IME composition is not semantic setText"}));
    }
    private AccessibilityNodeInfo root(){
        int selected=Integer.parseInt(options.getString("display_id","0"));
        if(selected!=0||android.os.Build.VERSION.SDK_INT>=30){
            if(android.os.Build.VERSION.SDK_INT<30)throw new IllegalStateException("Selected display semantics require Android 11+");
            List<android.view.accessibility.AccessibilityWindowInfo> windows=automation.getWindowsOnAllDisplays().get(selected);AccessibilityNodeInfo found=null;
            if(windows!=null)for(android.view.accessibility.AccessibilityWindowInfo window:windows){AccessibilityNodeInfo value=window.getRoot();if(value==null)continue;if(window.isFocused()&&app.equals(String.valueOf(value.getPackageName()))){if(found!=null){found.recycle();value.recycle();throw new IllegalStateException("Ambiguous selected display root");}found=value;}else value.recycle();}
            if(found==null)throw new IllegalStateException("App is not focused on selected display");return found;
        }
        AccessibilityNodeInfo root=automation.getRootInActiveWindow();
        if(root==null||!app.equals(String.valueOf(root.getPackageName())))throw new IllegalStateException("Target App is not foreground");
        return root;
    }
    private JSONObject display() throws Exception {
        DisplayManager manager=(DisplayManager)getTargetContext().getSystemService(android.content.Context.DISPLAY_SERVICE);
        int selected=Integer.parseInt(options.getString("display_id","0"));Display display=manager.getDisplay(selected);
        if(display==null)throw new IllegalStateException("Default display unavailable");
        Point size=new Point();display.getRealSize(size);
        return new JSONObject().put("id",selected).put("width",size.x).put("height",size.y).put("rotation",display.getRotation());
    }
    private void syncWindowGeometry() throws Exception {
        // Window transforms can change without accessibility content events.
        // Use the same WindowManager barrier as UiAutomation input, then drop stale nodes.
        if(android.os.Build.VERSION.SDK_INT>=29)automation.getClass().getMethod("syncInputTransactions").invoke(automation);
        if(android.os.Build.VERSION.SDK_INT>=34)automation.clearCache();
    }
    private static String string(CharSequence value){return value==null?"":value.toString();}
    private static String bounded(String value,int limit){
        if(value.length()<=limit)return value;
        if(Character.isHighSurrogate(value.charAt(limit-1)))limit--;
        return value.substring(0,limit);
    }
    private static final class TargetRecipe {
        final String id,text,label,type;final Rect bounds;
        TargetRecipe(JSONObject value)throws Exception{id=value.has("identifier")?value.optString("identifier"):null;text=value.has("text")?value.optString("text"):null;label=value.has("label")?value.optString("label"):null;type=value.has("type")?value.optString("type"):null;JSONArray b=value.optJSONArray("bounds");if(b!=null){if(b.length()!=4)throw new IllegalArgumentException("Invalid bounds");for(int i=0;i<4;i++){double v=b.getDouble(i);if(!Double.isFinite(v)||v!=Math.rint(v)||v < -32768||v > 32768)throw new IllegalArgumentException("Integer pixel bounds required");}bounds=new Rect(b.getInt(0),b.getInt(1),b.getInt(2),b.getInt(3));if(bounds.width()<=0||bounds.height()<=0)throw new IllegalArgumentException("Empty bounds");}else bounds=null;}
        boolean matches(AccessibilityNodeInfo node){
            Rect actual=new Rect();if(bounds!=null)node.getBoundsInScreen(actual);
            return (bounds==null||bounds.equals(actual))&&(id==null||id.equals(node.getViewIdResourceName()))&&(text==null||text.equals(string(node.getText())))&&(label==null||label.equals(string(node.getContentDescription())))&&(type==null||type.equals(string(node.getClassName())))&&node.isVisibleToUser();
        }
    }
    private TargetRecipe recipe(JSONObject target,boolean cache)throws Exception{
        if(!cache)return new TargetRecipe(target);
        String key=target.toString();TargetRecipe existing=recipes.get(key);if(existing!=null){recipeHits++;return existing;}
        if(recipes.size()>=32)recipes.remove(recipes.keySet().iterator().next());TargetRecipe value=new TargetRecipe(target);recipes.put(key,value);return value;
    }
    private static String fingerprint(AccessibilityNodeInfo node){
        Rect r=new Rect();node.getBoundsInScreen(r);
        String value=node.getWindowId()+":"+node.getViewIdResourceName()+":"+string(node.getText())+":"+string(node.getContentDescription())+":"+r;
        try{return Arrays.toString(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));}
        catch(java.security.NoSuchAlgorithmException e){throw new IllegalStateException(e);}
    }
    private JSONObject describe(AccessibilityNodeInfo node) throws Exception {
        Rect r=new Rect();node.getBoundsInScreen(r);
        String ref=UUID.randomUUID().toString();
        if(refs.size()>=64){String first=refs.keySet().iterator().next();refs.remove(first).recycle();fingerprints.remove(first);}
        refs.put(ref,AccessibilityNodeInfo.obtain(node));fingerprints.put(ref,fingerprint(node));
        String fullText=node.isPassword()?"[REDACTED]":string(node.getText());
        String fullLabel=string(node.getContentDescription());
        String visibleText=bounded(fullText,1000);JSONArray truncated=new JSONArray();
        if(fullText.length()>1000){truncated.put("text");truncated.put("value");}
        if(fullLabel.length()>500)truncated.put("label");
        return new JSONObject().put("ref",ref).put("identifier",node.getViewIdResourceName()).put("text",visibleText)
            .put("value",visibleText).put("secure",node.isPassword()).put("label",bounded(fullLabel,500)).put("type",string(node.getClassName())).put("truncated_attributes",truncated)
            .put("enabled",node.isEnabled()).put("selected",node.isSelected()).put("editable",node.isEditable()).put("focused",node.isFocused())
            .put("supports_accessibility_tap",node.getActionList().contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK))
            .put("bounds",new JSONArray(new int[]{r.left,r.top,r.right,r.bottom})).put("window_id",node.getWindowId());
    }
    private JSONObject outline(JSONObject args,long deadline) throws Exception {
        deadline=Math.min(deadline,SystemClock.uptimeMillis()+250);
        int maximum=args.optInt("max_nodes",512),limit=args.optInt("limit",12);
        if(maximum<1||maximum>1024||limit<1||limit>12)throw new IllegalArgumentException("Outline budget out of range");
        long before=version.get();ArrayList<AccessibilityNodeInfo> nodes=new ArrayList<>();ArrayList<AccessibilityNodeInfo> selected=new ArrayList<>();
        nodes.add(root());boolean complete=true;int visited=0;
        try {
            for(int i=0;i<nodes.size();i++){
                if(SystemClock.uptimeMillis()>=deadline){complete=false;break;}
                AccessibilityNodeInfo node=nodes.get(i);visited++;
                boolean labelled=!string(node.getText()).isEmpty()||!string(node.getContentDescription()).isEmpty();
                if(node.isVisibleToUser()&&(node.isClickable()||node.isLongClickable()||node.isScrollable()||node.isEditable()||labelled&&node.getChildCount()==0))selected.add(node);
                int children=node.getChildCount();
                for(int j=0;j<children;j++){
                    if(nodes.size()>=maximum||SystemClock.uptimeMillis()>=deadline){complete=false;break;}
                    AccessibilityNodeInfo child=node.getChild(j);if(child!=null)nodes.add(child);
                }
            }
            Collections.sort(selected,(a,b)->Integer.compare(outlineRank(a),outlineRank(b)));
            Rect viewport=null;int viewportCount=0;JSONArray regions=new JSONArray();
            for(AccessibilityNodeInfo node:nodes)if("android:id/content".equals(node.getViewIdResourceName())){viewport=new Rect();node.getBoundsInScreen(viewport);viewportCount++;}
            if(viewportCount!=1||viewport==null||viewport.width()<=0||viewport.height()<=0)viewport=null;
            if(viewport!=null)regions.put(new JSONObject().put("name","Application viewport").put("target",new JSONObject().put("identifier","android:id/content")));
            JSONArray controls=new JSONArray();
            for(AccessibilityNodeInfo node:selected){
                if(controls.length()>=limit||SystemClock.uptimeMillis()>=deadline){complete=false;break;}
                JSONObject row=describe(node),target=new JSONObject();String id=node.getViewIdResourceName(),label=string(node.getContentDescription()),text=node.isPassword()?"":string(node.getText());
                if(id!=null&&!id.isEmpty())target.put("identifier",id);
                else if(!label.isEmpty()&&label.length()<=500)target.put("type",string(node.getClassName())).put("label",label);
                else if(!text.isEmpty()&&text.length()<=500)target.put("type",string(node.getClassName())).put("text",text);
                else target.put("type",string(node.getClassName())).put("label","");
                boolean visual=(id==null||id.isEmpty())&&label.isEmpty()&&text.isEmpty();
                if(visual)target.put("bounds",row.getJSONArray("bounds"));
                row.remove("ref");row.remove("value");row.remove("window_id");row.put("target",target).put("visual_required",visual);
                if(visual&&viewport!=null){Rect rect=new Rect();node.getBoundsInScreen(rect);if(rect.intersect(viewport)&&rect.width()>0&&rect.height()>0)row.put("visual_region",new JSONObject().put("target",new JSONObject().put("identifier","android:id/content")).put("visual_crop",new JSONObject().put("x",(rect.left-viewport.left)/(double)viewport.width()).put("y",(rect.top-viewport.top)/(double)viewport.height()).put("width",rect.width()/(double)viewport.width()).put("height",rect.height()/(double)viewport.height())));}
                controls.put(row);
            }
            return new JSONObject().put("controls",controls).put("regions",regions).put("complete",complete&&before==version.get()).put("visited_nodes",visited)
                .put("app_id",app).put("display",display()).put("state_version",version.get()).put("proves_uniqueness",false)
                .put("evidence","Discovery only; actions and assertions require a fresh unique target query");
        }finally{for(AccessibilityNodeInfo node:nodes)node.recycle();}
    }
    private JSONObject stableQuery(JSONObject args,long deadline) throws Exception {
        deadline=Math.min(deadline,SystemClock.uptimeMillis()+Math.min(1000,Math.max(1,args.optInt("snapshot_budget_ms",250))));
        int maximum=args.optInt("max_nodes",1024),visited=0,attempts=0;
        boolean unknown=false;JSONObject result;
        do {
            JSONObject attempt=new JSONObject(args.toString()).put("max_nodes",maximum-visited);
            if(attempts>0&&args.getJSONObject("target").has("identifier"))attempt.put("bounded_id_retry",true);
            result=query(attempt,deadline);attempts++;
            visited+=result.optInt("visited_nodes",0);unknown|=result.optBoolean("visited_nodes_unknown");
            if(result.optBoolean("complete")||result.has("scope_error")||result.optBoolean("node_cache_hit")||visited>=maximum||attempts>=3||SystemClock.uptimeMillis()+8>=deadline)break;
            // Only repeat incomplete read-only snapshots, with shared time/node caps.
            SystemClock.sleep(8);ControlServer.check(deadline);
        }while(true);
        result.put("query_attempts",attempts);
        if(attempts>1)result.put("visited_nodes",visited).put("lookup_strategy",args.getJSONObject("target").has("identifier")?"bounded_id_retry":"bounded_snapshot_retry");
        if(unknown)result.put("visited_nodes_unknown",true);
        return result;
    }
    private int outlineRank(AccessibilityNodeInfo node){
        boolean labelled=!string(node.getViewIdResourceName()).isEmpty()||!string(node.getText()).isEmpty()||!string(node.getContentDescription()).isEmpty();
        return node.isEditable()?0:labelled&&(node.isClickable()||node.isScrollable())?1:labelled?2:3;
    }
    private JSONObject query(JSONObject args,long deadline) throws Exception {
        deadline=Math.min(deadline,SystemClock.uptimeMillis()+Math.min(1000,Math.max(1,args.optInt("snapshot_budget_ms",250))));
        JSONObject target=args.getJSONObject("target");
        TargetRecipe recipe=recipe(target,args.optBoolean("cache",true));
        int limit=args.optInt("limit",3), maxNodes=args.optInt("max_nodes",1024);
        if(limit<1||limit>8||maxNodes<1||maxNodes>8192)throw new IllegalArgumentException("Query budget out of range");
        if(!args.optBoolean("cache",true)&&android.os.Build.VERSION.SDK_INT>=34)automation.clearCache();
        long before=version.get();
        AccessibilityNodeInfo queryRoot=root();
        if(args.optBoolean("cache",true)){
            List<AccessibilityNodeInfo> cached=nodeCache.get(target,queryRoot.getWindowId());
            if(cached!=null){
                queryRoot.recycle();JSONArray candidates=new JSONArray();int count=0;
                try{for(AccessibilityNodeInfo n:cached)if(recipe.matches(n)){count++;if(candidates.length()<limit)candidates.put(describe(n));}}
                finally{for(AccessibilityNodeInfo n:cached)n.recycle();}
                return new JSONObject().put("candidates",candidates).put("match_count",count).put("complete",false).put("match_count_is_lower_bound",true)
                    .put("node_cache_hit",true).put("cache_evidence","advisory-only").put("state_version",version.get()).put("display",display()).put("visited_nodes",cached.size());
            }
        }
        if(target.has("container_identifier")){
            List<AccessibilityNodeInfo> containers=queryRoot.findAccessibilityNodeInfosByViewId(target.getString("container_identifier"));
            queryRoot.recycle();
            if(containers.size()!=1){for(AccessibilityNodeInfo n:containers)n.recycle();
                return new JSONObject().put("candidates",new JSONArray()).put("complete",false).put("match_count",0).put("scope_error","Container must be unique");}
            queryRoot=containers.get(0);
        }
        if(target.has("anchor")){
            JSONObject anchor=target.getJSONObject("anchor");List<AccessibilityNodeInfo> anchors=queryRoot.findAccessibilityNodeInfosByViewId(anchor.getString("identifier"));
            AccessibilityNodeInfo selected=null;int count=0;
            TargetRecipe anchorRecipe=recipe(anchor,args.optBoolean("cache",true));
            for(AccessibilityNodeInfo node:anchors){if(anchorRecipe.matches(node)){count++;selected=node;}}
            AccessibilityNodeInfo parent=count==1?selected.getParent():null;
            boolean inside=false;
            AccessibilityNodeInfo cursor=parent==null?null:AccessibilityNodeInfo.obtain(parent);
            try{
                for(int depth=0;cursor!=null&&depth<24&&SystemClock.uptimeMillis()<deadline;depth++){
                    if(cursor.equals(queryRoot)){inside=true;break;}
                    AccessibilityNodeInfo next=cursor.getParent();cursor.recycle();cursor=next;
                }
            }finally{if(cursor!=null)cursor.recycle();}
            if(!inside&&parent!=null){parent.recycle();parent=null;}
            for(AccessibilityNodeInfo n:anchors)n.recycle();queryRoot.recycle();
            if(parent==null)return new JSONObject().put("candidates",new JSONArray()).put("complete",false).put("match_count",0).put("scope_error","Anchor must identify one row");
            queryRoot=parent;
        }
        if(target.has("identifier")&&!args.optBoolean("bounded_id_retry",false)){
            List<AccessibilityNodeInfo> found=queryRoot.findAccessibilityNodeInfosByViewId(target.getString("identifier"));queryRoot.recycle();
            JSONArray candidates=new JSONArray();int count=0;
            try{
                ArrayList<AccessibilityNodeInfo> matched=new ArrayList<>();
                for(AccessibilityNodeInfo node:found)if(recipe.matches(node)){count++;if(candidates.length()<limit)candidates.put(describe(node));}
                long after=version.get();boolean complete=before==after&&SystemClock.uptimeMillis()<deadline;
                if(complete&&args.optBoolean("cache",true)){for(AccessibilityNodeInfo n:found)if(recipe.matches(n))matched.add(n);nodeCache.store(target,matched,found.isEmpty()?-1:found.get(0).getWindowId());}
                return new JSONObject().put("candidates",candidates).put("complete",complete).put("match_count",count)
                    .put("match_count_is_lower_bound",!complete).put("visited_nodes_unknown",true).put("lookup_strategy","platform_id")
                    .put("state_version",after).put("display",display()).put("app_id",app);
            }finally{for(AccessibilityNodeInfo n:found)n.recycle();}
        }
        ArrayList<AccessibilityNodeInfo> nodes=new ArrayList<>();ArrayList<Integer> parents=new ArrayList<>();
        nodes.add(queryRoot);parents.add(-1);boolean complete=true;
        try{
            for(int i=0;i<nodes.size();i++){
                if(SystemClock.uptimeMillis()>=deadline){complete=false;break;}
                AccessibilityNodeInfo node=nodes.get(i);
                int childCount=node.getChildCount();
                for(int j=0;j<childCount;j++){
                    if(nodes.size()>=maxNodes || SystemClock.uptimeMillis()>=deadline){complete=false;break;}
                    AccessibilityNodeInfo child=node.getChild(j);
                    if(child!=null){nodes.add(child);parents.add(i);}
                }
                if(!complete)break;
            }
            int scope=0;
            JSONArray candidates=new JSONArray();int matches=0;
            for(int i=0;i<nodes.size();i++)if(descendant(i,scope,parents)&&recipe.matches(nodes.get(i))){
                matches++;if(candidates.length()<limit)candidates.put(describe(nodes.get(i)));
            }
            long after=version.get();
            if(complete&&before==after&&args.optBoolean("cache",true)){
                ArrayList<AccessibilityNodeInfo> matched=new ArrayList<>();for(AccessibilityNodeInfo n:nodes)if(recipe.matches(n))matched.add(n);nodeCache.store(target,matched,queryRoot.getWindowId());
            }
            return new JSONObject().put("candidates",candidates).put("complete",complete&&before==after).put("match_count",matches)
                .put("match_count_is_lower_bound",!complete||before!=after).put("visited_nodes",nodes.size())
                .put("state_version",after).put("display",display()).put("app_id",app);
        }finally{for(AccessibilityNodeInfo n:nodes)n.recycle();}
    }
    private boolean descendant(int index,int scope,List<Integer> parents){
        while(index>=0){if(index==scope)return true;index=parents.get(index);}return false;
    }
    private JSONObject fragment(JSONObject args,long deadline) throws Exception {
        JSONArray steps=args.getJSONArray("steps"),rows=new JSONArray();
        if(steps.length()<2||steps.length()>8||!"expect".equals(steps.getJSONObject(steps.length()-1).optString("op")))throw new IllegalArgumentException("Fragment needs 2–8 steps and a final expectation");
        deadline=Math.min(deadline,SystemClock.uptimeMillis()+5000);
        int dispatched=0;
        try{
            for(int i=0;i<steps.length();i++){
                ControlServer.check(deadline);JSONObject step=steps.getJSONObject(i);String op=step.getString("op");
                if(!Arrays.asList("tap","text","expect").contains(op))throw new IllegalArgumentException("Unsupported fragment operation");
                JSONObject observation=query(new JSONObject().put("target",step.getJSONObject("target")).put("limit",2).put("max_nodes",512),deadline);
                if(!observation.optBoolean("complete")||observation.optBoolean("match_count_is_lower_bound"))throw new IllegalStateException("Fragment observation incomplete");
                int count=observation.getInt("match_count");
                if("expect".equals(op)){
                    String attribute=step.optString("attribute","exists");Object expected=step.get("expected"),actual;
                    if("exists".equals(attribute)){if(count>1)throw new IllegalStateException("Ambiguous fragment expectation");actual=count==1;}
                    else {if(count!=1)throw new IllegalStateException("Fragment target missing or ambiguous");
                        JSONObject node=observation.getJSONArray("candidates").getJSONObject(0);
                        if(node.optBoolean("secure")||node.getJSONArray("truncated_attributes").toString().contains("\""+attribute+"\""))throw new IllegalStateException("Secure or truncated fragment evidence");
                        actual=node.opt(attribute);
                    }
                    boolean passed=expected==JSONObject.NULL?actual==null||actual==JSONObject.NULL:expected.equals(actual);
                    rows.put(new JSONObject().put("index",i).put("op",op).put("passed",passed));
                    if(!passed)return new JSONObject().put("status","failed").put("rows",rows).put("dispatched",dispatched).put("postcondition_verified",false);
                    continue;
                }
                if(count!=1)throw new IllegalStateException("Fragment action target missing or ambiguous");
                JSONObject candidate=observation.getJSONArray("candidates").getJSONObject(0);
                String ref=candidate.getString("ref");AccessibilityNodeInfo node=refs.get(ref);
                AccessibilityNodeInfo active=root();active.recycle();
                if(node==null||!node.refresh()||!candidate.optBoolean("enabled")||!fingerprint(node).equals(fingerprints.get(ref))||version.get()!=observation.getLong("state_version"))throw new IllegalStateException("Fragment target became stale");
                boolean accepted;
                if("text".equals(op)){
                    JSONObject response=execute("text",new JSONObject().put("ref",ref).put("state_version",observation.getLong("state_version")).put("value",step.getString("value")).put("mode",step.getString("mode")),deadline);
                    accepted=response.optBoolean("accepted");
                }else{
                    Rect r=new Rect();node.getBoundsInScreen(r);if(r.isEmpty())throw new IllegalStateException("Empty fragment target");
                    long down=SystemClock.uptimeMillis();float x=r.exactCenterX(),y=r.exactCenterY();boolean touching=false;
                    dispatched++;
                    try{
                        MotionEvent event=MotionEvent.obtain(down,down,MotionEvent.ACTION_DOWN,x,y,0);event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
                        try{accepted=automation.injectInputEvent(event,true);touching=accepted;}finally{event.recycle();}
                        if(!accepted)throw new IllegalStateException("Touch rejected");
                        event=MotionEvent.obtain(down,SystemClock.uptimeMillis(),MotionEvent.ACTION_UP,x,y,0);event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
                        try{accepted=automation.injectInputEvent(event,true);touching=!accepted;}finally{event.recycle();}
                    }finally{if(touching){MotionEvent cancel=MotionEvent.obtain(down,SystemClock.uptimeMillis(),MotionEvent.ACTION_CANCEL,x,y,0);cancel.setSource(InputDevice.SOURCE_TOUCHSCREEN);try{automation.injectInputEvent(cancel,true);}finally{cancel.recycle();}}}
                }
                if("text".equals(op))dispatched++;
                if(!accepted)throw new IllegalStateException("Fragment input rejected");
                rows.put(new JSONObject().put("index",i).put("op",op).put("acknowledged",true));
            }
            return new JSONObject().put("status","injection_call_returned").put("accepted",true).put("rows",rows).put("dispatched",dispatched).put("postcondition_verified",true);
        }catch(Exception e){return new JSONObject().put("status","unknown").put("rows",rows).put("dispatched",dispatched).put("error",e.getClass().getSimpleName());}
    }
    public JSONObject execute(String op,JSONObject args,long deadline) throws Exception {
        if("permission_state".equals(op)){
            String permission=args.getString("permission"),target=args.optString("app_id",app);
            if(!permission.matches("android\\.permission\\.[A-Z_]+")||!target.matches("[A-Za-z][A-Za-z0-9_.]+"))throw new IllegalArgumentException("Invalid permission query");
            return new JSONObject().put("granted",getContext().getPackageManager().checkPermission(permission,target)==android.content.pm.PackageManager.PERMISSION_GRANTED);
        }
        image=null;ControlServer.check(deadline);
        if("media_open".equals(op)){
            String format=args.optString("codec","png");
            if(!"png".equals(format)&&!"h264".equals(format))throw new IllegalArgumentException("Unsupported media format");
            JSONObject limits=args.has("media_limits")?args.getJSONObject("media_limits"):new JSONObject();
            for(Iterator<String> keys=limits.keys();keys.hasNext();)if(!Arrays.asList("duration_seconds","byte_limit").contains(keys.next()))throw new IllegalArgumentException("Unknown media budget");
            Object seconds=limits.opt("duration_seconds"),bytes=limits.opt("byte_limit");
            if(seconds!=null&&!(seconds instanceof Integer)&&!(seconds instanceof Long)||bytes!=null&&!(bytes instanceof Integer)&&!(bytes instanceof Long))throw new IllegalArgumentException("Media budgets require integers");
            final MediaBudget budget=new MediaBudget(seconds==null?120:((Number)seconds).longValue(),bytes==null?67108864:((Number)bytes).longValue(),SystemClock.uptimeMillis());
            if(mediaThread!=null&&mediaThread.isAlive()){
                mediaServer.close();mediaThread.join(2000);if(mediaThread.isAlive())throw new IllegalStateException("Previous media channel did not stop");
            }
            mediaFormat=format;
            ControlServer.Backend backend;
            if("h264".equals(format))backend=new VideoEncoder(automation,()->{AccessibilityNodeInfo current=root();current.recycle();},getContext(),args.optString("capture_source","screenshot"),budget);
            else backend=new ControlServer.Backend(){
                private byte[] pixels;private long lastFrame;
                public JSONObject capabilities() throws Exception{return new JSONObject().put("component","android-png-media").put("continuous_video",false).put("max_fps",2).put("media_limits",new JSONObject().put("duration_seconds",budget.durationMs/1000).put("byte_limit",budget.byteLimit));}
                public JSONObject execute(String operation,JSONObject arguments,long end) throws Exception {
                    if(!"screenshot".equals(operation))throw new IllegalArgumentException("Read-only PNG media channel");
                    budget.check(SystemClock.uptimeMillis());
                    long interval=SystemClock.uptimeMillis()-lastFrame;if(interval<500)SystemClock.sleep(500-interval);ControlServer.check(end);
                    lastFrame=SystemClock.uptimeMillis();AccessibilityNodeInfo current=root();current.recycle();
                    Bitmap bitmap=DisplayCapture.capture(automation,getContext(),InputMain.displayId,end);
                    float ratio=Math.min(1f,720f/Math.max(bitmap.getWidth(),bitmap.getHeight()));int width=Math.max(1,Math.round(bitmap.getWidth()*ratio)),height=Math.max(1,Math.round(bitmap.getHeight()*ratio));
                    Bitmap scaled=Bitmap.createScaledBitmap(bitmap,width,height,true);
                    try{ByteArrayOutputStream bytes=new ByteArrayOutputStream();scaled.compress(Bitmap.CompressFormat.PNG,100,bytes);pixels=bytes.toByteArray();}
                    finally{if(scaled!=bitmap)scaled.recycle();bitmap.recycle();}
                    if(pixels.length>8388608)throw new IllegalStateException("Media packet quota exceeded");budget.charge(pixels.length,SystemClock.uptimeMillis());
                    return new JSONObject().put("binary_length",pixels.length).put("content_type","image/png").put("width",width).put("height",height).put("display_id",InputMain.displayId).put("capture_started_uptime_ms",lastFrame);
                }
                public byte[] binary(){byte[] value=pixels;pixels=null;return value;}
                public void release(){pixels=null;}
            };
            mediaServer=new ControlServer(backend,options.getString("session"),options.getString("token"),app);
            mediaThread=new Thread(()->{try{mediaServer.serve(options.getString("socket")+"_media");}catch(Exception ignored){}}, "iatest-png-media");
            mediaThread.setDaemon(true);mediaThread.start();return new JSONObject().put("status","received");
        }
        if("cache_clear".equals(op)){nodeCache.clear();return new JSONObject().put("node_cache_entries",0);}
        if("outline".equals(op))return outline(args,deadline);
        if("query".equals(op))return stableQuery(args,deadline).put("query_plan_cache_hits",recipeHits).put("node_cache_hits",nodeCache.hits).put("node_cache_invalidations",nodeCache.invalidations).put("node_cache_entries",nodeCache.size());
        if("fragment".equals(op))return fragment(args,deadline);
        if("ime".equals(op)){
            AccessibilityNodeInfo current=root();current.recycle();AccessibilityNodeInfo node=refs.get(args.getString("ref"));
            if(node==null||!node.refresh()||!node.isFocused()||!node.isEditable()||!fingerprints.get(args.getString("ref")).equals(fingerprint(node))||args.getLong("state_version")!=version.get())throw new ControlServer.BeforeDispatch("IME requires the same fresh focused editor");
            if(!"input_connection".equals(args.optString("ime_backend")))throw new IllegalArgumentException("Explicit InputConnection backend required");
            nodeCache.clear();return IATestIme.run(args,app,Math.min(deadline,SystemClock.uptimeMillis()+5000));
        }
        if("reset_observation".equals(op)){
            nodeCache.clear();for(AccessibilityNodeInfo node:refs.values())node.recycle();refs.clear();fingerprints.clear();
            if(android.os.Build.VERSION.SDK_INT>=34)automation.clearCache();
            return new JSONObject().put("cleared",true).put("state_version",version.incrementAndGet());
        }
        if("state".equals(op)){syncWindowGeometry();AccessibilityNodeInfo node=root();try{Rect rect=new Rect();node.getBoundsInScreen(rect);return new JSONObject().put("app_id",app).put("state_version",version.get()).put("display",display()).put("root_bounds",new JSONArray(new int[]{rect.left,rect.top,rect.right,rect.bottom}));}finally{node.recycle();}}
        if("pointer_validate".equals(op)){
            syncWindowGeometry();
            AccessibilityNodeInfo current=root();current.recycle();String ref=args.getString("ref");AccessibilityNodeInfo node=refs.get(ref);
            if(node==null||!node.refresh()||!node.isEnabled()||!fingerprint(node).equals(fingerprints.get(ref))||args.getLong("state_version")!=version.get())throw new ControlServer.BeforeDispatch("Pointer target changed before dispatch");
            JSONObject actual=display(),expected=args.getJSONObject("display");
            for(String key:new String[]{"id","width","height","rotation"})if(actual.getInt(key)!=expected.getInt(key))throw new ControlServer.BeforeDispatch("Pointer display changed before dispatch");
            Rect bounds=new Rect();node.getBoundsInScreen(bounds);
            if(bounds.isEmpty()||!bounds.contains((int)args.getDouble("x"),(int)args.getDouble("y")))throw new ControlServer.BeforeDispatch("Pointer point is outside target");
            return new JSONObject().put("validated",true);
        }
        if("tap".equals(op)){
            if(!"accessibility".equals(args.optString("tap_backend")))throw new IllegalArgumentException("Explicit accessibility tap backend required");
            AccessibilityNodeInfo current=root();current.recycle();AccessibilityNodeInfo node=refs.get(args.getString("ref"));
            if(node==null||!node.refresh()||!node.isEnabled()||!fingerprints.get(args.getString("ref")).equals(fingerprint(node)))throw new ControlServer.BeforeDispatch("Stale tap reference");
            if(args.getLong("state_version")!=version.get())throw new ControlServer.BeforeDispatch("State changed before accessibility tap");
            if(!node.getActionList().contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK))throw new IllegalArgumentException("Target does not advertise accessibility click");
            nodeCache.clear();boolean accepted=node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            return new JSONObject().put("status",accepted?"injection_call_returned":"failed").put("accepted",accepted).put("tap_backend","accessibility").put("postcondition_verified",false);
        }
        if("text".equals(op)){
            if(!"semantic".equals(args.optString("mode")))throw new IllegalArgumentException("Probe only supports semantic text");
            String text=args.getString("value");if(text.getBytes("UTF-8").length>16384)throw new IllegalArgumentException("Text limit");
            AccessibilityNodeInfo current=root();current.recycle();
            AccessibilityNodeInfo node=refs.get(args.getString("ref"));
            if(node==null||!node.refresh()||!fingerprints.get(args.getString("ref")).equals(fingerprint(node)))throw new ControlServer.BeforeDispatch("Stale reference");
            if(args.getLong("state_version")!=version.get())throw new ControlServer.BeforeDispatch("State changed before semantic input");
            Bundle bundle=new Bundle();bundle.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,text);
            boolean accepted=node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,bundle);
            node.refresh();
            return new JSONObject().put("status",accepted?"injection_call_returned":"failed").put("accepted",accepted)
                .put("input_mode","semantic").put("readback",node.isPassword()?"[REDACTED]":string(node.getText())).put("postcondition_verified",accepted&&text.equals(string(node.getText())));
        }
        if("screenshot".equals(op)){
            AccessibilityNodeInfo current=root();current.recycle();
            Bitmap bitmap=DisplayCapture.capture(automation,getContext(),InputMain.displayId,deadline);
            int fullWidth=bitmap.getWidth(),fullHeight=bitmap.getHeight();JSONObject screen=display();
            if(fullWidth!=screen.getInt("width")||fullHeight!=screen.getInt("height")){bitmap.recycle();throw new IllegalStateException("Screen rotated during capture");}
            int x=0,y=0,width=fullWidth,height=fullHeight;boolean region=args.has("region");
            if(region){JSONObject rect=args.getJSONObject("region");x=(int)Math.floor(rect.getDouble("x"));y=(int)Math.floor(rect.getDouble("y"));width=(int)Math.ceil(rect.getDouble("x")+rect.getDouble("width"))-x;height=(int)Math.ceil(rect.getDouble("y")+rect.getDouble("height"))-y;
                if(x<0||y<0||width<=0||height<=0||x+width>fullWidth||y+height>fullHeight){bitmap.recycle();throw new IllegalArgumentException("Visual region outside screenshot");}}
            Bitmap selected=region?Bitmap.createBitmap(bitmap,x,y,width,height):bitmap;
            float scale=Math.min(1f,(region?1024f:1280f)/Math.max(width,height));if(region)scale=Math.min(scale,(float)Math.sqrt(524288d/((double)width*height)));
            int outputWidth=Math.max(1,(int)Math.floor(width*scale)),outputHeight=Math.max(1,(int)Math.floor(height*scale));
            Bitmap scaled=Bitmap.createScaledBitmap(selected,outputWidth,outputHeight,true);
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();scaled.compress(Bitmap.CompressFormat.PNG,100,bytes);
            if(scaled!=selected)scaled.recycle();if(selected!=bitmap)selected.recycle();bitmap.recycle();image=bytes.toByteArray();
            if(image.length>(region?1048576:8388608))throw new IllegalStateException("Screenshot exceeds limit");
            return new JSONObject().put("binary_length",image.length).put("content_type","image/png").put("state_version",version.get()).put("display_id",InputMain.displayId)
                .put("width",outputWidth).put("height",outputHeight).put("coordinate_width",fullWidth).put("coordinate_height",fullHeight)
                .put("crop_rect",new JSONObject().put("x",x).put("y",y).put("width",width).put("height",height))
                .put("viewport",new JSONObject().put("width",fullWidth).put("height",fullHeight).put("dpr",1).put("rotation",screen.getInt("rotation"))).put("captured_uptime_ms",SystemClock.uptimeMillis());
        }
        throw new IllegalArgumentException("Unsupported probe operation: "+op);
    }
    public byte[] binary(){byte[] result=image;image=null;return result;}
    public void release(){if(mediaServer!=null)mediaServer.close();nodeCache.clear();for(AccessibilityNodeInfo n:refs.values())n.recycle();refs.clear();fingerprints.clear();recipes.clear();}
}
