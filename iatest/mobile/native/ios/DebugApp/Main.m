#import <UIKit/UIKit.h>
#import <WebKit/WebKit.h>
#import <arpa/inet.h>
#import <sys/socket.h>
#import <unistd.h>
#if DEBUG
#import <Metal/Metal.h>
#endif
#import "../../bridge/ios/IATestBridge.h"

#if DEBUG
// Opt-in acceptance diagnostics for this owned fixture, never editor contents.
@interface LabInputWindow : UIWindow
@property(nonatomic,strong) NSMutableArray *inputEvents;
@end
@implementation LabInputWindow
- (void)sendEvent:(UIEvent *)event {
    BOOL trace=[NSProcessInfo.processInfo.environment[@"IATEST_LAB_TRACE_POINTER"] isEqual:@"1"];
    if(trace)for(UITouch *touch in event.allTouches){
        if(touch.phase!=UITouchPhaseBegan&&touch.phase!=UITouchPhaseEnded&&touch.phase!=UITouchPhaseCancelled)continue;
        CGPoint point=[touch locationInView:self];UIView *view=touch.view;
        for(NSUInteger depth=0;view&&!view.accessibilityIdentifier.length&&depth<8;depth++)view=view.superview;
        if(!self.inputEvents)self.inputEvents=[NSMutableArray new];
        [self.inputEvents addObject:@{@"timestamp_ms":@((long long)(NSDate.date.timeIntervalSince1970*1000)),
            @"x":@(point.x),@"y":@(point.y),@"phase":@(touch.phase),@"type":@(touch.type),
            @"identifier":view.accessibilityIdentifier?:@""}];
        if(self.inputEvents.count>32)[self.inputEvents removeObjectAtIndex:0];
    }
    [super sendEvent:event];
    if(trace&&self.inputEvents.count){
        NSURL *cache=[NSFileManager.defaultManager URLForDirectory:NSCachesDirectory inDomain:NSUserDomainMask appropriateForURL:nil create:YES error:nil];
        NSData *data=[NSJSONSerialization dataWithJSONObject:@{@"width":@(self.bounds.size.width),@"height":@(self.bounds.size.height),@"events":self.inputEvents} options:0 error:nil];
        if(data.length<=16384)[data writeToURL:[cache URLByAppendingPathComponent:@"iatest-lab-pointer.json"] options:NSDataWritingAtomic error:nil];
    }
}
@end
#endif

@interface GesturePad : UIView
@property(nonatomic) NSUInteger pans;
@property(nonatomic) NSUInteger pinches;
@property(nonatomic) CGFloat scale;
@end

@implementation GesturePad
- (instancetype)init {
    if ((self=[super init])) {
        self.backgroundColor=UIColor.systemBlueColor;
        self.layer.cornerRadius=12;
        self.isAccessibilityElement=YES;
        self.accessibilityIdentifier=@"gesture_pad";
        self.accessibilityLabel=@"Gesture pad";
        self.scale=1;
        UIPanGestureRecognizer *pan=[[UIPanGestureRecognizer alloc] initWithTarget:self action:@selector(pan:)];
        pan.maximumNumberOfTouches=1;[self addGestureRecognizer:pan];
        [self addGestureRecognizer:[[UIPinchGestureRecognizer alloc] initWithTarget:self action:@selector(pinch:)]];
        [self updateValue];
    }
    return self;
}
- (void)updateValue {
    self.accessibilityValue=[NSString stringWithFormat:@"pans=%lu pinches=%lu",(unsigned long)self.pans,(unsigned long)self.pinches];
    [self setNeedsDisplay];
}
- (void)pan:(UIPanGestureRecognizer *)sender {
    if(sender.state==UIGestureRecognizerStateEnded){self.pans++;[self updateValue];}
}
- (void)pinch:(UIPinchGestureRecognizer *)sender {
    if(sender.state==UIGestureRecognizerStateEnded){self.pinches++;self.scale*=sender.scale;[self updateValue];}
}
- (void)drawRect:(CGRect)rect {
    NSString *text=[NSString stringWithFormat:@"Swipe / Pinch (%.2f)\n%@",self.scale,self.accessibilityValue];
    NSMutableParagraphStyle *style=[NSMutableParagraphStyle new];style.alignment=NSTextAlignmentCenter;
    [text drawInRect:CGRectInset(rect,8,20) withAttributes:@{NSFontAttributeName:[UIFont systemFontOfSize:17],NSForegroundColorAttributeName:UIColor.whiteColor,NSParagraphStyleAttributeName:style}];
}
@end

#if DEBUG
@interface HybridController : UIViewController <WKUIDelegate>
@property(nonatomic) int listener;
@property(nonatomic,strong) WKWebView *mainWeb;
@property(nonatomic,strong) WKWebView *secondaryWeb;
@end
@implementation HybridController
- (void)viewDidLoad {
    [super viewDidLoad];self.title=@"Hybrid lab";self.view.backgroundColor=UIColor.systemBackgroundColor;
    int listener=socket(AF_INET,SOCK_STREAM,0);self.listener=listener;
    struct sockaddr_in address={0};address.sin_family=AF_INET;address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);address.sin_port=0;
    if(bind(listener,(struct sockaddr *)&address,sizeof(address))!=0||listen(listener,4)!=0)return;
    socklen_t length=sizeof(address);getsockname(listener,(struct sockaddr *)&address,&length);
    NSString *html=@"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>IATest Hybrid Lab</title></head><body><h1>Hybrid Ready</h1><label for='nickname'>Nickname</label><input id='nickname' autocomplete='off'><button id='save' onclick=\"document.getElementById('status').textContent=document.getElementById('nickname').value\">Save</button><p id='status'>Waiting</p></body></html>";
    unsigned int port=ntohs(address.sin_port);
    NSString *contexts=@"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><title>IATest Contexts</title><style>body{margin:8px;font:16px sans-serif}input{font-size:16px;width:160px}iframe{height:180px;width:340px;max-width:90vw;transform:scale(.95);transform-origin:top left}section{margin:8px 0;padding:4px;border:1px solid #ddd}h2{font-size:16px}</style><h2 id='contexts_ready'>Contexts Ready</h2><iframe id='outer' src='/frame'></iframe><section id='open-host'></section><section id='closed-host'></section><button id='open_secondary' onclick=\"window.open('/secondary','iatest-secondary')\">Open second page</button><script>for(const [id,mode] of [['open-host','open'],['closed-host','closed']]){const r=document.getElementById(id).attachShadow({mode});r.innerHTML='<style>input{font-size:16px;width:150px}</style><label>Nickname <input id=\"shadow_nickname\"></label><button id=\"shadow_save\">Save</button><span id=\"shadow_status\">Waiting</span>';r.getElementById('shadow_save').onclick=()=>{r.getElementById('shadow_status').textContent=r.getElementById('shadow_nickname').value;};}</script>";
    NSString *frame=[NSString stringWithFormat:@"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><title>Outer frame</title><style>body{margin:4px}iframe{width:290px;height:140px;border:3px solid blue}</style><iframe id='inner' src='http://localhost:%u/frame-child'></iframe>",port];
    NSString *child=@"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><title>Child frame</title><style>body{font:16px sans-serif;margin:6px}input{font-size:16px;width:150px}</style><label>Nickname <input id='frame_nickname'></label><button id='frame_save' onclick=\"document.getElementById('frame_status').textContent=document.getElementById('frame_nickname').value\">Save</button><p id='frame_status'>Waiting</p>";
    NSDictionary *routes=@{@"/":html,@"/contexts":contexts,@"/frame":frame,@"/frame-child":child,@"/secondary":@"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><title>IATest Secondary</title><h1 id='secondary_ready'>Secondary Ready</h1>"};
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY,0), ^{
        for(NSUInteger n=0;n<300;n++){
            int client=accept(listener,NULL,NULL);if(client<0)break;
            int yes=1;setsockopt(client,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));struct timeval timeout={2,0};setsockopt(client,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(client,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
            char input[4096];ssize_t received=recv(client,input,sizeof(input),0);if(received>0){
                NSString *request=[[NSString alloc] initWithBytes:input length:received encoding:NSUTF8StringEncoding];NSArray *parts=[[request componentsSeparatedByString:@"\r\n"].firstObject componentsSeparatedByString:@" "];
                NSString *path=parts.count>=2?parts[1]:@"";NSString *content=routes[path];NSData *body=[(content?:@"Not found") dataUsingEncoding:NSUTF8StringEncoding];
                NSData *header=[[NSString stringWithFormat:@"HTTP/1.1 %@\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %lu\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n",content?@"200 OK":@"404 Not Found",(unsigned long)body.length] dataUsingEncoding:NSUTF8StringEncoding];
                NSMutableData *response=[header mutableCopy];[response appendData:body];NSUInteger offset=0;while(offset<response.length){ssize_t count=send(client,(const char *)response.bytes+offset,response.length-offset,0);if(count<=0)break;offset+=count;}}
            close(client);
        }
    });
    WKWebView *web=[[WKWebView alloc] initWithFrame:CGRectZero];web.accessibilityIdentifier=@"hybrid_webview";web.translatesAutoresizingMaskIntoConstraints=NO;if(@available(iOS 16.4,*))web.inspectable=YES;
    self.mainWeb=web;web.UIDelegate=self;
    [self.view addSubview:web];[NSLayoutConstraint activateConstraints:@[
        [web.topAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.topAnchor],
        [web.bottomAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.bottomAnchor],
        [web.leadingAnchor constraintEqualToAnchor:self.view.leadingAnchor],
        [web.trailingAnchor constraintEqualToAnchor:self.view.trailingAnchor]]];
    [web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%u/",ntohs(address.sin_port)]]]];
}
- (WKWebView *)webView:(WKWebView *)webView createWebViewWithConfiguration:(WKWebViewConfiguration *)configuration forNavigationAction:(WKNavigationAction *)action windowFeatures:(WKWindowFeatures *)features {
    if(self.secondaryWeb)return nil;
    WKWebView *web=[[WKWebView alloc] initWithFrame:CGRectZero configuration:configuration];web.accessibilityIdentifier=@"hybrid_secondary_webview";web.translatesAutoresizingMaskIntoConstraints=NO;web.UIDelegate=self;if(@available(iOS 16.4,*))web.inspectable=YES;
    self.secondaryWeb=web;self.mainWeb.hidden=YES;[self.view addSubview:web];[NSLayoutConstraint activateConstraints:@[[web.topAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.topAnchor],[web.bottomAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.bottomAnchor],[web.leadingAnchor constraintEqualToAnchor:self.view.leadingAnchor],[web.trailingAnchor constraintEqualToAnchor:self.view.trailingAnchor]]];
    self.navigationItem.rightBarButtonItem=[[UIBarButtonItem alloc] initWithTitle:@"Main page" style:UIBarButtonItemStylePlain target:self action:@selector(showMainPage)];self.navigationItem.rightBarButtonItem.accessibilityIdentifier=@"hybrid_main_button";
    return web;
}
- (void)showMainPage {[self.secondaryWeb removeFromSuperview];self.secondaryWeb=nil;self.mainWeb.hidden=NO;}
- (void)dealloc {if(self.listener>=0){shutdown(self.listener,SHUT_RDWR);close(self.listener);}}
@end
#endif

@interface LabController : UIViewController <UITableViewDataSource,UITableViewDelegate>
#if DEBUG
@property(nonatomic,strong) UILabel *calibrationReady;
@property(nonatomic) NSUInteger calibrationStalls;
@property(nonatomic,strong) id<MTLDevice> labGPU;
@property(nonatomic,strong) id<MTLBuffer> labGPUBuffer;
@property(nonatomic,strong) id<MTLCommandQueue> labGPUQueue;
@property(nonatomic) BOOL labGPUSampling;
#endif
@property(nonatomic,strong) UITextField *nickname;
@property(nonatomic,strong) UILabel *status;
@property(nonatomic,strong) UILabel *optionStatus;
@property(nonatomic,strong) UISwitch *option;
@property(nonatomic,strong) GesturePad *pad;
@property(nonatomic,strong) NSTimer *bridgeTimer;
@end

@implementation LabController
- (UIButton *)button:(NSString *)title identifier:(NSString *)identifier action:(SEL)action {
    UIButton *button=[UIButton buttonWithType:UIButtonTypeSystem];
    [button setTitle:title forState:UIControlStateNormal];button.accessibilityIdentifier=identifier;
    [button addTarget:self action:action forControlEvents:UIControlEventTouchUpInside];
    [button.heightAnchor constraintEqualToConstant:44].active=YES;
    return button;
}
- (void)viewDidLoad {
    [super viewDidLoad];self.title=@"IATest Lab";self.view.backgroundColor=UIColor.systemBackgroundColor;
#if DEBUG
    // Controlled fixture allocation verifies target-process GPU telemetry only.
    self.labGPU=MTLCreateSystemDefaultDevice();self.labGPUBuffer=[self.labGPU newBufferWithLength:32768 options:MTLResourceStorageModeShared];
    self.labGPUQueue=[self.labGPU newCommandQueue];
    IATestBridgeObserveMetalDevice(self.labGPU);UIDevice.currentDevice.batteryMonitoringEnabled=YES;
    self.navigationItem.rightBarButtonItem=[[UIBarButtonItem alloc] initWithTitle:@"Hybrid" style:UIBarButtonItemStylePlain target:self action:@selector(openHybrid)];self.navigationItem.rightBarButtonItem.accessibilityIdentifier=@"hybrid_button";
    self.navigationItem.leftBarButtonItem=[[UIBarButtonItem alloc] initWithTitle:@"GPU sample" style:UIBarButtonItemStylePlain target:self action:@selector(toggleGPU)];self.navigationItem.leftBarButtonItem.accessibilityIdentifier=@"metal_gpu_toggle";
#endif
    UIBarButtonItem *back=[[UIBarButtonItem alloc] initWithTitle:@"Back to lab" style:UIBarButtonItemStylePlain target:nil action:nil];
    back.accessibilityIdentifier=@"back_to_lab";self.navigationItem.backBarButtonItem=back;
    UILabel *ready=[UILabel new];ready.text=@"Ready";ready.accessibilityIdentifier=@"ready_label";
#if DEBUG
    self.calibrationReady=ready;ready.accessibilityValue=@"stalls=0";
#endif
    self.nickname=[UITextField new];self.nickname.placeholder=@"Nickname";
    self.nickname.accessibilityIdentifier=@"nickname_field";
    self.nickname.borderStyle=UITextBorderStyleRoundedRect;
    self.nickname.autocorrectionType=UITextAutocorrectionTypeNo;
    self.nickname.autocapitalizationType=UITextAutocapitalizationTypeNone;
    self.nickname.returnKeyType=UIReturnKeyDone;
    self.nickname.clearButtonMode=UITextFieldViewModeWhileEditing;
    self.nickname.text=[NSUserDefaults.standardUserDefaults stringForKey:@"nickname"]?:@"";
    [self.nickname.heightAnchor constraintEqualToConstant:44].active=YES;
    UIStackView *buttons=[[UIStackView alloc] initWithArrangedSubviews:@[
        [self button:@"Save" identifier:@"save_button" action:@selector(save)],
        [self button:@"Reset lab" identifier:@"reset_button" action:@selector(reset)]]];
    buttons.axis=UILayoutConstraintAxisHorizontal;buttons.distribution=UIStackViewDistributionFillEqually;
    self.status=[UILabel new];self.status.accessibilityIdentifier=@"saved_status";
    self.status.text=self.nickname.text.length?[@"Saved: " stringByAppendingString:self.nickname.text]:@"Not saved";
    self.option=[UISwitch new];self.option.accessibilityIdentifier=@"option_toggle";
    [self.option addTarget:self action:@selector(toggle) forControlEvents:UIControlEventValueChanged];
    self.optionStatus=[UILabel new];self.optionStatus.text=@"Option off";self.optionStatus.accessibilityIdentifier=@"option_status";
    UIStackView *optionRow=[[UIStackView alloc] initWithArrangedSubviews:@[self.optionStatus,self.option]];
    optionRow.axis=UILayoutConstraintAxisHorizontal;optionRow.alignment=UIStackViewAlignmentCenter;
    self.pad=[GesturePad new];[self.pad.heightAnchor constraintEqualToConstant:105].active=YES;
    UITableView *table=[[UITableView alloc] initWithFrame:CGRectZero style:UITableViewStylePlain];
    table.accessibilityIdentifier=@"item_list";table.dataSource=self;table.delegate=self;table.rowHeight=44;
#if DEBUG
    if([NSProcessInfo.processInfo.environment[@"IATEST_LAB_TRACE_POINTER"] isEqual:@"1"])table.accessibilityValue=@"offset_y=0.00";
#endif
    UIStackView *stack=[[UIStackView alloc] initWithArrangedSubviews:@[ready,self.nickname,buttons,self.status,optionRow,self.pad,table]];
    stack.axis=UILayoutConstraintAxisVertical;stack.spacing=8;stack.translatesAutoresizingMaskIntoConstraints=NO;
    [self.view addSubview:stack];UILayoutGuide *safe=self.view.safeAreaLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[[stack.topAnchor constraintEqualToAnchor:safe.topAnchor constant:8],
        [stack.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:16],
        [stack.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-16],
        [stack.bottomAnchor constraintEqualToAnchor:safe.bottomAnchor constant:-8],
        [table.heightAnchor constraintGreaterThanOrEqualToConstant:80]]];
    [self.status setContentCompressionResistancePriority:UILayoutPriorityRequired forAxis:UILayoutConstraintAxisVertical];
    [table setContentHuggingPriority:UILayoutPriorityDefaultLow forAxis:UILayoutConstraintAxisVertical];
#if DEBUG
    __weak LabController *weakSelf=self;
    self.bridgeTimer=[NSTimer scheduledTimerWithTimeInterval:0.25 repeats:YES block:^(NSTimer *timer){
        LabController *owner=weakSelf;if(!owner){[timer invalidate];return;}
        id<MTLCommandBuffer> command=owner.labGPUSampling&&owner.navigationController.topViewController==owner?[owner.labGPUQueue commandBuffer]:nil;
        if(command){id<MTLBlitCommandEncoder> encoder=[command blitCommandEncoder];
            [encoder fillBuffer:owner.labGPUBuffer range:NSMakeRange(0,owner.labGPUBuffer.length) value:1];[encoder endEncoding];
            IATestBridgeObserveMetalCommandBuffer(command);
            [command addCompletedHandler:^(id<MTLCommandBuffer> buffer){dispatch_async(dispatch_get_main_queue(),^{[weakSelf publishBridge];});}];[command commit];
        }else [owner publishBridge];
    }];
#endif
}
#if DEBUG
- (void)publishBridge {
    IATestBridgePublishInputState(@{@"lab_ready":@YES},@{@"nickname":@"nickname_field",@"save":@"save_button"},@{@"nickname":self.nickname},nil);
    if([NSProcessInfo.processInfo.environment[@"IATEST_LAB_TRACE_POINTER"] isEqual:@"1"]){
        NSMutableArray *languages=[NSMutableArray new];
        for(UITextInputMode *mode in UITextInputMode.activeInputModes)if(mode.primaryLanguage)[languages addObject:mode.primaryLanguage];
        // Apple documents the AssistiveTouch getter as reliable only with Guided Access.
        BOOL assistiveObservable=UIAccessibilityIsGuidedAccessEnabled();
        NSDictionary *state=@{@"timestamp_ms":@((long long)(NSDate.date.timeIntervalSince1970*1000)),
            @"primary_language":self.nickname.textInputMode.primaryLanguage?:@"",@"active_languages":languages,
            @"voice_over":@(UIAccessibilityIsVoiceOverRunning()),@"switch_control":@(UIAccessibilityIsSwitchControlRunning()),
            @"assistive_touch_observable":@(assistiveObservable),
            @"assistive_touch":assistiveObservable?@(UIAccessibilityIsAssistiveTouchRunning()):NSNull.null};
        NSURL *cache=[NSFileManager.defaultManager URLForDirectory:NSCachesDirectory inDomain:NSUserDomainMask appropriateForURL:nil create:YES error:nil];
        [[NSJSONSerialization dataWithJSONObject:state options:0 error:nil] writeToURL:[cache URLByAppendingPathComponent:@"iatest-lab-input.json"] options:NSDataWritingAtomic error:nil];
    }
}
- (void)toggleGPU {self.labGPUSampling=!self.labGPUSampling;self.navigationItem.leftBarButtonItem.title=self.labGPUSampling?@"GPU stop":@"GPU sample";}
- (void)openHybrid {[self.navigationController pushViewController:[HybridController new] animated:YES];}
#endif
- (void)save {
    [self.view endEditing:YES];
    [NSUserDefaults.standardUserDefaults setObject:self.nickname.text?:@"" forKey:@"nickname"];
    self.status.text=[@"Saved: " stringByAppendingString:self.nickname.text?:@""];
}
- (void)reset {
    [self.view endEditing:YES];self.nickname.text=@"";self.status.text=@"Not saved";
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"nickname"];
    self.option.on=NO;[self toggle];self.pad.pans=0;self.pad.pinches=0;self.pad.scale=1;[self.pad updateValue];
}
- (void)toggle { self.optionStatus.text=self.option.on?@"Option on":@"Option off"; }
- (NSInteger)tableView:(UITableView *)tableView numberOfRowsInSection:(NSInteger)section {return 100;}
#if DEBUG
// Only the owned Debug fixture exposes a positive control. No setting persists.
- (void)scrollViewDidScroll:(UIScrollView *)scrollView {
    if([NSProcessInfo.processInfo.environment[@"IATEST_LAB_TRACE_POINTER"] isEqual:@"1"])
        scrollView.accessibilityValue=[NSString stringWithFormat:@"offset_y=%.2f",scrollView.contentOffset.y];
    if(![NSProcessInfo.processInfo.environment[@"IATEST_LAB_INJECT_HITCH"] isEqual:@"1"])return;
    if(scrollView.contentOffset.y<=1){self.calibrationStalls=0;self.calibrationReady.accessibilityValue=@"stalls=0";return;}
    if(scrollView.contentOffset.y>60&&self.calibrationStalls<3){
        self.calibrationStalls++;[NSThread sleepForTimeInterval:0.25];
        self.calibrationReady.accessibilityValue=[NSString stringWithFormat:@"stalls=%lu",(unsigned long)self.calibrationStalls];
    }
}
#endif
- (UITableViewCell *)tableView:(UITableView *)tableView cellForRowAtIndexPath:(NSIndexPath *)path {
    UITableViewCell *cell=[tableView dequeueReusableCellWithIdentifier:@"row"]?:[[UITableViewCell alloc] initWithStyle:UITableViewCellStyleDefault reuseIdentifier:@"row"];
    cell.textLabel.text=[NSString stringWithFormat:@"Item %ld",(long)path.row+1];
    cell.accessibilityIdentifier=[NSString stringWithFormat:@"item_%ld",(long)path.row+1];
    return cell;
}
- (void)tableView:(UITableView *)tableView didSelectRowAtIndexPath:(NSIndexPath *)path {
    [self.view endEditing:YES];UIViewController *detail=[UIViewController new];detail.title=@"Item detail";
    detail.view.backgroundColor=UIColor.systemBackgroundColor;
    UILabel *label=[UILabel new];label.text=[NSString stringWithFormat:@"Item %ld detail",(long)path.row+1];
    label.accessibilityIdentifier=@"detail_label";label.translatesAutoresizingMaskIntoConstraints=NO;
    [detail.view addSubview:label];[NSLayoutConstraint activateConstraints:@[
        [label.centerXAnchor constraintEqualToAnchor:detail.view.centerXAnchor],
        [label.centerYAnchor constraintEqualToAnchor:detail.view.centerYAnchor]]];
    [self.navigationController pushViewController:detail animated:YES];
}
@end

@interface LabScene : UIResponder <UIWindowSceneDelegate>
@property(nonatomic,strong) UIWindow *window;
@end
@implementation LabScene
- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    if(![scene isKindOfClass:UIWindowScene.class])return;
#if DEBUG
    self.window=[[LabInputWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
#else
    self.window=[[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
#endif
    self.window.rootViewController=[[UINavigationController alloc] initWithRootViewController:[LabController new]];
    [self.window makeKeyAndVisible];
}
@end

@interface LabDelegate : UIResponder <UIApplicationDelegate>
@end
@implementation LabDelegate
- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)options {return YES;}
- (UISceneConfiguration *)application:(UIApplication *)application configurationForConnectingSceneSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    UISceneConfiguration *configuration=[[UISceneConfiguration alloc] initWithName:@"IATest Lab" sessionRole:session.role];
    configuration.delegateClass=LabScene.class;return configuration;
}
@end

int main(int argc,char **argv){@autoreleasepool{return UIApplicationMain(argc,argv,nil,NSStringFromClass(LabDelegate.class));}}
