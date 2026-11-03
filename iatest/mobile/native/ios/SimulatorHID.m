// Independent DTUHID wire implementation. Runtime-only Apple interfaces are opt-in.
#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <dlfcn.h>
#import <xpc/xpc.h>

static void Need(BOOL ok, NSString *why) { if(!ok) @throw [NSException exceptionWithName:@"IATestSimulatorHID" reason:why userInfo:nil]; }
static id Send0(id o, SEL s) { Need(o&&[o respondsToSelector:s],@"Installed Xcode runtime interface unavailable");return ((id(*)(id,SEL))objc_msgSend)(o,s); }
static xpc_object_t Encode(id v) {
    if([v isKindOfClass:NSDictionary.class]) { xpc_object_t d=xpc_dictionary_create(NULL,NULL,0);for(NSString *k in v)xpc_dictionary_set_value(d,k.UTF8String,Encode(v[k]));return d; }
    if([v isKindOfClass:NSString.class])return xpc_string_create([v UTF8String]);
    if(strcmp([v objCType],@encode(BOOL))==0||CFGetTypeID((__bridge CFTypeRef)v)==CFBooleanGetTypeID())return xpc_bool_create([v boolValue]);
    if(strcmp([v objCType],@encode(double))==0||strcmp([v objCType],@encode(float))==0)return xpc_double_create([v doubleValue]);
    return xpc_uint64_create([v unsignedLongLongValue]);
}
static xpc_object_t Message(NSString *service, NSString *type, NSDictionary *payload, BOOL barrier) {
    return Encode(@{@"messageType":type,@"featureIdentifier":service,@"isBarrier":@(barrier),@"payload":payload});
}
static void Probe(xpc_connection_t c, NSString *service) {
    dispatch_semaphore_t ready=dispatch_semaphore_create(0);__block BOOL answered=NO;
    xpc_connection_send_message_with_reply(c,Message(service,@"IndigoKeyboardButtonEvent",@{@"usageCode":@0,@"state":@2},YES),dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^(xpc_object_t reply){answered=xpc_get_type(reply)!=XPC_TYPE_ERROR;dispatch_semaphore_signal(ready);});
    Need(dispatch_semaphore_wait(ready,dispatch_time(DISPATCH_TIME_NOW,2*NSEC_PER_SEC))==0&&answered,@"Simulator DTUHID liveness probe failed; no legacy fallback");
    // A liveness reply precedes the guest opening this connection's input device.
    // Let that initialization finish before sending the first touch, without replay.
    [NSThread sleepForTimeInterval:.2];
}
int main(int argc,const char **argv) { @autoreleasepool { xpc_connection_t connection=NULL;BOOL sent=NO;
    @try {
        Need(argc==3,@"Expected selected booted Simulator UUID and Xcode developer directory");
        NSString *udid=@(argv[1]),*developer=@(argv[2]);Need([[NSUUID alloc] initWithUUIDString:udid]!=nil,@"Invalid Simulator UUID");
        NSArray *paths=@[@"/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator",[developer stringByAppendingPathComponent:@"Library/PrivateFrameworks/CoreSimulator.framework/CoreSimulator"]];BOOL loaded=NO;for(NSString *p in paths)if(dlopen(p.UTF8String,RTLD_NOW|RTLD_GLOBAL)){loaded=YES;break;}Need(loaded,@"CoreSimulator framework unavailable");
        Class cls=NSClassFromString(@"SimServiceContext");SEL serviceSel=NSSelectorFromString(@"sharedServiceContextForDeveloperDir:error:");Need(cls&&[cls respondsToSelector:serviceSel],@"CoreSimulator service ABI unavailable");NSError *error=nil;
        id context=((id(*)(id,SEL,id,NSError **))objc_msgSend)(cls,serviceSel,developer,&error);Need(context!=nil,@"CoreSimulator service unavailable");
        SEL setSel=NSSelectorFromString(@"defaultDeviceSetWithError:");Need([context respondsToSelector:setSel],@"Device set interface unavailable");id set=((id(*)(id,SEL,NSError **))objc_msgSend)(context,setSel,&error);
        id device=nil;for(id candidate in Send0(set,NSSelectorFromString(@"devices"))){if([[[Send0(candidate,NSSelectorFromString(@"UDID")) UUIDString] uppercaseString] isEqual:udid.uppercaseString]){Need(device==nil,@"Ambiguous Simulator UUID");device=candidate;}}
        Need(device&&((NSUInteger(*)(id,SEL))objc_msgSend)(device,NSSelectorFromString(@"state"))==3,@"Selected Simulator must already be booted; no device boot is performed");
        NSString *service=@"com.apple.coredevice.feature.remote.hid.digitizer";SEL lookup=NSSelectorFromString(@"lookup:error:");Need([device respondsToSelector:lookup],@"Simulator service lookup unavailable");
        mach_port_t port=((mach_port_t(*)(id,SEL,id,NSError **))objc_msgSend)(device,lookup,service,&error);Need(port!=MACH_PORT_NULL,@"Simulator runtime does not provide DTUHID");
        xpc_object_t(*endpoint)(mach_port_t,uint64_t,uint64_t)=dlsym(RTLD_DEFAULT,"xpc_endpoint_create_mach_port_4sim");void(*enable)(xpc_connection_t)=dlsym(RTLD_DEFAULT,"xpc_connection_enable_sim2host_4sim");Need(endpoint&&enable,@"Simulator XPC ABI unavailable");
        xpc_object_t ep=endpoint(port,0,0);Need(ep!=NULL,@"Simulator endpoint unavailable");connection=xpc_connection_create_from_endpoint(ep);Need(connection!=NULL,@"Simulator connection unavailable");enable(connection);xpc_connection_set_event_handler(connection,^(xpc_object_t event){});xpc_connection_resume(connection);Probe(connection,service);
        char input[32769];Need(fgets(input,sizeof(input),stdin)!=NULL&&strchr(input,'\n')!=NULL,@"Bounded input request required");NSDictionary *request=[NSJSONSerialization JSONObjectWithData:[[@(input) stringByTrimmingCharactersInSet:NSCharacterSet.newlineCharacterSet] dataUsingEncoding:NSUTF8StringEncoding] options:0 error:nil];Need([request isKindOfClass:NSDictionary.class],@"Invalid HID request");
        NSArray *points=request[@"points"];Need(points.count>=2&&points.count<=120,@"Gesture requires 2..120 bounded points");
        for(NSUInteger i=0;i<points.count;i++){NSDictionary *p=points[i];double x=[p[@"x"] doubleValue],y=[p[@"y"] doubleValue],delay=[p[@"delay_ms"] doubleValue];Need(isfinite(x)&&isfinite(y)&&x>=0&&x<=1&&y>=0&&y<=1&&delay>=0&&delay<=100,@"Invalid normalized HID point");if(delay>0)[NSThread sleepForTimeInterval:delay/1000];NSDictionary *payload=@{@"pointOne":@{@"x":@(x),@"y":@(y)},@"eventType":@(i==0?0:i+1==points.count?2:1),@"edge":@0,@"target":@0};xpc_connection_send_message(connection,Message(service,@"IndigoDigitizerEvent",payload,NO));sent=YES;}
        dispatch_semaphore_t drained=dispatch_semaphore_create(0);xpc_connection_send_barrier(connection,^{dispatch_semaphore_signal(drained);});Need(dispatch_semaphore_wait(drained,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC))==0,@"HID send barrier timed out");[NSThread sleepForTimeInterval:.1];
        NSData *out=[NSJSONSerialization dataWithJSONObject:@{@"status":@"injection_call_returned",@"accepted":@YES,@"backend":@"simulator-dtuhid",@"postcondition_verified":@NO} options:0 error:nil];fwrite(out.bytes,1,out.length,stdout);fputc('\n',stdout);
    } @catch(NSException *error) { NSData *out=[NSJSONSerialization dataWithJSONObject:@{@"status":sent?@"unknown":@"failed",@"error":error.reason?:@"Simulator HID unavailable"} options:0 error:nil];fwrite(out.bytes,1,out.length,stdout);fputc('\n',stdout);if(connection)xpc_connection_cancel(connection);return 1; }
    if(connection)xpc_connection_cancel(connection);return 0;
}}
