/* IATest's scoped GNOME adapter. No input injection, Eval or permission bypass. */
const {Gio, GLib, Meta, Clutter, Shell, St} = imports.gi;
const Main = imports.ui.main;
const ModalDialog = imports.ui.modalDialog;
// Loads GNOME's own promisified screenshot methods.
const Screenshot = imports.ui.screenshot;
const BUS = 'dev.iatest.Wayland';
const PATH = '/dev/iatest/Wayland';
const XML = `<node><interface name="dev.iatest.Wayland">
<method name="Bind"><arg type="u" direction="in"/><arg type="s" direction="out"/></method>
<method name="Inspect"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Check"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="d" direction="in"/><arg type="d" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="out"/></method>
<method name="Capture"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/><arg type="ay" direction="out"/></method>
<method name="Release"><arg type="s" direction="in"/></method>
</interface></node>`;

function requireValue(value, message) {if (!value) throw new Error(message);}
function file(path) {let [ok, bytes] = GLib.file_get_contents(path);requireValue(ok, 'Process identity unavailable');return imports.byteArray.toString(bytes);}
function identity(pid) {
    requireValue(Number.isInteger(pid) && pid > 0, 'Invalid PID');
    const own = /^Uid:\s+(\d+)/m.exec(file('/proc/self/status'));
    const user = /^Uid:\s+(\d+)/m.exec(file(`/proc/${pid}/status`));
    requireValue(own && user && own[1] === user[1], 'Process belongs to another desktop user');
    const stat = file(`/proc/${pid}/stat`), tail = stat.slice(stat.lastIndexOf(')') + 1).trim().split(/\s+/);
    requireValue(tail[0] !== 'Z', 'Process exited');
    return tail[19];
}
function rect(r) {return [r.x,r.y,r.width,r.height];}
function invocationError(invocation, error) {invocation.return_dbus_error('dev.iatest.Wayland.Rejected', String(error.message || error).slice(0,500));}

class Service {
    constructor() {
        this.epoch = GLib.uuid_string_random();this.bindings = new Map();this.pending = null;
        this.object = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this.object.export(Gio.DBus.session, PATH);
        this.name = Gio.bus_own_name_on_connection(Gio.DBus.session, BUS, Gio.BusNameOwnerFlags.NONE, null, null);
        this.watch = Gio.DBus.session.signal_subscribe('org.freedesktop.DBus','org.freedesktop.DBus','NameOwnerChanged','/org/freedesktop/DBus',null,Gio.DBusSignalFlags.NONE,
            (_c,_s,_p,_i,_n,params) => {let [name,_old,next] = params.deep_unpack();if (!next) {for (let [token,b] of this.bindings) if (b.sender === name) this.bindings.delete(token);if(this.pending?.sender === name) this.pending.cancel();}});
    }
    valid(token, invocation) {
        const b = this.bindings.get(token);
        requireValue(b && b.sender === invocation.get_sender() && b.until > GLib.get_monotonic_time(), 'Capture consent is missing or expired');
        requireValue(identity(b.pid) === b.start, 'Process generation changed');
        requireValue(!Main.screenShield?.locked && !Main.sessionMode.isLocked, 'Desktop is locked');
        return b;
    }
    windows(b) {
        const result = global.get_window_actors().filter(a => a.meta_window?.get_pid() === b.pid && a.meta_window.get_client_type() === Meta.WindowClientType.WAYLAND);
        requireValue(result.length <= 16, 'Window count exceeds budget');return result;
    }
    window(b, id) {
        const matches = this.windows(b).filter(a => String(a.meta_window.get_id()) === id);
        requireValue(matches.length === 1, 'Selected window no longer exists');return matches[0];
    }
    info(actor) {
        const w = actor.meta_window;
        return {id:String(w.get_id()),frame:rect(w.get_frame_rect()),buffer:rect(w.get_buffer_rect()),
            active:global.display.focus_window === w && Main.modalCount === 0,
            visible:actor.visible && !w.minimized && w.showing_on_its_workspace()};
    }
    BindAsync([pid], invocation) {
        try {
            const start = identity(pid), sender = invocation.get_sender();
            requireValue(!Main.screenShield?.locked && !this.pending, 'Another approval is pending or desktop is locked');
            for(let [token,b] of this.bindings) if(b.until <= GLib.get_monotonic_time())this.bindings.delete(token);
            requireValue(this.bindings.size < 8, 'Capture session budget exceeded');
            const dialog = new ModalDialog.ModalDialog();
            const executable = GLib.path_get_basename(GLib.file_read_link(`/proc/${pid}/exe`));
            dialog.contentLayout.add_child(new St.Label({text:`Allow IATest to inspect and capture windows?\nApplication: ${executable} (PID ${pid})\nExpires after 10 minutes or when the client exits.\nInput needs separate system portal permission.`,style:'max-width: 560px;',x_expand:true}));
            let finished = false, timer = 0;
            const finish = allow => {
                if(finished)return;finished=true;this.pending=null;
                if(timer)GLib.source_remove(timer);
                dialog.connect('closed', () => {
                    try {
                        requireValue(allow && identity(pid) === start, 'Capture permission was declined');
                        const token = GLib.uuid_string_random();
                        this.bindings.set(token,{pid,start,sender,until:GLib.get_monotonic_time()+600000000,busy:false});
                        invocation.return_value(new GLib.Variant('(s)',[token]));
                    } catch(e) {invocationError(invocation,e);}
                    // ModalDialog destroys itself after emitting "closed".
                });
                dialog.close();
            };
            dialog.setButtons([{label:'Cancel',key:Clutter.KEY_Escape,action:()=>finish(false)},{label:'Allow',default:true,action:()=>finish(true)}]);
            this.pending={sender,cancel:()=>finish(false)};
            timer=GLib.timeout_add(GLib.PRIORITY_DEFAULT,45000,()=>{timer=0;finish(false);return GLib.SOURCE_REMOVE;});
            if(!dialog.open()) {
                this.pending=null;GLib.source_remove(timer);timer=0;dialog.destroy();
                throw new Error('Could not show capture permission dialog');
            }
        } catch(e) {invocationError(invocation,e);}
    }
    InspectAsync([token,title], invocation) {
        try {
            const b=this.valid(token,invocation);requireValue(typeof title === 'string' && title.length <= 500,'Invalid window title');
            const actors=this.windows(b).filter(a=>a.meta_window.get_title()===title);
            const monitors=Main.layoutManager.monitors.map(m=>[m.x,m.y,m.width,m.height]);
            invocation.return_value(new GLib.Variant('(s)',[JSON.stringify({epoch:this.epoch,pid:b.pid,start:b.start,windows:actors.map(a=>this.info(a)),monitors,pointer:global.get_pointer().slice(0,2)})]));
        } catch(e) {invocationError(invocation,e);}
    }
    CheckAsync([token,id,x,y,pointer], invocation) {
        try {
            const b=this.valid(token,invocation), actor=this.window(b,id), info=this.info(actor);
            requireValue(Number.isFinite(x)&&Number.isFinite(y)&&info.active&&info.visible,'Selected window is not active and visible');
            const r=info.frame;requireValue(x>=r[0]&&y>=r[1]&&x<r[0]+r[2]&&y<r[1]+r[3],'Point exceeds the selected window');
            let hit=global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE,x,y), match=false;
            for(let n=0;hit&&n<64;n++,hit=hit.get_parent()){if(hit===actor){match=true;break;}}
            if(pointer){const [px,py]=global.get_pointer();match=match&&Math.abs(px-x)<=1.5&&Math.abs(py-y)<=1.5;}
            invocation.return_value(new GLib.Variant('(b)',[match]));
        } catch(e) {invocationError(invocation,e);}
    }
    async CaptureAsync([token,id], invocation) {
        let b, stream;
        try {
            b=this.valid(token,invocation);requireValue(!b.busy,'A capture is already pending');
            const actor=this.window(b,id), before=this.info(actor);requireValue(before.active&&before.visible,'Capture requires the selected foreground window');
            requireValue(before.frame[2]>0&&before.frame[3]>0&&before.frame[2]*before.frame[3]<=4194304,'Window capture pixel budget exceeded');
            b.busy=true;stream=Gio.MemoryOutputStream.new_resizable();
            const result=await new Shell.Screenshot().screenshot_window(true,false,stream);
            const area=Array.isArray(result)?result.find(v=>v&&typeof v.width==='number'):result;
            requireValue(area&&area.width>0&&area.height>0,'Screenshot did not return its actual area');
            this.valid(token,invocation);const after=this.info(this.window(b,id));
            requireValue(after.active&&after.visible&&JSON.stringify(before)===JSON.stringify(after),'Window changed during capture');
            stream.close(null);requireValue(stream.get_data_size()<=8388608,'Window PNG byte budget exceeded');
            const data=stream.steal_as_bytes().get_data();
            // GNOME 43/44 screenshot_window reports the frame rectangle but
            // calls meta_window_actor_get_image(NULL), including CSD shadows.
            // Use the compositor's buffer extent; the client checks PNG scale.
            requireValue(JSON.stringify(rect(area))===JSON.stringify(before.frame),'Unexpected native screenshot area');
            invocation.return_value(new GLib.Variant('(say)',[JSON.stringify({epoch:this.epoch,pid:b.pid,start:b.start,window:id,area:before.buffer}),data]));
        } catch(e) {invocationError(invocation,e);}
        finally {if(b)b.busy=false;if(stream&&!stream.is_closed())stream.close(null);}
    }
    ReleaseAsync([token], invocation) {
        try {this.valid(token,invocation);this.bindings.delete(token);invocation.return_value(new GLib.Variant('()',[]));}
        catch(e){invocationError(invocation,e);}
    }
    destroy() {
        this.pending?.cancel();this.bindings.clear();
        Gio.DBus.session.signal_unsubscribe(this.watch);Gio.bus_unown_name(this.name);this.object.unexport();
    }
}
let service=null;
function init() {}
function enable() {service=new Service();}
function disable() {service?.destroy();service=null;}
