# Sasonica's server on Windows, in one line (PowerShell):
#
#   irm https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy/install.ps1 | iex
#
# The Windows twin of deploy/install.sh: the server as one file
# (sasonica-windows-x86_64.exe, from the server-latest release, built by
# deploy/binary/build.sh), checked against SHA256SUMS and put at
# %USERPROFILE%\.local\bin\sasonica.exe; then `sasonica install` (shims for
# every command, this machine's config, the agents' hooks, two logon tasks
# under Task Scheduler → Sasonica) and a pairing QR code for the app.
# Running it again updates. docs/proposals/2026-09-29-single-binary.md.
#
# $env:SASONICA_BINARY_BASE overrides where the binary is (tests).

$ErrorActionPreference = 'Stop'
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
Write-Host 'Scan this with Sasonica (Pair a server), or paste the link into its pairing screen:'
& $exe pair --device $env:COMPUTERNAME
