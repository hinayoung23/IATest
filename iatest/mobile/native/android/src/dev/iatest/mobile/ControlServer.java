package dev.iatest.mobile;

import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.SystemClock;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

/** Framework-owned framing/session/operation protocol, independent of scrcpy wire format. */
final class ControlServer {
    static final class BeforeDispatch extends IllegalStateException {
        BeforeDispatch(String message) { super(message); }
    }
    static final String VERSION = "iatest-mobile/1";
    static final int MAX_FRAME = 65536;
    interface Backend {
        JSONObject capabilities() throws Exception;
        JSONObject execute(String op, JSONObject arguments, long deadline) throws Exception;
        void release();
        default void sessionClosed() throws Exception {release();}
        default byte[] binary() { return null; }
    }
    private final Backend backend;
    private final String session, token, app, epoch = UUID.randomUUID().toString();
    private final Map<String, JSONObject> ledger = new LinkedHashMap<>();
    private final Map<String, String> hashes = new HashMap<>();
    private long lastSequence;
    private volatile boolean authenticated;
    private volatile LocalServerSocket activeListener;
    private volatile LocalSocket activeSocket;
    static final Set<String> ACTIONS = new HashSet<>(Arrays.asList("tap", "swipe", "pinch", "key", "text", "launch", "terminate", "fragment", "gesture", "ime", "clipboard_set", "clipboard_restore"));

    ControlServer(Backend backend, String session, String token, String app) {
        this.backend=backend; this.session=session; this.token=token; this.app=app;
        if (token.length()<32 || session.isEmpty() || app.isEmpty()) throw new IllegalArgumentException("Missing session credentials");
    }

    void serve(String name) throws Exception {
        try (LocalServerSocket listener = new LocalServerSocket(name)) {
            activeListener=listener;
            // A host crash before connect must not leave an indefinitely blocked helper.
            Thread startupTimeout = new Thread(() -> {
                try { Thread.sleep(60000); if (!authenticated) listener.close(); } catch (Exception ignored) { }
            });
            startupTimeout.setDaemon(true); startupTimeout.start();
            try (LocalSocket socket=listener.accept()) {
                activeSocket=socket;
                socket.setSoTimeout(60000);
                DataInputStream input=new DataInputStream(socket.getInputStream());
                DataOutputStream output=new DataOutputStream(socket.getOutputStream());
                while (true) {
                    int length;
                    try { length=input.readInt(); } catch (EOFException e) { break; }
                    if (length<=0 || length>MAX_FRAME) throw new IOException("Invalid control frame length");
                    byte[] bytes=new byte[length]; input.readFully(bytes);
                    String text=StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                        .decode(ByteBuffer.wrap(bytes)).toString();
                    JSONObject request=StrictJSON.parse(text);
                    JSONObject response=dispatch(request);
                    byte[] payload=response.toString().getBytes(StandardCharsets.UTF_8);
                    if (payload.length>MAX_FRAME) throw new IOException("Response exceeded control limit");
                    output.writeInt(payload.length); output.write(payload);
                    byte[] binary=backend.binary();
                    if (response.optInt("binary_length")>0 && binary!=null) output.write(binary);
                    output.flush();
                    if ("close".equals(request.optString("op"))) break;
                }
            }
        } finally { backend.sessionClosed(); }
    }
    void close(){
        try{if(activeSocket!=null)activeSocket.close();}catch(IOException ignored){}
        try{if(activeListener!=null)activeListener.close();}catch(IOException ignored){}
    }

    private JSONObject dispatch(JSONObject request) throws Exception {
        JSONObject result=new JSONObject();
        result.put("protocol",VERSION).put("session_id",session).put("driver_epoch",epoch)
            .put("sequence",request.optLong("sequence",-1));
        String op=request.optString("op"), id=request.optString("operation_id");
        try {
            if (!VERSION.equals(request.optString("protocol")) || !session.equals(request.optString("session_id")) ||
                !MessageDigest.isEqual(token.getBytes(StandardCharsets.UTF_8),request.optString("token").getBytes(StandardCharsets.UTF_8)) ||
                !app.equals(request.optString("app_id"))) throw new IllegalArgumentException("Session or target mismatch");
            long sequence=request.getLong("sequence");
            if (sequence<=lastSequence) throw new IllegalArgumentException("Sequence must increase");
            lastSequence=sequence;
            if (!authenticated && !"hello".equals(op)) throw new IllegalArgumentException("Handshake required");
            if (authenticated && !epoch.equals(request.optString("driver_epoch"))) throw new IllegalArgumentException("Stale driver epoch");
            JSONObject args=request.getJSONObject("arguments");
            int remaining=request.getInt("deadline_ms");
            if (remaining<1 || remaining>60000) throw new IllegalArgumentException("Invalid deadline");
            long deadline=SystemClock.uptimeMillis()+remaining;
            JSONObject value;
            if ("hello".equals(op)) { authenticated=true; value=backend.capabilities(); }
            else if ("close".equals(op)) { backend.sessionClosed(); value=new JSONObject().put("status","closed"); }
            else if ("ping".equals(op)) { value=new JSONObject().put("status","received"); }
            else {
                String fingerprint=op+":"+args.toString();
                if (ACTIONS.contains(op)) {
                    if (id.isEmpty() || id.length()>160) throw new IllegalArgumentException("Action operation_id required");
                    if (hashes.containsKey(id)) {
                        if (!hashes.get(id).equals(fingerprint)) throw new IllegalArgumentException("Operation ID reused with different payload");
                        value=new JSONObject(ledger.get(id).toString());
                        copy(value,result); return result;
                    }
                    if (ledger.size()>=1024) throw new IllegalArgumentException("Operation ledger exhausted; open a new session");
                    hashes.put(id,fingerprint);
                    ledger.put(id,new JSONObject().put("status","unknown").put("error","Operation did not complete"));
                }
                try { value=backend.execute(op,args,deadline); }
                catch (Exception e) {
                    boolean stale=e instanceof BeforeDispatch;
                    value=new JSONObject().put("status",ACTIONS.contains(op)&&!stale?"unknown":"rejected")
                        .put("error",e.getClass().getSimpleName()+": "+String.valueOf(e.getMessage()));
                    if(stale)value.put("dispatch_started",false).put("error_code","stale_observation");
                }
                if (ACTIONS.contains(op)) ledger.put(id,value);
            }
            copy(value,result);
            if (!result.has("status")) result.put("status","received");
        } catch (Exception e) { result.put("status","rejected").put("error",String.valueOf(e.getMessage())); }
        return result;
    }
    static void copy(JSONObject source,JSONObject destination) throws Exception {
        Iterator<String> keys=source.keys(); while(keys.hasNext()){String key=keys.next(); destination.put(key,source.get(key));}
    }
    static void check(long deadline) { if(SystemClock.uptimeMillis()>=deadline) throw new IllegalStateException("Deadline exceeded"); }

    /** Reject duplicate keys, non-JSON numbers, excessive nesting and trailing data. */
    static final class StrictJSON {
        private final String text; private int pos;
        private StrictJSON(String text){this.text=text;}
        static JSONObject parse(String text) throws Exception {
            StrictJSON parser=new StrictJSON(text); Object value=parser.value(0); parser.space();
            if(parser.pos!=text.length() || !(value instanceof JSONObject)) throw new IOException("Invalid JSON object");
            return (JSONObject)value;
        }
        void space(){while(pos<text.length() && " \r\n\t".indexOf(text.charAt(pos))>=0)pos++;}
        char peek() throws IOException {space(); if(pos>=text.length())throw new IOException("Truncated JSON"); return text.charAt(pos);}
        void expect(char c) throws IOException {if(peek()!=c)throw new IOException("Invalid JSON delimiter");pos++;}
        String string() throws Exception {
            expect('"'); int start=pos-1; boolean escape=false;
            while(pos<text.length()){
                char c=text.charAt(pos++); if(c<32)throw new IOException("Control character in string");
                if(!escape && c=='"') return (String)new JSONTokener(text.substring(start,pos)).nextValue();
                if(!escape && c=='\\')escape=true;else escape=false;
            } throw new IOException("Truncated JSON string");
        }
        Object value(int depth) throws Exception {
            if(depth>32)throw new IOException("JSON nesting limit"); char c=peek();
            if(c=='"')return string();
            if(c=='{'){
                pos++; JSONObject out=new JSONObject(); Set<String> seen=new HashSet<>();
                if(peek()=='}'){pos++;return out;}
                while(true){String key=string();if(!seen.add(key))throw new IOException("Duplicate JSON key");expect(':');out.put(key,value(depth+1));
                    c=peek();pos++;if(c=='}')return out;if(c!=',')throw new IOException("Invalid object");}
            }
            if(c=='['){pos++;JSONArray out=new JSONArray();if(peek()==']'){pos++;return out;}
                while(true){out.put(value(depth+1));c=peek();pos++;if(c==']')return out;if(c!=',')throw new IOException("Invalid array");}}
            int start=pos;while(pos<text.length() && ",]} \r\n\t".indexOf(text.charAt(pos))<0)pos++;
            String token=text.substring(start,pos);
            if("true".equals(token))return true;if("false".equals(token))return false;if("null".equals(token))return JSONObject.NULL;
            if(!token.matches("-?(0|[1-9][0-9]*)(\\.[0-9]+)?([eE][+-]?[0-9]+)?"))throw new IOException("Invalid JSON value");
            double number=Double.parseDouble(token);if(!Double.isFinite(number))throw new IOException("Non-finite number");
            return new JSONTokener(token).nextValue();
        }
    }
}
