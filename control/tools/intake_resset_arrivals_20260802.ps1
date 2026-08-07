[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$WorkspaceRoot = "",
    [string]$DownloadsRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function ConvertFrom-CodePoints {
    param([Parameter(Mandatory = $true)][int[]]$CodePoints)

    $builder = New-Object System.Text.StringBuilder
    foreach ($codePoint in $CodePoints) {
        [void]$builder.Append([char]$codePoint)
    }
    return $builder.ToString()
}

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

if (-not ("RessetIntakeStreamDigest" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Security.Cryptography;

public sealed class RessetIntakeDigestResult
{
    public long Bytes { get; set; }
    public uint Crc32 { get; set; }
    public string Sha256 { get; set; }
}

public static class RessetIntakeStreamDigest
{
    private static readonly uint[] Table = BuildTable();

    private static uint[] BuildTable()
    {
        uint[] table = new uint[256];
        for (uint index = 0; index < table.Length; index++)
        {
            uint value = index;
            for (int bit = 0; bit < 8; bit++)
            {
                value = (value & 1U) != 0U
                    ? 0xEDB88320U ^ (value >> 1)
                    : value >> 1;
            }
            table[index] = value;
        }
        return table;
    }

    public static RessetIntakeDigestResult Compute(Stream input)
    {
        byte[] buffer = new byte[1048576];
        uint crc = 0xFFFFFFFFU;
        long bytes = 0;
        using (SHA256 sha = SHA256.Create())
        {
            int read;
            while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
            {
                bytes += read;
                for (int index = 0; index < read; index++)
                {
                    crc = Table[(crc ^ buffer[index]) & 0xFFU] ^ (crc >> 8);
                }
                sha.TransformBlock(buffer, 0, read, buffer, 0);
            }
            sha.TransformFinalBlock(new byte[0], 0, 0);
            return new RessetIntakeDigestResult
            {
                Bytes = bytes,
                Crc32 = crc ^ 0xFFFFFFFFU,
                Sha256 = BitConverter.ToString(sha.Hash)
                    .Replace("-", "")
                    .ToLowerInvariant()
            };
        }
    }
}
"@
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
            if (
                [string]::IsNullOrWhiteSpace($entry.FullName) -or
                [System.IO.Path]::IsPathRooted($entry.FullName) -or
                @($entry.FullName -split "[/\\]") -contains ".."
            ) {
                throw "Unsafe ZIP member path: $($entry.FullName)"
            }
            $memberKey = $entry.FullName.ToLowerInvariant()
            if ($seen.ContainsKey($memberKey)) {
                throw "Duplicate case-insensitive ZIP member: $($entry.FullName)"
            }
            $seen[$memberKey] = $true

            $crcField = $entry.GetType().GetField(
                "_crc32",
                [System.Reflection.BindingFlags]::Instance -bor
                    [System.Reflection.BindingFlags]::NonPublic
            )
            if ($null -eq $crcField) {
                throw "ZIP central-directory CRC field is unavailable."
            }
            $declaredCrc32 = [uint32]$crcField.GetValue($entry)
            $stream = $entry.Open()
            try {
                $digest = [RessetIntakeStreamDigest]::Compute($stream)
            }
            finally {
                $stream.Dispose()
            }
            if ([int64]$digest.Bytes -ne [int64]$entry.Length) {
                throw "ZIP member length mismatch: $($entry.FullName)"
            }
            if ([uint32]$digest.Crc32 -ne $declaredCrc32) {
                throw "ZIP member CRC-32 mismatch: $($entry.FullName)"
            }
            $unpackedBytes += [int64]$entry.Length
            $members += [pscustomobject][ordered]@{
                member_path = $entry.FullName
                bytes = [int64]$entry.Length
                compressed_bytes = [int64]$entry.CompressedLength
                declared_crc32 = $declaredCrc32.ToString("x8")
                computed_crc32 = ([uint32]$digest.Crc32).ToString("x8")
                sha256 = [string]$digest.Sha256
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    if ($members.Count -eq 0) {
        throw "ZIP archive contains no file members: $Path"
    }
    return [pscustomobject][ordered]@{
        status = "PASS"
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
            [int64]$left.compressed_bytes -ne [int64]$right.compressed_bytes -or
            [string]$left.declared_crc32 -ne [string]$right.declared_crc32 -or
            [string]$left.computed_crc32 -ne [string]$right.computed_crc32 -or
            [string]$left.sha256 -ne [string]$right.sha256
        ) {
            throw "ZIP member evidence changed at index $index`: $Path"
        }
    }
}

$stock = ConvertFrom-CodePoints @(0x80A1, 0x7968)
$companyNameHistory = ConvertFrom-CodePoints @(
    0x516C, 0x53F8, 0x540D, 0x79F0, 0x53D8, 0x66F4, 0x5386, 0x53F2
)
$delistingInfo = ConvertFrom-CodePoints @(0x9000, 0x5E02, 0x4FE1, 0x606F)
$suspension = ConvertFrom-CodePoints @(0x505C, 0x590D, 0x724C)
$specialTreatment = ConvertFrom-CodePoints @(
    0x80A1, 0x7968, 0x7279, 0x522B, 0x5904, 0x7406,
    0x4E0E, 0x64A4, 0x9500
)

if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = Join-Path $PSScriptRoot "..\.."
}
if ([string]::IsNullOrWhiteSpace($DownloadsRoot)) {
    $DownloadsRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads"
}
$workspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
$sourceRoot = [System.IO.Path]::GetFullPath(
    $DownloadsRoot
)
$quarantineParent = [System.IO.Path]::GetFullPath(
    (Join-Path $workspaceRoot "data\market\quarantine\resset-manual-export")
)
$runId = "intake-20260802-04pkg-v1"
$finalRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent $runId)
)
$stagingRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent (".staging-" + $runId))
)
$expectedSources = @(
    [ordered]@{
        table_id = "COMNMHIS"
        filename = "RESSET $stock`_$companyNameHistory.zip"
        bytes = [int64]861662
        sha256 = "5b82e43131814dd15842111e5a69d67a28638d19f3474e7ad5ea968ddb060391"
    },
    [ordered]@{
        table_id = "DELINFO"
        filename = "RESSET $stock`_$delistingInfo.zip"
        bytes = [int64]1127756
        sha256 = "ca5dd51bda1b055f4bf27f37122f6074ba2deb62b1e5f3b7bf466865fab3304b"
    },
    [ordered]@{
        table_id = "SUSPNSNRESMPTN"
        filename = "RESSET $stock`_$suspension.zip"
        bytes = [int64]1794834
        sha256 = "cb2760ac788468e3b4c1c6eb6a92ab72a3aa9458e84c770e12e75edc40d3016d"
    },
    [ordered]@{
        table_id = "STKSPCTRMT"
        filename = "RESSET $stock`_$specialTreatment.zip"
        bytes = [int64]739790
        sha256 = "963e36bcbc8c3dc305932cf169829865d4c3d5375430c70f57e6f217533bcbdb"
    }
)

if (-not (Test-Path -LiteralPath $workspaceRoot -PathType Container)) {
    throw "Missing workspace root: $workspaceRoot"
}
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Missing source root: $sourceRoot"
}
Assert-NoReparsePoint -Path $workspaceRoot
Assert-NoReparsePoint -Path $sourceRoot
Assert-PathWithin -Path $quarantineParent -Root $workspaceRoot
Assert-PathWithin -Path $finalRoot -Root $quarantineParent
Assert-PathWithin -Path $stagingRoot -Root $quarantineParent
if (
    [System.IO.Path]::GetPathRoot($finalRoot) -cne
        [System.IO.Path]::GetPathRoot($stagingRoot)
) {
    throw "Staging and final roots must be on the same volume."
}
if (Test-Path -LiteralPath $finalRoot) {
    throw "Final intake already exists: $finalRoot"
}
if (Test-Path -LiteralPath $stagingRoot) {
    throw "Staging intake already exists: $stagingRoot"
}

$observed = @()
foreach ($expected in $expectedSources) {
    $sourcePath = [System.IO.Path]::GetFullPath(
        (Join-Path $sourceRoot ([string]$expected.filename))
    )
    Assert-PathWithin -Path $sourcePath -Root $sourceRoot
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Missing reviewed source: $sourcePath"
    }
    Assert-NoReparsePoint -Path $sourcePath
    $item = Get-Item -LiteralPath $sourcePath -Force
    if ([int64]$item.Length -ne [int64]$expected.bytes) {
        throw "Source byte length changed: $sourcePath"
    }
    $sha256 = Get-Sha256 -Path $sourcePath
    if ($sha256 -ne [string]$expected.sha256) {
        throw "Source SHA-256 changed: $sourcePath"
    }
    $zipEvidence = Get-ZipEvidence -Path $sourcePath
    $observed += [pscustomobject][ordered]@{
        table_id = [string]$expected.table_id
        filename = $item.Name
        source_path = $sourcePath
        bytes = [int64]$item.Length
        last_write_time = $item.LastWriteTime.ToString("o")
        sha256 = $sha256
        zip_evidence = $zipEvidence
    }
}

$hashGroups = @($observed | Group-Object sha256)
if ($hashGroups.Count -ne $expectedSources.Count) {
    throw "The four reviewed RESSET packages must have four unique hashes."
}
$copyPlans = @()
foreach ($source in $observed) {
    $relativePath = (
        Join-Path "raw\packages" $source.filename
    ).Replace("\", "/")
    $copyPlans += [pscustomobject][ordered]@{
        table_id = [string]$source.table_id
        canonical_source_path = [string]$source.source_path
        filename = [string]$source.filename
        destination_relative_path = $relativePath
        bytes = [int64]$source.bytes
        sha256 = [string]$source.sha256
        zip_evidence = $source.zip_evidence
    }
}

$formalBoundary = [ordered]@{
    formal_effect = "NONE"
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    formal_source_admission = $false
    catalog_write = $false
    quality_promotion = $false
    snapshot_write = $false
    research_use = $false
    source_delete = $false
}
$plan = [ordered]@{
    schema_version = 1
    mode = "COPY_VERIFY_SEAL_NO_SOURCE_DELETE"
    execution = "PLAN_ONLY_UNLESS_EXPLICIT_EXECUTE"
    run_id = $runId
    source_root = $sourceRoot
    destination = $finalRoot
    staging = $stagingRoot
    same_volume_staging = $true
    source_file_count = $observed.Count
    source_total_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
    unique_blob_count = $copyPlans.Count
    unique_storage_bytes = [int64](($copyPlans | Measure-Object bytes -Sum).Sum)
    formal_effect = "NONE"
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    catalog_write = $false
    research_use = $false
    formal_boundary = $formalBoundary
    source_files = $observed
    copy_plan = $copyPlans
}
if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 32
    exit 0
}

$movedToFinal = $false
try {
    if (-not (Test-Path -LiteralPath $quarantineParent -PathType Container)) {
        New-Item -ItemType Directory -Path $quarantineParent -Force | Out-Null
    }
    Assert-NoReparsePoint -Path $quarantineParent
    New-Item -ItemType Directory -Path $stagingRoot | Out-Null

    $copied = @()
    foreach ($blob in $copyPlans) {
        $destinationPath = [System.IO.Path]::GetFullPath(
            (Join-Path $stagingRoot $blob.destination_relative_path)
        )
        Assert-PathWithin -Path $destinationPath -Root $stagingRoot
        New-Item -ItemType Directory -Path (
            Split-Path -Parent $destinationPath
        ) -Force | Out-Null
        Copy-Item -LiteralPath $blob.canonical_source_path -Destination $destinationPath
        $destinationItem = Get-Item -LiteralPath $destinationPath -Force
        if (
            [int64]$destinationItem.Length -ne [int64]$blob.bytes -or
            (Get-Sha256 -Path $destinationPath) -ne [string]$blob.sha256
        ) {
            throw "Copied payload mismatch: $destinationPath"
        }
        $destinationZipEvidence = Get-ZipEvidence -Path $destinationPath
        Assert-ZipEvidenceEqual `
            -Expected $blob.zip_evidence `
            -Observed $destinationZipEvidence `
            -Path $destinationPath
        $copied += [pscustomobject][ordered]@{
            table_id = [string]$blob.table_id
            filename = [string]$blob.filename
            destination_relative_path = [string]$blob.destination_relative_path
            bytes = [int64]$blob.bytes
            sha256 = [string]$blob.sha256
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
        source_type = "RESSET_INSTITUTIONAL_MANUAL_EXPORT"
        source_access_note = (
            "Institution-provided academic research access; rights, semantics, " +
            "PIT and revision chains require separate admission evidence."
        )
        formal_effect = "NONE"
        purpose_ceiling = "INSPECTION_ONLY"
        admission_decision = "NOT_ADMITTED"
        catalog_write = $false
        research_use = $false
        formal_boundary = $formalBoundary
        source_root = $sourceRoot
        source_file_count = $observed.Count
        source_total_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
        unique_blob_count = $copied.Count
        unique_storage_bytes = [int64](($copied | Measure-Object bytes -Sum).Sum)
        hash_algorithm = "sha256"
        source_files = $observed
        sealed_blobs = $copied
    }
    $manifestPath = Join-Path $stagingRoot "INTAKE_MANIFEST.json"
    Write-JsonUtf8 -Path $manifestPath -Value $manifest
    $manifestHash = Get-Sha256 -Path $manifestPath
    [System.IO.File]::WriteAllText(
        (Join-Path $stagingRoot "INTAKE_MANIFEST.sha256"),
        $manifestHash + "  INTAKE_MANIFEST.json" + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )

    $sourceRetention = [ordered]@{
        schema_version = 1
        run_id = $runId
        completed_at = [DateTimeOffset]::Now.ToString("o")
        action = "NO_SOURCE_DELETE"
        formal_effect = "NONE"
        reason = (
            "Original Downloads files remain untouched; any later cleanup " +
            "requires a separate exact-path approval and transaction."
        )
        retained_source_count = $observed.Count
        retained_source_bytes = [int64](($observed | Measure-Object bytes -Sum).Sum)
        all_sources_still_exist = $true
        items = @(
            $observed | ForEach-Object {
                [ordered]@{
                    table_id = $_.table_id
                    source_path = $_.source_path
                    filename = $_.filename
                    bytes = [int64]$_.bytes
                    sha256 = $_.sha256
                    retained = $true
                }
            }
        )
    }
    Write-JsonUtf8 `
        -Path (Join-Path $stagingRoot "SOURCE_RETENTION_RECEIPT.json") `
        -Value $sourceRetention

    Move-Item -LiteralPath $stagingRoot -Destination $finalRoot
    $movedToFinal = $true

    $postSealFailures = @()
    foreach ($blob in $copied) {
        $sealedPath = [System.IO.Path]::GetFullPath(
            (Join-Path $finalRoot $blob.destination_relative_path)
        )
        Assert-PathWithin -Path $sealedPath -Root $finalRoot
        if (-not (Test-Path -LiteralPath $sealedPath -PathType Leaf)) {
            $postSealFailures += "missing:$sealedPath"
            continue
        }
        if ((Get-Sha256 -Path $sealedPath) -ne [string]$blob.sha256) {
            $postSealFailures += "hash:$sealedPath"
            continue
        }
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
    foreach ($source in $observed) {
        if (-not (Test-Path -LiteralPath $source.source_path -PathType Leaf)) {
            $postSealFailures += "source_missing:$($source.source_path)"
            continue
        }
        $sourceItem = Get-Item -LiteralPath $source.source_path -Force
        if (
            [int64]$sourceItem.Length -ne [int64]$source.bytes -or
            (Get-Sha256 -Path $source.source_path) -ne [string]$source.sha256
        ) {
            $postSealFailures += "source_changed:$($source.source_path)"
        }
    }
    $sealedManifestPath = Join-Path $finalRoot "INTAKE_MANIFEST.json"
    if ((Get-Sha256 -Path $sealedManifestPath) -ne $manifestHash) {
        $postSealFailures += "manifest_hash:$sealedManifestPath"
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
    unique_blob_count = $copyPlans.Count
    unique_storage_bytes = [int64](($copyPlans | Measure-Object bytes -Sum).Sum)
    post_seal_source_and_destination_hash_verification = "PASS"
    zip_member_crc_size_and_hash_verification = "PASS"
    manifest_sha256 = $manifestHash
    formal_effect = "NONE"
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    catalog_write = $false
    research_use = $false
    source_delete = $false
    all_sources_retained = $true
} | ConvertTo-Json -Depth 8
