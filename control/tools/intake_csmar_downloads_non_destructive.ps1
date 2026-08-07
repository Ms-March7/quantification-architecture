[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Spec,
    [switch]$Execute
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

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
        throw "Path escapes reviewed root: $resolvedPath"
    }
}

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Reparse points are not allowed: $Path"
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (
        Get-FileHash -LiteralPath $Path -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}

function Get-ZipEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    $members = @()
    $seen = @{}
    $unpackedBytes = [int64]0
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith("/")) {
                continue
            }
            $memberKey = $entry.FullName.ToLowerInvariant()
            if ($seen.ContainsKey($memberKey)) {
                throw "Duplicate case-insensitive ZIP member: $($entry.FullName)"
            }
            $seen[$memberKey] = $true
            $stream = $entry.Open()
            try {
                $sha = [System.Security.Cryptography.SHA256]::Create()
                try {
                    $memberHash = [System.BitConverter]::ToString(
                        $sha.ComputeHash($stream)
                    ).Replace("-", "").ToLowerInvariant()
                }
                finally {
                    $sha.Dispose()
                }
            }
            finally {
                $stream.Dispose()
            }
            $unpackedBytes += [int64]$entry.Length
            $members += [pscustomobject][ordered]@{
                member_path = $entry.FullName
                bytes = [int64]$entry.Length
                compressed_bytes = [int64]$entry.CompressedLength
                sha256 = $memberHash
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    return [pscustomobject][ordered]@{
        zip_crc_read_status = "PASS"
        archive_entry_count = $members.Count
        unpacked_bytes = $unpackedBytes
        members = @($members)
    }
}

function Assert-ZipEvidenceEqual {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Observed,
        [Parameter(Mandatory = $true)][string]$Path
    )
    if (
        [int]$Expected.archive_entry_count -ne
            [int]$Observed.archive_entry_count -or
        [int64]$Expected.unpacked_bytes -ne [int64]$Observed.unpacked_bytes
    ) {
        throw "ZIP aggregate evidence changed: $Path"
    }
    for ($index = 0; $index -lt [int]$Expected.archive_entry_count; $index++) {
        $left = $Expected.members[$index]
        $right = $Observed.members[$index]
        if (
            [string]$left.member_path -cne [string]$right.member_path -or
            [int64]$left.bytes -ne [int64]$right.bytes -or
            [string]$left.sha256 -ne [string]$right.sha256
        ) {
            throw "ZIP member evidence changed at index $index`: $Path"
        }
    }
}

function Write-JsonUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $json = $Value | ConvertTo-Json -Depth 32
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

$workspaceRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\..")
)
$quarantineParent = [System.IO.Path]::GetFullPath(
    (Join-Path $workspaceRoot "data\market\quarantine\csmar-manual-export")
)
$specPath = [System.IO.Path]::GetFullPath($Spec)
Assert-PathWithin -Path $specPath -Root $workspaceRoot
Assert-NoReparsePoint -Path $specPath
$request = Get-Content -LiteralPath $specPath -Encoding utf8 -Raw |
    ConvertFrom-Json

if ([int]$request.schema_version -ne 1) {
    throw "Unsupported schema_version"
}
$runId = [string]$request.run_id
if ($runId -notmatch "^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$") {
    throw "Invalid run_id: $runId"
}
$sourceRoot = [System.IO.Path]::GetFullPath([string]$request.source_root)
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Missing source_root: $sourceRoot"
}
Assert-NoReparsePoint -Path $sourceRoot
$finalRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent $runId)
)
$stagingRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent (".staging-" + $runId))
)
Assert-PathWithin -Path $finalRoot -Root $quarantineParent
Assert-PathWithin -Path $stagingRoot -Root $quarantineParent
if (Test-Path -LiteralPath $finalRoot) {
    throw "Final intake already exists: $finalRoot"
}
if (Test-Path -LiteralPath $stagingRoot) {
    throw "Staging intake already exists: $stagingRoot"
}

$sourceSpecs = @($request.sources)
if ($sourceSpecs.Count -eq 0) {
    throw "The exact source list is empty"
}
$nameKeys = @{}
$observed = @()
foreach ($sourceSpec in $sourceSpecs) {
    $filename = [string]$sourceSpec.filename
    if (
        [string]::IsNullOrWhiteSpace($filename) -or
        [System.IO.Path]::GetFileName($filename) -cne $filename
    ) {
        throw "Source filename must be one leaf name: $filename"
    }
    $nameKey = $filename.ToLowerInvariant()
    if ($nameKeys.ContainsKey($nameKey)) {
        throw "Duplicate case-insensitive source filename: $filename"
    }
    $nameKeys[$nameKey] = $true
    $kind = [string]$sourceSpec.kind
    if ($kind -notin @("package", "documentation")) {
        throw "Unsupported source kind: $kind"
    }
    $extension = [System.IO.Path]::GetExtension($filename).ToLowerInvariant()
    if (
        ($kind -eq "package" -and $extension -ne ".zip") -or
        ($kind -eq "documentation" -and $extension -ne ".pdf")
    ) {
        throw "Kind and extension disagree: $filename"
    }
    $expectedHash = ([string]$sourceSpec.sha256).ToLowerInvariant()
    if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
        throw "Invalid expected SHA-256: $filename"
    }
    $sourcePath = [System.IO.Path]::GetFullPath(
        (Join-Path $sourceRoot $filename)
    )
    Assert-PathWithin -Path $sourcePath -Root $sourceRoot
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Missing reviewed source: $sourcePath"
    }
    Assert-NoReparsePoint -Path $sourcePath
    $item = Get-Item -LiteralPath $sourcePath
    $observedHash = Get-Sha256 -Path $sourcePath
    if ([int64]$item.Length -ne [int64]$sourceSpec.bytes) {
        throw "Source byte length changed: $sourcePath"
    }
    if ($observedHash -ne $expectedHash) {
        throw "Source SHA-256 changed: $sourcePath"
    }
    $zipEvidence = $null
    if ($kind -eq "package") {
        $zipEvidence = Get-ZipEvidence -Path $sourcePath
    }
    $observed += [pscustomobject][ordered]@{
        filename = $filename
        source_path = $sourcePath
        kind = $kind
        bytes = [int64]$item.Length
        last_write_time = $item.LastWriteTime.ToString("o")
        sha256 = $observedHash
        zip_evidence = $zipEvidence
    }
}

$uniquePlans = @()
foreach ($group in @($observed | Group-Object sha256 | Sort-Object Name)) {
    $items = @($group.Group | Sort-Object filename)
    $kinds = @($items.kind | Sort-Object -Unique)
    if ($kinds.Count -ne 1) {
        throw "One content hash has multiple kinds: $($group.Name)"
    }
    $preferred = @(
        $items | Where-Object {
            $_.filename -notmatch "字节相同副本" -and
            $_.filename -notmatch " [(][0-9]+[)][.](zip|pdf)$"
        }
    )
    $canonical = if ($preferred.Count -gt 0) {
        $preferred[0]
    }
    else {
        $items[0]
    }
    $subdirectory = if ($canonical.kind -eq "package") {
        "raw\packages"
    }
    else {
        "raw\documentation"
    }
    $relativePath = (
        Join-Path $subdirectory $canonical.filename
    ).Replace("\", "/")
    $uniquePlans += [pscustomobject][ordered]@{
        sha256 = [string]$group.Name
        kind = [string]$canonical.kind
        canonical_source_path = [string]$canonical.source_path
        canonical_filename = [string]$canonical.filename
        destination_relative_path = $relativePath
        bytes = [int64]$canonical.bytes
        source_aliases = @($items.filename)
        source_alias_count = $items.Count
        zip_evidence = $canonical.zip_evidence
    }
}

$plan = [ordered]@{
    schema_version = 1
    mode = "COPY_VERIFY_DEDUP_SEAL_NO_SOURCE_DELETE"
    run_id = $runId
    source_root = $sourceRoot
    destination = $finalRoot
    source_file_count = $observed.Count
    source_total_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
    unique_blob_count = $uniquePlans.Count
    unique_storage_bytes = [int64](($uniquePlans | Measure-Object bytes -Sum).Sum)
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    catalog_write = $false
    source_delete = $false
    source_files = $observed
    unique_blobs = $uniquePlans
}
if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 32
    exit 0
}

$movedToFinal = $false
try {
    New-Item -ItemType Directory -Path $stagingRoot | Out-Null
    $copied = @()
    foreach ($blob in $uniquePlans) {
        $destinationPath = [System.IO.Path]::GetFullPath(
            (Join-Path $stagingRoot $blob.destination_relative_path)
        )
        Assert-PathWithin -Path $destinationPath -Root $stagingRoot
        New-Item -ItemType Directory -Path (
            Split-Path -Parent $destinationPath
        ) -Force | Out-Null
        Copy-Item -LiteralPath $blob.canonical_source_path -Destination $destinationPath
        $destinationItem = Get-Item -LiteralPath $destinationPath
        $destinationHash = Get-Sha256 -Path $destinationPath
        if (
            [int64]$destinationItem.Length -ne [int64]$blob.bytes -or
            $destinationHash -ne [string]$blob.sha256
        ) {
            throw "Copied payload mismatch: $destinationPath"
        }
        $destinationZipEvidence = $null
        if ($blob.kind -eq "package") {
            $destinationZipEvidence = Get-ZipEvidence -Path $destinationPath
            Assert-ZipEvidenceEqual `
                -Expected $blob.zip_evidence `
                -Observed $destinationZipEvidence `
                -Path $destinationPath
        }
        $copied += [pscustomobject][ordered]@{
            sha256 = [string]$blob.sha256
            kind = [string]$blob.kind
            canonical_filename = [string]$blob.canonical_filename
            destination_relative_path = [string]$blob.destination_relative_path
            bytes = [int64]$blob.bytes
            source_aliases = @($blob.source_aliases)
            source_alias_count = [int]$blob.source_alias_count
            copy_hash_verification = "PASS"
            zip_evidence = $destinationZipEvidence
        }
    }

    $manifest = [ordered]@{
        schema_version = 1
        run_id = $runId
        created_at = [DateTimeOffset]::Now.ToString("o")
        lifecycle = "quarantine_intake"
        mutability = "immutable_after_seal"
        purpose_ceiling = "INSPECTION_ONLY"
        admission_decision = "NOT_ADMITTED"
        catalog_registered = $false
        research_ready = $false
        source_root = $sourceRoot
        source_file_count = $observed.Count
        source_total_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
        unique_blob_count = $copied.Count
        unique_storage_bytes = [int64](($copied | Measure-Object bytes -Sum).Sum)
        hash_algorithm = "sha256"
        source_files = $observed
        unique_blobs = $copied
    }
    Write-JsonUtf8 `
        -Path (Join-Path $stagingRoot "INTAKE_MANIFEST.json") `
        -Value $manifest
    $retention = [ordered]@{
        schema_version = 1
        run_id = $runId
        completed_at = [DateTimeOffset]::Now.ToString("o")
        action = "NO_SOURCE_DELETE"
        reason = "Source cleanup is a separate exact-path recycle-bin transaction."
        retained_source_count = $observed.Count
        retained_source_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
        all_sources_still_exist = $true
        items = @($observed)
    }
    Write-JsonUtf8 `
        -Path (Join-Path $stagingRoot "SOURCE_RETENTION_RECEIPT.json") `
        -Value $retention

    Move-Item -LiteralPath $stagingRoot -Destination $finalRoot
    $movedToFinal = $true
    $postSealFailures = @()
    foreach ($blob in $copied) {
        $sealedPath = Join-Path $finalRoot $blob.destination_relative_path
        if (-not (Test-Path -LiteralPath $sealedPath -PathType Leaf)) {
            $postSealFailures += "missing:$sealedPath"
            continue
        }
        if ((Get-Sha256 -Path $sealedPath) -ne [string]$blob.sha256) {
            $postSealFailures += "hash:$sealedPath"
            continue
        }
        if ($blob.kind -eq "package") {
            try {
                $sealedZipEvidence = Get-ZipEvidence -Path $sealedPath
                Assert-ZipEvidenceEqual `
                    -Expected $blob.zip_evidence `
                    -Observed $sealedZipEvidence `
                    -Path $sealedPath
            }
            catch {
                $postSealFailures += "zip:$sealedPath"
            }
        }
    }
    foreach ($source in $observed) {
        if (-not (Test-Path -LiteralPath $source.source_path -PathType Leaf)) {
            $postSealFailures += "source_missing:$($source.source_path)"
            continue
        }
        $sourceItem = Get-Item -LiteralPath $source.source_path
        if (
            [int64]$sourceItem.Length -ne [int64]$source.bytes -or
            (Get-Sha256 -Path $source.source_path) -ne [string]$source.sha256
        ) {
            $postSealFailures += "source_changed:$($source.source_path)"
        }
    }
    if ($postSealFailures.Count -ne 0) {
        throw "Post-seal verification failed: $($postSealFailures -join ', ')"
    }
}
catch {
    $originalError = $_
    try {
        if ($movedToFinal -and (Test-Path -LiteralPath $finalRoot)) {
            Move-Item -LiteralPath $finalRoot -Destination $stagingRoot
            $movedToFinal = $false
        }
        if (Test-Path -LiteralPath $stagingRoot) {
            Assert-PathWithin -Path $stagingRoot -Root $quarantineParent
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
    catch {
        throw "Intake failed and rollback also failed. Original: $originalError Rollback: $_"
    }
    throw $originalError
}

[ordered]@{
    status = "COMPLETED"
    run_id = $runId
    final_root = $finalRoot
    source_file_count = $observed.Count
    source_total_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
    unique_blob_count = $uniquePlans.Count
    unique_storage_bytes = [int64](($uniquePlans | Measure-Object bytes -Sum).Sum)
    post_seal_source_and_destination_hash_verification = "PASS"
    zip_crc_and_member_hash_verification = "PASS"
    source_delete = $false
    all_sources_retained = $true
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
} | ConvertTo-Json -Depth 8
