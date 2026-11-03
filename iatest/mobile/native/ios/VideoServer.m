#import "VideoServer.h"
#import "NetworkTransport.h"
#import <VideoToolbox/VideoToolbox.h>
#import <UIKit/UIKit.h>
#import <arpa/inet.h>
#import <sys/socket.h>
#import <unistd.h>
#import <math.h>

static void Need(BOOL valid){if(!valid)@throw [NSException exceptionWithName:@"IATestVideo" reason:@"Unavailable or invalid bounded video operation" userInfo:nil];}

@interface IATestVideoServer ()
@property(nonatomic,strong) XCUIApplication *app;
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
@property(nonatomic) double durationSeconds;
@property(nonatomic) NSUInteger byteLimit;
@end

static void Encoded(void *context,void *source,OSStatus status,VTEncodeInfoFlags flags,CMSampleBufferRef sample){
    IATestVideoServer *server=(__bridge IATestVideoServer *)context;
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

@implementation IATestVideoServer
- (instancetype)initWithApp:(XCUIApplication *)app environment:(NSDictionary *)environment {
    return [self initWithApp:app environment:environment limits:nil];
}
- (instancetype)initWithApp:(XCUIApplication *)app environment:(NSDictionary *)environment limits:(NSDictionary *)limits {
    if((self=[super init])){
        Need(!limits||[limits isKindOfClass:NSDictionary.class]);
        for(NSString *key in limits)Need([@[@"duration_seconds",@"byte_limit"] containsObject:key]);
        id seconds=limits[@"duration_seconds"]?:@120,bytes=limits[@"byte_limit"]?:@67108864;
        for(id number in @[seconds,bytes])Need([number isKindOfClass:NSNumber.class]&&CFGetTypeID((__bridge CFTypeRef)number)!=CFBooleanGetTypeID()&&isfinite([number doubleValue])&&floor([number doubleValue])==[number doubleValue]);
        Need([seconds doubleValue]>=1&&[seconds doubleValue]<=7200&&[bytes doubleValue]>=1&&[bytes doubleValue]<=2147483648.0);
        self.durationSeconds=[seconds doubleValue];self.byteLimit=[bytes unsignedIntegerValue];
        self.app=app;self.environment=environment;self.listener=-1;self.client=-1;self.started=NSProcessInfo.processInfo.systemUptime;
    }
    return self;
}
- (void)start {__weak IATestVideoServer *weak=self;self.thread=[[NSThread alloc] initWithBlock:^{[weak serve];}];self.thread.name=@"iatest-h264-media";[self.thread start];}
- (void)invalidate {
    if(self.encoder){VTCompressionSessionInvalidate(self.encoder);CFRelease(self.encoder);self.encoder=NULL;}
    self.packet=nil;
}
- (void)configure:(UIImage *)image {
    [self invalidate];self.sourceWidth=CGImageGetWidth(image.CGImage);self.sourceHeight=CGImageGetHeight(image.CGImage);
    double scale=fmin(1,720.0/fmax(self.sourceWidth,self.sourceHeight));self.width=MAX(2,((size_t)(self.sourceWidth*scale)/2)*2);self.height=MAX(2,((size_t)(self.sourceHeight*scale)/2)*2);
    VTCompressionSessionRef encoder=NULL;
    Need(VTCompressionSessionCreate(kCFAllocatorDefault,(int32_t)self.width,(int32_t)self.height,kCMVideoCodecType_H264,NULL,
        (__bridge CFDictionaryRef)@{(id)kCVPixelBufferPixelFormatTypeKey:@(kCVPixelFormatType_32BGRA),(id)kCVPixelBufferCGImageCompatibilityKey:@YES,(id)kCVPixelBufferCGBitmapContextCompatibilityKey:@YES},NULL,Encoded,(__bridge void *)self,&encoder)==noErr);
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
    double now=NSProcessInfo.processInfo.systemUptime;Need(now-self.started<self.durationSeconds&&self.total<self.byteLimit&&self.app.state==XCUIApplicationStateRunningForeground);
    if(now-self.last<1.0/15)[NSThread sleepForTimeInterval:1.0/15-(now-self.last)];
    UIImage *image=XCUIScreen.mainScreen.screenshot.image;Need(image.CGImage!=NULL);
    if(!self.encoder||self.sourceWidth!=CGImageGetWidth(image.CGImage)||self.sourceHeight!=CGImageGetHeight(image.CGImage))[self configure:image];
    CVPixelBufferRef pixels=NULL;Need(CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault,VTCompressionSessionGetPixelBufferPool(self.encoder),&pixels)==kCVReturnSuccess);
    @try {
        CVPixelBufferLockBaseAddress(pixels,0);CGColorSpaceRef color=CGColorSpaceCreateDeviceRGB();
        CGContextRef drawing=CGBitmapContextCreate(CVPixelBufferGetBaseAddress(pixels),self.width,self.height,8,CVPixelBufferGetBytesPerRow(pixels),color,kCGImageAlphaPremultipliedFirst|kCGBitmapByteOrder32Little);
        CGColorSpaceRelease(color);Need(drawing!=NULL);CGContextDrawImage(drawing,CGRectMake(0,0,self.width,self.height),image.CGImage);CGContextRelease(drawing);CVPixelBufferUnlockBaseAddress(pixels,0);
        self.packet=nil;self.encodeError=0;self.ready=dispatch_semaphore_create(0);self.last=NSProcessInfo.processInfo.systemUptime;
        NSDictionary *options=[args[@"request_keyframe"] boolValue]?@{(id)kVTEncodeFrameOptionKey_ForceKeyFrame:@YES}:nil;
        Need(VTCompressionSessionEncodeFrame(self.encoder,pixels,CMTimeMakeWithSeconds(self.last,1000000),CMTimeMake(1,15),(__bridge CFDictionaryRef)options,NULL,NULL)==noErr);
        Need(dispatch_semaphore_wait(self.ready,dispatch_time(DISPATCH_TIME_NOW,1500000000))==0&&self.encodeError==noErr&&self.packet.length>0);
    } @finally {CVPixelBufferRelease(pixels);}
    Need(self.packet.length<=self.byteLimit-self.total&&NSProcessInfo.processInfo.systemUptime-self.started<self.durationSeconds);self.total+=self.packet.length;
    return @{@"binary_length":@(self.packet.length),@"codec":@"h264-annexb",@"no_b_frames":@YES,@"generation":self.generation,@"key_frame":@(self.keyFrame),@"width":@(self.width),@"height":@(self.height),@"coordinate_width":@(self.sourceWidth),@"coordinate_height":@(self.sourceHeight),@"capture_started_uptime_ms":@(self.last*1000)};
}
- (void)serve {
    IATestSocket channel={.fd=-1,.tls=NULL};
    @autoreleasepool {@try {
        if([self.environment[@"IATEST_OBSERVATION_HOST"] length])channel=IATestConnectObservation(self.environment,@"media");
        else {
        self.listener=socket(AF_INET,SOCK_STREAM,0);Need(self.listener>=0);int yes=1;setsockopt(self.listener,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
        struct sockaddr_in address={0};address.sin_family=AF_INET;address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);address.sin_port=htons(([self.environment[@"IATEST_PORT"] intValue]?:8101)+1);
        Need(bind(self.listener,(struct sockaddr *)&address,sizeof(address))==0&&listen(self.listener,1)==0);
        fd_set set;FD_ZERO(&set);FD_SET(self.listener,&set);struct timeval startup={30,0};Need(select(self.listener+1,&set,NULL,NULL,&startup)>0);
        channel.fd=accept(self.listener,NULL,NULL);Need(channel.fd>=0);struct timeval timeout={5,0};setsockopt(channel.fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(channel.fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));setsockopt(channel.fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
        }
        @synchronized(self){self.client=channel.fd;if(self.stopped)shutdown(channel.fd,SHUT_RDWR);}
        NSString *epoch=NSUUID.UUID.UUIDString;NSUInteger sequence=0;BOOL authenticated=NO;
        while(!self.stopped){@autoreleasepool {
            uint32_t length;Need(IATestSocketRead(&channel,&length,4));length=ntohl(length);Need(length>0&&length<=65536);
            NSMutableData *data=[NSMutableData dataWithLength:length];Need(IATestSocketRead(&channel,data.mutableBytes,length));NSDictionary *request=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];Need([request isKindOfClass:NSDictionary.class]);
            NSString *op=request[@"op"];Need([request[@"protocol"] isEqual:@"iatest-mobile/1"]&&[request[@"session_id"] isEqual:self.environment[@"IATEST_SESSION"]]&&[request[@"app_id"] isEqual:self.environment[@"IATEST_APP"]]&&[request[@"token"] isEqual:self.environment[@"IATEST_TOKEN"]]);
            Need([request[@"sequence"] unsignedIntegerValue]>sequence);sequence=[request[@"sequence"] unsignedIntegerValue];Need(!authenticated||[request[@"driver_epoch"] isEqual:epoch]);
            NSMutableDictionary *response=[@{@"protocol":@"iatest-mobile/1",@"session_id":self.environment[@"IATEST_SESSION"],@"driver_epoch":epoch,@"sequence":@(sequence),@"status":@"received"} mutableCopy];
            if([op isEqual:@"hello"]){Need(!authenticated);authenticated=YES;response[@"continuous_video"]=@YES;response[@"codec"]=@"h264-annexb";response[@"media_limits"]=@{@"duration_seconds":@(self.durationSeconds),@"byte_limit":@(self.byteLimit)};}
            else if([op isEqual:@"close"]){self.stopped=YES;}
            else {Need(authenticated&&[op isEqual:@"video_next"]);[response addEntriesFromDictionary:[self next:request[@"arguments"]]];}
            NSData *json=[NSJSONSerialization dataWithJSONObject:response options:0 error:nil];Need(json.length<=65536);length=htonl((uint32_t)json.length);Need(IATestSocketWrite(&channel,&length,4)&&IATestSocketWrite(&channel,json.bytes,json.length));
            if(response[@"binary_length"])Need(IATestSocketWrite(&channel,self.packet.bytes,self.packet.length));self.packet=nil;
        }}
    } @catch(NSException *error){} @finally {@synchronized(self){self.client=-1;IATestSocketClose(&channel);}[self invalidate];[self close];}}
}
- (void)close {
    @synchronized(self){self.stopped=YES;if(self.client>=0)shutdown(self.client,SHUT_RDWR);int fd=self.listener;self.listener=-1;if(fd>=0){shutdown(fd,SHUT_RDWR);close(fd);}}
    if(NSThread.currentThread!=self.thread){double end=NSProcessInfo.processInfo.systemUptime+2;while(self.thread&&!self.thread.finished&&NSProcessInfo.processInfo.systemUptime<end)[NSThread sleepForTimeInterval:0.01];}
}
@end
