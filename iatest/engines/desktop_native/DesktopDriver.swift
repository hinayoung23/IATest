import Foundation
import AppKit
import ApplicationServices
import ScreenCaptureKit

enum Failure: Error { case message(String) }
func need(_ test: Bool,_ message: String) throws { if !test { throw Failure.message(message) } }
func attr(_ e:AXUIElement,_ key:String)->AnyObject? {var value:CFTypeRef?;return AXUIElementCopyAttributeValue(e,key as CFString,&value) == .success ? value : nil}
func text(_ e:AXUIElement,_ key:String)->String {return attr(e,key) as? String ?? ""}
func bool(_ e:AXUIElement,_ key:String)->Bool {return (attr(e,key) as? NSNumber)?.boolValue ?? false}
func row(_ e:AXUIElement)->[String:Any] {let secure=text(e,kAXSubroleAttribute)=="AXSecureTextField";return ["identifier":text(e,kAXIdentifierAttribute),"name":String(text(e,kAXTitleAttribute).prefix(500)),"role":text(e,kAXRoleAttribute),"enabled":bool(e,kAXEnabledAttribute),"value":secure ? NSNull():String(text(e,kAXValueAttribute).prefix(2000)),"truncated_attributes": [(text(e,kAXTitleAttribute).count>500 ? "name":""),(text(e,kAXValueAttribute).count>2000 ? "value":"")].filter{!$0.isEmpty}]}
func rect(_ e:AXUIElement)throws->CGRect {
 guard let position=attr(e,kAXPositionAttribute),let size=attr(e,kAXSizeAttribute) else {throw Failure.message("Target geometry unavailable")}
 var p=CGPoint.zero,s=CGSize.zero
 try need(CFGetTypeID(position)==AXValueGetTypeID() && CFGetTypeID(size)==AXValueGetTypeID(),"Invalid target geometry")
 try need(AXValueGetValue(position as! AXValue,.cgPoint,&p) && AXValueGetValue(size as! AXValue,.cgSize,&s) && s.width>0 && s.height>0,"Empty target geometry")
 return CGRect(origin:p,size:s)
}
func hit(_ point:CGPoint,_ pid:pid_t,_ expected:AXUIElement)throws {
 var element:AXUIElement?;try need(AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(),Float(point.x),Float(point.y),&element) == .success,"Pointer hit test failed")
 var owner:pid_t=0;try need(element != nil && AXUIElementGetPid(element!,&owner) == .success && owner==pid,"Target is obscured by another App")
 var current=element;var matched=false
 for _ in 0..<32 {guard let node=current else {break};if CFEqual(node,expected){matched=true;break};current=attr(node,kAXParentAttribute) as! AXUIElement?}
 try need(matched,"Pointer target is obscured or its accessibility identity changed")
}
@available(macOS 14.0, *)
func capture(_ element:AXUIElement,_ pid:pid_t)throws->[String:Any] {
 // A screenshot may be the first graphics operation in this CLI process.
 // Initialize AppKit's WindowServer connection before ScreenCaptureKit.
 _ = NSApplication.shared
 try need(CGPreflightScreenCaptureAccess(),"Allow Screen Recording for the desktop driver before screenshots")
 let region=try rect(element);try need(region.width<=1024 && region.height<=1024 && region.width*region.height<=524288,"Screenshot region exceeds pixel budget")
 let ready=DispatchSemaphore(value:0);var content:SCShareableContent?
 SCShareableContent.getExcludingDesktopWindows(false,onScreenWindowsOnly:true){value,_ in content=value;ready.signal()}
 try need(ready.wait(timeout:.now()+3) == .success,"Screen capture inventory timed out")
 let windows=(content?.windows ?? []).filter{$0.owningApplication?.processID==pid && $0.frame.contains(region)}
 try need(windows.count==1,"One owned visible window must contain the screenshot region")
 try need(windows[0].frame.width*windows[0].frame.height<=16777216,"Owned window exceeds capture budget")
 let window=windows[0],config=SCStreamConfiguration();config.width=Int(window.frame.width);config.height=Int(window.frame.height);config.showsCursor=false;config.ignoreShadowsSingleWindow=true
 let done=DispatchSemaphore(value:0);var captured:CGImage?
 SCScreenshotManager.captureImage(contentFilter:SCContentFilter(desktopIndependentWindow:window),configuration:config){value,_ in captured=value;done.signal()}
 try need(done.wait(timeout:.now()+3) == .success,"Screen capture timed out")
 guard let image=captured,let cropped=image.cropping(to:CGRect(x:region.minX-window.frame.minX,y:region.minY-window.frame.minY,width:region.width,height:region.height)),let data=NSBitmapImageRep(cgImage:cropped).representation(using:.png,properties:[:]) else {throw Failure.message("Owned window capture failed")}
 try need(data.count<=700000,"Screenshot exceeds byte budget")
 return ["data":data.base64EncodedString(),"geometry":[region.minX,region.minY,region.width,region.height]]
}
var boundPID:pid_t=0
var boundDate:Date?=nil
var sequence=0
while let line=readLine() {
 var reply:[String:Any]=[:]
 do {
  try need(line.utf8.count<=65536,"Input quota exceeded")
  guard let input=try JSONSerialization.jsonObject(with:Data(line.utf8)) as? [String:Any] else {throw Failure.message("Expected a JSON object")}
  let seq=input["sequence"] as? Int ?? 0;reply["sequence"]=seq
  try need(seq==sequence+1,"Invalid operation sequence");sequence=seq
  let pid=pid_t(input["process_id"] as? Int ?? 0),appID=input["app_id"] as? String ?? ""
  guard let app=NSRunningApplication(processIdentifier:pid) else {throw Failure.message("Selected process is absent")}
  try need(app.bundleIdentifier==appID,"App identity mismatch")
  if boundPID==0 {boundPID=pid;boundDate=app.launchDate}
  try need(boundPID==pid && boundDate==app.launchDate,"Process generation changed")
  try need(AXIsProcessTrusted(),"macOS Accessibility permission is required; no permission settings were changed")
  let op=input["op"] as? String ?? "",target=input["target"] as? [String:String] ?? [:]
  try need(["query","click","fill","press","right_click","drag","scroll","screenshot","pointer_click"].contains(op) && !target.isEmpty,"Unsupported operation/empty target")
  let root=AXUIElementCreateApplication(pid);AXUIElementSetMessagingTimeout(root,0.25)
  var pending:[(AXUIElement,Int)]=[(root,0)],found:[AXUIElement]=[];var visited=0,complete=true
  let began=ProcessInfo.processInfo.systemUptime
  while !pending.isEmpty {
   let (node,depth)=pending.removeLast();visited+=1
   if visited>1024 || ProcessInfo.processInfo.systemUptime-began>0.25 {complete=false;break}
   let values=["identifier":text(node,kAXIdentifierAttribute),"name":text(node,kAXTitleAttribute),"role":text(node,kAXRoleAttribute)]
   if target.allSatisfy({values[$0.key]==$0.value}) {found.append(node);if found.count>=4 {complete=false;break}}
   if let children=attr(node,kAXChildrenAttribute) as? [AXUIElement] {if depth>=32 && !children.isEmpty {complete=false}else{for child in children.reversed(){pending.append((child,depth+1))}}}
  }
  if op=="query" {reply["result"]=["complete":complete,"match_count":found.count,"visited_nodes":visited,"candidates":found.prefix(3).map(row)]}
  else {
   try need(complete && found.count==1,"Action requires exactly one current complete target")
   let e=found[0];try need(op=="screenshot" || bool(e,kAXEnabledAttribute) || (["right_click","drag","scroll","pointer_click"].contains(op) && attr(e,kAXEnabledAttribute)==nil),"Target is disabled")
   if op=="screenshot" {if #available(macOS 14.0, *) {reply["result"]=try capture(e,pid)} else {throw Failure.message("Native window screenshots require macOS 14 or later")}}
   else if ["right_click","drag","scroll","pointer_click"].contains(op) {
    try need(app.activate(options:[]),"App activation failed");var attempts=0
    while NSWorkspace.shared.frontmostApplication?.processIdentifier != pid && attempts<20 {usleep(10000);attempts+=1}
    try need(NSWorkspace.shared.frontmostApplication?.processIdentifier==pid,"Selected App is not foreground")
    let r=try rect(e),fx=input["fraction_x"] as? Double ?? 0.5,fy=input["fraction_y"] as? Double ?? 0.5
    try need(fx.isFinite && fy.isFinite && fx>=0 && fx<1 && fy>=0 && fy<1,"Invalid pointer fraction")
    let start=CGPoint(x:r.minX+r.width*fx,y:r.minY+r.height*fy);try hit(start,pid,e)
    func mouse(_ type:CGEventType,_ point:CGPoint,_ button:CGMouseButton)throws {try need(NSWorkspace.shared.frontmostApplication?.processIdentifier==pid,"Foreground changed; input delivery may be partial");guard let event=CGEvent(mouseEventSource:nil,mouseType:type,mouseCursorPosition:point,mouseButton:button) else {throw Failure.message("Pointer event unavailable")};event.post(tap:.cghidEventTap)}
    if op=="drag" {
     guard let destination=input["destination"] as? [String:String],!destination.isEmpty else {throw Failure.message("Drag destination required")}
     var nodes:[(AXUIElement,Int)]=[(root,0)],matches:[AXUIElement]=[];var count=0;let started=ProcessInfo.processInfo.systemUptime
     while !nodes.isEmpty {let (n,depth)=nodes.removeLast();count+=1;try need(count<=1024 && depth<=32 && ProcessInfo.processInfo.systemUptime-started<0.25,"Destination query budget exceeded");let f=["identifier":text(n,kAXIdentifierAttribute),"name":text(n,kAXTitleAttribute),"role":text(n,kAXRoleAttribute)];if destination.allSatisfy({f[$0.key]==$0.value}){matches.append(n)};nodes += (attr(n,kAXChildrenAttribute) as? [AXUIElement] ?? []).map{($0,depth+1)}}
     try need(matches.count==1 && (bool(matches[0],kAXEnabledAttribute) || attr(matches[0],kAXEnabledAttribute)==nil),"Drag requires one enabled destination");let d=try rect(matches[0]),end=CGPoint(x:d.midX,y:d.midY);try hit(end,pid,matches[0])
     try mouse(.mouseMoved,start,.left);try mouse(.leftMouseDown,start,.left)
     defer {CGEvent(mouseEventSource:nil,mouseType:.leftMouseUp,mouseCursorPosition:CGEvent(source:nil)?.location ?? start,mouseButton:.left)?.post(tap:.cghidEventTap)}
     for n in 1...12 {let f=Double(n)/12;try mouse(.leftMouseDragged,CGPoint(x:start.x+(end.x-start.x)*f,y:start.y+(end.y-start.y)*f),.left);usleep(15000)}
    } else if op=="scroll" {
     let dx=input["delta_x"] as? Int ?? 0,dy=input["delta_y"] as? Int ?? 0;try need(abs(dx)<=20 && abs(dy)<=20 && (dx != 0 || dy != 0),"Invalid wheel steps")
     try mouse(.mouseMoved,start,.left);guard let event=CGEvent(scrollWheelEvent2Source:nil,units:.line,wheelCount:2,wheel1:Int32(-dy),wheel2:Int32(-dx),wheel3:0) else {throw Failure.message("Scroll event unavailable")};event.post(tap:.cghidEventTap)
    } else {let right=op=="right_click";try mouse(.mouseMoved,start,.left);try mouse(right ? .rightMouseDown:.leftMouseDown,start,right ? .right:.left);defer {CGEvent(mouseEventSource:nil,mouseType:right ? .rightMouseUp:.leftMouseUp,mouseCursorPosition:start,mouseButton:right ? .right:.left)?.post(tap:.cghidEventTap)}}
   }
   else if op=="click" {try need(AXUIElementPerformAction(e,kAXPressAction as CFString) == .success,"AXPress was rejected")}
   else if op=="fill" {let value=input["value"] as? String ?? "";try need(value.utf8.count<=16384,"Text quota exceeded");try need(AXUIElementSetAttributeValue(e,kAXValueAttribute as CFString,value as CFString) == .success,"AX value was rejected")}
   else {
    let keys:[String:CGKeyCode]=["Enter":36,"Tab":48,"Escape":53,"Backspace":51,"Delete":117,"Space":49,"ArrowLeft":123,"ArrowRight":124,"ArrowUp":126,"ArrowDown":125,"Home":115,"End":119,"PageUp":116,"PageDown":121]
    guard let key=keys[input["key"] as? String ?? ""] else {throw Failure.message("Unsupported key")}
    try need(app.activate(options:[]),"App activation failed")
    try need(AXUIElementSetAttributeValue(e,kAXFocusedAttribute as CFString,kCFBooleanTrue) == .success,"Focus rejected")
    let source=CGEventSource(stateID:.hidSystemState)
    guard let down=CGEvent(keyboardEventSource:source,virtualKey:key,keyDown:true),let up=CGEvent(keyboardEventSource:source,virtualKey:key,keyDown:false) else {throw Failure.message("Key events unavailable")}
    down.postToPid(pid);up.postToPid(pid)
   }
   if op != "screenshot" {reply["result"]=["dispatch":"acknowledged","backend":"AX","business_postcondition":"pending"]}
  }
 } catch Failure.message(let error) {reply["error"]=error} catch {reply["error"]="Desktop request rejected"}
 if let data=try? JSONSerialization.data(withJSONObject:reply,options:[.sortedKeys]),let string=String(data:data,encoding:.utf8){print(string);fflush(stdout)}
}
