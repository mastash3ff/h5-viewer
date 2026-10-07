[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Find-Python313 {
    $launcher = Join-Path $env:WINDIR "py.exe"
    if (Test-Path $launcher) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $candidate = & $launcher -3.13 -c "import sys; print(sys.executable)" 2>$null
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($exitCode -eq 0 -and $candidate) { return $candidate.Trim() }
    }
    return $null
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
    return $null
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
    return $null
}

$winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if (-not $winget) { throw "winget.exe is required to install the Windows build tools." }

if (-not (Find-Python313)) {
    & $winget.Source install --exact --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python 3.13 installation failed with exit code $LASTEXITCODE." }
}

if (-not (Find-Uv)) {
    & $winget.Source install --exact --id astral-sh.uv --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "uv installation failed with exit code $LASTEXITCODE." }
}

if (-not (Find-Iscc)) {
    & $winget.Source install --exact --id JRSoftware.InnoSetup --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup installation failed with exit code $LASTEXITCODE." }
}

$python = Find-Python313
$uv = Find-Uv
$iscc = Find-Iscc
if (-not $python -or -not $uv -or -not $iscc) {
    throw "A required tool was not discoverable after installation. Open a new shell and retry."
}

Write-Host "Python: $python"
& $python --version
Write-Host "uv: $uv"
& $uv --version
Write-Host "Inno Setup: $iscc"
& $iscc /? | Select-Object -First 1
