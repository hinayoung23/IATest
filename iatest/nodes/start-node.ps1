param([string]$PythonPath = "", [string]$Workspace = "")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Push-Location $PSScriptRoot
$previousToken = $null
$tokenName = $null
try {
    $hashes = Get-Content -LiteralPath "sha256.json" -Raw | ConvertFrom-Json
    foreach ($entry in $hashes.PSObject.Properties) {
        if ([IO.Path]::GetFileName($entry.Name) -ne $entry.Name) { throw "Invalid bundle entry" }
        $actual = (Get-FileHash -LiteralPath $entry.Name -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $entry.Value) { throw "Bundle content hash mismatch" }
    }
    $config = Get-Content -LiteralPath "node.json" -Raw | ConvertFrom-Json
    if ($PythonPath -eq "") {
        $candidate = Get-Command python -ErrorAction SilentlyContinue
        if (-not $candidate) { throw "Install Python 3.11 or later, or supply -PythonPath. System settings are not changed." }
        $PythonPath = $candidate.Source
    }
    & $PythonPath -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
    if ($LASTEXITCODE -ne 0) { throw "Python is unavailable or too old" }
    if ($Workspace -eq "") { $Workspace = Join-Path ([IO.Path]::GetTempPath()) ("iatest-lan-" + [Guid]::NewGuid().ToString("N")) }
    if (Test-Path -LiteralPath $Workspace) { throw "Choose a new workspace directory; existing environments are not overwritten" }
    New-Item -ItemType Directory -Path $Workspace | Out-Null
    $venv = Join-Path $Workspace ".venv"
    & $PythonPath -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create isolated virtual environment" }
    $python = Join-Path $venv "Scripts\python.exe"
    & $python -m pip install --disable-pip-version-check --no-input (Join-Path $PSScriptRoot $config.wheel)
    if ($LASTEXITCODE -ne 0) { throw "Could not install the framework and its existing dependencies" }
    $tokenName = [string]$config.token_env
    $previousToken = [Environment]::GetEnvironmentVariable($tokenName, "Process")
    if ([string]::IsNullOrEmpty($previousToken)) {
        $secure = Read-Host "Node pairing token (not your Windows password)" -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $pairing = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($pairing.Length -lt 16) { throw "Pairing token must contain at least 16 characters" }
        [Environment]::SetEnvironmentVariable($tokenName, $pairing, "Process")
        $pairing = $null
    }
    Set-Location -LiteralPath $Workspace
    & $python -m iatest init --project $config.project
    if ($LASTEXITCODE -ne 0) { throw "Could not initialize the test workspace" }
    $arguments = @("-m", "iatest", "node", "worker", $config.controller, $config.node_id, "--project", $config.project, "--token-env", $tokenName, "--kinds", ($config.kinds -join ","), "--ca", (Join-Path $PSScriptRoot $config.ca))
    foreach ($allowed in $config.allowed_origins) { $arguments += @("--origin", $allowed) }
    Write-Host ("Starting isolated node in " + $Workspace + "; Ctrl-C stops it. No firewall or execution policy settings are changed.")
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Worker exited with an error" }
} finally {
    if ($tokenName) { [Environment]::SetEnvironmentVariable($tokenName, $previousToken, "Process") }
    Pop-Location
}
