package dev.iatest.lab;

import android.app.Activity;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.view.MotionEvent;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebView;
import android.widget.*;
import dev.iatest.bridge.IATestBridge;
import java.util.Collections;

/** Developer-owned fixture: observations only; no callable business action bridge. */
public final class MainActivity extends Activity {
    private final Handler handler = new Handler();
    private SharedPreferences preferences;
    private EditText nickname;
    private TextView status;
    private WebView web;
    private java.net.ServerSocket webServer;
    private int version;
    private final Runnable publish = new Runnable() {
        public void run() {
            try {
                IATestBridge.publish(MainActivity.this, Integer.toString(version),
                    Collections.singletonMap("lab_ready", true),
                    Collections.singletonMap("nickname", getPackageName()+":id/nickname_field"));
            } catch (Exception error) { android.util.Log.e("IATestLab", "Bridge publish failed", error); }
            handler.postDelayed(this, 250);
        }
    };
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private <T extends View> T identify(T view, String name) {
        view.setId(getResources().getIdentifier(name, "id", getPackageName()));
        return view;
    }
    private TextView label(String name, String text) {
        TextView view=identify(new TextView(this), name); view.setText(text); return view;
    }
    private Button button(String name, String text, View.OnClickListener action) {
        Button view=identify(new Button(this), name); view.setText(text); view.setOnClickListener(action); return view;
    }
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        preferences=getSharedPreferences("lab", MODE_PRIVATE);
        try { version=getPackageManager().getPackageInfo(getPackageName(),0).versionCode; }
        catch (Exception error) { throw new IllegalStateException(error); }
        nativePage(); handler.post(publish); networkProbe();
    }
    /** Explicit disposable-fixture probe, limited to two local test ports. */
    private void networkProbe() {
        String id=getIntent().getStringExtra("iatest_probe_id");
        if(id==null)return;
        String ip=getIntent().getStringExtra("iatest_probe_ip");
        int port=getIntent().getIntExtra("iatest_probe_port",0);
        if(!id.matches("[a-f0-9]{32}")||!("127.0.0.1".equals(ip)||"::1".equals(ip))||(port!=18750&&port!=18751))
            throw new IllegalArgumentException("Invalid owned network fixture probe");
        new Thread(()->{
            org.json.JSONObject result=new org.json.JSONObject();
            try {
                result.put("id",id).put("uid",android.os.Process.myUid()).put("pid",android.os.Process.myPid()).put("ip",ip).put("port",port);
                try(java.net.Socket socket=new java.net.Socket()) {
                    socket.connect(new java.net.InetSocketAddress(ip,port),2000);socket.setSoTimeout(2000);
                    java.io.ByteArrayOutputStream bytes=new java.io.ByteArrayOutputStream();
                    byte[] buffer=new byte[64];int count;
                    while((count=socket.getInputStream().read(buffer))!=-1){if(bytes.size()+count>256)throw new java.io.IOException("Probe response quota exceeded");bytes.write(buffer,0,count);}
                    result.put("response",new String(bytes.toByteArray(),java.nio.charset.StandardCharsets.UTF_8));
                }catch(java.io.IOException error){result.put("error",error.getClass().getSimpleName());}
                java.io.File pending=new java.io.File(getFilesDir(),"iatest-network-probe.tmp"),output=new java.io.File(getFilesDir(),"iatest-network-probe.json");
                try(java.io.FileOutputStream stream=new java.io.FileOutputStream(pending)){stream.write(result.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));}
                if(!pending.renameTo(output))throw new java.io.IOException("Probe result publication failed");
            }catch(Exception error){android.util.Log.e("IATestLab","Owned network fixture probe failed",error);}
        },"iatest-owned-network-probe").start();
    }
    private void nativePage() {
        LinearLayout root=new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(12),dp(30),dp(12),dp(20));
        root.addView(label("ready_label", "Ready")); root.addView(label("version_label", "Build "+version));
        nickname=identify(new EditText(this),"nickname_field"); nickname.setSingleLine(true);
        nickname.setHint("Nickname"); nickname.setText(preferences.getString("nickname","")); root.addView(nickname);
        status=label("saved_status",preferences.getString("nickname","").isEmpty()?"Not saved":"Saved: "+preferences.getString("nickname",""));
        LinearLayout row=new LinearLayout(this);
        row.addView(button("save_button","Save",v->{
            if(!preferences.edit().putString("nickname",nickname.getText().toString()).commit())throw new IllegalStateException("Fixture save failed");
            status.setText("Saved: "+nickname.getText());
            ((InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(nickname.getWindowToken(),0);
            nickname.clearFocus();
        }));
        row.addView(button("reset_button","Reset lab",v->{preferences.edit().remove("nickname").apply();nickname.setText("");status.setText("Not saved");}));
        root.addView(row); root.addView(status);
        root.addView(button("hybrid_button","Open WebView",v->hybridPage()));
        GesturePad pad=identify(new GesturePad(this),"gesture_pad"); root.addView(pad,new LinearLayout.LayoutParams(-1,dp(100)));
        TextView clip=label("clipboard_status","Clipboard not read");
        root.addView(button("clipboard_button","Read test clipboard",v->{
            ClipboardManager manager=(ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
            android.content.ClipData data=manager.getPrimaryClip();
            clip.setText(data==null?"Clipboard empty":"Clipboard: "+data.getItemAt(0).coerceToText(this));
        })); root.addView(clip);
        root.addView(button("jank_button","Animate with stalls",v->{
            TextView ready=(TextView)findViewById(getResources().getIdentifier("ready_label","id",getPackageName()));ready.setText("Animating");
            final int[] count={0}; handler.post(new Runnable(){public void run(){
                if(isFinishing())return;if(count[0]>=20){ready.setText("Ready");return;}
                pad.setTranslationX((count[0]++%2==0)?dp(8):0);
                android.os.SystemClock.sleep(70);handler.postDelayed(this,16);
            }});
        }));
        ListView list=identify(new ListView(this),"item_list");String[] items=new String[100];
        for(int i=0;i<items.length;i++)items[i]="Item "+(i+1);
        list.setAdapter(new ArrayAdapter<String>(this,android.R.layout.simple_list_item_1,items));
        root.addView(list,new LinearLayout.LayoutParams(-1,0,1));setContentView(root);
    }
    private void hybridPage() {
        WebView.setWebContentsDebuggingEnabled(true);
        web=identify(new WebView(this),"hybrid_webview");web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);
        setContentView(web);
        final String html=
            "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><style>body{padding:40px;font:20px sans-serif}input,button{font:inherit;padding:12px}</style>"+
            "<label>Nickname<input id='nickname'></label><button id='save'>Save</button><p id='status'>Not saved</p>"+
            "<script>const field=document.getElementById('nickname'),button=document.getElementById('save'),output=document.getElementById('status');field.value=localStorage.getItem('nickname')||'';button.onclick=()=>{localStorage.setItem('nickname',field.value);output.textContent=field.value}</script>";
        if(webServer==null)try{
            webServer=new java.net.ServerSocket(18739,2,java.net.InetAddress.getByName("127.0.0.1"));
            final java.net.ServerSocket server=webServer;
            Thread worker=new Thread(()->{
                while(!server.isClosed())try(java.net.Socket socket=server.accept()){
                    socket.setSoTimeout(2000);java.io.InputStream input=socket.getInputStream();
                    int state=0;for(int n=0;n<4096&&state<4;n++){int c=input.read();if(c<0)break;state=(c==(state%2==0?'\r':'\n'))?state+1:0;}
                    byte[] body=html.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                    java.io.OutputStream output=socket.getOutputStream();
                    output.write(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "+body.length+"\r\nConnection: close\r\n\r\n").getBytes(java.nio.charset.StandardCharsets.US_ASCII));
                    output.write(body);output.flush();
                }catch(java.io.IOException error){if(!server.isClosed())android.util.Log.e("IATestLab","Loopback fixture request failed",error);}
            },"iatest-lab-loopback");worker.setDaemon(true);worker.start();
        }catch(java.io.IOException error){throw new IllegalStateException("Owned fixture loopback port unavailable",error);}
        web.loadUrl("http://127.0.0.1:18739/android-lab/");
    }
    @Override public void onBackPressed() {
        if(web!=null){web.destroy();web=null;nativePage();}else super.onBackPressed();
    }
    @Override public void onDestroy(){handler.removeCallbacksAndMessages(null);if(web!=null)web.destroy();if(webServer!=null)try{webServer.close();}catch(java.io.IOException ignored){}super.onDestroy();}
    private final class GesturePad extends TextView {
        private int maxPointers, completed;
        GesturePad(Context context){super(context);setText("Gesture pad");setBackgroundColor(0xffddeeff);setContentDescription("contacts=0 completed=0");}
        @Override public boolean onTouchEvent(MotionEvent event){
            if(event.getActionMasked()==MotionEvent.ACTION_DOWN)maxPointers=0;
            maxPointers=Math.max(maxPointers,event.getPointerCount());
            if(event.getActionMasked()==MotionEvent.ACTION_UP)completed++;
            setContentDescription("contacts="+maxPointers+" completed="+completed);
            return true;
        }
    }
}
