#import "IATestBridge.h"
#if DEBUG
#import <unistd.h>
#import <ifaddrs.h>
#import <net/if.h>
#import <Metal/Metal.h>
#import <math.h>
static __weak id<MTLDevice> observedMetalDevice;
static NSMutableArray<NSArray<NSNumber *> *> *metalIntervals;
static NSObject *metalLock;
static double retainedSince;
static NSUInteger metalCompleted,metalInvalid,metalPending;
static void MetalInitialize(void) {
    static dispatch_once_t once;dispatch_once(&once,^{metalLock=[NSObject new];metalIntervals=[NSMutableArray new];retainedSince=NSProcessInfo.processInfo.systemUptime*1000;});
}
void IATestBridgeObserveMetalCommandBuffer(id buffer) {
    if(![buffer conformsToProtocol:@protocol(MTLCommandBuffer)])return;
    id<MTLCommandBuffer> command=buffer;MetalInitialize();
    @synchronized(metalLock){metalPending++;}
    [command addCompletedHandler:^(id<MTLCommandBuffer> completed){
        double start=completed.GPUStartTime*1000,end=completed.GPUEndTime*1000;
        @synchronized(metalLock){
            metalPending--;
            if(completed.status!=MTLCommandBufferStatusCompleted||!isfinite(start)||!isfinite(end)||start<=0||end<=start){metalInvalid++;return;}
            metalCompleted++;
            [metalIntervals addObject:@[@(start),@(end)]];
            [metalIntervals sortUsingComparator:^NSComparisonResult(NSArray *a,NSArray *b){return [a[0] compare:b[0]];}];
            NSMutableArray *merged=[NSMutableArray new];
            for(NSArray *interval in metalIntervals){
                NSArray *last=merged.lastObject;
                if(last&&[interval[0] doubleValue]<=[last[1] doubleValue])merged[merged.count-1]=@[last[0],@(MAX([last[1] doubleValue],[interval[1] doubleValue]))];
                else [merged addObject:interval];
            }
            while(merged.count>128){retainedSince=MAX(retainedSince,[merged[0][1] doubleValue]);[merged removeObjectAtIndex:0];}
            metalIntervals=merged;
        }
    }];
}
static NSDictionary *MetalSnapshot(void) {
    MetalInitialize();@synchronized(metalLock){
        return @{@"sample_uptime_ms":@(NSProcessInfo.processInfo.systemUptime*1000),@"retained_since_uptime_ms":@(retainedSince),
            @"completed":@(metalCompleted),@"invalid":@(metalInvalid),@"pending":@(metalPending),@"intervals_ms":[metalIntervals copy]};
    }
}
void IATestBridgeObserveMetalDevice(id device) {
    if(device&&![device conformsToProtocol:@protocol(MTLDevice)])return;
    observedMetalDevice=device;
}

static NSDictionary *Performance(void) {
    NSMutableDictionary *metrics=[NSMutableDictionary dictionary];
    metrics[@"device_thermal_status"]=@(NSProcessInfo.processInfo.thermalState);
    // Observe the App's existing subscription; never change its monitoring settings.
    UIDevice *device=UIDevice.currentDevice;float battery=device.batteryLevel;
    if(battery>=0)metrics[@"device_battery_level_percent"]=@(battery*100);
    struct ifaddrs *interfaces=NULL;uint64_t rx=0,tx=0;BOOL found=NO;
    if(getifaddrs(&interfaces)==0){
        for(struct ifaddrs *item=interfaces;item;item=item->ifa_next){
            if(!item->ifa_addr||!item->ifa_data||item->ifa_addr->sa_family!=AF_LINK)continue;
            NSString *name=[NSString stringWithUTF8String:item->ifa_name];
            if(![name hasPrefix:@"en"]&&![name hasPrefix:@"pdp_ip"])continue;
            struct if_data *data=item->ifa_data;rx+=data->ifi_ibytes;tx+=data->ifi_obytes;found=YES;
        }
        freeifaddrs(interfaces);
    }
    if(found){metrics[@"device_network_rx_bytes"]=@(rx);metrics[@"device_network_tx_bytes"]=@(tx);}
    id<MTLDevice> gpu=observedMetalDevice;
    if(gpu)metrics[@"gpu_allocated_memory_kb"]=@(gpu.currentAllocatedSize/1024.0);
    return metrics;
}

static BOOL Publish(NSDictionary<NSString *,NSNumber *> *ready,NSDictionary<NSString *,NSString *> *identifiers,NSDictionary *inputs,NSError **error) {
    if(ready.count>20||identifiers.count>20||inputs.count>20)return NO;
    NSRegularExpression *blocked=[NSRegularExpression regularExpressionWithPattern:@"token|password|secret|cookie|authorization|apikey|api_key" options:NSRegularExpressionCaseInsensitive error:nil];
    for(NSString *key in [[ready.allKeys arrayByAddingObjectsFromArray:identifiers.allKeys] arrayByAddingObjectsFromArray:inputs.allKeys]){
        if(![key isKindOfClass:NSString.class]||key.length>80||[blocked firstMatchInString:key options:0 range:NSMakeRange(0,key.length)])return NO;
    }
    for(id value in ready.allValues)if(CFGetTypeID((__bridge CFTypeRef)value)!=CFBooleanGetTypeID())return NO;
    for(id value in identifiers.allValues)if(![value isKindOfClass:NSString.class]||[value length]==0||[value length]>500)return NO;
    if(!NSThread.isMainThread)return NO;NSMutableDictionary *states=[NSMutableDictionary dictionary];
    for(NSString *key in inputs){
        id input=inputs[key];if(![input isKindOfClass:UIView.class]||![input conformsToProtocol:@protocol(UITextInput)])return NO;
        if([input respondsToSelector:@selector(isSecureTextEntry)]&&[input isSecureTextEntry])return NO;
        id<UITextInput> field=input;UITextRange *marked=field.markedTextRange,*selected=field.selectedTextRange;
        NSInteger markedLength=marked?[field offsetFromPosition:marked.start toPosition:marked.end]:0;
        NSInteger selectedLength=selected?[field offsetFromPosition:selected.start toPosition:selected.end]:0;
        if(markedLength<0||markedLength>16384||selectedLength<0||selectedLength>16384)return NO;
        states[key]=@{@"marked":marked?@YES:@NO,@"marked_length":@(markedLength),@"selected_length":@(selectedLength),@"first_responder":@([input isFirstResponder])};
    }
    static NSUInteger sequence=0;
    @synchronized(NSBundle.mainBundle) {
        NSDictionary *value=@{@"contract_version":@1,@"test_build":@YES,@"app_id":NSBundle.mainBundle.bundleIdentifier,
            @"build":[NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleVersion"]?:@"",@"process_id":@(getpid()),
            @"sequence":@(++sequence),@"timestamp_ms":@((long long)(NSDate.date.timeIntervalSince1970*1000)),@"ready":ready,@"identifiers":identifiers,@"input_state":states,@"performance":Performance(),@"metal_gpu":MetalSnapshot()};
        NSData *data=[NSJSONSerialization dataWithJSONObject:value options:0 error:error];if(!data||data.length>16384)return NO;
        NSURL *cache=[NSFileManager.defaultManager URLForDirectory:NSCachesDirectory inDomain:NSUserDomainMask appropriateForURL:nil create:YES error:error];
        return cache&&[data writeToURL:[cache URLByAppendingPathComponent:@"iatest-bridge.json"] options:NSDataWritingAtomic error:error];
    }
}
BOOL IATestBridgePublish(NSDictionary *ready,NSDictionary *identifiers,NSError **error){return Publish(ready,identifiers,@{},error);}
BOOL IATestBridgePublishInputState(NSDictionary *ready,NSDictionary *identifiers,NSDictionary *inputs,NSError **error){return Publish(ready,identifiers,inputs,error);}
#endif
