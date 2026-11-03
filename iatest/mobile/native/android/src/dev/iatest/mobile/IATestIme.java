package dev.iatest.mobile;

import android.inputmethodservice.InputMethodService;
import android.view.inputmethod.*;
import android.os.*;
import android.widget.*;
import android.view.View;
import org.json.*;
import java.util.concurrent.*;

/** Opt-in development IME. Only operates the currently focused, declared App. */
public final class IATestIme extends InputMethodService {
    private static volatile IATestIme instance;
    private final Handler main=new Handler(Looper.getMainLooper());
    private String composing="";
    private JSONArray candidates=new JSONArray();
    private volatile int generation,markedStart=-1,markedEnd=-1;
    private LinearLayout view;
    @Override public void onCreate(){super.onCreate();instance=this;}
    @Override public void onStartInput(EditorInfo info,boolean restarting){super.onStartInput(info,restarting);generation++;composing="";markedStart=markedEnd=-1;}
    @Override public void onFinishInput(){generation++;composing="";markedStart=markedEnd=-1;super.onFinishInput();}
    @Override public void onDestroy(){instance=null;super.onDestroy();}
    @Override public void onUpdateSelection(int oldStart,int oldEnd,int newStart,int newEnd,int candidatesStart,int candidatesEnd){super.onUpdateSelection(oldStart,oldEnd,newStart,newEnd,candidatesStart,candidatesEnd);markedStart=candidatesStart;markedEnd=candidatesEnd;}
    @Override public View onCreateInputView(){view=new LinearLayout(this);view.setOrientation(LinearLayout.VERTICAL);render();return view;}
    private void render(){if(view==null)return;view.removeAllViews();TextView preedit=new TextView(this);preedit.setText(composing);view.addView(preedit);
        for(int i=0;i<candidates.length();i++){final String value=candidates.optString(i);Button button=new Button(this);button.setText(value);button.setOnClickListener(v->{InputConnection input=getCurrentInputConnection();if(input!=null&&input.commitText(value,1)){composing="";render();}});view.addView(button);}}
    private JSONObject event(JSONObject event,String app,int epoch) throws Exception {
        EditorInfo info=getCurrentInputEditorInfo();InputConnection input=getCurrentInputConnection();
        if(epoch!=generation||info==null||!app.equals(info.packageName)||input==null)throw new IllegalStateException("IME editor/App changed");
        String op=event.getString("op");boolean accepted=true;
        if("compose".equals(op)){String text=event.getString("text");if(text.getBytes("UTF-8").length>1024)throw new IllegalArgumentException("IME text limit");accepted=input.setComposingText(text,1);if(accepted){composing=text;candidates=event.optJSONArray("candidates");if(candidates==null)candidates=new JSONArray();render();}}
        else if("candidate".equals(op)){int index=event.getInt("index");if(index<0||index>=candidates.length())throw new IllegalArgumentException("IME candidate missing");accepted=input.commitText(candidates.getString(index),1);if(accepted){composing="";render();}}
        else if("commit".equals(op)){accepted=event.has("text")?input.commitText(event.getString("text"),1):input.finishComposingText();if(accepted){composing="";render();}}
        else if("cancel".equals(op)){accepted=input.setComposingText("",1)&&input.finishComposingText();if(accepted){composing="";render();}}
        else if("expect".equals(op)){
            boolean ok=!event.has("composing")||composing.equals(event.getString("composing"));
            if(event.has("marked"))ok&=event.getBoolean("marked")== (markedStart>=0&&markedEnd>markedStart);
            if(event.has("before_cursor")){String expected=event.getString("before_cursor");CharSequence actual=input.getTextBeforeCursor(Math.max(1,expected.length()),0);ok&=actual!=null&&(expected.isEmpty()?actual.length()==0:expected.contentEquals(actual));}
            return new JSONObject().put("op",op).put("passed",ok).put("marked_observed",markedStart>=0&&markedEnd>markedStart);
        }else throw new IllegalArgumentException("Unknown IME operation");
        if(!accepted)throw new IllegalStateException("InputConnection rejected IME operation");
        return new JSONObject().put("op",op).put("accepted",true).put("composing_length",composing.length());
    }
    static JSONObject run(JSONObject args,String app,long deadline) throws Exception {
        IATestIme ime=instance;if(ime==null)throw new IllegalStateException("Framework IME must be selected and target field focused");
        JSONArray events=args.getJSONArray("ime_events"),rows=new JSONArray();if(events.length()<2||events.length()>24||!"expect".equals(events.getJSONObject(events.length()-1).optString("op")))throw new IllegalArgumentException("IME sequence bounds/final expectation invalid");
        int epoch=ime.generation;
        try{
            for(int i=0;i<events.length();i++){
                JSONObject event=events.getJSONObject(i),result;long until=Math.min(deadline,SystemClock.uptimeMillis()+1000);
                do {ControlServer.check(deadline);FutureTask<JSONObject> task=new FutureTask<>(()->ime.event(event,app,epoch));ime.main.post(task);result=task.get(Math.max(1,deadline-SystemClock.uptimeMillis()),TimeUnit.MILLISECONDS);
                    if(!"expect".equals(event.getString("op"))||result.optBoolean("passed")||SystemClock.uptimeMillis()>=until)break;SystemClock.sleep(20);
                }while(true);
                rows.put(result);if("expect".equals(event.getString("op"))&&!result.optBoolean("passed"))return new JSONObject().put("status","failed").put("rows",rows).put("postcondition_verified",false);
            }
            return new JSONObject().put("status","injection_call_returned").put("accepted",true).put("rows",rows).put("postcondition_verified",true).put("input_mode","input_connection");
        }finally{
            if(epoch==ime.generation&&!ime.composing.isEmpty()){
                FutureTask<Boolean> cleanup=new FutureTask<>(()->{InputConnection input=ime.getCurrentInputConnection();if(epoch!=ime.generation||input==null)return false;boolean ok=input.setComposingText("",1)&&input.finishComposingText();if(ok)ime.composing="";return ok;});ime.main.post(cleanup);if(!cleanup.get(1,TimeUnit.SECONDS))throw new IllegalStateException("IME composing cleanup unconfirmed");
            }
        }
    }
}
