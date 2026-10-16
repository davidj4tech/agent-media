# Sasonica's server on Windows, in one line (PowerShell):
#
#   irm https://sasonica.com/install.ps1 | iex
#
# (sasonica.com/install.ps1 redirects here; until 16 Oct 2026 it was Sasonica
# Shell, now at sasonica.com/shell.ps1.)
#
# The Windows twin of deploy/install.sh: the server as one file
# (sasonica-windows-x86_64.exe, from the server-latest release, built by
# deploy/binary/build.sh), checked against SHA256SUMS and put at
# %USERPROFILE%\.local\bin\sasonica.exe; then `sasonica install` (shims for
# every command, this machine's config, the agents' hooks, logon tasks
# under Task Scheduler → Sasonica, a quick tunnel among them) and a pairing
# QR code for the app.
# Running it again updates. docs/proposals/2026-09-29-single-binary.md.
#
# Then one question, default No (David, 16 Oct 2026): also install Sasonica
# Shell, which lets an AI assistant such as Claude run commands on this
# computer through the hosted relay. It needs Node (Shell's installer gets it
# with winget) and a free Sasonica account: Shell prints a link and a code to
# sign in with. It is Shell's own one-liner, sasonica.com/shell.ps1 (its
# get.ps1: a zip to %LOCALAPPDATA%\sasonica\shell, install.ps1 -Hosted, the
# Scheduled Task sasonica-shell), run in a scope of its own. Read-Host asks;
# a host that cannot (-NonInteractive, input redirected, a service) is No. A
# Shell that does not install does not fail the server's install: it says how
# to try again. Shell's command is `sasonica` too (sasonica.cmd in
# %APPDATA%\sasonica\bin, after the server's folder on PATH, so the server's
# sasonica.exe wins): beside the server it is also sasonica-shell.cmd.
#
# Switches, for an unattended install. Through irm, as a script block:
#   & ([scriptblock]::Create((irm https://sasonica.com/install.ps1))) -Shell
#   -Shell        install Shell without asking   ($env:SASONICA_WITH_SHELL = '1')
#   -NoShell      skip it, no question            ($env:SASONICA_WITH_SHELL = '0')
#   -ShellOnly    Shell alone, no server          ($env:SASONICA_ONLY = 'shell')
#                 (the same as irm https://sasonica.com/shell.ps1 | iex)
#
# $env:SASONICA_BINARY_BASE overrides where the binary is (tests);
# $env:SASONICA_SHELL_URL where Shell's one-liner is (a URL or a file: tests).
# -Lib defines the functions and returns, installing nothing (CI).

param(
  [switch]$Shell,
  [switch]$NoShell,
  [switch]$ShellOnly,
  [switch]$Lib
)

$ErrorActionPreference = 'Stop'

# Shell or not. The switches, then $env:SASONICA_WITH_SHELL, then the
# question; no question where nobody can answer it.
function Test-CanAsk {
  if (-not [Environment]::UserInteractive) { return $false }
  if ([Environment]::GetCommandLineArgs() | Where-Object { $_ -match '^-noni' }) { return $false }
  try { if ([Console]::IsInputRedirected) { return $false } } catch { }
  return $true
}

function Get-ShellChoice {
  param([switch]$Yes, [switch]$No)
  if ($Yes) { return $true }
  if ($No) { return $false }
  switch -regex ("$env:SASONICA_WITH_SHELL") {
    '^(1|y|yes|true)$' { return $true }
    '^(0|n|no|false)$' { return $false }
  }
  if (-not (Test-CanAsk)) { return $false }
  Write-Host ''
  $a = Read-Host ("Also let an AI assistant such as Claude run commands on this computer`n" +
                  "(Sasonica Shell)? It needs a free Sasonica account. [y/N]")
  return ("$a" -match '^\s*y')
}

# Shell's `sasonica` loses to the server's on PATH: give it its own name too.
function Set-ShellAlias {
  $shim = Join-Path $env:APPDATA 'sasonica\bin\sasonica.cmd'
  $server = Join-Path $env:USERPROFILE '.local\bin\sasonica.exe'
  if ((Test-Path $shim) -and (Test-Path $server)) {
    Copy-Item -Force $shim (Join-Path (Split-Path -Parent $shim) 'sasonica-shell.cmd')
    Write-Host "Sasonica Shell's command here is sasonica-shell (sasonica is the server)."
  }
}

# Shell's own one-liner in a scope of its own (its $ErrorActionPreference and
# `return` stay there); its output to the screen, where its sign-in link and
# code are. Returns whether it installed.
function Invoke-ShellStep {
  param([switch]$Alone)
  $src = if ($env:SASONICA_SHELL_URL) { $env:SASONICA_SHELL_URL } else { 'https://sasonica.com/shell.ps1' }
  Write-Host "`n== Sasonica Shell"
  Write-Host 'Shell prints a link and a code: open the link, sign in with your Sasonica'
  Write-Host 'account (or make one, free), check the code matches and press Join.'
  try {
    $text = if (Test-Path -LiteralPath $src) { Get-Content -Raw -LiteralPath $src }
            else { Invoke-RestMethod -UseBasicParsing $src }
    & ([scriptblock]::Create([string]$text)) | Out-Host
    Set-ShellAlias
    return $true
  } catch {
    Write-Warning "Sasonica Shell did not install: $($_.Exception.Message)"
    if (-not $Alone) { Write-Host 'The server is installed and is not affected.' }
    Write-Host 'To try again:'
    Write-Host '  irm https://sasonica.com/shell.ps1 | iex'
    return $false
  }
}

if ($Lib) { return }

if ($ShellOnly -or $env:SASONICA_ONLY -eq 'shell') {
  # Write-Error, not exit: under irm | iex, exit would close their window.
  if (-not (Invoke-ShellStep -Alone)) { Write-Error 'Sasonica Shell did not install.' }
  return
}

$base = if ($env:SASONICA_BINARY_BASE) { $env:SASONICA_BINARY_BASE } else {
  'https://github.com/davidj4tech/agent-media/releases/download/server-latest' }

if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
  Write-Error "Sasonica's server is built for x64 Windows only so far (this is $env:PROCESSOR_ARCHITECTURE)."
}
$asset = 'sasonica-windows-x86_64.exe'
$bin = Join-Path $env:USERPROFILE '.local\bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$exe = Join-Path $bin 'sasonica.exe'
$new = "$exe.new"

Write-Host "`n== Sasonica's server (x86_64)"
Invoke-WebRequest -UseBasicParsing "$base/$asset" -OutFile $new
$sums = (Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS").Content
$want = ($sums -split "`n" | Where-Object { $_ -match "\s\*?$([regex]::Escape($asset))\s*$" } |
         ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
$got = (Get-FileHash -Algorithm SHA256 $new).Hash.ToLower()
if (-not $want -or $got -ne $want.ToLower()) {
  Remove-Item -Force $new
  Write-Error 'The download does not match its checksum; nothing was changed.'
}
# A running server keeps its file: the old one steps aside, the new one moves in.
if (Test-Path $exe) {
  Remove-Item -Force "$exe.old" -ErrorAction SilentlyContinue
  Move-Item -Force $exe "$exe.old"
}
Move-Item -Force $new $exe
& $exe version

# The shims' folder on this user's PATH, for new terminals.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not ($userPath -split ';' | Where-Object { $_ -eq $bin })) {
  [Environment]::SetEnvironmentVariable('Path', "$bin;$userPath", 'User')
  $env:Path = "$bin;$env:Path"
  Write-Host "  added $bin to your PATH"
}

& $exe install
if ($LASTEXITCODE -ne 0) { Write-Error 'sasonica install failed.' }
# Shell, installed before the server: its command keeps a name of its own.
Set-ShellAlias

# A function, so its early returns still reach the Shell step after it.
function Invoke-Pairing {
  Write-Host "`n== Pairing"
  $up = $false
  foreach ($i in 1..30) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8781/healthz' | Out-Null; $up = $true; break }
    catch { Start-Sleep 1 }
  }
  if (-not $up) {
    Write-Host 'The server is not answering on port 8781 yet; when it is, pair with:'
    Write-Host "  sasonica pair --device `"$env:COMPUTERNAME`""
    return
  }
  # The quick tunnel (sasonica install, where the phone has no other way in):
  # pair with its https address once cloudflared has named it.
  if (Get-ScheduledTask -TaskPath '\Sasonica\' -TaskName 'quick-tunnel' -ErrorAction SilentlyContinue) {
    $url = (& $exe media-tunnel url --wait 60) | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or -not $url) {
      Write-Host 'The quick tunnel has not started yet; when it has, pair with:'
      Write-Host "  sasonica pair --device `"$env:COMPUTERNAME`""
      Write-Host '(Task Scheduler -> Sasonica -> quick-tunnel)'
      return
    }
    Write-Host "The phone reaches this computer at $url (a Cloudflare quick tunnel)."
    foreach ($i in 1..30) {
      try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "$url/healthz" | Out-Null; break }
      catch { Start-Sleep 2 }
    }
  }
  Write-Host 'Scan this with Sasonica (Pair a server), or paste the link into its pairing screen:'
  & $exe pair --device $env:COMPUTERNAME | Out-Host
}
Invoke-Pairing

if (Get-ShellChoice -Yes:$Shell -No:$NoShell) { Invoke-ShellStep | Out-Null }
