[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Request,

    [Parameter(Mandatory = $true)]
    [string]$Inbox,

    [switch]$Execute,

    [string]$Python
)

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$builder = Join-Path $PSScriptRoot "build_downloaded_dataset.py"

if (-not $Python) {
    $centralRuntime = Join-Path $workspaceRoot "control\runtime\data-engineering\.venv\Scripts\python.exe"
    $compatibilityRuntime = Join-Path $workspaceRoot "capital-flow-attribution\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $centralRuntime -PathType Leaf) {
        $Python = $centralRuntime
    }
    elseif (Test-Path -LiteralPath $compatibilityRuntime -PathType Leaf) {
        Write-Warning "Central runtime is not installed; using the verified compatibility-witness environment."
        $Python = $compatibilityRuntime
    }
    else {
        throw "No usable qdata Python runtime was found. Pass -Python explicitly."
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable does not exist: $Python"
}
if (-not (Test-Path -LiteralPath $Request -PathType Leaf)) {
    throw "Request file does not exist: $Request"
}
if (-not (Test-Path -LiteralPath $Inbox -PathType Container)) {
    throw "Inbox directory does not exist: $Inbox"
}

$builderArgs = @(
    $builder,
    "--workspace-root", $workspaceRoot,
    "--request", (Resolve-Path -LiteralPath $Request).Path,
    "--inbox", (Resolve-Path -LiteralPath $Inbox).Path
)
if ($Execute) {
    $builderArgs += "--execute"
}

& $Python @builderArgs
exit $LASTEXITCODE
