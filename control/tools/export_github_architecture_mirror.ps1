[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$destination = [System.IO.Path]::GetFullPath($DestinationRoot)

if ($destination.TrimEnd('\') -ieq $sourceRoot.TrimEnd('\')) {
    throw "Destination must not be the live workspace root."
}
if (Test-Path -LiteralPath $destination) {
    $existing = @(Get-ChildItem -LiteralPath $destination -Force -ErrorAction Stop)
    if ($existing.Count -gt 0) {
        throw "Destination already exists and is not empty: $destination"
    }
} else {
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
}

$allowedExtensions = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
@(
    ".md", ".txt", ".py", ".ps1", ".cmd", ".bat", ".sh",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".sql", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".rst",
    ".lock"
) | ForEach-Object { [void]$allowedExtensions.Add($_) }

$allowedExtensionlessNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
@("Dockerfile", "Makefile", ".gitignore", ".gitattributes", ".env.example") |
    ForEach-Object { [void]$allowedExtensionlessNames.Add($_) }

$blockedDirectoryNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
@(
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", "node_modules", "dist", "build",
    ".state", ".tmp", "logs", "log", "coverage", "htmlcov",
    "data", "reports", "research_runs", "engineering_runs", "releases",
    "bootstrap", "archive", "tmp", "quarantine", "snapshots", "raw",
    "curated", "published", "checkpoints", "correlation_sketches"
) | ForEach-Object { [void]$blockedDirectoryNames.Add($_) }

$blockedFileNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
@(
    ".env", "catalog.sqlite3", "candidate_assets.json",
    ".datasets.json.transition.guard"
) | ForEach-Object { [void]$blockedFileNames.Add($_) }

$copied = [System.Collections.Generic.List[object]]::new()
$skipped = [System.Collections.Generic.List[object]]::new()

function Get-WorkspaceRelativePath {
    param([string]$Path)
    $base = $sourceRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($Path.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $Path.Substring($base.Length)
    }
    return $Path
}

function Add-SkipRecord {
    param([string]$Path, [string]$Reason)
    $recordPath = $Path
    if ([System.IO.Path]::IsPathRooted($Path)) {
        $recordPath = Get-WorkspaceRelativePath -Path $Path
    }
    $script:skipped.Add([ordered]@{
        path = $recordPath.Replace('\', '/')
        reason = $Reason
    })
}

function Test-AllowedFile {
    param([System.IO.FileInfo]$File)
    if (($File.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Add-SkipRecord -Path $File.FullName -Reason "REPARSE_POINT"
        return $false
    }
    if ($blockedFileNames.Contains($File.Name)) {
        Add-SkipRecord -Path $File.FullName -Reason "BLOCKED_FILE_NAME"
        return $false
    }
    if ($File.Name -match '(?i)(^|\.)(pem|pfx|p12|key|sqlite|sqlite3|db|parquet|feather|arrow|zip|rar|7z|tar|gz|bz2|xz|ipynb|pdf|docx|xlsx|xls|csv|tsv)$') {
        Add-SkipRecord -Path $File.FullName -Reason "BLOCKED_DATA_OR_BINARY_EXTENSION"
        return $false
    }
    if ($File.Length -gt 20MB) {
        Add-SkipRecord -Path $File.FullName -Reason "FILE_EXCEEDS_20_MIB"
        return $false
    }
    $extension = [System.IO.Path]::GetExtension($File.Name)
    if ($allowedExtensions.Contains($extension) -or $allowedExtensionlessNames.Contains($File.Name)) {
        return $true
    }
    Add-SkipRecord -Path $File.FullName -Reason "EXTENSION_NOT_ALLOWLISTED"
    return $false
}

function Copy-OneFile {
    param([string]$RelativePath)
    $source = Join-Path $sourceRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required architecture file is missing: $RelativePath"
    }
    $file = Get-Item -LiteralPath $source -Force
    if (-not (Test-AllowedFile -File $file)) {
        throw "Required architecture file was rejected by allowlist: $RelativePath"
    }
    $target = Join-Path $destination $RelativePath
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force
    $copied.Add([ordered]@{ path = $RelativePath.Replace('\', '/'); bytes = $file.Length })
}

function Copy-Tree {
    param([string]$RelativeRoot)
    $treeRoot = Join-Path $sourceRoot $RelativeRoot
    if (-not (Test-Path -LiteralPath $treeRoot -PathType Container)) {
        throw "Required architecture directory is missing: $RelativeRoot"
    }

    $pending = [System.Collections.Generic.Stack[System.IO.DirectoryInfo]]::new()
    $pending.Push((Get-Item -LiteralPath $treeRoot -Force))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory.FullName -Force) {
            if ($item.PSIsContainer) {
                if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Add-SkipRecord -Path $item.FullName -Reason "REPARSE_DIRECTORY"
                    continue
                }
                if ($blockedDirectoryNames.Contains($item.Name) -or $item.Name -like "*.egg-info") {
                    Add-SkipRecord -Path $item.FullName -Reason "BLOCKED_DIRECTORY"
                    continue
                }
                $pending.Push($item)
                continue
            }

            if (-not (Test-AllowedFile -File $item)) {
                continue
            }
            $relative = Get-WorkspaceRelativePath -Path $item.FullName
            $target = Join-Path $destination $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
            $copied.Add([ordered]@{ path = $relative.Replace('\', '/'); bytes = $item.Length })
        }
    }
}

# Root governance and reusable research-control architecture.
Copy-OneFile "AGENTS.md"
Copy-OneFile ".agents\SKILL_STATES.md"
Copy-Tree ".agents\harnesses"
Copy-Tree ".agents\tools"

@(
    "control\WORKSPACE_INDEX.md",
    "control\ROADMAP.md",
    "control\projects.yaml",
    "control\FILE_DISPOSITION.yaml",
    "control\DEPENDENCY_AUDIT.md",
    "control\STORAGE_POLICY.md",
    "control\MIGRATION_PLAN.md"
) | ForEach-Object { Copy-OneFile $_ }
Copy-Tree "control\tools"
Copy-Tree "control\tests"

# Catalog contracts, policies and code only. Business payloads remain excluded.
@(
    "data\catalog\README.md",
    "data\catalog\DATABASE_MANUAL.md",
    "data\catalog\MAINTENANCE_RUNBOOK.md",
    "data\catalog\SOURCE_POLICY.md",
    "data\catalog\DATA_QUALITY_POLICY.md",
    "data\catalog\ACCESS_POLICY.md",
    "data\catalog\dataset.schema.json",
    "data\catalog\candidate_assets.schema.json",
    "data\catalog\factor_registry.schema.json",
    "data\catalog\datasets.json",
    "data\catalog\planned_datasets.json",
    "data\catalog\factors.json",
    "data\catalog\dataset_cli.py",
    "data\catalog\snapshot_contract.py",
    "data\catalog\adapter_gate.py",
    "data\catalog\qdata.cmd"
) | ForEach-Object { Copy-OneFile $_ }
Copy-Tree "data\catalog\contracts"

# Multi-factor research source, protocols, configurations and tests.
$multiFactorRoot = "$([char]0x591A)$([char]0x56E0)$([char]0x5B50)"
@(
    (Join-Path $multiFactorRoot ".gitignore"),
    (Join-Path $multiFactorRoot "README.md"),
    (Join-Path $multiFactorRoot "BROKER_DATA_ACCESS.md"),
    (Join-Path $multiFactorRoot "DATA_LAKE.md"),
    (Join-Path $multiFactorRoot "FACTOR_FACTORY_ARCHITECTURE.md"),
    (Join-Path $multiFactorRoot "MIGRATION_20260716.md"),
    (Join-Path $multiFactorRoot "PROTOCOL_C_V1_1_FULL_MARKET_LAYERED_FACTOR_RESEARCH.md"),
    (Join-Path $multiFactorRoot "PROTOCOL_V3.md"),
    (Join-Path $multiFactorRoot "PROTOCOL_V6.md"),
    (Join-Path $multiFactorRoot "PROTOCOL_V7_ENSEMBLE_FIRST.md"),
    (Join-Path $multiFactorRoot "SHORT_CYCLE_RUNBOOK_20260716.md"),
    (Join-Path $multiFactorRoot "requirements.txt"),
    (Join-Path $multiFactorRoot "requirements-gpu.txt")
) | ForEach-Object { Copy-OneFile $_ }
Copy-Tree (Join-Path $multiFactorRoot "configs")
Copy-Tree (Join-Path $multiFactorRoot "factor_factory")
Copy-Tree (Join-Path $multiFactorRoot "ml")
Copy-Tree (Join-Path $multiFactorRoot "research_reporting")
Copy-Tree (Join-Path $multiFactorRoot "tools")
Copy-Tree (Join-Path $multiFactorRoot "quant_data_core")

# Active project source trees. Runtime data and generated reports are not selected.
@(
    "capital-flow-attribution\.gitignore",
    "capital-flow-attribution\README.md",
    "capital-flow-attribution\DATA_CATALOG.md",
    "capital-flow-attribution\DECISIONS.md",
    "capital-flow-attribution\ROADMAP.md",
    "capital-flow-attribution\STATUS.md",
    "capital-flow-attribution\pyproject.toml"
) | ForEach-Object { Copy-OneFile $_ }
Copy-Tree "capital-flow-attribution\configs"
Copy-Tree "capital-flow-attribution\docs"
Copy-Tree "capital-flow-attribution\protocol"
Copy-Tree "capital-flow-attribution\scripts"
Copy-Tree "capital-flow-attribution\src"
Copy-Tree "capital-flow-attribution\tests"

@(
    "news-signal-agent\.gitignore",
    "news-signal-agent\.env.example",
    "news-signal-agent\README.md",
    "news-signal-agent\backend\.env.example",
    "news-signal-agent\backend\pyproject.toml",
    "news-signal-agent\frontend\index.html",
    "news-signal-agent\frontend\package.json",
    "news-signal-agent\frontend\tsconfig.json",
    "news-signal-agent\frontend\vite.config.ts"
) | ForEach-Object { Copy-OneFile $_ }
Copy-Tree "news-signal-agent\backend\app"
Copy-Tree "news-signal-agent\backend\tests"
Copy-Tree "news-signal-agent\config"
Copy-Tree "news-signal-agent\docs"
Copy-Tree "news-signal-agent\frontend\src"
Copy-Tree "news-signal-agent\scripts"

# Redact known workstation roots from copied governance and metadata documents.
# Executable source files are intentionally left byte-identical to avoid changing semantics.
$redactableExtensions = @(
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".rst"
)
$workstationRoots = [ordered]@{
    '${QUANTIFICATION_ROOT}' = $sourceRoot
    '${CODEX_HOME}' = (Join-Path (Split-Path -Parent $sourceRoot) 'CodexHome')
    '${USER_PROFILE}' = [Environment]::GetFolderPath('UserProfile')
}
foreach ($file in Get-ChildItem -LiteralPath $destination -Recurse -File -Force) {
    if ($redactableExtensions -notcontains $file.Extension.ToLowerInvariant()) {
        continue
    }
    $content = [System.IO.File]::ReadAllText($file.FullName)
    $redacted = $content
    foreach ($entry in $workstationRoots.GetEnumerator()) {
        $path = [string]$entry.Value
        if ([string]::IsNullOrWhiteSpace($path)) {
            continue
        }
        foreach ($variant in @(
            $path,
            $path.Replace('\', '\\'),
            $path.Replace('\', '/')
        )) {
            $redacted = $redacted.Replace($variant, [string]$entry.Key)
        }
    }
    if ($redacted -cne $content) {
        [System.IO.File]::WriteAllText(
            $file.FullName,
            $redacted,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
}

$copiedBytes = [int64]0
foreach ($entry in $copied) {
    $copiedBytes += [int64]$entry["bytes"]
}

$audit = [ordered]@{
    schema_version = 1
    source_root = "`${QUANTIFICATION_ROOT}"
    destination = "`${PUBLISH_MIRROR}"
    generated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    copied_file_count = $copied.Count
    copied_bytes = $copiedBytes
    copied = @($copied | Sort-Object path)
    skipped_count = $skipped.Count
    skipped = @($skipped | Sort-Object path, reason)
    policy = "ALLOWLISTED_SOURCE_AND_GOVERNANCE_ONLY__NO_DATA_NO_SECRETS_NO_RUNTIME"
}
$auditPath = Join-Path $destination "COPY_AUDIT.json"
$audit | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $auditPath -Encoding utf8

[pscustomobject]@{
    status = "PASS"
    source_root = $sourceRoot
    destination = $destination
    copied_file_count = $copied.Count
    copied_bytes = $audit.copied_bytes
    skipped_count = $skipped.Count
    audit = $auditPath
} | ConvertTo-Json -Depth 4
