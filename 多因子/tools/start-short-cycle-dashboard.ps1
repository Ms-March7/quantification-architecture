param(
    [ValidateSet('public', 'qmt')]
    [string]$Broker = 'public',
    [int]$Port = 8791
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Dashboard = Join-Path $RepoRoot 'ml\short_cycle_dashboard.py'

if (-not (Test-Path -LiteralPath $Python)) {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $Launcher) {
        throw 'Python runtime not found; create .venv or install the Windows py launcher.'
    }
    $Python = $Launcher.Source
}

if ($Broker -eq 'qmt' -and (-not $env:MULTIFACTOR_QMT_USERDATA -or -not $env:MULTIFACTOR_QMT_ACCOUNT_ID)) {
    throw 'QMT mode requires local MULTIFACTOR_QMT_USERDATA and MULTIFACTOR_QMT_ACCOUNT_ID environment variables.'
}

Push-Location $RepoRoot
try {
    & $Python $Dashboard --broker $Broker --port $Port --open-browser
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
