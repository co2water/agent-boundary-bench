<#
agent-boundary-bench setup for Windows (Windows PowerShell 5.1 or PowerShell 7).

Reads agents.lock.json and installs the npm agents (OpenClaw, DeepSeek Harness)
at the pinned versions into $env:ABB_AGENTS_DIR (default <repo>\agents). Safe to
run again: an agent already at the pinned version is left alone.

Hermes Agent and the portable Node are NOT installed automatically; the script
prints what to do. -WithNode downloads the pinned portable Node and refuses to
unpack it unless its sha256 matches the lockfile.

  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-DryRun|--dry-run] [-WithNode|--with-node]

  -DryRun    print every action, change nothing, download nothing
  -WithNode  fetch + verify the portable Node from the lockfile into <repo>\runtime
#>
param(
    [switch]$DryRun,
    [switch]$WithNode
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# also accept the GNU-style spellings used by setup.sh
foreach ($a in $args) {
    switch ($a) {
        '--dry-run'   { $DryRun = $true }
        '--with-node' { $WithNode = $true }
        { $_ -in '-h', '--help', '/?' } {
            Write-Host 'usage: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-DryRun|--dry-run] [-WithNode|--with-node]'
            Write-Host '  -DryRun    print every action, change nothing, download nothing'
            Write-Host '  -WithNode  fetch + verify (sha256) the portable Node from agents.lock.json into <repo>\runtime'
            exit 0
        }
        default { Write-Host "unknown option: $a (try --help)" -ForegroundColor Red; exit 2 }
    }
}

$Repo = Split-Path -Parent $PSScriptRoot
$LockPath = Join-Path $Repo 'agents.lock.json'
$AgentsDir = if ($env:ABB_AGENTS_DIR) { $env:ABB_AGENTS_DIR } else { Join-Path $Repo 'agents' }
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
$script:Failed = $false

function Say([string]$msg) { Write-Host $msg }
function Warn([string]$msg) { Write-Warning $msg }
function Die([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Dry([string]$msg) { Write-Host "[dry-run] $msg" }

if (-not (Test-Path -LiteralPath $LockPath)) { Die "lockfile not found: $LockPath" }
$Lock = [System.IO.File]::ReadAllText($LockPath, $Utf8NoBom) | ConvertFrom-Json

# The OpenClaw engine range from the lockfile: >=24.16.0 <25 || >=26.1.0
function Test-NodeRange([string]$v) {
    if (-not $v) { return $false }
    $p = $v.TrimStart('v').Split('.')
    $major = 0; $minor = 0
    if (-not ([int]::TryParse($p[0], [ref]$major) -and [int]::TryParse($p[1], [ref]$minor))) { return $false }
    if ($major -eq 24 -and $minor -ge 16) { return $true }
    if ($major -eq 26 -and $minor -ge 1) { return $true }
    return ($major -gt 26)
}

function Get-NodeVersion([string]$exe) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $null }
    try { return (& $exe -p 'process.versions.node' 2>$null | Select-Object -First 1) } catch { return $null }
}

function Get-InstalledVersion([string]$dir, [string]$package) {
    $pj = Join-Path (Join-Path $dir 'node_modules') ($package -replace '/', '\')
    $pj = Join-Path $pj 'package.json'
    if (-not (Test-Path -LiteralPath $pj)) { return $null }
    try { return ([System.IO.File]::ReadAllText($pj, $Utf8NoBom) | ConvertFrom-Json).version } catch { return $null }
}

$pyVer = $null
try { $pyVer = (& python -c "import platform; print(platform.python_version())" 2>$null | Select-Object -First 1) } catch { }

Say 'agent-boundary-bench setup'
Say "  repo        $Repo"
Say "  lockfile    $LockPath"
Say "  agents dir  $AgentsDir  (ABB_AGENTS_DIR)"
Say ("  python      {0}  (lockfile: {1}; the bench needs 3.11+)" -f $(if ($pyVer) { $pyVer } else { 'not found' }), $Lock.platform.python)
if ($DryRun) { Say '  mode        DRY RUN: nothing is changed or downloaded' }
Say ''

# ------------------------------------------------------------------ portable Node
$NodeLock = $Lock.runtimes.node_openclaw
$NodeDefaultDir = Join-Path $Repo ($NodeLock.install_dir -replace '/', '\')
$NodeDir = if ($env:ABB_NODE_DIR) { $env:ABB_NODE_DIR } else { $NodeDefaultDir }

Say ("== Node for OpenClaw (needs {0}; lockfile pins {1})" -f $NodeLock.required_range, $NodeLock.version)
$HaveNodeDir = Get-NodeVersion (Join-Path $NodeDir 'node.exe')
if ($HaveNodeDir) {
    Say "  found Node $HaveNodeDir in $NodeDir"
    if (-not (Test-NodeRange $HaveNodeDir)) { Warn "Node $HaveNodeDir in $NodeDir is outside $($NodeLock.required_range)" }
}
elseif ($WithNode) {
    $RuntimeDir = Split-Path -Parent $NodeDefaultDir
    $Archive = Join-Path $RuntimeDir $NodeLock.archive
    Say "  downloading $($NodeLock.url)"
    if ($DryRun) {
        Dry "New-Item -ItemType Directory -Force $RuntimeDir"
        Dry "Invoke-WebRequest $($NodeLock.url) -OutFile $Archive"
        Dry "verify sha256($($NodeLock.archive)) == $($NodeLock.sha256), else delete it and stop"
        Dry "Expand-Archive $Archive -DestinationPath $RuntimeDir; Remove-Item $Archive"
    }
    else {
        New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $NodeLock.url -OutFile $Archive
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
        if ($got -ne $NodeLock.sha256.ToLowerInvariant()) {
            Remove-Item -LiteralPath $Archive -Force
            Die "sha256 mismatch for $($NodeLock.archive): got $got, lockfile says $($NodeLock.sha256) (archive deleted)"
        }
        Say "  sha256 OK ($got)"
        Expand-Archive -LiteralPath $Archive -DestinationPath $RuntimeDir -Force
        Remove-Item -LiteralPath $Archive -Force
    }
    if ($env:ABB_NODE_DIR -and ($env:ABB_NODE_DIR -ne $NodeDefaultDir)) {
        Warn "unpacked to $NodeDefaultDir, but ABB_NODE_DIR points to $($env:ABB_NODE_DIR); unset it or point it there"
    }
    $NodeDir = $NodeDefaultDir
    $HaveNodeDir = Get-NodeVersion (Join-Path $NodeDir 'node.exe')
}
else {
    $sysNode = $null
    try { $sysNode = (& node -p 'process.versions.node' 2>$null | Select-Object -First 1) } catch { }
    if ($sysNode -and (Test-NodeRange $sysNode)) {
        Say "  no portable Node in $NodeDir; system Node $sysNode satisfies $($NodeLock.required_range), OpenClaw will use it"
    }
    else {
        Say ("  no suitable Node found (portable: none in {0}; system: {1})." -f $NodeDir, $(if ($sysNode) { $sysNode } else { 'none' }))
        Say '  To reproduce the published runs:'
        Say "    1. Download $($NodeLock.url)"
        Say "    2. Check: (Get-FileHash -Algorithm SHA256 $($NodeLock.archive)).Hash"
        Say "       must equal $($NodeLock.sha256)"
        Say "    3. Unzip it so that $NodeDefaultDir\node.exe exists, or set ABB_NODE_DIR to the unzipped folder"
        Say '    or re-run this script with -WithNode to do 1-3 with the checksum enforced.'
    }
}
Say ''

# ------------------------------------------------------------------ npm agents
Say "== npm agents -> $AgentsDir"

function Invoke-Npm([string]$dir, [bool]$useNodeDir, [string[]]$npmArgs) {
    $npm = if ($useNodeDir) { Join-Path $NodeDir 'npm.cmd' } else { 'npm.cmd' }
    if ($DryRun) {
        $prefix = if ($useNodeDir) { "PATH=$NodeDir;%PATH% " } else { '' }
        Dry ("(cd {0}; {1}{2} {3})" -f $dir, $prefix, $npm, ($npmArgs -join ' '))
        return
    }
    $oldPath = $env:PATH
    Push-Location -LiteralPath $dir
    try {
        if ($useNodeDir) { $env:PATH = $NodeDir + [IO.Path]::PathSeparator + $env:PATH }
        & $npm @npmArgs
        if ($LASTEXITCODE -ne 0) { Die "npm $($npmArgs -join ' ') failed in $dir (exit $LASTEXITCODE)" }
    }
    finally {
        Pop-Location
        $env:PATH = $oldPath
    }
}

foreach ($agent in $Lock.agents) {
    $inst = $agent.install
    if ($inst.method -ne 'npm') { continue }
    $target = Join-Path $AgentsDir $inst.dir
    $want = $agent.version
    $have = Get-InstalledVersion $target $agent.package
    if ($have -eq $want) {
        Say "  ok    $($agent.display_name): $($agent.package)@$want already in $target"
        continue
    }
    if ($have) { Say "  update $($agent.display_name): found $($agent.package)@$have, lockfile pins $want" }
    else { Say "  install $($agent.display_name): $($agent.package)@$want -> $target" }

    $useNodeDir = $false
    if ($inst.node -eq 'node_openclaw') {
        if ($HaveNodeDir) { $useNodeDir = $true }
        else { Warn "$($agent.display_name) needs Node $($NodeLock.required_range); installing with the system Node may fail (see the Node section above)" }
    }

    if ($DryRun) { Dry "New-Item -ItemType Directory -Force $target" }
    else { New-Item -ItemType Directory -Force -Path $target | Out-Null }

    $lockDir = if ($inst.npm_lock) { Join-Path $Repo ($inst.npm_lock -replace '/', '\') } else { $null }
    if ($lockDir -and (Test-Path -LiteralPath (Join-Path $lockDir 'package-lock.json')) -and (Test-Path -LiteralPath (Join-Path $lockDir 'package.json'))) {
        # exact transitive tree recorded from the published runs
        foreach ($f in 'package.json', 'package-lock.json') {
            if ($DryRun) { Dry "Copy-Item $(Join-Path $lockDir $f) $target" }
            else { Copy-Item -LiteralPath (Join-Path $lockDir $f) -Destination $target -Force }
        }
        Invoke-Npm $target $useNodeDir @('ci', '--no-audit', '--no-fund')
    }
    else {
        $pjPath = Join-Path $target 'package.json'
        if ($DryRun) { Dry "write $pjPath (private, merged with package_json_extra from the lockfile)" }
        else {
            $pj = if (Test-Path -LiteralPath $pjPath) { [System.IO.File]::ReadAllText($pjPath, $Utf8NoBom) | ConvertFrom-Json } else { New-Object PSObject }
            foreach ($kv in @(@('name', $agent.id), @('version', '1.0.0'), @('private', $true))) {
                if (-not ($pj.PSObject.Properties.Name -contains $kv[0])) { $pj | Add-Member -NotePropertyName $kv[0] -NotePropertyValue $kv[1] }
            }
            if ($inst.package_json_extra) {
                foreach ($p in $inst.package_json_extra.PSObject.Properties) {
                    $pj | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
                }
            }
            [System.IO.File]::WriteAllText($pjPath, ($pj | ConvertTo-Json -Depth 20) + "`n", $Utf8NoBom)
        }
        Invoke-Npm $target $useNodeDir @('install', '--save-exact', '--no-audit', '--no-fund', "$($agent.package)@$want")
    }

    if (-not $DryRun) {
        $have = Get-InstalledVersion $target $agent.package
        if ($have -eq $want) { Say "  ok    $($agent.display_name): $($agent.package)@$want installed" }
        else { Warn "$($agent.display_name): expected $($agent.package)@$want after install, found '$have'"; $script:Failed = $true }
    }
}
Say ''

# ------------------------------------------------------------------ Hermes (manual)
$H = $Lock.agents | Where-Object { $_.id -eq 'hermes' } | Select-Object -First 1
$HDefault = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\hermes.exe'
$HExe = if ($env:ABB_HERMES_EXE) { $env:ABB_HERMES_EXE } else { $HDefault }
Say "== Hermes Agent $($H.version) (commit $($H.commit)) - manual install"
if (Test-Path -LiteralPath $HExe) {
    Say "  found $HExe"
    $HSrc = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $HExe))
    $HHead = $null
    if (Get-Command git -ErrorAction SilentlyContinue) {
        try { $HHead = (& git -C $HSrc rev-parse --short=8 HEAD 2>$null | Select-Object -First 1) } catch { }
    }
    if ($HHead) {
        if ($HHead.StartsWith($H.commit)) { Say "  source checkout at $HHead (matches lockfile)" }
        elseif ($H.local_commit -and $HHead.StartsWith($H.local_commit)) {
            Say "  source checkout at $HHead (the local install R1-R3 used: upstream $($H.commit) + carried commits)"
        }
        else { Warn "Hermes source checkout is at $HHead, lockfile pins $($H.commit)" }
    }
    else { Say "  could not read the source commit (no git checkout at $HSrc)" }
}
else { Say "  not found at $HExe" }
Say '  To reproduce the published runs:'
Say "    1. Install Hermes Agent with its official installer (git install) from $($H.repository)"
Say "    2. In the installed source checkout: git fetch; git checkout $($H.commit),"
Say '       then reinstall it into its venv as the upstream README describes'
Say "    3. Confirm the version is $($H.version), and set ABB_HERMES_EXE if hermes.exe is not at"
Say "       $HDefault"
Say '  Each bench run gives Hermes a fresh HERMES_HOME, so your own Hermes config is not used.'
Say ''

# ------------------------------------------------------------------ next steps
Say '== Next'
Say '  Keys are read from the environment only; never put them in a file in this repo:'
Say '    $env:DEEPSEEK_API_KEY = "..."      # R1/R2 model deepseek-v4-flash'
Say '    $env:OPENROUTER_API_KEY = "..."    # R3 model meta-llama/llama-3.1-8b-instruct'
Say '  (bench/run.py also reads them from HKCU\Environment if the shell predates them.)'
Say '  Optional paths (bench/config.py): ABB_SANDBOX_ROOT ABB_AGENTS_DIR ABB_NODE_DIR ABB_HERMES_EXE'
Say '  See docs/REPRODUCE.md for the run and scoring commands.'

if ($script:Failed) { Die 'one or more agents did not install at the pinned version' }
