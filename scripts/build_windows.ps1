[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$BuildRoot = (Join-Path $env:LOCALAPPDATA "H5ViewerBuild"),
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

$source = (Resolve-Path $SourceRoot).Path
foreach ($required in @("pyproject.toml", "uv.lock", "packaging\h5_viewer.spec", "packaging\h5_viewer.iss")) {
    if (-not (Test-Path (Join-Path $source $required))) { throw "Missing required source file: $required" }
}

$uv = Find-Uv
$iscc = if ($SkipInstaller) { $null } else { Find-Iscc }
$stage = Join-Path $BuildRoot "source"
$stageDist = Join-Path $stage "dist"
$output = Join-Path $source "dist"

if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage, $output -Force | Out-Null

$excludeNames = @(".git", ".venv", ".venv-windows", "build", "dist", "artifacts", "__pycache__", ".pytest_cache", ".ruff_cache")
Get-ChildItem -LiteralPath $source -Force |
    Where-Object { $_.Name -notin $excludeNames } |
    Copy-Item -Destination $stage -Recurse -Force

Push-Location $stage
try {
    Invoke-Checked $uv @("sync", "--frozen", "--group", "dev", "--python", "3.13")
    Invoke-Checked $uv @("run", "--frozen", "pyinstaller", "--noconfirm", "--clean", "packaging\h5_viewer.spec")

    $portable = Join-Path $stageDist "H5Viewer"
    if (-not (Test-Path (Join-Path $portable "H5Viewer.exe"))) { throw "PyInstaller did not produce H5Viewer.exe." }

    Copy-Item "packaging\THIRD_PARTY_NOTICES.txt" $portable -Force
    Copy-Item "README.md" $portable -Force
    Copy-Item "docs\USER_GUIDE.md" $portable -Force
    $licenseRoot = Join-Path $portable "licenses"
    New-Item -ItemType Directory -Path $licenseRoot -Force | Out-Null
    Copy-Item "packaging\licenses\*" $licenseRoot -Force
    $licenseScript = @'
from importlib import metadata
from pathlib import Path
import shutil
import sys

destination = Path(sys.argv[1])
packages = ("numpy", "h5py", "PySide6", "shiboken6", "pyqtgraph", "pyinstaller")
for name in packages:
    dist = metadata.distribution(name)
    package_dir = destination / name
    package_dir.mkdir(parents=True, exist_ok=True)
    matches = []
    for entry in dist.files or ():
        basename = Path(str(entry)).name.lower()
        if basename.startswith(("license", "copying", "notice")):
            source = Path(dist.locate_file(entry))
            if source.is_file():
                matches.append(source)
    if not matches:
        raise SystemExit(f"No license file found for {name}")
    for index, source in enumerate(dict.fromkeys(matches), start=1):
        suffix = source.suffix or ".txt"
        shutil.copy2(source, package_dir / f"LICENSE-{index}{suffix}")

python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise SystemExit(f"Python license not found: {python_license}")
shutil.copy2(python_license, destination / "PYTHON-LICENSE.txt")
'@
    $licenseScript | & $uv run --frozen python - $licenseRoot
    if ($LASTEXITCODE -ne 0) { throw "License collection failed with exit code $LASTEXITCODE." }

    $portableZip = Join-Path $stageDist "H5Viewer-$((Select-String '^version = ' pyproject.toml).Line.Split('"')[1])-windows-x64-portable.zip"
    if (Test-Path $portableZip) { Remove-Item -LiteralPath $portableZip -Force }
    Compress-Archive -Path (Join-Path $portable "*") -DestinationPath $portableZip -CompressionLevel Optimal

    $version = (Select-String '^version = ' pyproject.toml).Line.Split('"')[1]
    if (-not $SkipInstaller) {
        Invoke-Checked $iscc @(
            "/DMyAppVersion=$version",
            "/DSourceDir=$portable",
            "/DOutputDir=$stageDist",
            "packaging\h5_viewer.iss"
        )
    }

    Copy-Item $portableZip $output -Force
    if (-not $SkipInstaller) {
        Copy-Item (Join-Path $stageDist "H5Viewer-$version-windows-x64-setup.exe") $output -Force
    }

    Get-FileHash (Join-Path $output "H5Viewer-$version-windows-x64-*") -Algorithm SHA256 |
        Select-Object Path, Hash
}
finally {
    Pop-Location
}
