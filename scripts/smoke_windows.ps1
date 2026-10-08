[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PortableZip,
    [string]$Installer,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [switch]$TestInstallerLifecycle,
    [ValidateRange(30, 600)][int]$TimeoutSeconds = 180
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'This smoke check requires Windows.' }
if ($TestInstallerLifecycle -and (
        $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted')) {
    throw 'Installer lifecycle checks are allowed only on disposable GitHub-hosted runners.'
}
if ($TestInstallerLifecycle -and -not $Installer) { throw '-Installer is required for lifecycle checks.' }
$PortableZip = (Resolve-Path -LiteralPath $PortableZip).ProviderPath
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).ProviderPath
if ($Installer) { $Installer = (Resolve-Path -LiteralPath $Installer).ProviderPath }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$work = Join-Path $OutputRoot ('work-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$versionMatch = [regex]::Match((Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\pyproject.toml') -Raw), '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) { throw 'Cannot read the project version from pyproject.toml.' }
$expectedVersion = $versionMatch.Groups[1].Value
$summary = [ordered]@{ ok = $false; version = $expectedVersion; portable = 'pending'; installer_lifecycle = 'not_requested' }

function Invoke-BoundedProcess([string]$Executable, [string[]]$Arguments) {
    # Windows CommandLineToArgvW quoting, including embedded quotes and final backslashes.
    $quoted = foreach ($argument in $Arguments) {
        '"' + [regex]::Replace([regex]::Replace($argument, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = $quoted -join ' '
    $start.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($start)
    try {
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
            $killCode = $LASTEXITCODE
            if ($killCode -ne 0 -and -not $process.HasExited) { $process.Kill() }
            throw "Process timed out: $Executable"
        }
        if ($process.ExitCode -ne 0) { throw "Process exited with code $($process.ExitCode): $Executable" }
    } finally { $process.Dispose() }
}

function Assert-Reader($reader) {
    if (-not $reader.enforced -or $reader.platform -ne 'win32' -or
        $reader.mechanism -ne 'Windows Job Object' -or $reader.memory_limit -ne 1073741824 -or
        $reader.commit_limit -ne 1073741824 -or -not $reader.working_set_hard -or
        $reader.working_set_limit -gt 1073741824 -or $reader.working_set_limit -le 0 -or
        $reader.qt_loaded) { throw 'Reader did not confirm the required Windows 1 GiB limits.' }
}

function Test-Application([string]$Executable, [string]$Name) {
    $report = Join-Path $OutputRoot "$Name-check.json"
    if (Test-Path -LiteralPath $report) { throw "Refusing to reuse diagnostic report: $report" }
    $before = (Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash
    try {
        Invoke-BoundedProcess $Executable @('--check', $fixture, '--report', $report)
    } finally {
        if ((Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash -ne $before) {
            throw 'Source fixture SHA-256 changed during the check.'
        }
    }
    if (-not (Test-Path -LiteralPath $report)) { throw 'The application did not write its JSON report.' }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (-not $result.ok -or $result.application_version -ne $expectedVersion -or $result.qt_platform -ne 'windows' -or
        -not $result.gui.window_created -or -not $result.gui.closed -or
        -not $result.gui.contents.rendered) { throw "GUI diagnostic failed: $report" }
    foreach ($path in @('/gzip', '/lzf', '/complex', '/text', '/compound')) {
        $read = @($result.reads | Where-Object { $_.path -eq $path })
        $expectedLength = if ($path -in @('/text', '/compound')) { 2 } else { 16 }
        if ($read.Count -ne 1 -or $read[0].shape.Count -ne 1 -or $read[0].shape[0] -ne $expectedLength) {
            throw "Diagnostic did not return the expected selection for $path"
        }
    }
    foreach ($name in @('table', 'plot', 'image')) {
        $view = $result.gui.views.$name
        if ($view.status -ne 'passed' -or -not $view.rendered -or $view.width -le 0 -or $view.height -le 0) {
            throw "Production GUI view did not render: $name"
        }
    }
    Assert-Reader $result.reader
    Assert-Reader $result.gui.reader
    return [ordered]@{ status = 'passed'; source_sha256 = $before; report = [IO.Path]::GetFileName($report) }
}

function Get-KeySnapshot([string]$Path, [switch]$OnlyDefault) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Path)
    try {
        if ($null -eq $key) { return [ordered]@{ exists = $false; values = @{} } }
        $values = [ordered]@{}
        foreach ($name in ($key.GetValueNames() | Sort-Object)) {
            if (-not $OnlyDefault -or $name -eq '') {
                $values[$name] = [ordered]@{ kind = $key.GetValueKind($name).ToString(); value = $key.GetValue($name) }
            }
        }
        # Installer may add OpenWithProgids below an otherwise absent extension key;
        # preserve default-value existence, not existence of the containing key.
        return [ordered]@{ exists = $(if ($OnlyDefault) { $values.Contains('') } else { $true }); values = $values }
    } finally { if ($null -ne $key) { $key.Dispose() } }
}

function Get-DefaultState {
    $state = [ordered]@{}
    foreach ($extension in @('.h5', '.hdf5')) {
        $state["$extension-default"] = Get-KeySnapshot "Software\Classes\$extension" -OnlyDefault
        $state["$extension-UserChoice"] = Get-KeySnapshot "Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$extension\UserChoice"
    }
    return ($state | ConvertTo-Json -Depth 10 -Compress)
}

function Assert-DefaultState([string]$Expected) {
    if ((Get-DefaultState) -cne $Expected) { throw 'Existing extension defaults or protected UserChoice changed.' }
}

$ownedKeys = @(
    'Software\H5Viewer\Capabilities',
    'Software\Classes\Applications\H5Viewer.exe',
    'Software\Classes\H5Viewer.h5',
    'Software\Classes\H5Viewer.hdf5',
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{A6F91DAA-A59A-4D6F-B43B-55231790384A}_is1'
)
function Assert-Registration([bool]$Present, [string]$Executable) {
    foreach ($key in $ownedKeys) {
        if ((Test-Path -LiteralPath "HKCU:\$key") -ne $Present) { throw "Unexpected registration state: $key" }
    }
    if ($Present) {
        $uninstall = Get-KeySnapshot $ownedKeys[-1]
        if ($uninstall.values['DisplayVersion'].value -ne $expectedVersion) { throw 'Installed version does not match pyproject.toml.' }
    }
    $registered = Get-KeySnapshot 'Software\RegisteredApplications'
    if ($registered.values.Contains('H5 Viewer') -ne $Present) { throw 'Unexpected RegisteredApplications value.' }
    foreach ($extension in @('.h5', '.hdf5')) {
        $progId = "H5Viewer$extension"
        $openWith = Get-KeySnapshot "Software\Classes\$extension\OpenWithProgids"
        if ($openWith.values.Contains($progId) -ne $Present) { throw "Unexpected Open With registration: $progId" }
        if ($Present) {
            $command = (Get-KeySnapshot "Software\Classes\$progId\shell\open\command").values[''].value
            if ($command -cne ('"' + $Executable + '" "%1"')) { throw "Incorrect registered command: $progId" }
            $association = (Get-KeySnapshot 'Software\H5Viewer\Capabilities\FileAssociations').values[$extension].value
            if ($association -ne $progId) { throw "Incorrect capability association: $extension" }
        }
    }
}

$installed = $false
try {
    # ASCII script source still exercises a Unicode and spaced source path on Windows PowerShell 5.
    $fixture = Join-Path $work ('synthetic caf' + [char]0x00e9 + ' data\fixture sample.h5')
    Invoke-BoundedProcess $PythonExe @((Join-Path $PSScriptRoot 'create_smoke_fixture.py'), $fixture)
    $portable = Join-Path $work 'portable'
    Expand-Archive -LiteralPath $PortableZip -DestinationPath $portable
    $executables = @(Get-ChildItem -LiteralPath $portable -Filter H5Viewer.exe -Recurse -File)
    if ($executables.Count -ne 1) { throw 'Portable ZIP must contain exactly one H5Viewer.exe.' }
    $summary.portable = Test-Application $executables[0].FullName 'portable'

    if ($TestInstallerLifecycle) {
        Assert-Registration $false ''
        $defaultsBefore = Get-DefaultState
        $summary.default_state_before = $defaultsBefore | ConvertFrom-Json
        $installDir = Join-Path $work 'installed application'
        $installedExe = Join-Path $installDir 'H5Viewer.exe'
        $installArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=$installDir")
        $installed = $true # Clean up a partial installation too, if an uninstaller exists.
        Invoke-BoundedProcess $Installer ($installArguments + @('/LOG=' + (Join-Path $OutputRoot 'install.log')))
        Assert-Registration $true $installedExe
        $summary.default_state_after_install = Get-DefaultState | ConvertFrom-Json
        Assert-DefaultState $defaultsBefore
        $summary.installed = Test-Application $installedExe 'installed'
        $obsolete = Join-Path $installDir '_internal\obsolete-smoke-file.txt'
        $unrelated = Join-Path $installDir 'unrelated-user-sentinel.txt'
        Set-Content -LiteralPath $obsolete -Value 'obsolete application-owned test file'
        Set-Content -LiteralPath $unrelated -Value 'preserve this unrelated file'
        $sentinelHash = (Get-FileHash -LiteralPath $unrelated -Algorithm SHA256).Hash
        Invoke-BoundedProcess $Installer ($installArguments + @('/LOG=' + (Join-Path $OutputRoot 'reinstall.log')))
        if (Test-Path -LiteralPath $obsolete) { throw 'Same-version reinstall retained an obsolete owned file.' }
        if ((Get-FileHash -LiteralPath $unrelated -Algorithm SHA256).Hash -ne $sentinelHash) { throw 'Reinstall changed the unrelated sentinel.' }
        Assert-Registration $true $installedExe
        $summary.default_state_after_reinstall = Get-DefaultState | ConvertFrom-Json
        Assert-DefaultState $defaultsBefore
        $summary.same_version_reinstall = Test-Application $installedExe 'reinstalled'
        $uninstaller = Join-Path $installDir 'unins000.exe'
        Invoke-BoundedProcess $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG=' + (Join-Path $OutputRoot 'uninstall.log')))
        # Allow Inno's temporary uninstaller process to finish deleting owned files.
        $uninstallWait = [Diagnostics.Stopwatch]::StartNew()
        while ((Test-Path -LiteralPath $installedExe) -or
               (Test-Path -LiteralPath ("HKCU:\" + $ownedKeys[-1]))) {
            if ($uninstallWait.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw 'Uninstall did not finish removing the application.' }
            Start-Sleep -Milliseconds 100
        }
        Assert-Registration $false ''
        $summary.default_state_after_uninstall = Get-DefaultState | ConvertFrom-Json
        Assert-DefaultState $defaultsBefore
        if (Test-Path -LiteralPath $installedExe) { throw 'Uninstall retained the application executable.' }
        if ((Get-FileHash -LiteralPath $unrelated -Algorithm SHA256).Hash -ne $sentinelHash) { throw 'Uninstall changed the unrelated sentinel.' }
        $installed = $false
        $summary.installer_lifecycle = 'passed'
        $summary.default_state_unchanged = $true
        $summary.unrelated_sentinel_preserved = $true
    }
    $summary.ok = $true
} catch {
    $summary.error = $_.Exception.Message
    throw
} finally {
    if ($installed) {
        $uninstaller = Join-Path $installDir 'unins000.exe'
        if (Test-Path -LiteralPath $uninstaller) {
            try { Invoke-BoundedProcess $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') }
            catch { $summary.cleanup_error = $_.Exception.Message }
        }
    }
    $summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $OutputRoot 'smoke-summary.json') -Encoding UTF8
    # Everything under work was generated by this invocation; reports remain outside it.
    Remove-Item -LiteralPath $work -Recurse -Force
}
