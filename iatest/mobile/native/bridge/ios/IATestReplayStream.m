#import "IATestReplayStream.h"
#if DEBUG
#import <VideoToolbox/VideoToolbox.h>
#import <VideoToolbox/VideoToolbox.h>
#import <UIKit/UIKit.h>
#import <arpa/inet.h>
#import <sys/socket.h>
#import <unistd.h>

static void Need(BOOL valid){if(!valid)@throw [NSException exceptionWithName:@"IATestVideo" reason:@"Unavailable or invalid bounded video operation" userInfo:nil];}
static BOOL Transfer(int fd,void *data,NSUInteger length,BOOL write){NSUInteger n=0;while(n<length){ssize_t r=write?send(fd,(char*)data+n,length-n,0):recv(fd,(char*)data+n,length-n,0);if(r<=0)return NO;n+=r;}return YES;}

@interface IATestReplayStream ()
@property(nonatomic) CVPixelBufferRef latestPixels;
@property(nonatomic) double latestTime;
@property(nonatomic) BOOL captureOwned;
@property(nonatomic,strong) NSDictionary *environment;
@property(nonatomic,strong) NSThread *thread;
@property(nonatomic) int listener,client;
@property(nonatomic) BOOL stopped;
@property(nonatomic) VTCompressionSessionRef encoder;
@property(nonatomic) size_t width,height,sourceWidth,sourceHeight;
@property(nonatomic,copy) NSString *generation;
@property(nonatomic,strong) NSData *packet;
@property(nonatomic) BOOL keyFrame;
@property(nonatomic) OSStatus encodeError;
@property(nonatomic,strong) dispatch_semaphore_t ready;
@property(nonatomic) double started,last;
@property(nonatomic) NSUInteger total;
@end

static void Encoded(void *context,void *source,OSStatus status,VTEncodeInfoFlags flags,CMSampleBufferRef sample){
    IATestReplayStream *server=(__bridge IATestReplayStream *)context;
    @try {
        server.encodeError=status;Need(status==noErr&&sample&&CMSampleBufferDataIsReady(sample));
        CFArrayRef attachments=CMSampleBufferGetSampleAttachmentsArray(sample,false);
        BOOL key=!attachments||!CFDictionaryContainsKey(CFArrayGetValueAtIndex(attachments,0),kCMSampleAttachmentKey_NotSync);
        NSMutableData *out=[NSMutableData data];const uint8_t prefix[]={0,0,0,1};
        if(key){
            CMFormatDescriptionRef format=CMSampleBufferGetFormatDescription(sample);size_t count=0;
            const uint8_t *bytes;size_t size;int header;
            Need(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,0,&bytes,&size,&count,&header)==noErr&&header==4);
            for(size_t i=0;i<count;i++){Need(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,i,&bytes,&size,NULL,NULL)==noErr);[out appendBytes:prefix length:4];[out appendBytes:bytes length:size];}
        }
        CMBlockBufferRef block=CMSampleBufferGetDataBuffer(sample);size_t length=CMBlockBufferGetDataLength(block);Need(length<=2097152);
        NSMutableData *data=[NSMutableData dataWithLength:length];Need(CMBlockBufferCopyDataBytes(block,0,length,data.mutableBytes)==noErr);
        NSUInteger offset=0;const uint8_t *bytes=data.bytes;
        while(offset<length){Need(offset+4<=length);uint32_t n;memcpy(&n,bytes+offset,4);n=ntohl(n);offset+=4;Need(n>0&&offset+n<=length);[out appendBytes:prefix length:4];[out appendBytes:bytes+offset length:n];offset+=n;}
        Need(out.length>0&&out.length<=2097152);server.packet=out;server.keyFrame=key;
    } @catch(NSException *error){server.encodeError=-1;}
    dispatch_semaphore_signal(server.ready);
}

@implementation IATestReplayStream
- (instancetype)initWithPort:(uint16_t)port token:(NSString *)token {
    Need(port>=1024&&token.length>=32);
    if((self=[super init])){self.environment=@{@"IATEST_PORT":@(port-1).stringValue,@"IATEST_TOKEN":token,@"IATEST_APP":NSBundle.mainBundle.bundleIdentifier};self.listener=-1;self.client=-1;self.started=NSProcessInfo.processInfo.systemUptime;}
    return self;
}
- (void)startWithCompletion:(void (^)(NSError *))completion {
    Need(NSThread.isMainThread&&!RPScreenRecorder.sharedRecorder.isRecording);__weak IATestReplayStream *weak=self;
    [RPScreenRecorder.sharedRecorder startCaptureWithHandler:^(CMSampleBufferRef sample,RPSampleBufferType type,NSError *error){
        IATestReplayStream *strong=weak;if(!strong||strong.stopped)return;
        if(error){strong.captureOwned=NO;[strong close];return;}if(type!=RPSampleBufferTypeVideo)return;
        CVPixelBufferRef pixels=CMSampleBufferGetImageBuffer(sample);if(!pixels||CVPixelBufferGetWidth(pixels)*CVPixelBufferGetHeight(pixels)>16777216)return;
        CVPixelBufferRetain(pixels);@synchronized(strong){if(strong.stopped){CVPixelBufferRelease(pixels);}else{CVPixelBufferRef old=strong.latestPixels;strong.latestPixels=pixels;strong.latestTime=NSProcessInfo.processInfo.systemUptime;if(old)CVPixelBufferRelease(old);}}
    } completionHandler:^(NSError *error){IATestReplayStream *strong=weak;if(!error){if(!strong||strong.stopped){[RPScreenRecorder.sharedRecorder stopCaptureWithHandler:^(NSError *stopError){}];}else{strong.captureOwned=YES;strong.thread=[[NSThread alloc] initWithBlock:^{[weak serve];}];[strong.thread start];}}if(completion)completion(error);}];
}
- (void)invalidate {
    if(self.encoder){VTCompressionSessionInvalidate(self.encoder);CFRelease(self.encoder);self.encoder=NULL;}
    self.packet=nil;
}
- (void)configure:(CVPixelBufferRef)image {
    [self invalidate];self.sourceWidth=CVPixelBufferGetWidth(image);self.sourceHeight=CVPixelBufferGetHeight(image);
    double scale=fmin(1,720.0/fmax(self.sourceWidth,self.sourceHeight));self.width=MAX(2,((size_t)(self.sourceWidth*scale)/2)*2);self.height=MAX(2,((size_t)(self.sourceHeight*scale)/2)*2);
    VTCompressionSessionRef encoder=NULL;
    Need(VTCompressionSessionCreate(kCFAllocatorDefault,(int32_t)self.width,(int32_t)self.height,kCMVideoCodecType_H264,NULL,
        NULL,NULL,Encoded,(__bridge void *)self,&encoder)==noErr);
    self.encoder=encoder;
    Need(VTSessionSetProperty(encoder,kVTCompressionPropertyKey_RealTime,kCFBooleanTrue)==noErr);
    Need(VTSessionSetProperty(encoder,kVTCompressionPropertyKey_AllowFrameReordering,kCFBooleanFalse)==noErr);
    Need(VTSessionSetProperty(encoder,kVTCompressionPropertyKey_ProfileLevel,kVTProfileLevel_H264_Baseline_AutoLevel)==noErr);
    Need(VTSessionSetProperty(encoder,kVTCompressionPropertyKey_AverageBitRate,(__bridge CFTypeRef)@2000000)==noErr);
    Need(VTSessionSetProperty(encoder,kVTCompressionPropertyKey_ExpectedFrameRate,(__bridge CFTypeRef)@15)==noErr);
    Need(VTSessionSetProperty(encoder,kVTCompressionPropertyKey_MaxKeyFrameInterval,(__bridge CFTypeRef)@15)==noErr);
    Need(VTCompressionSessionPrepareToEncodeFrames(encoder)==noErr);self.generation=NSUUID.UUID.UUIDString;
}
- (NSDictionary *)next:(NSDictionary *)args {
    double now=NSProcessInfo.processInfo.systemUptime;Need(now-self.started<120&&self.total<67108864&&self.captureOwned);
    __block BOOL active=NO;dispatch_sync(dispatch_get_main_queue(),^{active=UIApplication.sharedApplication.applicationState==UIApplicationStateActive;});Need(active);
    CVPixelBufferRef pixels=NULL;double captured=0;double deadline=now+1;while(!pixels&&!self.stopped&&NSProcessInfo.processInfo.systemUptime<deadline){@synchronized(self){captured=self.latestTime;if(self.latestPixels&&NSProcessInfo.processInfo.systemUptime-captured<1){pixels=self.latestPixels;CVPixelBufferRetain(pixels);}}if(!pixels)[NSThread sleepForTimeInterval:0.01];}
    @try {
        Need(pixels&&NSProcessInfo.processInfo.systemUptime-captured<1);
        if(!self.encoder||self.sourceWidth!=CVPixelBufferGetWidth(pixels)||self.sourceHeight!=CVPixelBufferGetHeight(pixels))[self configure:pixels];
        self.packet=nil;self.encodeError=0;self.ready=dispatch_semaphore_create(0);self.last=now;
        NSDictionary *options=[args[@"request_keyframe"] boolValue]?@{(id)kVTEncodeFrameOptionKey_ForceKeyFrame:@YES}:nil;
        Need(VTCompressionSessionEncodeFrame(self.encoder,pixels,CMTimeMakeWithSeconds(now,1000000),CMTimeMake(1,15),(__bridge CFDictionaryRef)options,NULL,NULL)==noErr);
        Need(dispatch_semaphore_wait(self.ready,dispatch_time(DISPATCH_TIME_NOW,1500000000))==0&&self.encodeError==noErr&&self.packet.length>0);
    } @finally {if(pixels)CVPixelBufferRelease(pixels);}
    self.total+=self.packet.length;Need(self.total<=67108864);
    return @{@"binary_length":@(self.packet.length),@"codec":@"h264-annexb",@"no_b_frames":@YES,@"generation":self.generation,@"key_frame":@(self.keyFrame),@"width":@(self.width),@"height":@(self.height),@"coordinate_width":@(self.sourceWidth),@"coordinate_height":@(self.sourceHeight),@"capture_started_uptime_ms":@(captured*1000),@"capture_source":@"replaykit-pixelbuffer",@"capture_cpu_pixel_copy":@NO};
}
- (void)serve {
    @autoreleasepool {@try {
        self.listener=socket(AF_INET,SOCK_STREAM,0);Need(self.listener>=0);int yes=1;setsockopt(self.listener,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
        struct sockaddr_in address={0};address.sin_family=AF_INET;address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);address.sin_port=htons(([self.environment[@"IATEST_PORT"] intValue]?:8101)+1);
        Need(bind(self.listener,(struct sockaddr *)&address,sizeof(address))==0&&listen(self.listener,1)==0);
        fd_set set;FD_ZERO(&set);FD_SET(self.listener,&set);struct timeval startup={30,0};Need(select(self.listener+1,&set,NULL,NULL,&startup)>0);
        self.client=accept(self.listener,NULL,NULL);Need(self.client>=0);struct timeval timeout={5,0};setsockopt(self.client,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(self.client,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));setsockopt(self.client,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
        NSString *epoch=NSUUID.UUID.UUIDString;NSUInteger sequence=0;BOOL authenticated=NO;
        while(!self.stopped){@autoreleasepool {
            uint32_t length;Need(Transfer(self.client,&length,4,NO));length=ntohl(length);Need(length>0&&length<=65536);
            NSMutableData *data=[NSMutableData dataWithLength:length];Need(Transfer(self.client,data.mutableBytes,length,NO));NSDictionary *request=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];Need([request isKindOfClass:NSDictionary.class]);
            NSString *op=request[@"op"];if([op isEqual:@"hello"]&&!authenticated){Need([request[@"token"] isEqual:self.environment[@"IATEST_TOKEN"]]&&[request[@"session_id"] isKindOfClass:NSString.class]&&[request[@"session_id"] length]<=100);NSMutableDictionary *bound=[self.environment mutableCopy];bound[@"IATEST_SESSION"]=request[@"session_id"];self.environment=bound;}Need([request[@"protocol"] isEqual:@"iatest-mobile/1"]&&[request[@"session_id"] isEqual:self.environment[@"IATEST_SESSION"]]&&[request[@"app_id"] isEqual:self.environment[@"IATEST_APP"]]&&[request[@"token"] isEqual:self.environment[@"IATEST_TOKEN"]]);
            Need([request[@"sequence"] unsignedIntegerValue]>sequence);sequence=[request[@"sequence"] unsignedIntegerValue];Need(!authenticated||[request[@"driver_epoch"] isEqual:epoch]);
            NSMutableDictionary *response=[@{@"protocol":@"iatest-mobile/1",@"session_id":self.environment[@"IATEST_SESSION"],@"driver_epoch":epoch,@"sequence":@(sequence),@"status":@"received"} mutableCopy];
            if([op isEqual:@"hello"]){Need(!authenticated);authenticated=YES;response[@"continuous_video"]=@YES;response[@"codec"]=@"h264-annexb";}
            else if([op isEqual:@"close"]){self.stopped=YES;}
            else {Need(authenticated&&[op isEqual:@"video_next"]);[response addEntriesFromDictionary:[self next:request[@"arguments"]]];}
            NSData *json=[NSJSONSerialization dataWithJSONObject:response options:0 error:nil];Need(json.length<=65536);length=htonl((uint32_t)json.length);Need(Transfer(self.client,&length,4,YES)&&Transfer(self.client,(void *)json.bytes,json.length,YES));
            if(response[@"binary_length"])Need(Transfer(self.client,(void *)self.packet.bytes,self.packet.length,YES));self.packet=nil;
        }}
    } @catch(NSException *error){} @finally {[self invalidate];[self close];}}
}
- (void)close {
    self.stopped=YES;if(self.captureOwned){self.captureOwned=NO;dispatch_async(dispatch_get_main_queue(),^{[RPScreenRecorder.sharedRecorder stopCaptureWithHandler:^(NSError *error){}];});}@synchronized(self){if(self.latestPixels){CVPixelBufferRelease(self.latestPixels);self.latestPixels=NULL;}}int fd=self.client;self.client=-1;if(fd>=0){shutdown(fd,SHUT_RDWR);close(fd);}fd=self.listener;self.listener=-1;if(fd>=0){shutdown(fd,SHUT_RDWR);close(fd);}
    if(!NSThread.isMainThread&&NSThread.currentThread!=self.thread){double end=NSProcessInfo.processInfo.systemUptime+2;while(self.thread&&!self.thread.finished&&NSProcessInfo.processInfo.systemUptime<end)[NSThread sleepForTimeInterval:0.01];}
}
@end

#endif
