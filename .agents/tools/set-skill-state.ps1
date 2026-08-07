[CmdletBinding()]
param(
    [string]$Name,
    [ValidateSet("Active", "NeedsUpgrade", "PendingReplacement", "Retired")]
    [string]$State,
    [switch]$Apply,
    [switch]$List
)

$ErrorActionPreference = "Stop"

$agentsRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ((Split-Path -Leaf $agentsRoot) -ne ".agents") {
    throw "This tool must run from the workspace .agents\tools directory."
}

$stateRoots = [ordered]@{
    Active             = Join-Path $agentsRoot "skills"
    NeedsUpgrade       = Join-Path $agentsRoot "skills-disabled\needs-upgrade"
    PendingReplacement = Join-Path $agentsRoot "skills-disabled\pending-replacement"
    Retired            = Join-Path $agentsRoot "skills-disabled\retired"
}

function Assert-SafePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $agentsRoot.TrimEnd("\") + "\"
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside .agents: $fullPath"
    }
    return $fullPath
}

function Get-SkillLocations {
    param([Parameter(Mandatory = $true)][string]$SkillName)

    $locations = @()
    foreach ($entry in $stateRoots.GetEnumerator()) {
        $candidate = Assert-SafePath (Join-Path $entry.Value $SkillName)
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $locations += [pscustomobject]@{
                State = $entry.Key
                Path  = $candidate
            }
        }
    }
    return $locations
}

if ($List -or [string]::IsNullOrWhiteSpace($Name)) {
    $rows = @()
    foreach ($entry in $stateRoots.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $entry.Value -PathType Container)) {
            continue
        }
        foreach ($directory in Get-ChildItem -LiteralPath $entry.Value -Directory -Force) {
            $rows += [pscustomobject]@{
                Name  = $directory.Name
                State = $entry.Key
                Path  = $directory.FullName
            }
        }
    }
    $rows | Sort-Object State, Name | Format-Table -AutoSize
    if ($List) {
        return
    }
    throw "Provide -Name and -State, or use -List."
}

if ($Name -notmatch "^[a-z0-9]+(?:-[a-z0-9]+)*$") {
    throw "Skill name must use lowercase hyphen-case."
}
if ([string]::IsNullOrWhiteSpace($State)) {
    throw "-State is required when changing a skill state."
}

$locations = @(Get-SkillLocations -SkillName $Name)
if ($locations.Count -eq 0) {
    throw "Skill not found in any managed state: $Name"
}
if ($locations.Count -gt 1) {
    throw "Skill exists in multiple states; resolve manually before moving: $Name"
}

$source = $locations[0]
$destinationRoot = Assert-SafePath $stateRoots[$State]
$destination = Assert-SafePath (Join-Path $destinationRoot $Name)

if ($source.State -eq $State) {
    Write-Output "$Name is already in state $State at $($source.Path)"
    return
}
if (Test-Path -LiteralPath $destination) {
    throw "Destination already exists: $destination"
}

Write-Output "Skill: $Name"
Write-Output "Current state: $($source.State)"
Write-Output "Requested state: $State"
Write-Output "Source: $($source.Path)"
Write-Output "Destination: $destination"

if (-not $Apply) {
    Write-Output "Dry-run only. Re-run with -Apply to move the skill."
    return
}

New-Item -ItemType Directory -Force -Path $destinationRoot | Out-Null
Move-Item -LiteralPath $source.Path -Destination $destination
Write-Output "Moved successfully. Restart or open a new Codex task to refresh discovered skills."
