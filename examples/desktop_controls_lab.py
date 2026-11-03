"""Small owned GTK3 fixture; launch with the desktop's python3-gi interpreter."""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk


def accessible(widget, identifier):
    obj = widget.get_accessible()
    obj.set_name(identifier)
    obj.set_accessible_id(identifier)
    return widget


window = Gtk.Window(title="IATest Linux Desktop Lab")
window.set_default_size(500, 360)
window.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
window.add(box)
entry = accessible(Gtk.Entry(), "nickname")
status = accessible(Gtk.Label(label="Not saved"), "status")
counter = accessible(Gtk.Label(label="0"), "count")
password = accessible(Gtk.Entry(), "password")
password.set_visibility(False)
password.set_text("private-fixture-secret")
save = accessible(Gtk.Button(label="Save"), "save")
actions = 0


def saved(*_):
    global actions
    actions += 1
    status.set_text("Saved: " + entry.get_text())
    counter.set_text(str(actions))


save.connect("clicked", saved)
entry.connect("activate", saved)
for widget in (entry, save, status, counter, password):
    box.pack_start(widget, False, False, 0)
for number in range(2):
    duplicate = accessible(Gtk.Button(label="Duplicate"), "duplicate_" + str(number))
    duplicate.get_accessible().set_name("Duplicate")
    duplicate.connect("clicked", saved)
    box.pack_start(duplicate, False, False, 0)
events = accessible(Gtk.Label(label="Waiting"), "events")
pads = Gtk.Box(spacing=20)
for identifier in ("source", "destination"):
    pad = accessible(Gtk.EventBox(), identifier)
    pad.add(Gtk.Label(label=identifier))
    pad.set_size_request(170, 70)
    pad.add_events(
        Gdk.EventMask.BUTTON_PRESS_MASK
        | Gdk.EventMask.BUTTON_RELEASE_MASK
        | Gdk.EventMask.POINTER_MOTION_MASK
        | Gdk.EventMask.SCROLL_MASK
    )

    def down(widget, event):
        widget.began = (event.x, event.y)
        if event.button == 3:
            events.set_text("right_click")
        return True

    def up(widget, event):
        if event.button == 1:
            x, y = getattr(widget, "began", (event.x, event.y))
            events.set_text(
                "drag" if abs(event.x - x) + abs(event.y - y) > 20 else "click"
            )
        return True

    pad.connect("button-press-event", down)
    pad.connect("button-release-event", up)
    pad.connect("scroll-event", lambda *_: events.set_text("scroll") or True)
    pads.pack_start(pad, True, True, 0)
box.pack_start(pads, False, False, 0)
box.pack_start(events, False, False, 0)
window.show_all()
window.present()
entry.grab_focus()
Gtk.main()
