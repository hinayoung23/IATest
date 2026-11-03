#import <XCTest/XCTest.h>
#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <arpa/inet.h>
#import <sys/socket.h>
#import <unistd.h>
#import <math.h>
#import "VideoServer.h"
#import "Gestures.h"
#import "ElementText.h"
#import "VisualFreshness.h"
#import "SpatialBounds.h"
#import "NetworkTransport.h"
#import <CommonCrypto/CommonDigest.h>
#import <TargetConditionals.h>

static const NSUInteger FrameLimit=65536;
static void Require(BOOL condition,NSString *message){if(!condition)@throw [NSException exceptionWithName:@"IATestRejected" reason:message userInfo:nil];}

@interface MobileRunner : XCTestCase
@property(nonatomic,strong) XCUIApplication *target;
@property(nonatomic,copy) NSString *targetID;
@property(nonatomic,strong) NSMutableDictionary<NSString *,NSDictionary *> *references;
@property(nonatomic,strong) NSMutableDictionary<NSString *,NSDictionary *> *ledger;
@property(nonatomic,strong) NSMutableDictionary<NSString *,NSString *> *fingerprints;
@property(nonatomic,strong) NSData *image;
@property(nonatomic) double imageCapturedAt;
@property(nonatomic,copy) NSString *epoch;
@property(nonatomic) NSUInteger sequence;
@property(nonatomic) BOOL authenticated;
@property(nonatomic,strong) NSMutableDictionary<NSString *,NSPredicate *> *queryPlans;
@property(nonatomic) NSUInteger queryPlanHits;
@property(nonatomic) BOOL queryPlanCacheEnabled;
@property(nonatomic,strong) IATestVideoServer *video;
@property(nonatomic,strong) NSMutableDictionary *nodeRegions;
@property(nonatomic) NSUInteger nodeCacheHits;
@property(nonatomic,copy) NSString *previousClipboard;
@property(nonatomic) BOOL clipboardChanged;
@property(nonatomic) NSInteger ownedClipboardChange;
@end

@implementation MobileRunner
- (void)setUp {
    [super setUp];self.continueAfterFailure=NO;
    self.references=[NSMutableDictionary dictionary];self.ledger=[NSMutableDictionary dictionary];
    self.fingerprints=[NSMutableDictionary dictionary];self.epoch=NSUUID.UUID.UUIDString;
    self.queryPlans=[NSMutableDictionary dictionary];self.queryPlanCacheEnabled=YES;
    self.nodeRegions=[NSMutableDictionary dictionary];
}
- (NSDictionary *)capabilities {
    BOOL trajectories=IATestGesturesAvailable();NSMutableArray *actions=[@[@"query",@"outline",@"tap",@"swipe",@"pinch",@"text",@"key",@"state",@"screenshot",@"launch",@"activate_running",@"terminate",@"fragment",@"ime",@"clipboard_set",@"clipboard_expect",@"clipboard_restore"] mutableCopy];if(trajectories)[actions addObject:@"gesture"];
    return @{@"component":@"ios-xctest",@"actions":actions,@"webkit_native_input":@YES,@"hid_geometry_validation":@YES,@"hid_keyboard_candidate_validation":@YES,
             @"max_touch_points":@(trajectories?10:2),@"gesture_types":trajectories?@[@"tap",@"swipe",@"pinch",@"trajectory"]:@[@"tap",@"swipe",@"pinch"],@"ime_composition":@YES,@"ime_backends":@[@"keyboard_ui"],@"ime_marked_range_observable":@NO,
             @"query_plan_cache_entries":@32,@"reuses_cached_node_state":@NO,@"separate_png_media":@NO,
             @"spatial_bounds":@YES,@"spatial_bounds_mode":@"exact_frame",@"spatial_bounds_max_nodes":@512,
             @"persistent_node_cache":@YES,@"node_cache_entries":@16,@"node_cache_ttl_ms":@250,@"cached_observations_prove_uniqueness":@NO,
             @"fragment_max_steps":@8,@"fragment_deadline_ms":@5000,@"fragment_deadline_is_hard":@NO,
             @"input_modes":@[@"keyboard"],@"keycodes":@[@3],@"continuous_video":@YES,@"video_codecs":@[@"h264-annexb"],@"visual_timestamp_checks":@YES,
             @"limitations":@[@"XCTest queries may perform internal snapshots; visited node count is unknown",
                              @"IME uses actual current keyboard and visible candidates; UITextInput marked ranges are unavailable to the Runner",
                              @"Physical device signing and platform support require separate acceptance"]};
}
- (BOOL)restoreClipboard {
    if(!self.clipboardChanged)return YES;
    UIPasteboard *pasteboard=UIPasteboard.generalPasteboard;
    if(pasteboard.changeCount!=self.ownedClipboardChange)return NO;
    pasteboard.items=self.previousClipboard?@[@{@"public.utf8-plain-text":self.previousClipboard}]:@[];
    self.clipboardChanged=NO;self.previousClipboard=nil;return YES;
}
- (void)foreground {Require(self.target.state==XCUIApplicationStateRunningForeground,@"Target App is not foreground");}
- (XCUIElementQuery *)queryFor:(NSDictionary *)target scope:(XCUIElement *)scope {
    NSDictionary *types=@{@"button":@(XCUIElementTypeButton),@"text":@(XCUIElementTypeStaticText),@"staticText":@(XCUIElementTypeStaticText),
                          @"textField":@(XCUIElementTypeTextField),@"secureTextField":@(XCUIElementTypeSecureTextField),
                          @"cell":@(XCUIElementTypeCell),@"switch":@(XCUIElementTypeSwitch),@"webView":@(XCUIElementTypeWebView),@"any":@(XCUIElementTypeAny)};
    NSNumber *type=target[@"type"]?nil:@(XCUIElementTypeAny);
    NSString *requested=target[@"type"];
    if([requested hasPrefix:@"XCUIElementType"])requested=[requested substringFromIndex:@"XCUIElementType".length];
    if(requested)for(NSString *key in types)if([key caseInsensitiveCompare:requested]==NSOrderedSame){type=types[key];break;}
    Require(type!=nil,@"Unsupported iOS element type");
    XCUIElementQuery *query=[scope descendantsMatchingType:type.unsignedIntegerValue];
    NSMutableArray *predicates=[NSMutableArray array];
    if(target[@"identifier"])[predicates addObject:[NSPredicate predicateWithFormat:@"identifier == %@",target[@"identifier"]]];
    if(target[@"label"])[predicates addObject:[NSPredicate predicateWithFormat:@"label == %@",target[@"label"]]];
    if(target[@"text"])[predicates addObject:[NSPredicate predicateWithFormat:@"label == %@ OR value == %@",target[@"text"],target[@"text"]]];
    if(predicates.count){
        NSString *key=[[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:target options:NSJSONWritingSortedKeys error:nil] encoding:NSUTF8StringEncoding];
        NSPredicate *compiled=self.queryPlanCacheEnabled?self.queryPlans[key]:nil;
        if(compiled)self.queryPlanHits++;else {compiled=[NSCompoundPredicate andPredicateWithSubpredicates:predicates];if(self.queryPlanCacheEnabled){if(self.queryPlans.count>=32)[self.queryPlans removeObjectForKey:self.queryPlans.allKeys.firstObject];self.queryPlans[key]=compiled;}}
        query=[query matchingPredicate:compiled];
    }
    return query;
}
- (NSDictionary *)describe:(XCUIElement *)element target:(NSDictionary *)target {
    NSString *reference=NSUUID.UUID.UUIDString;
    if(self.references.count>=64)[self.references removeAllObjects];
    CGRect frame=element.frame;
    XCUIElementType type=element.elementType;
    BOOL editable=type==XCUIElementTypeTextField||type==XCUIElementTypeSecureTextField||type==XCUIElementTypeTextView||type==XCUIElementTypeSearchField;
    NSString *label=element.label?:@"";id value=element.value;
    NSMutableDictionary *description=[@{@"ref":reference,@"identifier":element.identifier?:@"",@"label":element.label?:@"",
                                @"text":IATestElementText(label,value,editable),
                                @"value":value?:@"",@"enabled":@(element.enabled),@"selected":@(element.selected),
                                @"hittable":@(element.hittable),@"editable":@(editable),@"bounds":@[@(frame.origin.x),@(frame.origin.y),@(CGRectGetMaxX(frame)),@(CGRectGetMaxY(frame))]} mutableCopy];
    if(type==XCUIElementTypeSecureTextField){description[@"secure"]=@YES;description[@"text"]=@"[REDACTED]";description[@"value"]=@"[REDACTED]";}
    else if(editable&&IATestEditorValueAmbiguous(value,element.placeholderValue))description[@"ambiguous_attributes"]=@[@"text",@"value"];
    self.references[reference]=@{@"target":target,@"description":description};
    return description;
}
- (XCUIElementQuery *)scopedQuery:(NSDictionary *)target {
    XCUIElement *scope=self.target;
    NSString *container=target[@"container_identifier"];
    if(container){
        XCUIElementQuery *containers=[[scope descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:container];
        Require(containers.count==1,@"Container must be unique");scope=containers.element;
    }
    if(target[@"anchor"]){
        NSDictionary *anchor=target[@"anchor"];
        XCUIElementQuery *cells=[scope descendantsMatchingType:XCUIElementTypeCell];
        cells=[cells containingPredicate:[NSPredicate predicateWithFormat:@"identifier == %@ AND (label == %@ OR value == %@)",anchor[@"identifier"],anchor[@"text"],anchor[@"text"]]];
        Require(cells.count==1,@"Business anchor must identify one cell");scope=cells.element;
    }
    return [self queryFor:target scope:scope];
}
- (NSArray *)matchedElements:(NSDictionary *)target deadline:(NSTimeInterval)deadline count:(NSUInteger *)count complete:(BOOL *)complete {
    NSArray *bounds=target[@"bounds"];
    if(bounds)Require(IATestBoundsValid(bounds),@"Non-empty finite spatial bounds required");
    XCUIElementQuery *query=[self scopedQuery:target];NSUInteger total=query.count;
    NSMutableArray *nodes=[NSMutableArray array];NSUInteger matched=0;
    *complete=YES;NSUInteger scan=MIN(bounds?512:8,total);
    for(NSUInteger i=0;i<scan;i++){
        if(NSProcessInfo.processInfo.systemUptime>=deadline){*complete=NO;break;}
        XCUIElement *element=[query elementBoundByIndex:i];
        if(!element.exists){*complete=NO;continue;}
        if(!bounds||IATestBoundsMatch(element.frame,bounds)){matched++;if(nodes.count<8)[nodes addObject:element];}
    }
    if(bounds&&total>scan)*complete=NO;
    *count=bounds?matched:total;return nodes;
}
- (NSDictionary *)observe:(NSDictionary *)arguments deadline:(NSTimeInterval)deadline {
    self.queryPlanCacheEnabled=arguments[@"cache"]?[arguments[@"cache"] boolValue]:YES;
    [self foreground];NSDictionary *target=arguments[@"target"];Require([target isKindOfClass:NSDictionary.class],@"Target required");
    NSUInteger limit=arguments[@"limit"]?[arguments[@"limit"] unsignedIntegerValue]:3;
    Require(limit>=1&&limit<=8,@"Candidate limit out of range");
    NSMutableDictionary *recipe=[target mutableCopy];[recipe removeObjectForKey:@"require_unique"];
    NSString *key=[[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:recipe options:NSJSONWritingSortedKeys error:nil] encoding:NSUTF8StringEncoding];
    NSString *generation=[NSString stringWithFormat:@"%@:%@:%ld",self.targetID,NSStringFromCGRect(self.target.frame),(long)XCUIDevice.sharedDevice.orientation];
    NSDictionary *entry=self.nodeRegions[key];
    if(!target[@"bounds"]&&self.queryPlanCacheEnabled&&target[@"require_unique"]&&![target[@"require_unique"] boolValue]&&entry&&
        [entry[@"generation"] isEqual:generation]&&NSProcessInfo.processInfo.systemUptime-[entry[@"time"] doubleValue]<0.25){
        NSMutableArray *cached=[NSMutableArray array];BOOL valid=YES;
        for(XCUIElement *element in entry[@"nodes"]){if(!element.exists){valid=NO;break;}if(cached.count<limit)[cached addObject:[self describe:element target:target]];}
        if(valid){self.nodeCacheHits++;return @{@"candidates":cached,@"match_count":@(cached.count),@"complete":@NO,@"match_count_is_lower_bound":@YES,@"node_cache_hit":@YES,@"node_cache_hits":@(self.nodeCacheHits),@"cache_evidence":@"advisory-only",@"state_version":NSUUID.UUID.UUIDString,@"visited_nodes_unknown":@YES};}
        [self.nodeRegions removeObjectForKey:key];
    }
    NSUInteger count=0;BOOL scanned=NO;
    NSArray *matched=[self matchedElements:target deadline:deadline count:&count complete:&scanned];
    NSMutableArray *candidates=[NSMutableArray array];
    NSMutableArray *nodes=[NSMutableArray array];
    for(NSUInteger i=0;i<MIN(limit,count);i++){
        if(NSProcessInfo.processInfo.systemUptime>=deadline)break;
        if(i>=matched.count)break;
        XCUIElement *element=matched[i];if(element.exists){[candidates addObject:[self describe:element target:target]];[nodes addObject:element];}
    }
    BOOL complete=scanned&&NSProcessInfo.processInfo.systemUptime<deadline && candidates.count==MIN(limit,count);
    if(!target[@"bounds"]&&complete&&count<=limit&&self.queryPlanCacheEnabled){if(self.nodeRegions.count>=16)[self.nodeRegions removeObjectForKey:self.nodeRegions.allKeys.firstObject];self.nodeRegions[key]=@{@"nodes":nodes,@"generation":generation,@"time":@(NSProcessInfo.processInfo.systemUptime)};}
    return @{@"candidates":candidates,@"match_count":@(count),@"complete":@(complete),@"match_count_is_lower_bound":@(!complete),
             @"state_version":NSUUID.UUID.UUIDString,@"visited_nodes_unknown":@YES,
             @"query_plan_cache_hits":@(self.queryPlanHits),
             @"node_cache_hits":@(self.nodeCacheHits),@"node_cache_entries":@(self.nodeRegions.count),
             @"app_id":self.targetID};
}
- (XCUIElement *)resolve:(NSDictionary *)arguments deadline:(NSTimeInterval)deadline {
    [self foreground];NSDictionary *record=self.references[arguments[@"ref"]];Require(record!=nil,@"Reference expired");
    NSUInteger count=0;BOOL complete=NO;NSArray *nodes=[self matchedElements:record[@"target"] deadline:deadline count:&count complete:&complete];
    Require(complete&&count==1&&nodes.count==1,@"Target changed, became ambiguous or query is incomplete");
    XCUIElement *element=nodes.firstObject;NSDictionary *previous=record[@"description"];
    CGRect frame=element.frame;NSArray *bounds=@[@(frame.origin.x),@(frame.origin.y),@(CGRectGetMaxX(frame)),@(CGRectGetMaxY(frame))];
    Require([previous[@"bounds"] isEqual:bounds] && [previous[@"identifier"] isEqual:element.identifier] &&
            [previous[@"label"] isEqual:element.label],@"Stale target reference");
    Require(element.enabled&&element.hittable&&NSProcessInfo.processInfo.systemUptime<deadline,@"Target is not actionable or deadline expired");return element;
}
- (NSDictionary *)execute:(NSString *)op arguments:(NSDictionary *)arguments deadline:(NSTimeInterval)deadline {
    self.image=nil;Require(NSProcessInfo.processInfo.systemUptime<deadline,@"Deadline exceeded");
    if([op isEqual:@"web_tap"]){
        XCUIElement *element=[self resolve:arguments deadline:deadline];Require(element.elementType==XCUIElementTypeWebView,@"WKWebView container required");
        double x=[arguments[@"x"] doubleValue],y=[arguments[@"y"] doubleValue],w=[arguments[@"width"] doubleValue],h=[arguments[@"height"] doubleValue];
        Require(isfinite(x)&&isfinite(y)&&isfinite(w)&&isfinite(h)&&w>0&&h>0&&x>=0&&y>=0&&x<w&&y<h,@"Invalid WebKit viewport point");
        CGRect frame=element.frame;double sx=frame.size.width/w;
        Require(sx>0&&h*sx<=frame.size.height+2&&y*sx<frame.size.height,@"WebKit/native visible viewport geometry differs");
        [[[element coordinateWithNormalizedOffset:CGVectorMake(0,0)] coordinateWithOffset:CGVectorMake(x*sx,y*sx)] tap];
        [self.nodeRegions removeAllObjects];return @{@"accepted":@YES,@"status":@"injection_call_returned"};
    }
    if([op isEqual:@"web_type"]){
        XCUIElement *element=[self resolve:arguments deadline:deadline];Require(element.elementType==XCUIElementTypeWebView,@"WKWebView container required");
        Require(self.target.keyboards.count==1,@"Active keyboard required");NSString *value=arguments[@"value"];
        Require([value isKindOfClass:NSString.class]&&[value lengthOfBytesUsingEncoding:NSUTF8StringEncoding]<=16384,@"Text quota exceeded");
        if([arguments[@"replace"] boolValue])value=[XCUIKeyboardKeyDelete stringByAppendingString:value];
        [self.target typeText:value];[self.nodeRegions removeAllObjects];return @{@"accepted":@YES,@"status":@"injection_call_returned"};
    }
    if([op isEqual:@"gesture"]){[self.nodeRegions removeAllObjects];return IATestPerformGesture([self resolve:arguments deadline:deadline],arguments[@"contacts"],deadline);}
    if([op isEqual:@"ime"]){
        Require([arguments[@"ime_backend"] isEqual:@"keyboard_ui"],@"Explicit keyboard UI backend required");
        [self.nodeRegions removeAllObjects];XCUIElement *editor=[self resolve:arguments deadline:deadline];Require(editor.elementType!=XCUIElementTypeSecureTextField,@"Secure field values cannot prove composition");
        NSArray *events=arguments[@"ime_events"];Require(events.count>=2&&events.count<=24&&[events.lastObject[@"op"] isEqual:@"expect"],@"IME sequence requires final expectation");
        [editor tap];XCUIElement *keyboard=self.target.keyboards.firstMatch;Require([keyboard waitForExistenceWithTimeout:MIN(2,MAX(0,deadline-NSProcessInfo.processInfo.systemUptime))],@"Current system keyboard unavailable");
        NSMutableArray *rows=[NSMutableArray array];NSString *baseline=nil;BOOL composing=NO;
        for(NSDictionary *event in events){
            Require(NSProcessInfo.processInfo.systemUptime<deadline,@"IME deadline exceeded");NSString *kind=event[@"op"];
            if([kind isEqual:@"compose"]){Require([event[@"text"] isKindOfClass:NSString.class]&&[event[@"text"] length]<=256,@"IME text budget exceeded");if(!composing)baseline=[editor.value isKindOfClass:NSString.class]?editor.value:nil;Require(baseline!=nil,@"Current editor value unavailable");[keyboard typeText:event[@"text"]];composing=YES;}
            else if([kind isEqual:@"expect"]){Require(event[@"value"]!=nil,@"iOS IME requires a native whole-value expectation");BOOL ok=[editor.value isEqual:event[@"value"]];[rows addObject:@{@"op":kind,@"passed":@(ok)}];if(!ok)return @{@"status":@"failed",@"rows":rows,@"postcondition_verified":@NO};continue;}
            else if([@[@"candidate",@"commit",@"cancel"] containsObject:kind]){
                NSString *label=[kind isEqual:@"candidate"]?event[@"text"]:event[@"key"];Require(label.length>0,@"Explicit visible candidate/key label required");
                XCUIElementQuery *matches=[[keyboard descendantsMatchingType:XCUIElementTypeAny] matchingPredicate:[NSPredicate predicateWithFormat:@"label == %@ AND hittable == YES",label]];Require(matches.count==1,@"Keyboard candidate/key is missing or ambiguous");
                if([kind isEqual:@"cancel"]){Require(composing&&baseline!=nil,@"No pending composition to cancel");NSUInteger attempts=0;while(![editor.value isEqual:baseline]&&attempts++<256&&NSProcessInfo.processInfo.systemUptime<deadline)[matches.element tap];Require([editor.value isEqual:baseline],@"Keyboard cancellation could not restore pre-composition value");}
                else [matches.element tap];composing=NO;
            }else Require(NO,@"Unsupported IME operation");
            [rows addObject:@{@"op":kind,@"acknowledged":@YES}];
        }
        return @{@"status":@"injection_call_returned",@"accepted":@YES,@"rows":rows,@"postcondition_verified":@YES,@"input_mode":@"keyboard_ui",@"marked_range_observable":@NO};
    }
    if([op isEqual:@"cache_clear"]){[self.nodeRegions removeAllObjects];return @{@"node_cache_entries":@0};}
    if([op hasPrefix:@"clipboard_"]){
        UIPasteboard *pasteboard=UIPasteboard.generalPasteboard;
        if([op isEqual:@"clipboard_restore"])return @{@"restored":@([self restoreClipboard])};
        if([op isEqual:@"clipboard_set"]){
            NSString *value=arguments[@"value"];Require([value isKindOfClass:NSString.class]&&[value lengthOfBytesUsingEncoding:NSUTF8StringEncoding]<=16384,@"Clipboard text limit");
            if(!self.clipboardChanged){Require(!pasteboard.hasImages&&!pasteboard.hasURLs&&!pasteboard.hasColors&&pasteboard.numberOfItems<=1,@"Only bounded plain-text clipboards can be restored");self.previousClipboard=pasteboard.string;Require(!self.previousClipboard||[self.previousClipboard lengthOfBytesUsingEncoding:NSUTF8StringEncoding]<=16384,@"Previous clipboard exceeds restore quota");}
            pasteboard.string=value;self.clipboardChanged=YES;self.ownedClipboardChange=pasteboard.changeCount;
            Require([pasteboard.string isEqual:value],@"Clipboard set not observed");return @{@"status":@"injection_call_returned",@"accepted":@YES};
        }
        Require([op isEqual:@"clipboard_expect"],@"Unknown clipboard operation");NSString *value=pasteboard.string;Require(value||pasteboard.numberOfItems==0,@"Clipboard value inaccessible");NSData *data=[(value?:@"") dataUsingEncoding:NSUTF8StringEncoding];Require(data.length<=16384,@"Clipboard limit");unsigned char hash[CC_SHA256_DIGEST_LENGTH];CC_SHA256(data.bytes,(CC_LONG)data.length,hash);NSMutableString *digest=[NSMutableString string];for(int i=0;i<CC_SHA256_DIGEST_LENGTH;i++)[digest appendFormat:@"%02x",hash[i]];
        return @{@"matched":@([digest isEqual:arguments[@"expected_sha256"]]),@"postcondition_verified":@YES};
    }
    if([@[@"tap",@"swipe",@"pinch",@"text",@"key",@"launch",@"terminate",@"fragment",@"visual_tap"] containsObject:op])[self.nodeRegions removeAllObjects];
    if([op isEqual:@"media_open"]){
        Require([arguments[@"codec"] isEqual:@"h264"],@"iOS separate media requires H264");
        [self.video close];self.video=[[IATestVideoServer alloc] initWithApp:self.target environment:NSProcessInfo.processInfo.environment limits:arguments[@"media_limits"]];[self.video start];return @{@"status":@"received"};
    }
    if([op isEqual:@"fragment"]){
        NSArray *steps=arguments[@"steps"];Require([steps isKindOfClass:NSArray.class]&&steps.count>=2&&steps.count<=8&&[steps.lastObject[@"op"] isEqual:@"expect"],@"Fragment needs 2–8 steps and final expectation");
        deadline=MIN(deadline,NSProcessInfo.processInfo.systemUptime+5);NSMutableArray *rows=[NSMutableArray array];NSUInteger dispatched=0;
        @try{
            for(NSDictionary *step in steps){
                NSString *kind=step[@"op"];Require([@[@"tap",@"text",@"expect"] containsObject:kind],@"Unsupported fragment operation");
                NSDictionary *observation=[self observe:@{@"target":step[@"target"],@"limit":@2} deadline:deadline];
                Require([observation[@"complete"] boolValue]&&![observation[@"match_count_is_lower_bound"] boolValue],@"Incomplete fragment observation");
                NSUInteger count=[observation[@"match_count"] unsignedIntegerValue];NSDictionary *node=count==1?[observation[@"candidates"] firstObject]:nil;
                if([kind isEqual:@"expect"]){
                    NSString *attribute=step[@"attribute"]?:@"exists";id actual=nil;
                    if([attribute isEqual:@"exists"]){Require(count<=1,@"Ambiguous expectation");actual=@(count==1);}
                    else {Require(node!=nil&&![node[@"secure"] boolValue]&&![node[@"truncated_attributes"] containsObject:attribute]&&![node[@"ambiguous_attributes"] containsObject:attribute],@"Missing/secure/truncated/ambiguous evidence");actual=node[attribute];}
                    BOOL passed=[actual isEqual:step[@"expected"]];[rows addObject:@{@"op":kind,@"passed":@(passed)}];
                    if(!passed)return @{@"status":@"failed",@"rows":rows,@"dispatched":@(dispatched),@"postcondition_verified":@NO};
                }else{
                    Require(node!=nil,@"Action target missing or ambiguous");NSMutableDictionary *action=[step mutableCopy];action[@"ref"]=node[@"ref"];
                    dispatched++;NSDictionary *response=[self execute:kind arguments:action deadline:deadline];
                    Require([response[@"accepted"] boolValue],@"Fragment input rejected");[rows addObject:@{@"op":kind,@"acknowledged":@YES}];
                }
            }
            return @{@"status":@"injection_call_returned",@"accepted":@YES,@"rows":rows,@"dispatched":@(dispatched),@"postcondition_verified":@YES};
        }@catch(NSException *exception){return @{@"status":@"unknown",@"rows":rows,@"dispatched":@(dispatched),@"error":exception.reason?:@"Fragment interrupted"};}
    }
    if([op isEqual:@"outline"]){
        [self foreground];NSUInteger limit=MIN(12,[arguments[@"limit"] unsignedIntegerValue]?:12);deadline=MIN(deadline,NSProcessInfo.processInfo.systemUptime+3);
        NSMutableArray *controls=[NSMutableArray array];
        for(NSString *kind in @[@"textField",@"button",@"switch",@"text"]){
            if(controls.count>=limit||NSProcessInfo.processInfo.systemUptime>=deadline)break;
            NSDictionary *recipe=@{@"type":kind};XCUIElementQuery *query=[self queryFor:recipe scope:self.target];
            NSUInteger count=MIN([kind isEqual:@"button"]?5:2,MIN(limit-controls.count,query.count));
            for(NSUInteger i=0;i<count&&NSProcessInfo.processInfo.systemUptime<deadline;i++){
                XCUIElement *element=[query elementBoundByIndex:i];if(!element.exists)continue;
                NSMutableDictionary *target=[recipe mutableCopy];
                NSString *identifier=element.identifier?:@"",*label=element.label?:@"";
                if(identifier.length&&identifier.length<=300)target[@"identifier"]=identifier;
                else if(label.length&&label.length<=300)target[@"label"]=label;else continue;
                NSString *text=label.length>300?[label substringToIndex:300]:label;
                NSMutableDictionary *item=[@{@"identifier":identifier,@"label":text,@"type":kind,@"target":target,@"advisory_only":@YES} mutableCopy];
                if([kind isEqual:@"textField"])item[@"value_not_read"]=@YES;else item[@"text"]=text;
                [controls addObject:item];
            }
        }
        return @{@"controls":controls,@"complete":@NO,@"match_count_is_lower_bound":@YES,@"visited_nodes_unknown":@YES,@"app_id":self.targetID};
    }
    if([op isEqual:@"query"])return [self observe:arguments deadline:deadline];
    if([op isEqual:@"hid_validate"]){
        XCUIElement *element=[self resolve:arguments deadline:deadline];Require(element.hittable,@"HID target is not hittable");
        CGRect frame=element.frame;BOOL candidate=[arguments[@"keyboard_candidate"] boolValue];
        UIImage *screen=XCUIScreen.mainScreen.screenshot.image;CGFloat scale=UIScreen.mainScreen.scale;
        Require(screen.CGImage!=NULL&&scale>0,@"Active display coordinate space unavailable");
        CGSize viewport=CGSizeMake(CGImageGetWidth(screen.CGImage)/scale,CGImageGetHeight(screen.CGImage)/scale);
        if(candidate){
            XCUIElementQuery *keyboards=self.target.keyboards;Require(keyboards.count==1,@"One current keyboard is required");
            XCUIElementQuery *items=[[keyboards.element descendantsMatchingType:element.elementType] matchingPredicate:[NSPredicate predicateWithFormat:@"label == %@ AND identifier == %@",element.label,element.identifier]];
            Require(items.count==1&&CGRectEqualToRect(items.element.frame,frame),@"Candidate is outside the current keyboard");
        }
        return @{@"bounds":@[@(frame.origin.x),@(frame.origin.y),@(CGRectGetMaxX(frame)),@(CGRectGetMaxY(frame))],@"width":@(viewport.width),@"height":@(viewport.height),@"orientation":@((NSInteger)XCUIDevice.sharedDevice.orientation),@"keyboard_candidate_verified":@(candidate)};
    }
    if([op isEqual:@"activate_running"]){[self.target activate];[self foreground];[self.references removeAllObjects];[self.nodeRegions removeAllObjects];return @{@"accepted":@YES};}
    if([op isEqual:@"state"]){[self foreground];return @{@"app_id":self.targetID,@"state_version":NSUUID.UUID.UUIDString};}
    if([op isEqual:@"screenshot"]){
        [self foreground];self.imageCapturedAt=NSProcessInfo.processInfo.systemUptime*1000;XCUIScreenshot *shot=self.target.screenshot;UIImage *original=[UIImage imageWithData:shot.PNGRepresentation];
        Require(original!=nil,@"Screenshot unavailable");CGRect frame=self.target.frame;Require(frame.size.width>0&&frame.size.height>0,@"App geometry unavailable");
        CGFloat sx=original.size.width/frame.size.width,sy=original.size.height/frame.size.height;BOOL region=arguments[@"region"]!=nil;CGRect requested=frame;
        if(region){NSDictionary *r=arguments[@"region"];requested=CGRectMake([r[@"x"] doubleValue],[r[@"y"] doubleValue],[r[@"width"] doubleValue],[r[@"height"] doubleValue]);Require(CGRectContainsRect(frame,requested)&&requested.size.width>0&&requested.size.height>0,@"Visual region outside App screenshot");}
        CGRect crop=CGRectIntegral(CGRectMake((requested.origin.x-frame.origin.x)*sx,(requested.origin.y-frame.origin.y)*sy,requested.size.width*sx,requested.size.height*sy));
        CGImageRef part=CGImageCreateWithImageInRect(original.CGImage,crop);Require(part!=NULL,@"Cannot crop screenshot");UIImage *selected=[UIImage imageWithCGImage:part];CGImageRelease(part);
        CGFloat scale=MIN(1.0,(region?1024.0:1280.0)/MAX(selected.size.width,selected.size.height));if(region)scale=MIN(scale,sqrt(524288.0/(selected.size.width*selected.size.height)));
        CGSize size=CGSizeMake(MAX(1,floor(selected.size.width*scale)),MAX(1,floor(selected.size.height*scale)));
        UIGraphicsImageRendererFormat *format=[UIGraphicsImageRendererFormat defaultFormat];format.scale=1;
        UIGraphicsImageRenderer *renderer=[[UIGraphicsImageRenderer alloc] initWithSize:size format:format];
        self.image=UIImagePNGRepresentation([renderer imageWithActions:^(UIGraphicsImageRendererContext *context){[selected drawInRect:(CGRect){CGPointZero,size}];}]);
        Require(self.image.length<=(region?1048576:8388608),@"Screenshot exceeds limit");
        return @{@"binary_length":@(self.image.length),@"content_type":@"image/png",@"width":@(size.width),@"height":@(size.height),
                 @"crop_rect":@{@"x":@(frame.origin.x+crop.origin.x/sx),@"y":@(frame.origin.y+crop.origin.y/sy),@"width":@(crop.size.width/sx),@"height":@(crop.size.height/sy)},
                 @"viewport":@{@"width":@(CGRectGetMaxX(frame)),@"height":@(CGRectGetMaxY(frame)),@"dpr":@(sx),@"rotation":@((NSInteger)XCUIDevice.sharedDevice.orientation)},
                 @"captured_uptime_ms":@(self.imageCapturedAt)};
    }
    if([op isEqual:@"visual_tap"]){
        NSNumber *captured=arguments[@"image_captured_uptime_ms"],*age=arguments[@"max_image_age_ms"];
        Require([captured isKindOfClass:NSNumber.class]&&[age isKindOfClass:NSNumber.class]&&IATestVisualFresh(captured.doubleValue,self.imageCapturedAt,NSProcessInfo.processInfo.systemUptime*1000,age.doubleValue),@"Visual capture is stale or changed");
        XCUIElement *e=[self resolve:arguments deadline:deadline];CGFloat x=[arguments[@"x"] doubleValue],y=[arguments[@"y"] doubleValue];Require(isfinite(x)&&isfinite(y)&&CGRectContainsPoint(e.frame,CGPointMake(x,y)),@"Visual point outside target");
        Require(IATestVisualFresh(captured.doubleValue,self.imageCapturedAt,NSProcessInfo.processInfo.systemUptime*1000,age.doubleValue),@"Visual capture expired while resolving target");
        [[[e coordinateWithNormalizedOffset:CGVectorMake(0,0)] coordinateWithOffset:CGVectorMake(x-e.frame.origin.x,y-e.frame.origin.y)] tap];
    }
    else if([op isEqual:@"tap"]){XCUIElement *e=[self resolve:arguments deadline:deadline];[e tap];}
    else if([op isEqual:@"swipe"]){
        XCUIElement *e=[self resolve:arguments deadline:deadline];NSString *direction=arguments[@"direction"]?:@"up";
        if([direction isEqual:@"up"])[e swipeUp];else if([direction isEqual:@"down"])[e swipeDown];
        else if([direction isEqual:@"left"])[e swipeLeft];else if([direction isEqual:@"right"])[e swipeRight];
        else Require(NO,@"Invalid swipe direction");
    }else if([op isEqual:@"pinch"]){
        XCUIElement *e=[self resolve:arguments deadline:deadline];double scale=[arguments[@"scale"] doubleValue],velocity=[arguments[@"velocity"] doubleValue];
        Require(isfinite(scale)&&scale>=0.1&&scale<=5&&scale!=1&&isfinite(velocity)&&velocity>=0.1&&velocity<=5,@"Invalid pinch scale/velocity");
        [e pinchWithScale:scale velocity:scale<1?-velocity:velocity];
    }else if([op isEqual:@"text"]){
        Require([arguments[@"mode"] isEqual:@"keyboard"],@"iOS only supports explicit keyboard mode");
        NSString *value=arguments[@"value"];Require([value isKindOfClass:NSString.class]&&[value lengthOfBytesUsingEncoding:NSUTF8StringEncoding]<=16384,@"Text limit");
        XCUIElement *e=[self resolve:arguments deadline:deadline];[e tap];[e typeText:value];
    }else if([op isEqual:@"key"]){Require([arguments[@"keycode"] intValue]==3,@"Only home key is supported on iOS");[XCUIDevice.sharedDevice pressButton:XCUIDeviceButtonHome];}
    else if([op isEqual:@"launch"])[self.target launch];
    else if([op isEqual:@"terminate"])[self.target terminate];
    else Require(NO,@"Unsupported operation");
    [self.references removeAllObjects];
    return @{@"status":@"injection_call_returned",@"accepted":@YES,@"input_mode":@"xctest",@"postcondition_verified":@NO};
}
- (NSDictionary *)dispatch:(NSDictionary *)request environment:(NSDictionary *)env {
    NSMutableDictionary *response=[@{@"protocol":@"iatest-mobile/1",@"session_id":env[@"IATEST_SESSION"],@"driver_epoch":self.epoch,
                                     @"sequence":request[@"sequence"]?:@0,@"started_timestamp_ms":@((long long)(NSDate.date.timeIntervalSince1970*1000))} mutableCopy];
    NSString *op=request[@"op"],*operation=request[@"operation_id"];
    NSSet *actions=[NSSet setWithArray:@[@"tap",@"swipe",@"pinch",@"text",@"key",@"launch",@"activate_running",@"terminate",@"fragment",@"visual_tap",@"gesture",@"ime",@"clipboard_set",@"clipboard_restore",@"web_tap",@"web_type"]];
    BOOL action=[actions containsObject:op];
    @try {
        Require([request[@"protocol"] isEqual:@"iatest-mobile/1"] && [request[@"session_id"] isEqual:env[@"IATEST_SESSION"]] &&
                [request[@"token"] isEqual:env[@"IATEST_TOKEN"]] && [request[@"app_id"] isEqual:env[@"IATEST_APP"]],@"Session or target mismatch");
        NSUInteger sequence=[request[@"sequence"] unsignedIntegerValue];Require(sequence>self.sequence,@"Sequence must increase");self.sequence=sequence;
        Require(self.authenticated||[op isEqual:@"hello"],@"Handshake required");
        Require(!self.authenticated||[request[@"driver_epoch"] isEqual:self.epoch],@"Stale driver epoch");
        NSDictionary *arguments=request[@"arguments"];Require([arguments isKindOfClass:NSDictionary.class],@"Arguments required");
        NSInteger ms=[request[@"deadline_ms"] integerValue];Require(ms>0&&ms<=60000,@"Invalid deadline");
        NSDictionary *result;
        if([op isEqual:@"hello"]){self.authenticated=YES;result=[self capabilities];}
        else if([op isEqual:@"close"]){Require([self restoreClipboard],@"Clipboard cleanup requires reconciliation");result=@{@"status":@"closed"};}
        else if([op isEqual:@"ping"])result=@{@"status":@"received"};
        else {
            NSString *fingerprint=[[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:@[op,arguments] options:NSJSONWritingSortedKeys error:nil] encoding:NSUTF8StringEncoding];
            if(action){
                Require(operation.length>0&&operation.length<=160,@"Action operation_id required");
                if(self.ledger[operation]){Require([self.fingerprints[operation] isEqual:fingerprint],@"Operation ID payload mismatch");[response addEntriesFromDictionary:self.ledger[operation]];return response;}
                Require(self.ledger.count<1024,@"Operation ledger exhausted");self.fingerprints[operation]=fingerprint;
                self.ledger[operation]=@{@"status":@"unknown",@"error":@"Operation did not complete"};
            }
            @try{result=[self execute:op arguments:arguments deadline:NSProcessInfo.processInfo.systemUptime+ms/1000.0];}
            @catch(NSException *exception){result=@{@"status":action?@"unknown":@"rejected",@"error":exception.reason?:@"Native exception"};}
            if(action)self.ledger[operation]=result;
        }
        [response addEntriesFromDictionary:result];if(!response[@"status"])response[@"status"]=@"received";
    }@catch(NSException *exception){response[@"status"]=@"rejected";response[@"error"]=exception.reason?:@"Invalid command";}
    return response;
}
- (XCUIApplication *)measurementApp {
    NSString *bundle=NSProcessInfo.processInfo.environment[@"IATEST_PERF_APP"];
    Require(bundle.length>0,@"Explicit performance App ID required");
    XCUIApplication *app=[[XCUIApplication alloc] initWithBundleIdentifier:bundle];
    NSString *calibration=NSProcessInfo.processInfo.environment[@"IATEST_PERF_CALIBRATION"];
    if(calibration){Require([bundle isEqual:@"dev.iatest.mobile.lab"]&&[@[@"0",@"1"] containsObject:calibration],@"Calibration injection is restricted to the owned Debug Lab");app.launchEnvironment=@{@"IATEST_LAB_INJECT_HITCH":calibration};}
    return app;
}
- (XCTMeasureOptions *)measurementOptions {
    NSInteger count=[NSProcessInfo.processInfo.environment[@"IATEST_PERF_ITERATIONS"] integerValue];
    Require(count>=1&&count<=10,@"Performance iterations must be 1–10");
    XCTMeasureOptions *options=[XCTMeasureOptions defaultOptions];options.iterationCount=count;return options;
}
- (void)testMeasureLaunch {
    XCUIApplication *app=[self measurementApp];
    @try {
        [self measureWithMetrics:@[[[XCTApplicationLaunchMetric alloc] init]] options:[self measurementOptions] block:^{
            [app terminate];[app launch];
        }];
    } @finally { [app terminate]; }
}
- (void)testMeasureScroll {
    XCUIApplication *app=[self measurementApp];
    @try {
        [app launch];
        NSString *identifier=NSProcessInfo.processInfo.environment[@"IATEST_PERF_TARGET"];
        Require(identifier.length>0,@"Explicit scroll target identifier required");
        XCUIElementQuery *query=[[app descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:identifier];
        Require(query.count==1&&query.element.hittable,@"Scroll target missing, ambiguous or not hittable");
        XCUIElement *scroll=query.element;
        Require(scroll.elementType==XCUIElementTypeTable||scroll.elementType==XCUIElementTypeCollectionView||scroll.elementType==XCUIElementTypeScrollView,@"Measurement requires a native scroll container");
        NSDictionary *env=NSProcessInfo.processInfo.environment;
        NSString *originID=env[@"IATEST_PERF_ORIGIN"];if(!originID.length)originID=scroll.cells.firstMatch.identifier;
        Require(originID.length>0,@"Provide a visible, stable scroll origin identifier");
        XCUIElementQuery *origins=[[scroll descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:originID];
        Require(origins.count==1&&origins.element.hittable,@"Scroll origin must be unique and visible");
        CGRect initial=origins.element.frame;
        NSInteger resets=[env[@"IATEST_PERF_MAX_RESETS"] integerValue];Require(resets>=1&&resets<=5,@"Reset swipe budget must be 1–5");
        NSString *collector=env[@"IATEST_PERF_COLLECTOR"]?:@"signpost";
        id<XCTMetric> metric=XCTOSSignpostMetric.scrollingAndDecelerationMetric;
        if([collector isEqual:@"application"]){if(@available(iOS 26.0,*))metric=[[XCTHitchMetric alloc] initWithApplication:app];else Require(NO,@"Application hitch collector requires iOS 26+");}
        else Require([collector isEqual:@"signpost"],@"Unknown hitch collector");
        NSString *measurementID=env[@"IATEST_PERF_MEASUREMENT_ID"];Require(measurementID.length>0,@"Rebuild the host/Runner for scroll evidence");
        XCTMeasureOptions *options=[self measurementOptions];options.invocationOptions=XCTMeasurementInvocationManuallyStart|XCTMeasurementInvocationManuallyStop;
        __block NSUInteger invocation=0;
        [self measureWithMetrics:@[metric] options:options block:^{
            NSString *resetID=env[@"IATEST_PERF_RESET"];
            if(resetID.length){XCUIElementQuery *buttons=[[app descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:resetID];Require(buttons.count==1&&buttons.element.hittable,@"Reset target must be unique and visible");[buttons.element tap];}
            for(NSInteger attempt=0;attempt<resets&&(origins.count!=1||!origins.element.hittable||fabs(origins.element.frame.origin.y-initial.origin.y)>1||fabs(origins.element.frame.origin.x-initial.origin.x)>1);attempt++)[scroll swipeDownWithVelocity:XCUIGestureVelocityFast];
            Require(query.count==1&&scroll.hittable&&origins.count==1&&origins.element.hittable&&fabs(origins.element.frame.origin.y-initial.origin.y)<=1&&fabs(origins.element.frame.origin.x-initial.origin.x)<=1,@"Cannot restore the measured scroll origin");
            CGRect before=origins.element.frame;
            [self startMeasuring];[scroll swipeUpWithVelocity:XCUIGestureVelocityFast];[self stopMeasuring];
            BOOL exists=origins.count==1;CGRect after=exists?origins.element.frame:CGRectZero;
            BOOL moved=!exists||fabs(after.origin.y-before.origin.y)>2||fabs(after.origin.x-before.origin.x)>2;
            Require(query.count==1&&scroll.hittable&&moved,@"The measured gesture did not move scroll content");
            id stalls=NSNull.null;NSString *calibration=env[@"IATEST_PERF_CALIBRATION"];
            if(calibration){XCUIElementQuery *ready=[[app descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:@"ready_label"];Require(ready.count==1,@"Calibration requires the owned Debug Lab");NSString *expected=[calibration isEqual:@"1"]?@"stalls=3":@"stalls=0";Require([ready.element.value isEqual:expected],@"Calibration injection was not observed; install the current Debug Lab");stalls=[calibration isEqual:@"1"]?@3:@0;}
            NSDictionary *evidence=@{@"measurement_id":measurementID,@"invocation":@(invocation++),@"origin_restored":@YES,@"moved":@(moved),@"start_x":@(before.origin.x),@"start_y":@(before.origin.y),@"end_x":exists?@(after.origin.x):NSNull.null,@"end_y":exists?@(after.origin.y):NSNull.null,@"injected_stalls":stalls};
            NSLog(@"IATestScrollEvidence %@",[[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:evidence options:0 error:nil] encoding:NSUTF8StringEncoding]);
        }];
    } @finally { [app terminate]; }
}
- (void)testServe {
    NSDictionary *env=NSProcessInfo.processInfo.environment;
#if TARGET_OS_SIMULATOR
    // Explicit test setup only; DTUHID still delivers tap/swipe independently.
    if([env[@"IATEST_SIMULATOR_PORTRAIT"] isEqual:@"1"])XCUIDevice.sharedDevice.orientation=UIDeviceOrientationPortrait;
#endif
    XCTAssertTrue([env[@"IATEST_TOKEN"] length]>=32);XCTAssertNotNil(env[@"IATEST_SESSION"]);XCTAssertNotNil(env[@"IATEST_APP"]);
    self.targetID=env[@"IATEST_APP"];self.target=[[XCUIApplication alloc] initWithBundleIdentifier:self.targetID];
    IATestSocket channel={.fd=-1,.tls=NULL};
    @try {
    if([env[@"IATEST_OBSERVATION_HOST"] length]){
#if !TARGET_OS_SIMULATOR
        // An undetermined Local Network grant is denied without a prompt while
        // the UI-test host is backgrounded. Foreground only our own Runner so
        // iOS can present its consent alert; the user still decides the grant.
        NSString *runnerID=NSBundle.mainBundle.bundleIdentifier;
        Require([runnerID hasSuffix:@".xctrunner"],@"Expected the signed XCTest Runner host");
        [[[XCUIApplication alloc] initWithBundleIdentifier:runnerID] activate];
#endif
        @try {channel=IATestConnectObservation(env,@"control");}
        @finally {
            XCUIApplicationState state=self.target.state;
            // State can still report foreground immediately after activating
            // the Runner. Explicitly reactivate a running target, rather than
            // trusting that transient value; never launch a stopped target.
            if(state==XCUIApplicationStateRunningForeground||state==XCUIApplicationStateRunningBackground||state==XCUIApplicationStateRunningBackgroundSuspended)[self.target activate];
        }
    }
    else {
    int listener=socket(AF_INET,SOCK_STREAM,0);XCTAssertGreaterThanOrEqual(listener,0);
    int yes=1;setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
    struct sockaddr_in address={0};address.sin_family=AF_INET;address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    address.sin_port=htons([env[@"IATEST_PORT"] intValue]?:8101);
    if(bind(listener,(struct sockaddr *)&address,sizeof(address))!=0||listen(listener,1)!=0){close(listener);XCTFail(@"Cannot bind Runner loopback port");return;}
    fd_set ready;FD_ZERO(&ready);FD_SET(listener,&ready);struct timeval startup={90,0};
    if(select(listener+1,&ready,NULL,NULL,&startup)<=0){close(listener);XCTFail(@"No host connected before startup deadline");return;}
    int client=accept(listener,NULL,NULL);close(listener);if(client<0){XCTFail(@"Host accept failed");return;}
    struct timeval timeout={60,0};setsockopt(client,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(client,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    int noSig=1;setsockopt(client,SOL_SOCKET,SO_NOSIGPIPE,&noSig,sizeof(noSig));
    channel.fd=client;
    }
        while(YES){@autoreleasepool {
            uint32_t networkLength;if(!IATestSocketRead(&channel,&networkLength,4))break;NSUInteger length=ntohl(networkLength);
            if(length==0||length>FrameLimit)break;NSMutableData *data=[NSMutableData dataWithLength:length];if(!IATestSocketRead(&channel,data.mutableBytes,length))break;
            id request=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];if(![request isKindOfClass:NSDictionary.class])break;
            self.image=nil;NSDictionary *response=[self dispatch:request environment:env];
            NSData *output=[NSJSONSerialization dataWithJSONObject:response options:0 error:nil];if(!output||output.length>FrameLimit)break;
            networkLength=htonl((uint32_t)output.length);if(!IATestSocketWrite(&channel,&networkLength,4)||!IATestSocketWrite(&channel,output.bytes,output.length))break;
            if([response[@"binary_length"] unsignedIntegerValue]>0 && !IATestSocketWrite(&channel,self.image.bytes,self.image.length))break;
            if([request[@"op"] isEqual:@"close"])break;
        }}
    }@finally{[self restoreClipboard];[self.video close];IATestSocketClose(&channel);[self.references removeAllObjects];[self.nodeRegions removeAllObjects];}
}
@end
