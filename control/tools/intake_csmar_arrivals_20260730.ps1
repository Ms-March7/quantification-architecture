[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$WorkspaceRoot = "",
    [string]$DownloadsRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

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
    (Join-Path $workspaceRoot "data\market\quarantine\csmar-manual-export")
)
$runId = "intake-20260730-08pkg-03manual-v1"
$finalRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent $runId)
)
$stagingRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $quarantineParent (".staging-" + $runId))
)

$expectedSources = @(
    [ordered]@{
        filename = "现金流量表(间接法)223059026(仅供清华大学使用).zip"
        kind = "package"
        bytes = 27157268
        sha256 = "d5974d11309397e5f4dd430849ddf5e90b75728a6ba3631bc4a70731c4382548"
    },
    [ordered]@{
        filename = "特殊处理变动文件010715522(仅供清华大学使用).zip"
        kind = "package"
        bytes = 1543743
        sha256 = "c8b1a6b657a011a5926b825a1991f4c87657e72912ff8c88dd40e31acb29009c"
    },
    [ordered]@{
        filename = "公司基本信息表010812543(仅供清华大学使用).zip"
        kind = "package"
        bytes = 1460932
        sha256 = "218a65a8d2e6426de71911c509333611333e2abfba82afc6249c80d0cfab110e"
    },
    [ordered]@{
        filename = "指数基本信息文件011942405(仅供清华大学使用).zip"
        kind = "package"
        bytes = 760073
        sha256 = "5d678e2e4d7213d9c072c88e61f0e32c921616b2f398e120f3bcaa270705a1b9"
    },
    [ordered]@{
        filename = "指数取样变更文件094857265(仅供清华大学使用).zip"
        kind = "package"
        bytes = 3181332
        sha256 = "a92786e597918636e6fb5390462fcd1fb3be7612c7bda8fe667756d2db682847"
    },
    [ordered]@{
        filename = "指数取样变更文件014214039(仅供清华大学使用).zip"
        kind = "package"
        bytes = 74890824
        sha256 = "1b17a12f5b770daa20773459246d039a2a7a963dc3cb86135fa35f232efede4e"
    },
    [ordered]@{
        filename = "指数取样变更文件013710529(仅供清华大学使用).zip"
        kind = "package"
        bytes = 34824140
        sha256 = "1ae12f58a0c685816f146ad65a3ea7b95ac515a85b51ea4679417cbf61a4dd99"
    },
    [ordered]@{
        filename = "指数取样变更文件013710529(仅供清华大学使用) (1).zip"
        kind = "package"
        bytes = 34824140
        sha256 = "1ae12f58a0c685816f146ad65a3ea7b95ac515a85b51ea4679417cbf61a4dd99"
    },
    [ordered]@{
        filename = "CSMAR_TRD_中国股票市场交易数据库_数据库说明书_20250616.pdf"
        kind = "documentation"
        bytes = 2003246
        sha256 = "2bf1ce56aabbad96a0c0e0ce0d785fb1434f6a80029a4422ede244c2a2bc526c"
    },
    [ordered]@{
        filename = "CSMAR_TRD_中国股票市场交易数据库_数据库说明书_20250616_字节相同副本01.pdf"
        kind = "documentation"
        bytes = 2003246
        sha256 = "2bf1ce56aabbad96a0c0e0ce0d785fb1434f6a80029a4422ede244c2a2bc526c"
    },
    [ordered]@{
        filename = "CSMAR_TRD_中国股票市场交易数据库_数据库说明书_20250616_字节相同副本02.pdf"
        kind = "documentation"
        bytes = 2003246
        sha256 = "2bf1ce56aabbad96a0c0e0ce0d785fb1434f6a80029a4422ede244c2a2bc526c"
    },
    [ordered]@{
        filename = "CSMAR_TRD_中国股票市场交易数据库_数据库说明书_20250616_字节相同副本03.pdf"
        kind = "documentation"
        bytes = 2003246
        sha256 = "2bf1ce56aabbad96a0c0e0ce0d785fb1434f6a80029a4422ede244c2a2bc526c"
    },
    [ordered]@{
        filename = "CSMAR_TRD_中国股票市场交易数据库_数据库说明书_20250616_字节相同副本04.pdf"
        kind = "documentation"
        bytes = 2003246
        sha256 = "2bf1ce56aabbad96a0c0e0ce0d785fb1434f6a80029a4422ede244c2a2bc526c"
    },
    [ordered]@{
        filename = "CSMAR_TRDNEW_中国股票市场交易数据库_升级版_数据库说明书_20250625.pdf"
        kind = "documentation"
        bytes = 2036280
        sha256 = "641117cf97ce4bdf47fa34a892d513a38ddbf75fd24c149ed7d3dd5f8be4fd62"
    },
    [ordered]@{
        filename = "CSMAR_TRDNEW_中国股票市场交易数据库_升级版_数据库说明书_20250625_字节相同副本01.pdf"
        kind = "documentation"
        bytes = 2036280
        sha256 = "641117cf97ce4bdf47fa34a892d513a38ddbf75fd24c149ed7d3dd5f8be4fd62"
    },
    [ordered]@{
        filename = "CSMAR_BDT_中国上市公司经营困境研究数据库_数据库说明书_20260618.pdf"
        kind = "documentation"
        bytes = 1913866
        sha256 = "d98207bc871f969efe13f92c65f8cd48a5a4a105dbf7054b56171044fcbff4dc"
    },
    [ordered]@{
        filename = "CSMAR_BDT_中国上市公司经营困境研究数据库_数据库说明书_20260618_字节相同副本01.pdf"
        kind = "documentation"
        bytes = 1913866
        sha256 = "d98207bc871f969efe13f92c65f8cd48a5a4a105dbf7054b56171044fcbff4dc"
    }
)

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

function Write-JsonUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $json = $Value | ConvertTo-Json -Depth 16
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
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
        status = "PASS"
        entry_count = $entryCount
        unpacked_bytes = $unpackedBytes
    }
}

Assert-PathWithin -Path $quarantineParent -Root $workspaceRoot
Assert-PathWithin -Path $finalRoot -Root $quarantineParent
Assert-PathWithin -Path $stagingRoot -Root $quarantineParent

$observed = @()
foreach ($expected in $expectedSources) {
    $sourcePath = [System.IO.Path]::GetFullPath(
        (Join-Path $sourceRoot ([string]$expected.filename))
    )
    Assert-PathWithin -Path $sourcePath -Root $sourceRoot
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Missing reviewed source: $sourcePath"
    }

    $item = Get-Item -LiteralPath $sourcePath
    $sha256 = (
        Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($item.Length -ne [int64]$expected.bytes) {
        throw "Source byte length changed: $sourcePath"
    }
    if ($sha256 -ne [string]$expected.sha256) {
        throw "Source SHA-256 changed: $sourcePath"
    }

    $observed += [pscustomobject][ordered]@{
        filename = $item.Name
        source_path = $sourcePath
        source_drive = $item.PSDrive.Name
        kind = [string]$expected.kind
        bytes = [int64]$item.Length
        sha256 = $sha256
    }
}

$groups = @(
    $observed |
        Group-Object sha256 |
        Sort-Object Name
)
$uniquePlans = @()
foreach ($group in $groups) {
    $items = @($group.Group | Sort-Object filename)
    $kinds = @($items.kind | Sort-Object -Unique)
    if ($kinds.Count -ne 1) {
        throw "One content hash was classified as multiple kinds: $($group.Name)"
    }

    $preferred = @(
        $items |
            Where-Object {
                $_.filename -notmatch "字节相同副本" -and
                $_.filename -notmatch " [(][0-9]+[)][.]zip$"
            }
    )
    if ($preferred.Count -eq 0) {
        $canonical = $items[0]
    }
    else {
        $canonical = $preferred[0]
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
    }
}

$plan = [ordered]@{
    schema_version = 1
    mode = "COPY_VERIFY_DEDUP_SEAL_NO_SOURCE_DELETE"
    run_id = $runId
    source_root = $sourceRoot
    destination = $finalRoot
    source_file_count = $observed.Count
    source_total_bytes = [int64]((
        $observed |
            Measure-Object bytes -Sum
    ).Sum)
    unique_blob_count = $uniquePlans.Count
    unique_storage_bytes = [int64]((
        $uniquePlans |
            Measure-Object bytes -Sum
    ).Sum)
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
    catalog_write = $false
    source_delete = $false
    unique_blobs = $uniquePlans
}

if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 16
    exit 0
}

if (Test-Path -LiteralPath $finalRoot) {
    throw "Final intake already exists: $finalRoot"
}
if (Test-Path -LiteralPath $stagingRoot) {
    throw "Staging intake already exists: $stagingRoot"
}

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
    $destinationHash = (
        Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($destinationItem.Length -ne [int64]$blob.bytes) {
        throw "Copied byte length mismatch: $destinationPath"
    }
    if ($destinationHash -ne [string]$blob.sha256) {
        throw "Copied SHA-256 mismatch: $destinationPath"
    }

    $zipEvidence = $null
    if ($blob.kind -eq "package") {
        $zipEvidence = Test-ZipPayload -Path $destinationPath
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
        zip_crc_read_status = if ($null -eq $zipEvidence) {
            "NOT_APPLICABLE"
        }
        else {
            [string]$zipEvidence.status
        }
        archive_entry_count = if ($null -eq $zipEvidence) {
            $null
        }
        else {
            [int]$zipEvidence.entry_count
        }
        unpacked_bytes = if ($null -eq $zipEvidence) {
            $null
        }
        else {
            [int64]$zipEvidence.unpacked_bytes
        }
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
    source_total_bytes = [int64]((
        $observed |
            Measure-Object bytes -Sum
    ).Sum)
    unique_blob_count = $copied.Count
    unique_storage_bytes = [int64]((
        $copied |
            Measure-Object bytes -Sum
    ).Sum)
    hash_algorithm = "sha256"
    source_files = $observed
    unique_blobs = $copied
}
Write-JsonUtf8 -Path (
    Join-Path $stagingRoot "INTAKE_MANIFEST.json"
) -Value $manifest

$sourceRetention = [ordered]@{
    schema_version = 1
    run_id = $runId
    completed_at = [DateTimeOffset]::Now.ToString("o")
    action = "NO_SOURCE_DELETE"
    reason = "Sources remain in Downloads pending separate exact deletion approval."
    retained_source_count = $observed.Count
    retained_source_bytes = [int64]((
        $observed |
            Measure-Object bytes -Sum
    ).Sum)
    all_sources_still_exist = $true
    items = @(
        $observed |
            ForEach-Object {
                [ordered]@{
                    source_path = $_.source_path
                    filename = $_.filename
                    bytes = [int64]$_.bytes
                    sha256 = $_.sha256
                    retained = $true
                }
            }
    )
}
Write-JsonUtf8 -Path (
    Join-Path $stagingRoot "SOURCE_RETENTION_RECEIPT.json"
) -Value $sourceRetention

Move-Item -LiteralPath $stagingRoot -Destination $finalRoot
if (-not (Test-Path -LiteralPath $finalRoot -PathType Container)) {
    throw "Final same-volume seal failed: $finalRoot"
}

$postSealFailures = @()
foreach ($blob in $copied) {
    $sealedPath = Join-Path $finalRoot $blob.destination_relative_path
    if (-not (Test-Path -LiteralPath $sealedPath -PathType Leaf)) {
        $postSealFailures += "missing:$sealedPath"
        continue
    }
    $sealedHash = (
        Get-FileHash -LiteralPath $sealedPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($sealedHash -ne [string]$blob.sha256) {
        $postSealFailures += "hash:$sealedPath"
    }
}
foreach ($source in $observed) {
    if (-not (Test-Path -LiteralPath $source.source_path -PathType Leaf)) {
        $postSealFailures += "source_missing:$($source.source_path)"
    }
}
if ($postSealFailures.Count -ne 0) {
    throw "Post-seal verification failed: $($postSealFailures -join ', ')"
}

[ordered]@{
    status = "COMPLETED"
    run_id = $runId
    final_root = $finalRoot
    source_file_count = $observed.Count
    source_total_bytes = [int64]((
        $observed |
            Measure-Object bytes -Sum
    ).Sum)
    unique_blob_count = $copied.Count
    unique_storage_bytes = [int64]((
        $copied |
            Measure-Object bytes -Sum
    ).Sum)
    post_seal_hash_verification = "PASS"
    zip_crc_read_status = "PASS"
    source_delete = $false
    all_sources_retained = $true
    purpose_ceiling = "INSPECTION_ONLY"
    admission_decision = "NOT_ADMITTED"
} | ConvertTo-Json -Depth 6
