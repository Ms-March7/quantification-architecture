[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$PrimaryInputRoot = "",
    [Parameter(Mandatory = $true)]
    [string]$SecondaryInputRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspaceRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\..")
)
if ([string]::IsNullOrWhiteSpace($PrimaryInputRoot)) {
    $PrimaryInputRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads"
}
$cInputRoot = [System.IO.Path]::GetFullPath($PrimaryInputRoot)
$fInputRoot = [System.IO.Path]::GetFullPath($SecondaryInputRoot)
$quarantineParent = [System.IO.Path]::GetFullPath(
    (Join-Path $workspaceRoot "data\market\quarantine\csmar-manual-export")
)
$runId = "intake-20260729-33pkg-v1"
$finalRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent $runId)
)
$stagingRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent (".staging-" + $runId))
)
$rawRoot = Join-Path $stagingRoot "raw"

function Assert-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Path escapes the reviewed root: $resolvedPath"
    }
}

function Get-InputPackages {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    function Test-IsCsmarPackage {
        param(
            [Parameter(Mandatory = $true)][System.IO.FileInfo]$Package
        )

        $archive = [System.IO.Compression.ZipFile]::OpenRead($Package.FullName)
        try {
            $hasBusinessCsv = $false
            $hasDescriptor = $false
            foreach ($entry in $archive.Entries) {
                if ($entry.Name -match "^(TRD_|FS_|FIN_).+[.]csv$") {
                    $hasBusinessCsv = $true
                }
                if ($entry.Name -match "[[]DES[]][[]csv[]][.]txt$") {
                    $hasDescriptor = $true
                }
            }
            return ($hasBusinessCsv -and $hasDescriptor)
        }
        finally {
            $archive.Dispose()
        }
    }

    $cPackages = @(
        Get-ChildItem -LiteralPath $cInputRoot -File -Filter "*.zip" |
            Where-Object { Test-IsCsmarPackage -Package $_ } |
            Sort-Object Name
    )
    $fPackages = @(
        Get-ChildItem -LiteralPath $fInputRoot -File -Filter "*.zip" |
            Where-Object { Test-IsCsmarPackage -Package $_ } |
            Sort-Object Name
    )

    if ($cPackages.Count -ne 18) {
        throw "Expected exactly 18 reviewed C-drive packages; found $($cPackages.Count)"
    }
    if ($fPackages.Count -ne 15) {
        throw "Expected exactly 15 reviewed F-drive packages; found $($fPackages.Count)"
    }

    $packages = @($cPackages + $fPackages)
    $duplicateNames = @(
        $packages |
            Group-Object Name |
            Where-Object { $_.Count -ne 1 }
    )
    if ($duplicateNames.Count -ne 0) {
        throw "Package filenames are not unique across the reviewed inputs"
    }
    foreach ($package in $packages) {
        $sourcePath = [System.IO.Path]::GetFullPath($package.FullName)
        $allowed = $sourcePath.StartsWith(
            $cInputRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or $sourcePath.StartsWith(
            $fInputRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        if (-not $allowed) {
            throw "Input package escaped the two reviewed roots: $sourcePath"
        }
    }
    return $packages
}

function Test-ZipPayload {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    $entryCount = 0
    $unpackedBytes = [int64]0
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith("/")) {
                continue
            }
            $entryCount += 1
            $unpackedBytes += [int64]$entry.Length
            $stream = $entry.Open()
            try {
                $buffer = New-Object byte[] 1048576
                while ($stream.Read($buffer, 0, $buffer.Length) -gt 0) {
                }
            }
            finally {
                $stream.Dispose()
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    return [ordered]@{
        entry_count = $entryCount
        unpacked_bytes = $unpackedBytes
    }
}

function Write-JsonUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $json = $Value | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

$packages = @(Get-InputPackages)
$totalBytes = [int64](($packages | Measure-Object Length -Sum).Sum)
$plan = [ordered]@{
    schema_version = 1
    mode = "TRANSACTIONAL_COPY_VERIFY_DELETE"
    run_id = $runId
    package_count = $packages.Count
    total_bytes = $totalBytes
    input_roots = @($cInputRoot, $fInputRoot)
    destination = $finalRoot
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    catalog_write = $false
    source_deletion_requires_verified_copy = $true
    packages = @(
        $packages | ForEach-Object {
            [ordered]@{
                source_path = $_.FullName
                filename = $_.Name
                bytes = [int64]$_.Length
            }
        }
    )
}

if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 8
    exit 0
}

Assert-PathWithin -Path $quarantineParent -Root $workspaceRoot
Assert-PathWithin -Path $finalRoot -Root $quarantineParent
Assert-PathWithin -Path $stagingRoot -Root $quarantineParent

if (Test-Path -LiteralPath $finalRoot) {
    throw "Final intake already exists: $finalRoot"
}
New-Item -ItemType Directory -Path $rawRoot -Force | Out-Null

$copied = @()
foreach ($package in $packages) {
    $sourcePath = [System.IO.Path]::GetFullPath($package.FullName)
    $destinationPath = [System.IO.Path]::GetFullPath(
        (Join-Path $rawRoot $package.Name)
    )
    Assert-PathWithin -Path $destinationPath -Root $stagingRoot

    $sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Leaf)) {
        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath
    }
    $destinationItem = Get-Item -LiteralPath $destinationPath
    $destinationHash = (
        Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($destinationItem.Length -ne $package.Length) {
        throw "Copied byte length mismatch: $sourcePath"
    }
    if ($destinationHash -ne $sourceHash) {
        throw "Copied SHA-256 mismatch: $sourcePath"
    }
    $zipEvidence = Test-ZipPayload -Path $destinationPath

    $copied += [pscustomobject][ordered]@{
        filename = $package.Name
        source_path = $sourcePath
        source_drive = $package.PSDrive.Name
        destination_relative_path = "raw/$($package.Name)"
        bytes = [int64]$package.Length
        sha256 = $sourceHash
        zip_crc_read_status = "PASS"
        archive_entry_count = $zipEvidence.entry_count
        unpacked_bytes = $zipEvidence.unpacked_bytes
    }
}

$intakeManifest = [ordered]@{
    schema_version = 1
    run_id = $runId
    created_at = [DateTimeOffset]::Now.ToString("o")
    lifecycle = "quarantine_intake"
    mutability = "immutable_after_seal"
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    catalog_registered = $false
    research_ready = $false
    package_count = $copied.Count
    total_bytes = [int64]((
        $copied |
            ForEach-Object { [int64]$_.bytes } |
            Measure-Object -Sum
    ).Sum)
    hash_algorithm = "sha256"
    packages = $copied
}
Write-JsonUtf8 -Path (Join-Path $stagingRoot "INTAKE_MANIFEST.json") -Value $intakeManifest

$deletions = @()
foreach ($item in $copied) {
    $sourcePath = [string]$item.source_path
    $destinationPath = Join-Path $rawRoot ([string]$item.filename)
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Source disappeared before verified deletion: $sourcePath"
    }
    $sourceItem = Get-Item -LiteralPath $sourcePath
    $sourceHash = (
        Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $destinationHash = (
        Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (
        $sourceItem.Length -ne [int64]$item.bytes -or
        $sourceHash -ne [string]$item.sha256 -or
        $destinationHash -ne [string]$item.sha256
    ) {
        throw "Pre-delete verification failed: $sourcePath"
    }

    Remove-Item -LiteralPath $sourcePath -Force
    if (Test-Path -LiteralPath $sourcePath) {
        throw "Verified source deletion did not complete: $sourcePath"
    }
    $deletions += [pscustomobject][ordered]@{
        source_path = $sourcePath
        filename = [string]$item.filename
        bytes = [int64]$item.bytes
        sha256 = [string]$item.sha256
        destination_relative_path = [string]$item.destination_relative_path
        deleted = $true
        deleted_at = [DateTimeOffset]::Now.ToString("o")
    }
}

$deletionReceipt = [ordered]@{
    schema_version = 1
    run_id = $runId
    transaction = "COPY_VERIFY_DELETE"
    completed_at = [DateTimeOffset]::Now.ToString("o")
    deleted_source_count = $deletions.Count
    deleted_source_bytes = [int64]((
        $deletions |
            ForEach-Object { [int64]$_.bytes } |
            Measure-Object -Sum
    ).Sum)
    recovery_root_before_seal = $stagingRoot
    all_deletions_have_verified_sha256_copy = $true
    items = $deletions
}
Write-JsonUtf8 -Path (Join-Path $stagingRoot "SOURCE_DELETION_RECEIPT.json") -Value $deletionReceipt

Move-Item -LiteralPath $stagingRoot -Destination $finalRoot
if (-not (Test-Path -LiteralPath $finalRoot -PathType Container)) {
    throw "Final same-volume seal failed: $finalRoot"
}

$postSealFailures = @()
foreach ($item in $copied) {
    $sealedPath = Join-Path $finalRoot ([string]$item.destination_relative_path)
    if (-not (Test-Path -LiteralPath $sealedPath -PathType Leaf)) {
        $postSealFailures += "missing:$sealedPath"
        continue
    }
    $sealedHash = (
        Get-FileHash -LiteralPath $sealedPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($sealedHash -ne [string]$item.sha256) {
        $postSealFailures += "hash:$sealedPath"
    }
}
if ($postSealFailures.Count -ne 0) {
    throw "Post-seal verification failed: $($postSealFailures -join ', ')"
}

[ordered]@{
    status = "COMPLETED"
    run_id = $runId
    final_root = $finalRoot
    package_count = $copied.Count
    total_bytes = [int64]((
        $copied |
            ForEach-Object { [int64]$_.bytes } |
            Measure-Object -Sum
    ).Sum)
    source_deleted_count = $deletions.Count
    post_seal_hash_verification = "PASS"
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
} | ConvertTo-Json -Depth 4
