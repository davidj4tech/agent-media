# canvas-wake-watch.ps1 — turn this PC's display on when the canvas asks.
#
# Watches the agent-media canvas SSE stream; when a show event arrives stamped
# wake=<this screen> AND the canvas is the foreground window here, it forces
# the display awake with a 1px mouse jiggle (SendInput counts as user input,
# which ends DPMS-off). A browser page cannot do this itself — wake locks only
# prevent sleep — hence this tiny host-side half.
#
# MUST run in the interactive session: processes in session 0 (ssh, services)
# cannot see the desktop's windows, so GetForegroundWindow returns nothing.
# Install as a logon scheduled task (runs only when the user is logged on):
#
#   schtasks /Create /F /TN canvas-wake-watch /SC ONLOGON /TR ^
#     "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File %USERPROFILE%\canvas-wake-watch.ps1"
#   schtasks /Run /TN canvas-wake-watch
#
# One-time page setup on this screen: open <canvas>/?screen=<name> in the
# browser and pair it (QR at /pair) — the page's activity beacons are what
# make this screen the wake target.
param(
  [string]$Canvas = "http://100.103.43.93:8781",   # red5's canvas (tailnet IP)
  [string]$Screen = $env:COMPUTERNAME.ToLower(),
  [string]$MatchTitle = "agent-media canvas",
  [switch]$FiguresOnly                              # skip ambient art, wake only for [[visual/reveal]] figures
)

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class WakeUtil {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int c);
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public MOUSEINPUT mi; }
  [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
  public static void Jiggle() {
    var inp = new INPUT[2];
    inp[0].type = 0; inp[0].mi.dx = 1;  inp[0].mi.dwFlags = 0x0001; // MOUSEEVENTF_MOVE
    inp[1].type = 0; inp[1].mi.dx = -1; inp[1].mi.dwFlags = 0x0001;
    SendInput(2, inp, Marshal.SizeOf(typeof(INPUT)));
  }
  public static string FgTitle() {
    var sb = new StringBuilder(512);
    GetWindowText(GetForegroundWindow(), sb, 512);
    return sb.ToString();
  }
}
"@

while ($true) {
  try {
    $req = [System.Net.WebRequest]::Create("$Canvas/events")
    $req.Timeout = 10000
    $req.ReadWriteTimeout = 90000    # server pings every ~25s keep this alive
    $resp = $req.GetResponse()
    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
    while ($null -ne ($line = $reader.ReadLine())) {
      if (-not $line.StartsWith("data:")) { continue }
      try { $d = $line.Substring(5).Trim() | ConvertFrom-Json } catch { continue }
      if ($d.wake -ne $Screen) { continue }
      if (-not ($d.image -or $d.sequence)) { continue }
      if ($FiguresOnly -and $d.purpose -ne "figure") { continue }
      if ([WakeUtil]::FgTitle() -notlike "*$MatchTitle*") { continue }
      [WakeUtil]::Jiggle()
    }
  } catch { }
  Start-Sleep -Seconds 5   # stream dropped (or red5 restarting) — reconnect
}
