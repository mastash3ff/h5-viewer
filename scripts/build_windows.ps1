[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$BuildRoot = (Join-Path $env:LOCALAPPDATA "H5ViewerBuild"),
    [string]$OutputRoot,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $SourceRoot) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
}

function Find-Uv {
    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe")
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "uv.exe was not found. Run scripts\bootstrap_windows.ps1 first."
}

function Find-Iscc {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "ISCC.exe was not found. Run scripts\bootstrap_windows.ps1 first."
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE."
    }
}

$source = (Resolve-Path $SourceRoot).ProviderPath
$selected = @("pyproject.toml", "uv.lock", "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "src", "packaging", "scripts", "docs")
foreach ($required in $selected) {
    if (-not (Test-Path (Join-Path $source $required))) { throw "Missing required source: $required" }
}
$gitSource = $source.Replace("\", "/")
$commitOutput = & git -c "safe.directory=$gitSource" -C $source rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "Cannot determine source Git commit." }
$commit = $commitOutput.Trim()
$dirty = [bool](& git -c "safe.directory=$gitSource" -C $source status --porcelain)
if ($LASTEXITCODE -ne 0) { throw "Cannot determine source Git status." }
$uv = Find-Uv
$iscc = if ($SkipInstaller) { $null } else { Find-Iscc }
$stage = Join-Path $BuildRoot "source"
$stageDist = Join-Path $stage "dist"
$output = if ($OutputRoot) { [IO.Path]::GetFullPath($OutputRoot) } else { Join-Path $source "dist\release" }
$cache = Join-Path $source "artifacts\source-cache"

# Only task-owned staging and dedicated release output may be replaced.
if ([IO.Path]::GetFullPath($stage).TrimEnd('\') -eq $source.TrimEnd('\')) { throw "Build staging overlaps source." }
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path $output) {
    if (@(Get-ChildItem -LiteralPath $output -Force).Count -gt 0) {
        throw "Release output must be empty; choose a new -OutputRoot or archive its previous contents."
    }
}
New-Item -ItemType Directory -Path $stage, $output -Force | Out-Null
if (-not $dirty) {
    # Read immutable committed bytes: neither ignored descendants nor edits made
    # during the build can enter a release carrying this commit SHA.
    $sourceArchive = Join-Path $BuildRoot "selected-source.zip"
    Invoke-Checked "git" (@("-c", "safe.directory=$gitSource", "-C", $source, "archive", "--format=zip", "--output=$sourceArchive", $commit, "--") + $selected)
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $stage
    Remove-Item -LiteralPath $sourceArchive
}
else {
    # Development builds may include reviewed worktree files, but never ignored
    # descendants. The manifest records dirty=true and publication rejects it.
    $paths = & git -c "safe.directory=$gitSource" -C $source ls-files --cached --others --exclude-standard -- $selected
    if ($LASTEXITCODE -ne 0) { throw "Cannot enumerate development source files." }
    foreach ($relative in $paths) {
        $from = Join-Path $source $relative
        if (-not (Test-Path -LiteralPath $from -PathType Leaf)) { continue }
        $to = Join-Path $stage $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $to) -Force | Out-Null
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
}

Push-Location $stage
try {
    Invoke-Checked $uv @("sync", "--locked", "--group", "dev", "--python", "3.13")
    Invoke-Checked $uv @("run", "--locked", "pyinstaller", "--noconfirm", "--clean", "packaging\h5_viewer.spec")
    $portable = Join-Path $stageDist "H5Viewer"
    if (-not (Test-Path (Join-Path $portable "H5Viewer.exe"))) { throw "PyInstaller did not produce H5Viewer.exe." }
    Invoke-Checked $uv @("run", "--locked", "python", "scripts\package_licenses.py", "prepare", "--portable", $portable, "--release", $output, "--cache", $cache)
    $version = (& $uv run --locked python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot determine project version." }
    $portableZip = Join-Path $output "H5Viewer-$version-windows-x64-portable.zip"
    Compress-Archive -Path (Join-Path $portable "*") -DestinationPath $portableZip -CompressionLevel Optimal
    if (-not $SkipInstaller) {
        Invoke-Checked $iscc @("/DMyAppVersion=$version", "/DSourceDir=$portable", "/DOutputDir=$output", "packaging\h5_viewer.iss")
    }
    $finalize = @("run", "--locked", "python", "scripts\package_licenses.py", "finalize", "--release", $output, "--version", $version, "--commit", $commit)
    if ($dirty) { $finalize += "--dirty" }
    Invoke-Checked $uv $finalize
    Get-Content (Join-Path $output "SHA256SUMS.txt")
}
finally { Pop-Location }
