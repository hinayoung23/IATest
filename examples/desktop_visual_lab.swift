// Owned visual acceptance fixture: tile labels are pixels, not AX children.
import AppKit

final class Tiles: NSView {
    var labels:[String]=[]
    var selected:((String)->Void)?
    override var isFlipped:Bool {true}
    override func isAccessibilityEnabled()->Bool {true}
    override func acceptsFirstMouse(for event:NSEvent?)->Bool {true}
    func tile(_ n:Int)->NSRect {NSRect(x:14+(n%3)*164,y:14+(n/3)*92,width:144,height:74)}
    func shuffle(){labels=(0..<9).map{_ in String(UUID().uuidString.replacingOccurrences(of:"-",with:"").prefix(5))};needsDisplay=true}
    override func draw(_ dirty:NSRect){
        NSColor.white.setFill();bounds.fill()
        let colors:[NSColor]=[.systemBlue,.systemRed,.systemGreen,.systemPurple,.systemBrown,.systemIndigo,.systemTeal,.systemPink,.darkGray]
        for (n,label) in labels.enumerated(){let r=tile(n);colors[n].setFill();NSBezierPath(roundedRect:r,xRadius:8,yRadius:8).fill();let attrs:[NSAttributedString.Key:Any]=[.font:NSFont.monospacedSystemFont(ofSize:22,weight:.bold),.foregroundColor:NSColor.white];let text=label as NSString;text.draw(at:NSPoint(x:r.midX-text.size(withAttributes:attrs).width/2,y:r.midY-13),withAttributes:attrs)}
    }
    override func mouseUp(with event:NSEvent){let p=convert(event.locationInWindow,from:nil);selected?(labels.enumerated().first{tile($0.offset).contains(p)}?.element ?? "MISS")}
}

final class Lab:NSObject,NSApplicationDelegate {
    var window:NSWindow!
    let canvas=Tiles(frame:NSRect(x:0,y:0,width:520,height:300))
    let status=NSTextField(labelWithString:"Waiting"),count=NSTextField(labelWithString:"0")
    var clicks=0
    func manifest(){
        let rows=canvas.labels.enumerated().map{n,label -> [String:Any] in let r=canvas.tile(n);return ["label":label,"rect":[r.minX,r.minY,r.width,r.height]]}
        if CommandLine.arguments.count>1,let data=try? JSONSerialization.data(withJSONObject:["tiles":rows]){try? data.write(to:URL(fileURLWithPath:CommandLine.arguments[1]),options:.atomic)}
    }
    @objc func reset(){clicks=0;status.stringValue="Waiting";count.stringValue="0";canvas.shuffle();manifest()}
    func applicationDidFinishLaunching(_ notification:Notification){
        NSApp.appearance=NSAppearance(named:.aqua)
        window=NSWindow(contentRect:NSRect(x:0,y:0,width:560,height:430),styleMask:[.titled,.closable],backing:.buffered,defer:false);window.title="IATest Visual Lab"
        canvas.setAccessibilityElement(true);canvas.setAccessibilityRole(.group);canvas.setAccessibilityIdentifier("vision_canvas");canvas.setAccessibilityLabel("Visual tiles")
        status.setAccessibilityIdentifier("vision_status");count.setAccessibilityIdentifier("vision_count")
        let button=NSButton(title:"New tiles",target:self,action:#selector(reset));button.setAccessibilityIdentifier("vision_reset")
        let stack=NSStackView(views:[canvas,status,count,button]);stack.orientation = .vertical;stack.spacing=8;stack.translatesAutoresizingMaskIntoConstraints=false
        canvas.widthAnchor.constraint(equalToConstant:520).isActive=true;canvas.heightAnchor.constraint(equalToConstant:300).isActive=true
        window.contentView!.addSubview(stack);stack.centerXAnchor.constraint(equalTo:window.contentView!.centerXAnchor).isActive=true;stack.centerYAnchor.constraint(equalTo:window.contentView!.centerYAnchor).isActive=true
        canvas.selected={label in self.clicks+=1;self.status.stringValue=label;self.count.stringValue=String(self.clicks)}
        reset();window.center();window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {true}
}
let app=NSApplication.shared,delegate=Lab();app.delegate=delegate;app.setActivationPolicy(.regular);app.run()
