package dev.iatest.mobile.hid;

/** Optional HOGP touchscreen reports. Host subscription is required separately. */
final class HidDigitizer {
    // Five parallel contacts keep the combined keyboard/mouse Report Map
    // within a GATT attribute's 512-byte limit (the USB map is larger).
    static final int INPUT=4,FEATURE=5,LENGTH=31,CONTACTS=5;
    private static byte[] hex(String value) {
        byte[] out=new byte[value.length()/2];
        for(int i=0;i<out.length;i++)out[i]=(byte)Integer.parseInt(value.substring(i*2,i*2+2),16);
        return out;
    }
    static byte[] descriptor() {
        String value="050d0904a1018504095415002505750895018102";
        String finger="050d0922a102094215002501750195018102750795018103095115002504750895018102050109300931150026ff7f751095028102c0";
        for(int i=0;i<CONTACTS;i++)value+=finger;
        return hex(value+"050d850509551500250575089501b102c0");
    }
    static boolean valid(byte[] data) {
        if(data==null||data.length!=LENGTH||(data[0]&255)>CONTACTS)return false;
        int mask=0,count=data[0]&255;
        for(int i=0;i<count;i++) {
            int offset=1+i*6,id=data[offset+1]&255;
            if((data[offset]&255)>1||id>=CONTACTS||(mask&(1<<id))!=0
                ||(data[offset+3]&128)!=0||(data[offset+5]&128)!=0)return false;
            mask|=1<<id;
        }
        for(int i=1+count*6;i<data.length;i++)if(data[i]!=0)return false;
        return true;
    }
    static boolean released(byte[] data) {
        if(!valid(data))return false;
        for(int i=0;i<(data[0]&255);i++)if(data[1+i*6]!=0)return false;
        return true;
    }
    static byte[] release(byte[] data) {
        if(!valid(data))throw new IllegalArgumentException("Invalid digitizer release state");
        byte[] out=data.clone();
        for(int i=0;i<(out[0]&255);i++)out[1+i*6]=0;
        return out;
    }
}
