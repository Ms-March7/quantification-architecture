param(
  [string]$Source = (Resolve-Path "$PSScriptRoot\..").Path,
  [string]$Target = (Join-Path (Split-Path (Resolve-Path "$PSScriptRoot\..").Path -Parent) "news-signal-agent-github")
)

$ErrorActionPreference = "Stop"

$sourcePath = (Resolve-Path $Source).Path
$targetParent = Split-Path $Target -Parent
if (-not (Test-Path $targetParent)) {
  throw "Target parent does not exist: $targetParent"
}
$targetPath = [System.IO.Path]::GetFullPath($Target)

if ($sourcePath -eq $targetPath) {
  throw "Source and target must be different directories."
}
if (-not (Split-Path $targetPath -Leaf).EndsWith("-github")) {
  throw "Refusing to sync to '$targetPath'. Target directory name must end with '-github'."
}

New-Item -ItemType Directory -Force -Path $targetPath | Out-Null

$excludeDirs = @(
  ".git",
  ".venv",
  ".pytest_cache",
  ".ruff_cache",
  "__pycache__",
  "logs",
  "news_signal_agent_backend.egg-info",
  "data",
  "frontend",
  "reports"
)

$excludeFiles = @(
  ".env",
  "watchlist.yaml",
  "*.db",
  "*.sqlite",
  "*.sqlite3",
  "*.pyc",
  "*.pyo",
  "*.pyd"
)

$args = @(
  $sourcePath,
  $targetPath,
  "/E",
  "/NFL",
  "/NDL",
  "/NJH",
  "/NJS",
  "/XD"
) + $excludeDirs + @("/XF") + $excludeFiles

& robocopy @args
$exitCode = $LASTEXITCODE
if ($exitCode -ge 8) {
  throw "robocopy failed with exit code $exitCode"
}

New-Item -ItemType Directory -Force -Path (Join-Path $targetPath "data\cache") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $targetPath "reports\details") | Out-Null
Set-Content -Path (Join-Path $targetPath "data\cache\.gitkeep") -Value "" -Encoding utf8
Set-Content -Path (Join-Path $targetPath "reports\details\.gitkeep") -Value "" -Encoding utf8

Write-Host "Synced clean release tree to: $targetPath"
Write-Host "Next:"
Write-Host "  cd $targetPath"
Write-Host "  git init"
Write-Host "  git status"
