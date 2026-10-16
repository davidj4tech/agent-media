# deploy/install.ps1's Sasonica Shell step (David, 16 Oct 2026), on stand-ins
# for Shell's one-liner: no download, no hosted relay, no server installed.
# Run by test_install_ps1.py under `powershell -NonInteractive`, so the
# question cannot be asked and the default must be No.
#
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File check-install-ps1.ps1 <repo>

param([Parameter(Mandatory = $true)][string]$Repo)

$ErrorActionPreference = 'Stop'
$install = Join-Path $Repo 'deploy\install.ps1'
$failed = 0
function Check([bool]$ok, [string]$what) {
  if ($ok) { Write-Output "ok    $what" } else { Write-Output "FAIL  $what"; $script:failed++ }
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("sasonica-check-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
$mark = Join-Path $tmp 'shell-ran'
$good = Join-Path $tmp 'good-get.ps1'
$bad = Join-Path $tmp 'bad-get.ps1'
# Like get.ps1: its own preference, a `return`, the sign-in link and code.
Set-Content -Path $good -Value @"
`$ErrorActionPreference = 'Stop'
Set-Content -Path '$mark' -Value 'ran'
Write-Output 'Open this link: https://relay.example.invalid/join  code: TEST-CODE'
return
"@
Set-Content -Path $bad -Value "`$ErrorActionPreference = 'Stop'`nthrow 'the installer stopped (exit 3)'"

foreach ($k in 'SASONICA_WITH_SHELL', 'SASONICA_ONLY', 'SASONICA_SHELL_URL') {
  Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
}

. $install -Lib

# The architecture (David, 16 Oct 2026: ARM64 runs the x64 build emulated):
# the OS's, whatever this PowerShell is emulated as; then the variables.
Check ((Get-WindowsArch -OS 'X64' -W6432 '' -Proc 'AMD64') -eq 'AMD64') 'arch: x64'
Check ((Get-WindowsArch -OS 'Arm64' -W6432 '' -Proc 'AMD64') -eq 'ARM64') 'arch: ARM64 from an emulated x64 PowerShell'
Check ((Get-WindowsArch -OS 'X86' -W6432 '' -Proc 'x86') -eq 'x86') 'arch: 32-bit'
Check ((Get-WindowsArch -OS '' -W6432 'ARM64' -Proc 'x86') -eq 'ARM64') 'arch: no .NET answer, PROCESSOR_ARCHITEW6432'
Check ((Get-WindowsArch -OS '' -W6432 '' -Proc 'ARM64') -eq 'ARM64') 'arch: no .NET answer, PROCESSOR_ARCHITECTURE'
if ($env:SASONICA_EXPECT_ARCH) {
  Check ((Get-WindowsArch) -eq $env:SASONICA_EXPECT_ARCH) "arch: this machine is $env:SASONICA_EXPECT_ARCH (said $(Get-WindowsArch))"
}

# The question: no terminal to ask (-NonInteractive) is No; the variable and
# the switches answer it.
Check (-not (Test-CanAsk)) 'a -NonInteractive host cannot be asked'
Check ((Get-ShellChoice) -eq $false) 'default: No'
$env:SASONICA_WITH_SHELL = '1'; Check ((Get-ShellChoice) -eq $true) 'SASONICA_WITH_SHELL=1: Yes'
$env:SASONICA_WITH_SHELL = '0'; Check ((Get-ShellChoice) -eq $false) 'SASONICA_WITH_SHELL=0: No'
Check ((Get-ShellChoice -Yes) -eq $true) '-Shell: Yes'
Remove-Item Env:SASONICA_WITH_SHELL
Check ((Get-ShellChoice -No) -eq $false) '-NoShell: No'

# The step runs Shell's one-liner and says yes; a failing one is caught,
# with the way to try again.
$env:SASONICA_SHELL_URL = $good
function Said($r) { @($r | Where-Object { $_ -is [bool] })[-1] }
$r = @(Invoke-ShellStep 6>&1)
Check ((Said $r) -eq $true) 'a Shell that installs: the step says so'
Check (Test-Path $mark) "Shell's one-liner ran"
$env:SASONICA_SHELL_URL = $bad
$r = @(Invoke-ShellStep 6>&1 3>&1)
$text = ($r | ForEach-Object { "$_" }) -join "`n"
Check ((Said $r) -eq $false) 'a Shell that fails: the step says so, without throwing'
Check ($text -match 'not affected') 'the server is said to be unaffected'
Check ($text -match [regex]::Escape('irm https://sasonica.com/shell.ps1 | iex')) 'how to try again'

# Beside the server, Shell has no command of its own: its .cmd files go,
# their runner recorded for `sasonica shell` (David, 16 Oct 2026).
$saved = @{ APPDATA = $env:APPDATA; USERPROFILE = $env:USERPROFILE }
$env:APPDATA = Join-Path $tmp 'appdata'; $env:USERPROFILE = Join-Path $tmp 'profile'
$sbin = Join-Path $env:APPDATA 'sasonica\bin'
New-Item -ItemType Directory -Force $sbin | Out-Null
$runner = Join-Path $tmp 'shell dir\sasonica.mjs'
New-Item -ItemType Directory -Force (Split-Path -Parent $runner) | Out-Null
Set-Content $runner '// stand-in'
$shimText = "@echo off`r`n`"C:\node\node.exe`" `"$runner`" %*`r`n"
Set-Content (Join-Path $sbin 'sasonica.cmd') $shimText
Remove-ShellShims 6>&1 | Out-Null
Check (Test-Path (Join-Path $sbin 'sasonica.cmd')) 'Shell alone keeps its sasonica.cmd'
Check (-not (Test-Path (Join-Path $env:APPDATA 'sasonica\runner'))) 'Shell alone: no record written here'
New-Item -ItemType Directory -Force (Join-Path $env:USERPROFILE '.local\bin') | Out-Null
Set-Content (Join-Path $env:USERPROFILE '.local\bin\sasonica.exe') 'MZ'
Set-Content (Join-Path $sbin 'sasonica-shell.cmd') $shimText
Set-Content (Join-Path $sbin 'other.cmd') '@echo off'
$said = (Remove-ShellShims 6>&1 | ForEach-Object { "$_" }) -join "`n"
Check (-not (Test-Path (Join-Path $sbin 'sasonica.cmd'))) 'beside the server: sasonica.cmd removed'
Check (-not (Test-Path (Join-Path $sbin 'sasonica-shell.cmd'))) 'beside the server: sasonica-shell.cmd removed'
Check (Test-Path (Join-Path $sbin 'other.cmd')) 'only Shell''s shims are removed'
$rec = @(Get-Content (Join-Path $env:APPDATA 'sasonica\runner'))
Check ($rec[0] -eq 'C:\node\node.exe' -and $rec[1] -eq $runner) "the runner is recorded ($($rec -join ' | '))"
Check ($said -match 'sasonica shell') 'it says Shell is `sasonica shell`'
$env:APPDATA = $saved.APPDATA; $env:USERPROFILE = $saved.USERPROFILE

# -ShellOnly / SASONICA_ONLY=shell: Shell alone, the server never fetched
# (its base points nowhere, so any server step would fail the run).
$ps = (Get-Process -Id $PID).Path
# Continue: under Stop, Windows PowerShell turns a child's stderr, merged by
# 2>&1, into a terminating error.
$ErrorActionPreference = 'Continue'
$env:SASONICA_BINARY_BASE = 'https://server-must-not-be-fetched.invalid'
Remove-Item $mark -ErrorAction SilentlyContinue
$env:SASONICA_SHELL_URL = $good
$out = & $ps -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $install -ShellOnly 2>&1 | Out-String
Check ($LASTEXITCODE -eq 0) "-ShellOnly: exit 0 ($LASTEXITCODE)"
Check (Test-Path $mark) '-ShellOnly: Shell ran'
Check (-not ($out -match "Sasonica's server")) '-ShellOnly: no server step'
$env:SASONICA_ONLY = 'shell'
$env:SASONICA_SHELL_URL = $bad
$out = & $ps -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $install 2>&1 | Out-String
Check ($LASTEXITCODE -ne 0) "SASONICA_ONLY=shell, Shell fails: the run fails ($LASTEXITCODE)"
Check (-not ($out -match 'not affected')) 'Shell alone: no server to be unaffected'
Remove-Item Env:SASONICA_ONLY, Env:SASONICA_SHELL_URL, Env:SASONICA_BINARY_BASE

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
if ($failed) { Write-Output "$failed failed"; exit 1 }
Write-Output 'all passed'
