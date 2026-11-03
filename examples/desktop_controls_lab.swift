// Framework-owned deterministic AX acceptance fixture; never uses business accounts.
import AppKit

final class PointerPad: NSView {
    override func isAccessibilityEnabled() -> Bool { true }
    var report: ((String) -> Void)?
    var began = NSPoint.zero
    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func draw(_ rect: NSRect) { NSColor.systemBlue.setFill(); bounds.fill() }
    override func rightMouseDown(with event: NSEvent) { report?("right_click") }
    override func mouseDown(with event: NSEvent) { began = convert(event.locationInWindow, from:nil) }
    override func mouseUp(with event: NSEvent) { let end=convert(event.locationInWindow,from:nil);report?(abs(end.x-began.x)+abs(end.y-began.y)>20 ? "drag":"click") }
    override func scrollWheel(with event: NSEvent) { report?("scroll") }
}

final class Lab: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let input = NSTextField(string: "")
    let status = NSTextField(labelWithString: "Waiting")
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 420),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "IATest Desktop Lab"
        input.identifier = NSUserInterfaceItemIdentifier("nickname")
        input.setAccessibilityIdentifier("nickname")
        input.setAccessibilityLabel("Nickname")
        status.setAccessibilityIdentifier("status")
        let save = NSButton(title: "Save", target: self, action: #selector(saveValue))
        save.setAccessibilityIdentifier("save")
        let events=NSTextField(labelWithString:"Waiting");events.setAccessibilityIdentifier("events")
        let pads=NSStackView();pads.orientation = .horizontal;pads.spacing=20
        for id in ["source","destination"] {let pad=PointerPad();pad.setAccessibilityElement(true);pad.setAccessibilityRole(.group);pad.setAccessibilityIdentifier(id);pad.setAccessibilityLabel(id);pad.report={events.stringValue=$0};pad.translatesAutoresizingMaskIntoConstraints=false;pad.widthAnchor.constraint(equalToConstant:170).isActive=true;pad.heightAnchor.constraint(equalToConstant:70).isActive=true;pads.addArrangedSubview(pad)}
        let stack = NSStackView(views: [input, save, status, pads, events])
        stack.orientation = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            stack.centerYAnchor.constraint(equalTo: window.contentView!.centerYAnchor),
        ])
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc func saveValue() { status.stringValue = input.stringValue }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
let application = NSApplication.shared
let delegate = Lab()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
