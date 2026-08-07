[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Request,
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

function Test-ZipPayload {
    param([Parameter(Mandatory = $true)][string]$Path)
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
    return [pscustomobject][ordered]@{
        status = "PASS"
        entry_count = $entryCount
        unpacked_bytes = $unpackedBytes
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
$requestPath = [System.IO.Path]::GetFullPath($Request)
Assert-PathWithin -Path $requestPath -Root $workspaceRoot
Assert-NoReparsePoint -Path $requestPath
$cleanupRequest = Get-Content -LiteralPath $requestPath -Encoding utf8 -Raw |
    ConvertFrom-Json
if ([int]$cleanupRequest.schema_version -ne 1) {
    throw "Unsupported schema_version"
}
if ([string]$cleanupRequest.action -cne "SEND_TO_RECYCLE_BIN") {
    throw "Only SEND_TO_RECYCLE_BIN is supported"
}
$sourceRoot = [System.IO.Path]::GetFullPath(
    [string]$cleanupRequest.source_root
)
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Missing source root: $sourceRoot"
}
Assert-NoReparsePoint -Path $sourceRoot
$allBefore = @(
    Get-ChildItem -LiteralPath $sourceRoot -File -Force |
        Sort-Object FullName
)
if (
    $allBefore.Count -ne [int]$cleanupRequest.expected_total_file_count
) {
    throw "Downloads file count changed: expected $($cleanupRequest.expected_total_file_count), observed $($allBefore.Count)"
}
$partialFiles = @(
    $allBefore | Where-Object {
        $_.Extension -in @(".crdownload", ".part", ".partial", ".tmp")
    }
)
if ($partialFiles.Count -ne [int]$cleanupRequest.expected_partial_file_count) {
    throw "Partial-download count changed"
}

$targets = @()
$manifestBindings = @()
$institutionMarker = -join (
    [char[]]@(
        0x4EC5, 0x4F9B, 0x6E05, 0x534E, 0x5927,
        0x5B66, 0x4F7F, 0x7528
    )
)
foreach ($manifestRelative in @($cleanupRequest.intake_manifests)) {
    $manifestPath = [System.IO.Path]::GetFullPath(
        (Join-Path $workspaceRoot ([string]$manifestRelative))
    )
    Assert-PathWithin -Path $manifestPath -Root $workspaceRoot
    Assert-NoReparsePoint -Path $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Encoding utf8 -Raw |
        ConvertFrom-Json
    if (
        [string]$manifest.purpose_ceiling -cne "INSPECTION_ONLY" -or
        [string]$manifest.admission_decision -cne "NOT_ADMITTED"
    ) {
        throw "Unexpected intake boundary: $manifestPath"
    }
    $intakeRoot = Split-Path -Parent $manifestPath
    $manifestBindings += [pscustomobject][ordered]@{
        path = $manifestPath
        sha256 = Get-Sha256 -Path $manifestPath
        run_id = [string]$manifest.run_id
    }
    foreach ($source in @($manifest.source_files)) {
        $sourcePath = [System.IO.Path]::GetFullPath([string]$source.source_path)
        Assert-PathWithin -Path $sourcePath -Root $sourceRoot
        $filename = [System.IO.Path]::GetFileName($sourcePath)
        if (
            -not (
                $filename.StartsWith(
                    "CSMAR_",
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -or
                $filename.Contains($institutionMarker)
            )
        ) {
            throw "Source does not satisfy the reviewed CSMAR name predicate: $sourcePath"
        }
        $matchingBlobs = @(
            @($manifest.unique_blobs) | Where-Object {
                [string]$_.sha256 -eq [string]$source.sha256 -and
                @($_.source_aliases) -contains [string]$source.filename
            }
        )
        if ($matchingBlobs.Count -ne 1) {
            throw "Source does not resolve to exactly one sealed blob: $sourcePath"
        }
        $blob = $matchingBlobs[0]
        $destinationPath = [System.IO.Path]::GetFullPath(
            (Join-Path $intakeRoot ([string]$blob.destination_relative_path))
        )
        Assert-PathWithin -Path $destinationPath -Root $intakeRoot
        $targets += [pscustomobject][ordered]@{
            source_path = $sourcePath
            filename = $filename
            bytes = [int64]$source.bytes
            sha256 = [string]$source.sha256
            intake_run_id = [string]$manifest.run_id
            intake_manifest = $manifestPath
            destination_path = $destinationPath
        }
    }
}

$duplicateTargets = @(
    $targets |
        Group-Object { $_.source_path.ToLowerInvariant() } |
        Where-Object { $_.Count -ne 1 }
)
if ($duplicateTargets.Count -ne 0) {
    throw "Duplicate cleanup source paths were resolved"
}
if ($targets.Count -ne [int]$cleanupRequest.expected_target_count) {
    throw "Cleanup target count changed"
}
$targetBytes = [int64](($targets | Measure-Object bytes -Sum).Sum)
if ($targetBytes -ne [int64]$cleanupRequest.expected_target_bytes) {
    throw "Cleanup target bytes changed"
}
$targetKeys = @{}
foreach ($target in $targets) {
    $targetKeys[$target.source_path.ToLowerInvariant()] = $true
}
$excludedBefore = @(
    $allBefore | Where-Object {
        -not $targetKeys.ContainsKey($_.FullName.ToLowerInvariant())
    }
)
if (
    $excludedBefore.Count -ne
        [int]$cleanupRequest.expected_excluded_file_count
) {
    throw "Excluded Downloads count changed"
}

$destinationCache = @{}
$verified = @()
foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target.source_path -PathType Leaf)) {
        throw "Cleanup source disappeared: $($target.source_path)"
    }
    Assert-NoReparsePoint -Path $target.source_path
    $sourceItem = Get-Item -LiteralPath $target.source_path
    if (
        [int64]$sourceItem.Length -ne [int64]$target.bytes -or
        (Get-Sha256 -Path $target.source_path) -ne [string]$target.sha256
    ) {
        throw "Cleanup source failed exact verification: $($target.source_path)"
    }
    if (-not (Test-Path -LiteralPath $target.destination_path -PathType Leaf)) {
        throw "Sealed destination is missing: $($target.destination_path)"
    }
    Assert-NoReparsePoint -Path $target.destination_path
    $destinationItem = Get-Item -LiteralPath $target.destination_path
    if (
        [int64]$destinationItem.Length -ne [int64]$target.bytes -or
        (Get-Sha256 -Path $target.destination_path) -ne [string]$target.sha256
    ) {
        throw "Sealed destination failed exact verification: $($target.destination_path)"
    }
    $destinationKey = $target.destination_path.ToLowerInvariant()
    if (-not $destinationCache.ContainsKey($destinationKey)) {
        $zipStatus = "NOT_APPLICABLE"
        $zipEntryCount = $null
        $zipUnpackedBytes = $null
        if ([System.IO.Path]::GetExtension($target.destination_path) -ieq ".zip") {
            $zipEvidence = Test-ZipPayload -Path $target.destination_path
            $zipStatus = [string]$zipEvidence.status
            $zipEntryCount = [int]$zipEvidence.entry_count
            $zipUnpackedBytes = [int64]$zipEvidence.unpacked_bytes
        }
        $destinationCache[$destinationKey] = [pscustomobject][ordered]@{
            destination_path = $target.destination_path
            bytes = [int64]$target.bytes
            sha256 = [string]$target.sha256
            zip_crc_read_status = $zipStatus
            archive_entry_count = $zipEntryCount
            unpacked_bytes = $zipUnpackedBytes
        }
    }
    $verified += [pscustomobject][ordered]@{
        source_path = $target.source_path
        filename = $target.filename
        bytes = [int64]$target.bytes
        sha256 = [string]$target.sha256
        intake_run_id = $target.intake_run_id
        intake_manifest = $target.intake_manifest
        destination_path = $target.destination_path
        source_destination_hash_match = $true
        recycle_status = "PENDING"
    }
}

$plan = [ordered]@{
    schema_version = 1
    action = "SEND_TO_RECYCLE_BIN"
    source_root = $sourceRoot
    target_count = $verified.Count
    target_bytes = $targetBytes
    unique_sealed_destination_count = $destinationCache.Count
    excluded_file_count = $excludedBefore.Count
    partial_file_count = $partialFiles.Count
    intake_manifests = $manifestBindings
    destination_zip_verification = @($destinationCache.Values)
    targets = $verified
}
if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 32
    exit 0
}

$reportRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $workspaceRoot ([string]$cleanupRequest.report_root))
)
Assert-PathWithin -Path $reportRoot -Root (
    Join-Path $workspaceRoot "reports"
)
if (Test-Path -LiteralPath $reportRoot) {
    throw "Cleanup report root already exists: $reportRoot"
}
New-Item -ItemType Directory -Path $reportRoot | Out-Null
$receiptPath = Join-Path $reportRoot "CLEANUP_RECEIPT.json"
$receipt = [ordered]@{
    schema_version = 1
    status = "IN_PROGRESS"
    started_at = [DateTimeOffset]::Now.ToString("o")
    action = "SEND_TO_RECYCLE_BIN"
    recycle_bin_emptied = $false
    source_root = $sourceRoot
    target_count = $verified.Count
    target_bytes = $targetBytes
    moved_count = 0
    moved_bytes = [int64]0
    excluded_file_count = $excludedBefore.Count
    partial_file_count = $partialFiles.Count
    intake_manifests = $manifestBindings
    destination_zip_verification = @($destinationCache.Values)
    items = $verified
}
Write-JsonUtf8 -Path $receiptPath -Value $receipt

Add-Type -AssemblyName Microsoft.VisualBasic
try {
    foreach ($item in $verified) {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            [string]$item.source_path,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
        )
        if (Test-Path -LiteralPath $item.source_path) {
            throw "Source still exists after recycle operation: $($item.source_path)"
        }
        $item.recycle_status = "MOVED_TO_RECYCLE_BIN"
        $item | Add-Member -NotePropertyName recycled_at `
            -NotePropertyValue ([DateTimeOffset]::Now.ToString("o"))
        $receipt.moved_count = [int]$receipt.moved_count + 1
        $receipt.moved_bytes = [int64]$receipt.moved_bytes + [int64]$item.bytes
        Write-JsonUtf8 -Path $receiptPath -Value $receipt
    }
}
catch {
    $receipt.status = "PARTIAL_FAILURE"
    $receipt | Add-Member -NotePropertyName failure `
        -NotePropertyValue ([string]$_)
    $receipt | Add-Member -NotePropertyName failed_at `
        -NotePropertyValue ([DateTimeOffset]::Now.ToString("o"))
    Write-JsonUtf8 -Path $receiptPath -Value $receipt
    throw
}

$allAfter = @(
    Get-ChildItem -LiteralPath $sourceRoot -File -Force |
        Sort-Object FullName
)
$afterKeys = @{}
foreach ($item in $allAfter) {
    $afterKeys[$item.FullName.ToLowerInvariant()] = $true
}
$unexpectedMissing = @(
    $excludedBefore | Where-Object {
        -not $afterKeys.ContainsKey($_.FullName.ToLowerInvariant())
    }
)
$targetsStillPresent = @(
    $verified | Where-Object {
        Test-Path -LiteralPath $_.source_path
    }
)
if ($unexpectedMissing.Count -ne 0 -or $targetsStillPresent.Count -ne 0) {
    $receipt.status = "POST_VERIFY_FAILED"
    $receipt | Add-Member -NotePropertyName unexpected_missing_excluded `
        -NotePropertyValue @($unexpectedMissing.FullName)
    $receipt | Add-Member -NotePropertyName targets_still_present `
        -NotePropertyValue @($targetsStillPresent.source_path)
    Write-JsonUtf8 -Path $receiptPath -Value $receipt
    throw "Post-cleanup exact-scope verification failed"
}

$receipt.status = "COMPLETED"
$receipt | Add-Member -NotePropertyName completed_at `
    -NotePropertyValue ([DateTimeOffset]::Now.ToString("o"))
$receipt | Add-Member -NotePropertyName post_cleanup_target_absence `
    -NotePropertyValue "PASS"
$receipt | Add-Member -NotePropertyName excluded_files_preserved `
    -NotePropertyValue "PASS"
$receipt | Add-Member -NotePropertyName remaining_download_files `
    -NotePropertyValue $allAfter.Count
Write-JsonUtf8 -Path $receiptPath -Value $receipt

[ordered]@{
    status = "COMPLETED"
    receipt = $receiptPath
    moved_to_recycle_bin_count = $receipt.moved_count
    moved_to_recycle_bin_bytes = $receipt.moved_bytes
    excluded_files_preserved = $excludedBefore.Count
    partial_files_processed = 0
    recycle_bin_emptied = $false
    remaining_download_files = $allAfter.Count
} | ConvertTo-Json -Depth 8
