#import "Gestures.h"
#import <UIKit/UIKit.h>
@interface NSObject (IATestGestureInterfaces)
- (id)initForTouchAtPoint:(CGPoint)point offset:(NSTimeInterval)offset;
- (void)moveToPoint:(CGPoint)point atOffset:(NSTimeInterval)offset;
- (void)liftUpAtOffset:(NSTimeInterval)offset;
- (id)initWithName:(NSString *)name interfaceOrientation:(NSInteger)orientation;
- (void)addPointerEventPath:(id)path;
- (id)eventSynthesizer;
- (void)synthesizeEvent:(id)event completion:(void(^)(BOOL,NSError *))completion;
@end
static void Check(BOOL b){if(!b)@throw [NSException exceptionWithName:@"IATestGesture" reason:@"Unavailable or invalid bounded trajectory" userInfo:nil];}
BOOL IATestGesturesAvailable(void){
    if(![NSProcessInfo.processInfo.environment[@"IATEST_PRIVATE_GESTURES"] isEqual:@"1"])return NO;
    Class path=NSClassFromString(@"XCPointerEventPath"),record=NSClassFromString(@"XCSynthesizedEventRecord");
    if(![path instancesRespondToSelector:@selector(initForTouchAtPoint:offset:)]||![path instancesRespondToSelector:@selector(moveToPoint:atOffset:)]||![path instancesRespondToSelector:@selector(liftUpAtOffset:)]||![record instancesRespondToSelector:@selector(addPointerEventPath:)]||![record instancesRespondToSelector:@selector(initWithName:interfaceOrientation:)]||![XCUIDevice.sharedDevice respondsToSelector:@selector(eventSynthesizer)])return NO;
    return [[XCUIDevice.sharedDevice eventSynthesizer] respondsToSelector:@selector(synthesizeEvent:completion:)];
}
NSDictionary *IATestPerformGesture(XCUIElement *element,NSArray *contacts,NSTimeInterval deadline){
    Check(IATestGesturesAvailable()&&contacts.count>=1&&contacts.count<=10&&element.hittable);
    CGRect r=element.frame;Check(r.size.width>1&&r.size.height>1);NSMutableSet *ids=[NSMutableSet set];NSUInteger total=0;double end=0,start=5001;
    for(NSDictionary *c in contacts){NSNumber *identifier=c[@"id"];NSArray *points=c[@"points"];Check(identifier&&identifier.intValue>=0&&identifier.intValue<=31&&![ids containsObject:identifier]&&points.count>=2&&points.count<=128);[ids addObject:identifier];total+=points.count;Check(total<=256);double last=-1;
        for(NSDictionary *p in points){double t=[p[@"t_ms"] doubleValue],x=[p[@"x"] doubleValue],y=[p[@"y"] doubleValue];Check(isfinite(t)&&isfinite(x)&&isfinite(y)&&t>last&&t>=0&&t<=5000&&x>=0&&x<=1&&y>=0&&y<=1);last=t;}
        start=MIN(start,[points.firstObject[@"t_ms"] doubleValue]);end=MAX(end,last);}
    Check(start==0&&NSProcessInfo.processInfo.systemUptime+end/1000.0<deadline);
    NSInteger orientation=UIInterfaceOrientationPortrait;switch(XCUIDevice.sharedDevice.orientation){case UIDeviceOrientationLandscapeLeft:orientation=UIInterfaceOrientationLandscapeRight;break;case UIDeviceOrientationLandscapeRight:orientation=UIInterfaceOrientationLandscapeLeft;break;case UIDeviceOrientationPortraitUpsideDown:orientation=UIInterfaceOrientationPortraitUpsideDown;break;default:break;}
    id record=[NSClassFromString(@"XCSynthesizedEventRecord") alloc];record=[record initWithName:@"IATest bounded trajectory" interfaceOrientation:orientation];
    for(NSDictionary *c in contacts){NSArray *points=c[@"points"];NSDictionary *p=points.firstObject;id path=[NSClassFromString(@"XCPointerEventPath") alloc];path=[path initForTouchAtPoint:CGPointMake(r.origin.x+[p[@"x"] doubleValue]*(r.size.width-0.5),r.origin.y+[p[@"y"] doubleValue]*(r.size.height-0.5)) offset:[p[@"t_ms"] doubleValue]/1000.0];
        for(NSUInteger i=1;i<points.count;i++){p=points[i];[path moveToPoint:CGPointMake(r.origin.x+[p[@"x"] doubleValue]*(r.size.width-0.5),r.origin.y+[p[@"y"] doubleValue]*(r.size.height-0.5)) atOffset:[p[@"t_ms"] doubleValue]/1000.0];}
        [path liftUpAtOffset:[points.lastObject[@"t_ms"] doubleValue]/1000.0];[record addPointerEventPath:path];}
    NSObject *lock=[NSObject new];__block BOOL complete=NO,accepted=NO;__block NSError *failure=nil;
    [[XCUIDevice.sharedDevice eventSynthesizer] synthesizeEvent:record completion:^(BOOL ok,NSError *error){@synchronized(lock){complete=YES;accepted=ok;failure=error;}}];
    while(NSProcessInfo.processInfo.systemUptime<deadline){@synchronized(lock){if(complete)break;}[[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];}
    @synchronized(lock){if(!complete||failure||!accepted)return @{@"status":@"unknown",@"error":@"XCTest contact completion/release unconfirmed; no replay",@"cleanup":@"Every path includes final lift; opaque platform calls cannot be preempted"};}
    return @{@"status":@"injection_call_returned",@"accepted":@YES,@"contacts":@(contacts.count),@"postcondition_verified":@NO,@"input_mode":@"xctest-synthetic-record"};
}
