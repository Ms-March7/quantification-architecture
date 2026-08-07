param(
    [ValidateSet('public', 'qmt')]
    [string]$Broker = 'public',
    [string]$Symbols = '159995,159516,603986,588170,159338,159819,588330,512050,600487,508033',
    [ValidateSet('1m', '5m', '15m')]
    [string]$Period = '5m',
    [int]$WatchSeconds = 30,
    [int]$MaxCycles = 0
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Monitor = Join-Path $RepoRoot 'ml\short_cycle_live_monitor.py'
$Output = Join-Path $RepoRoot '.tmp\short-cycle\latest-monitor.json'
$Cache = Join-Path $RepoRoot '.tmp\short-cycle\market'
$PositionFile = Join-Path $RepoRoot '.tmp\short-cycle\manual-account.json'

if (-not (Test-Path -LiteralPath $Python)) {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $Launcher) {
        throw 'Python runtime not found; create .venv or install the Windows py launcher.'
    }
    $Python = $Launcher.Source
}
if (-not (Test-Path -LiteralPath $Monitor)) {
    throw "Short-cycle monitor not found: $Monitor"
}
if ($Broker -eq 'qmt') {
    if (-not $env:MULTIFACTOR_QMT_USERDATA -or -not $env:MULTIFACTOR_QMT_ACCOUNT_ID) {
        throw 'QMT mode requires local MULTIFACTOR_QMT_USERDATA and MULTIFACTOR_QMT_ACCOUNT_ID environment variables.'
    }
}

$Arguments = @(
    $Monitor,
    '--broker', $Broker,
    '--period', $Period,
    '--limit', '300',
    '--workers', '4',
    '--output', $Output,
    '--cache-dir', $Cache,
    '--position-file', $PositionFile,
    '--watch-seconds', [string]$WatchSeconds,
    '--max-cycles', [string]$MaxCycles
)
if ($Symbols) {
    $Arguments += @('--symbols', $Symbols)
}

Push-Location $RepoRoot
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
