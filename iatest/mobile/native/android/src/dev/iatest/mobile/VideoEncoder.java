package dev.iatest.mobile;

import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.opengl.*;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.Surface;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.nio.*;
import java.util.UUID;
import java.util.Arrays;

/** Framework-owned screenshot-to-surface H264 encoder. No hidden display API. */
final class VideoEncoder implements ControlServer.Backend {
    private final UiAutomation automation;
    private final Runnable foreground;
    private android.content.Context captureContext;
    private boolean mirror;
    private android.hardware.display.VirtualDisplay virtualDisplay;
    private MediaCodec codec;
    private Surface input;
    private EGLDisplay egl;
    private EGLContext context;
    private EGLSurface surface;
    private int program,texture,width,height,sourceWidth,sourceHeight,rotation;
    private byte[] packet,configuration;
    private String generation;
    private long lastCapture;
    private MediaBudget budget=new MediaBudget(120,67108864,SystemClock.uptimeMillis());
    private final FloatBuffer vertices=ByteBuffer.allocateDirect(32).order(ByteOrder.nativeOrder()).asFloatBuffer();
    private final FloatBuffer coordinates=ByteBuffer.allocateDirect(32).order(ByteOrder.nativeOrder()).asFloatBuffer();
    VideoEncoder(UiAutomation automation,Runnable foreground){
        this.automation=automation;this.foreground=foreground;
        vertices.put(new float[]{-1,-1,1,-1,-1,1,1,1}).position(0);
        coordinates.put(new float[]{0,1,1,1,0,0,1,0}).position(0);
    }
    VideoEncoder(UiAutomation automation,Runnable foreground,android.content.Context context,String source){this(automation,foreground);if(!Arrays.asList("screenshot","mirror").contains(source))throw new IllegalArgumentException("Unknown capture source");this.captureContext=context;this.mirror="mirror".equals(source);}
    VideoEncoder(UiAutomation automation,Runnable foreground,android.content.Context context,String source,MediaBudget limits){this(automation,foreground,context,source);budget=limits;}
    public JSONObject capabilities() throws Exception {
        return new JSONObject().put("component","android-h264-media").put("continuous_video",true)
            .put("codec","h264-annexb").put("max_fps",15).put("max_edge",720).put("bitrate",2000000)
            .put("media_limits",new JSONObject().put("duration_seconds",budget.durationMs/1000).put("byte_limit",budget.byteLimit));
    }
    private static int shader(int type,String source){
        int id=GLES20.glCreateShader(type);GLES20.glShaderSource(id,source);GLES20.glCompileShader(id);
        int[] ok=new int[1];GLES20.glGetShaderiv(id,GLES20.GL_COMPILE_STATUS,ok,0);
        if(ok[0]==0)throw new IllegalStateException("Video shader compilation failed");return id;
    }
    private void configure(int w,int h,int r) throws Exception {
        releaseCodec();sourceWidth=w;sourceHeight=h;rotation=r;
        double ratio=Math.min(1d,720d/Math.max(w,h));width=Math.max(2,((int)(w*ratio)/2)*2);height=Math.max(2,((int)(h*ratio)/2)*2);
        MediaFormat format=MediaFormat.createVideoFormat("video/avc",width,height);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        format.setInteger(MediaFormat.KEY_BIT_RATE,2000000);format.setInteger(MediaFormat.KEY_FRAME_RATE,15);
        format.setFloat("max-fps-to-encoder",15f);
        // Repeats are bounded by Android. A static source may subsequently be idle.
        if(mirror)format.setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER,1000000L/15);
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL,1);format.setInteger("max-bframes",0);
        format.setInteger(MediaFormat.KEY_PROFILE,MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline);
        codec=MediaCodec.createEncoderByType("video/avc");codec.configure(format,null,null,MediaCodec.CONFIGURE_FLAG_ENCODE);
        input=codec.createInputSurface();
        if(mirror){
            if(android.os.Build.VERSION.SDK_INT<29)throw new IllegalStateException("Direct display capture requires Android 10+ shell capture permission");
            virtualDisplay=DisplayCapture.mirror(automation,captureContext,"IATest mirror",width,height,InputMain.displayId,input);
            codec.start();generation=UUID.randomUUID().toString();configuration=null;return;
        }
        egl=EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY);int[] version=new int[2];
        if(!EGL14.eglInitialize(egl,version,0,version,1))throw new IllegalStateException("Cannot initialize video EGL");
        int[] attributes={EGL14.EGL_RED_SIZE,8,EGL14.EGL_GREEN_SIZE,8,EGL14.EGL_BLUE_SIZE,8,EGL14.EGL_ALPHA_SIZE,8,
            EGL14.EGL_RENDERABLE_TYPE,EGL14.EGL_OPENGL_ES2_BIT,0x3142,1,EGL14.EGL_NONE};
        EGLConfig[] configs=new EGLConfig[1];int[] count=new int[1];
        if(!EGL14.eglChooseConfig(egl,attributes,0,configs,0,1,count,0)||count[0]!=1)throw new IllegalStateException("No recordable EGL config");
        context=EGL14.eglCreateContext(egl,configs[0],EGL14.EGL_NO_CONTEXT,new int[]{EGL14.EGL_CONTEXT_CLIENT_VERSION,2,EGL14.EGL_NONE},0);
        surface=EGL14.eglCreateWindowSurface(egl,configs[0],input,new int[]{EGL14.EGL_NONE},0);
        if(!EGL14.eglMakeCurrent(egl,surface,surface,context))throw new IllegalStateException("Cannot bind encoder surface");
        program=GLES20.glCreateProgram();
        int vs=shader(GLES20.GL_VERTEX_SHADER,"attribute vec4 p;attribute vec2 uv;varying vec2 v;void main(){gl_Position=p;v=uv;}");
        int fs=shader(GLES20.GL_FRAGMENT_SHADER,"precision mediump float;varying vec2 v;uniform sampler2D image;void main(){gl_FragColor=texture2D(image,v);}");
        GLES20.glAttachShader(program,vs);GLES20.glAttachShader(program,fs);GLES20.glLinkProgram(program);
        int[] linked=new int[1];GLES20.glGetProgramiv(program,GLES20.GL_LINK_STATUS,linked,0);GLES20.glDeleteShader(vs);GLES20.glDeleteShader(fs);
        if(linked[0]!=1)throw new IllegalStateException("Video shader link failed");
        int[] textures=new int[1];GLES20.glGenTextures(1,textures,0);texture=textures[0];GLES20.glBindTexture(GLES20.GL_TEXTURE_2D,texture);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,GLES20.GL_TEXTURE_MIN_FILTER,GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,GLES20.GL_TEXTURE_MAG_FILTER,GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,GLES20.GL_TEXTURE_WRAP_S,GLES20.GL_CLAMP_TO_EDGE);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,GLES20.GL_TEXTURE_WRAP_T,GLES20.GL_CLAMP_TO_EDGE);
        codec.start();configuration=null;generation=UUID.randomUUID().toString();
    }
    private static byte[] bytes(ByteBuffer buffer){byte[] out=new byte[buffer.remaining()];buffer.get(out);return out;}
    public JSONObject execute(String op,JSONObject args,long deadline) throws Exception {
        if(!"video_next".equals(op))throw new IllegalArgumentException("Read-only encoded media channel");
        budget.check(SystemClock.uptimeMillis());
        foreground.run();long wait=67-(SystemClock.uptimeMillis()-lastCapture);if(wait>0)SystemClock.sleep(wait);ControlServer.check(deadline);
        Bitmap bitmap=mirror?null:DisplayCapture.capture(automation,captureContext,InputMain.displayId,deadline);
        if(bitmap!=null&&bitmap.getConfig()==Bitmap.Config.HARDWARE){Bitmap software=bitmap.copy(Bitmap.Config.ARGB_8888,false);bitmap.recycle();bitmap=software;if(bitmap==null)throw new IllegalStateException("Screenshot conversion failed");}
        long capture=SystemClock.uptimeMillis();
        try{
            int currentRotation=InputMain.display().getInt("rotation");
            int w=mirror?InputMain.display().getInt("width"):bitmap.getWidth(),h=mirror?InputMain.display().getInt("height"):bitmap.getHeight();
            if(codec==null||sourceWidth!=w||sourceHeight!=h||rotation!=currentRotation)configure(w,h,currentRotation);
            if(args.optBoolean("request_keyframe")){Bundle parameters=new Bundle();parameters.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME,0);codec.setParameters(parameters);}
            if(!mirror){GLES20.glViewport(0,0,width,height);GLES20.glUseProgram(program);GLES20.glBindTexture(GLES20.GL_TEXTURE_2D,texture);
            GLUtils.texImage2D(GLES20.GL_TEXTURE_2D,0,bitmap,0);
            int p=GLES20.glGetAttribLocation(program,"p"),uv=GLES20.glGetAttribLocation(program,"uv");
            GLES20.glEnableVertexAttribArray(p);GLES20.glVertexAttribPointer(p,2,GLES20.GL_FLOAT,false,0,vertices);
            GLES20.glEnableVertexAttribArray(uv);GLES20.glVertexAttribPointer(uv,2,GLES20.GL_FLOAT,false,0,coordinates);
            GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP,0,4);EGLExt.eglPresentationTimeANDROID(egl,surface,capture*1000000);
            if(!EGL14.eglSwapBuffers(egl,surface))throw new IllegalStateException("Video surface write failed");}lastCapture=capture;
        }finally{if(bitmap!=null)bitmap.recycle();}
        MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
        long outputWaitStarted=SystemClock.uptimeMillis();
        while(true){
            ControlServer.check(deadline);int index=codec.dequeueOutputBuffer(info,20000);
            if(index==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED){
                MediaFormat format=codec.getOutputFormat();ByteArrayOutputStream csd=new ByteArrayOutputStream();
                for(String key:new String[]{"csd-0","csd-1"})if(format.containsKey(key))csd.write(bytes(format.getByteBuffer(key)));
                configuration=csd.toByteArray();continue;
            }
            if(index<0){
                if(mirror&&SystemClock.uptimeMillis()-outputWaitStarted>=250){
                    packet=null;
                    return new JSONObject().put("binary_length",0).put("idle",true)
                        .put("codec","h264-annexb").put("no_b_frames",true).put("key_frame",false)
                        .put("generation",generation).put("width",width).put("height",height)
                        .put("coordinate_width",sourceWidth).put("coordinate_height",sourceHeight).put("rotation",rotation).put("display_id",InputMain.displayId)
                        .put("source_checked_uptime_ms",capture).put("capture_source","display-surface").put("capture_cpu_pixel_copy",false);
                }
                continue;
            }
            try{
                ByteBuffer value=codec.getOutputBuffer(index);value.position(info.offset);value.limit(info.offset+info.size);byte[] encoded=bytes(value);
                if((info.flags&MediaCodec.BUFFER_FLAG_CODEC_CONFIG)!=0){configuration=encoded;continue;}
                if(encoded.length==0)continue;
                boolean key=(info.flags&MediaCodec.BUFFER_FLAG_KEY_FRAME)!=0;
                ByteArrayOutputStream result=new ByteArrayOutputStream();if(key&&configuration!=null)result.write(configuration);result.write(encoded);packet=result.toByteArray();
                if(packet.length>2097152)throw new IllegalStateException("Video packet quota exceeded");budget.charge(packet.length,SystemClock.uptimeMillis());
                return new JSONObject().put("binary_length",packet.length).put("codec","h264-annexb").put("no_b_frames",true)
                    .put("generation",generation).put("key_frame",key).put("width",width).put("height",height)
                    .put("coordinate_width",sourceWidth).put("coordinate_height",sourceHeight).put("rotation",rotation).put("display_id",InputMain.displayId)
                    .put("presentation_us",info.presentationTimeUs).put("capture_started_uptime_ms",capture).put("capture_source",mirror?"display-surface":"screenshot-egl").put("capture_cpu_pixel_copy",!mirror);
            }finally{codec.releaseOutputBuffer(index,false);}
        }
    }
    public byte[] binary(){byte[] result=packet;packet=null;return result;}
    private void releaseCodec(){
        if(virtualDisplay!=null){virtualDisplay.release();virtualDisplay=null;}
        if(codec!=null){try{codec.stop();}catch(Exception ignored){}codec.release();codec=null;}
        if(egl!=null){EGL14.eglMakeCurrent(egl,EGL14.EGL_NO_SURFACE,EGL14.EGL_NO_SURFACE,EGL14.EGL_NO_CONTEXT);
            if(surface!=null)EGL14.eglDestroySurface(egl,surface);if(context!=null)EGL14.eglDestroyContext(egl,context);EGL14.eglTerminate(egl);egl=null;}
        if(input!=null){input.release();input=null;}surface=null;context=null;
    }
    public void release(){releaseCodec();packet=null;configuration=null;}
}
