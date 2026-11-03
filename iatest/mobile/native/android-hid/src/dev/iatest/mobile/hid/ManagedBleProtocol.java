package dev.iatest.mobile.hid;

import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import org.json.JSONObject;

final class ManagedBleProtocol {
    static final String VERSION="iatest-managed-ble/1";
    static final int MAX_FRAME=16384;
    static void binding(String socket,String epoch) {
        if(socket==null||!socket.matches("iatest_hid_owner_[a-f0-9]{32}")||epoch==null||!epoch.matches("[a-f0-9]{32}"))
            throw new IllegalArgumentException("Invalid managed BLE owner binding");
    }
    static JSONObject read(DataInputStream input)throws Exception {
        int length=input.readInt();
        if(length<1||length>MAX_FRAME)throw new IllegalArgumentException("Managed BLE frame exceeds quota");
        byte[] data=new byte[length];input.readFully(data);
        String text=StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(data)).toString();
        return new JSONObject(text);
    }
    static void write(DataOutputStream output,JSONObject value)throws Exception {
        byte[] data=value.toString().getBytes(StandardCharsets.UTF_8);
        if(data.length<1||data.length>MAX_FRAME)throw new IllegalArgumentException("Managed BLE frame exceeds quota");
        output.writeInt(data.length);output.write(data);output.flush();
    }
    static JSONObject request(String socket,String epoch,JSONObject request,int timeout)throws Exception {
        binding(socket,epoch);
        String id=UUID.randomUUID().toString().replace("-","");
        request.put("protocol",VERSION).put("owner_epoch",epoch).put("request_id",id);
        try(LocalSocket channel=new LocalSocket()) {
            channel.connect(new LocalSocketAddress(socket,LocalSocketAddress.Namespace.ABSTRACT));
            channel.setSoTimeout(timeout);
            if(channel.getPeerCredentials().getUid()!=2000)throw new SecurityException("ADB shell BLE owner required");
            write(new DataOutputStream(channel.getOutputStream()),request);
            JSONObject reply=read(new DataInputStream(channel.getInputStream()));
            if(!VERSION.equals(reply.optString("protocol"))||!epoch.equals(reply.optString("owner_epoch"))||!id.equals(reply.optString("request_id")))
                throw new SecurityException("Managed BLE response binding differs");
            if(!"acknowledged".equals(reply.optString("status")))throw new IllegalStateException(reply.optString("error","Managed BLE operation rejected"));
            return reply;
        }
    }
}
