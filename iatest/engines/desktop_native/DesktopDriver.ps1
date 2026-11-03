$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase
Add-Type -AssemblyName UIAutomationClientsideProviders
[void][Windows.Automation.AutomationElement]::RootElement
[Windows.Automation.ClientSettings]::RegisterClientSideProviderAssembly([UIAutomationClientsideProviders.UIAutomationClientSideProviders].Assembly.GetName())
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public static class IATestFocus{[DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();[DllImport("user32.dll")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);}'
Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;
public static class IATestPointer {
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint x,uint y,int data,UIntPtr extra);
 [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h,IntPtr dc,uint flags);
 [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
}
'@
[void][IATestPointer]::SetThreadDpiAwarenessContext([IntPtr](-4))
function Check-IATestPoint($x,$y,$pidValue,$expected) {
 $hit=[Windows.Automation.AutomationElement]::FromPoint([Windows.Point]::new($x,$y))
 if ($null -eq $hit -or $hit.Current.ProcessId -ne $pidValue) {throw 'Pointer target is obscured by another App'}
 $matched=$false;$current=$hit
 for($n=0;$n -lt 32 -and $null -ne $current;$n++){if([Windows.Automation.Automation]::Compare($current,$expected)){$matched=$true;break};$current=[Windows.Automation.TreeWalker]::RawViewWalker.GetParent($current)}
 if(!$matched){throw 'Pointer target is obscured or its identity changed'}
}
$sequence = 0
$bound = $null
while ($null -ne ($line = [Console]::ReadLine())) {
  $reply = @{}
  try {
    if ([Text.Encoding]::UTF8.GetByteCount($line) -gt 65536) { throw 'Input quota exceeded' }
    $value = $line | ConvertFrom-Json
    $reply.sequence = $value.sequence
    if ($value.sequence -ne ($sequence + 1)) { throw 'Sequence mismatch' }
    $sequence = $value.sequence
    $app = Get-Process -Id $value.process_id
    if ($app.ProcessName -cne $value.app_id) { throw 'App identity mismatch' }
    $generation = "$($app.Id):$($app.StartTime.ToUniversalTime().Ticks)"
    if ($null -eq $bound) { $bound = $generation }
    if ($generation -ne $bound) { throw 'Process generation changed' }
    if ($value.op -notin @('query','click','fill','press','right_click','drag','scroll','screenshot','pointer_click')) { throw 'Unsupported desktop operation' }
    if ($app.MainWindowHandle -eq 0) { throw 'App has no main window' }
    $root = [Windows.Automation.AutomationElement]::FromHandle($app.MainWindowHandle)
    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    $pending = [Collections.Generic.Stack[object]]::new()
    $pending.Push(@($root,0)); $found = [Collections.Generic.List[object]]::new()
    $watch = [Diagnostics.Stopwatch]::StartNew(); $visited = 0; $complete = $true
    if ($value.target.PSObject.Properties.Count -eq 0) { throw 'Explicit target is required' }
    while ($pending.Count -gt 0) {
      $entry = $pending.Pop(); $node = $entry[0]; $depth = $entry[1]; $visited++
      if ($visited -gt 1024 -or $watch.ElapsedMilliseconds -gt 250) { $complete=$false;break }
      $current=$node.Current
      $fields=@{identifier=$current.AutomationId;name=$current.Name;role=$current.ControlType.ProgrammaticName}
      $matched=$true
      foreach ($property in $value.target.PSObject.Properties) { if (!$fields.ContainsKey($property.Name) -or $fields[$property.Name] -cne $property.Value) {$matched=$false} }
      if ($matched) {$found.Add($node);if($found.Count -ge 4){$complete=$false;break}}
      $child=$walker.GetFirstChild($node)
      while ($null -ne $child) {if($depth -ge 32){$complete=$false;break};$pending.Push(@($child,($depth+1)));$child=$walker.GetNextSibling($child)}
    }
    if ($value.op -eq 'query') {
      $rows=@()
      foreach ($node in ($found | Select-Object -First 3)) {
        $current=$node.Current;$text=$null;$pattern=$null;$truncated=@()
        if (!$current.IsPassword -and $node.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) { $text=$pattern.Current.Value;if($text.Length -gt 2000){$text=$text.Substring(0,2000);$truncated+='value'} }
        $rows+=@{identifier=$current.AutomationId;name=$current.Name;role=$current.ControlType.ProgrammaticName;enabled=$current.IsEnabled;value=$text;truncated_attributes=$truncated}
      }
      $reply.result=@{complete=$complete;match_count=$found.Count;visited_nodes=$visited;candidates=@($rows)}
    } else {
      if (!$complete -or $found.Count -ne 1 -or !$found[0].Current.IsEnabled) {throw 'Action requires one complete enabled target'}
      $node=$found[0];$pattern=$null
      if ($value.op -eq 'screenshot') {
        Add-Type -AssemblyName System.Drawing
        $r=$node.Current.BoundingRectangle;$w=$root.Current.BoundingRectangle
        if ($r.IsEmpty -or $r.Width -le 0 -or $r.Height -le 0 -or $r.Width -gt 1024 -or $r.Height -gt 1024 -or $r.Width*$r.Height -gt 524288 -or !$w.Contains($r)) {throw 'Screenshot region exceeds the owned window or pixel budget'}
        if($w.Width*$w.Height -gt 16777216){throw 'Owned window exceeds capture budget'}
        $bitmap=[Drawing.Bitmap]::new([int]$w.Width,[int]$w.Height);$g=[Drawing.Graphics]::FromImage($bitmap);$dc=$g.GetHdc()
        try {if (![IATestPointer]::PrintWindow($app.MainWindowHandle,$dc,2)) {throw 'Owned window capture failed'}} finally {$g.ReleaseHdc($dc);$g.Dispose()}
        try {
          $clip=[Drawing.Rectangle]::new([int]($r.X-$w.X),[int]($r.Y-$w.Y),[int]$r.Width,[int]$r.Height)
          $cropped=$bitmap.Clone($clip,[Drawing.Imaging.PixelFormat]::Format32bppArgb);$stream=[IO.MemoryStream]::new()
          try {$cropped.Save($stream,[Drawing.Imaging.ImageFormat]::Png);$bytes=$stream.ToArray();if($bytes.Length -gt 700000){throw 'Screenshot byte budget exceeded'};$reply.result=@{data=[Convert]::ToBase64String($bytes);geometry=@($r.X,$r.Y,$r.Width,$r.Height)}} finally {$cropped.Dispose();$stream.Dispose()}
        } finally {$bitmap.Dispose()}
      } elseif ($value.op -in @('right_click','drag','scroll','pointer_click')) {
        [void][IATestPointer]::SetForegroundWindow($app.MainWindowHandle)
        [uint32]$foregroundPID=0;[void][IATestFocus]::GetWindowThreadProcessId([IATestFocus]::GetForegroundWindow(),[ref]$foregroundPID)
        if ($foregroundPID -ne [uint32]$value.process_id) {throw 'Selected App is not foreground'}
        $r=$node.Current.BoundingRectangle;$fx=0.5;$fy=0.5
        if($value.PSObject.Properties['fraction_x']){$fx=[double]$value.fraction_x;$fy=[double]$value.fraction_y}
        if($r.IsEmpty -or $r.Width -le 0 -or $r.Height -le 0 -or $fx -lt 0 -or $fx -ge 1 -or $fy -lt 0 -or $fy -ge 1){throw 'Invalid pointer geometry'}
        $x=[int]($r.X+$r.Width*$fx);$y=[int]($r.Y+$r.Height*$fy);Check-IATestPoint $x $y $value.process_id $node
        if($value.op -eq 'drag') {
          if(!$value.destination -or !$value.destination.PSObject.Properties.Count){throw 'Semantic drag destination required'}
          $nodes=[Collections.Generic.Stack[object]]::new();$nodes.Push(@($root,0));$matches=@();$visited=0;$watch.Restart()
          while($nodes.Count) {$entry=$nodes.Pop();$n=$entry[0];$depth=$entry[1];$visited++;if($visited -gt 1024 -or $depth -gt 32 -or $watch.ElapsedMilliseconds -gt 250){throw 'Destination query budget exceeded'};$f=@{identifier=$n.Current.AutomationId;name=$n.Current.Name;role=$n.Current.ControlType.ProgrammaticName};$ok=$true;foreach($p in $value.destination.PSObject.Properties){if(!$f.ContainsKey($p.Name) -or $f[$p.Name] -cne $p.Value){$ok=$false}};if($ok){$matches+=,$n};$c=$walker.GetFirstChild($n);while($null -ne $c){$nodes.Push(@($c,($depth+1)));$c=$walker.GetNextSibling($c)}}
          if($matches.Count -ne 1 -or !$matches[0].Current.IsEnabled){throw 'Drag destination must be unique and enabled'}
          $d=$matches[0].Current.BoundingRectangle;$ex=[int]($d.X+$d.Width/2);$ey=[int]($d.Y+$d.Height/2);Check-IATestPoint $ex $ey $value.process_id $matches[0]
          if(![IATestPointer]::SetCursorPos($x,$y)){throw 'Pointer move failed'};[IATestPointer]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
          try {for($i=1;$i -le 12;$i++){[void][IATestFocus]::GetWindowThreadProcessId([IATestFocus]::GetForegroundWindow(),[ref]$foregroundPID);if($foregroundPID -ne [uint32]$value.process_id){throw 'Foreground changed; drag delivery may be partial'};if(![IATestPointer]::SetCursorPos([int]($x+($ex-$x)*$i/12),[int]($y+($ey-$y)*$i/12))){throw 'Drag move failed'};Start-Sleep -Milliseconds 15}} finally {[IATestPointer]::mouse_event(4,0,0,0,[UIntPtr]::Zero)}
        } elseif($value.op -eq 'scroll') {
          $dx=[int]$value.delta_x;$dy=[int]$value.delta_y;if([Math]::Abs($dx) -gt 20 -or [Math]::Abs($dy) -gt 20 -or ($dx -eq 0 -and $dy -eq 0)){throw 'Invalid wheel steps'}
          if(![IATestPointer]::SetCursorPos($x,$y)){throw 'Pointer move failed'}
          if($dy){[IATestPointer]::mouse_event(0x800,0,0,(-120*$dy),[UIntPtr]::Zero)};if($dx){[IATestPointer]::mouse_event(0x1000,0,0,(120*$dx),[UIntPtr]::Zero)}
        } else {if(![IATestPointer]::SetCursorPos($x,$y)){throw 'Pointer move failed'};$down=2;$up=4;if($value.op -eq 'right_click'){$down=8;$up=16};[IATestPointer]::mouse_event($down,0,0,0,[UIntPtr]::Zero);[IATestPointer]::mouse_event($up,0,0,0,[UIntPtr]::Zero)}
      } elseif ($value.op -eq 'click') {
        if (!$node.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) {throw 'Invoke pattern unavailable'}
        $pattern.Invoke()
      } elseif ($value.op -eq 'fill') {
        if (!$node.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern) -or $pattern.Current.IsReadOnly) {throw 'Editable value pattern unavailable'}
        $pattern.SetValue([string]$value.value)
      } else {
        $keys=@{Enter='{ENTER}';Tab='{TAB}';Escape='{ESC}';Backspace='{BACKSPACE}';Delete='{DELETE}';Space=' ';ArrowLeft='{LEFT}';ArrowRight='{RIGHT}';ArrowUp='{UP}';ArrowDown='{DOWN}';Home='{HOME}';End='{END}';PageUp='{PGUP}';PageDown='{PGDN}'}
        if (!$keys.ContainsKey([string]$value.key)) {throw 'Unsupported key'}
        $node.SetFocus();Add-Type -AssemblyName System.Windows.Forms
        [uint32]$foregroundPID=0
        [void][IATestFocus]::GetWindowThreadProcessId([IATestFocus]::GetForegroundWindow(),[ref]$foregroundPID)
        if ($foregroundPID -ne [uint32]$value.process_id -or !$node.Current.HasKeyboardFocus) {throw 'Selected App does not own foreground keyboard focus'}
        [Windows.Forms.SendKeys]::SendWait($keys[[string]$value.key])
      }
      if($value.op -ne 'screenshot'){$reply.result=@{dispatch='acknowledged';backend='UIAutomation';business_postcondition='pending'}}
    }
  } catch {$reply.error=$_.Exception.Message}
  [Console]::WriteLine(($reply|ConvertTo-Json -Depth 12 -Compress));[Console]::Out.Flush()
}
