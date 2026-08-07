[CmdletBinding()]
param(
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$taskName = 'Quantification-Candidate-Daily-Maintenance'
$legacyTasks = @(
    'Quantification-RQData-SHSZ-Daily',
    'Quantification-RQData-B-Path-Daily'
)
$entrypoint = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot 'run_candidate_daily_maintenance.cmd')
)

function Get-TaskSummary {
    param([Parameter(Mandatory = $true)][string]$Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [ordered]@{ task_name = $Name; exists = $false }
    }
    $info = Get-ScheduledTaskInfo -TaskName $Name
    return [ordered]@{
        task_name = $Name
        exists = $true
        state = [string]$task.State
        enabled = [bool]$task.Settings.Enabled
        execute = [string]$task.Actions[0].Execute
        arguments = [string]$task.Actions[0].Arguments
        start_boundary = [string]$task.Triggers[0].StartBoundary
        user_id = [string]$task.Principal.UserId
        logon_type = [string]$task.Principal.LogonType
        run_level = [string]$task.Principal.RunLevel
        last_run_time = $info.LastRunTime.ToString('o')
        last_task_result = [int64]$info.LastTaskResult
        next_run_time = $info.NextRunTime.ToString('o')
    }
}

$before = @(
    Get-TaskSummary -Name $taskName
    foreach ($name in $legacyTasks) { Get-TaskSummary -Name $name }
)

if (-not $Install) {
    [ordered]@{
        status = 'PLAN__NO_TASK_CHANGE'
        task_name = $taskName
        entrypoint = $entrypoint
        schedule = 'WEEKDAYS_19_20_ASIA_SHANGHAI'
        legacy_tasks_to_disable_not_delete = $legacyTasks
        before = $before
        credential_value_read = $false
        credential_value_captured = $false
    } | ConvertTo-Json -Depth 8
    exit 0
}

if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw "Unified maintenance entrypoint is missing: $entrypoint"
}

$action = New-ScheduledTaskAction -Execute $entrypoint -Argument '--run'
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '19:20'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Candidate-only maintenance: SHSZ PV, B path, central candidate index. No formal promotion.' `
    -Force | Out-Null

foreach ($name in $legacyTasks) {
    $legacy = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -ne $legacy) {
        Disable-ScheduledTask -TaskName $name | Out-Null
    }
}

$after = @(
    Get-TaskSummary -Name $taskName
    foreach ($name in $legacyTasks) { Get-TaskSummary -Name $name }
)

[ordered]@{
    status = 'PASS__UNIFIED_TASK_REGISTERED__LEGACY_TASKS_DISABLED_NOT_DELETED'
    task_name = $taskName
    entrypoint = $entrypoint
    schedule = 'WEEKDAYS_19_20_ASIA_SHANGHAI'
    before = $before
    after = $after
    rollback = [ordered]@{
        disable_unified = "Disable-ScheduledTask -TaskName '$taskName'"
        enable_legacy = @(
            foreach ($name in $legacyTasks) { "Enable-ScheduledTask -TaskName '$name'" }
        )
    }
    formal_catalog_write = $false
    formal_snapshot_write = $false
    quality_promotion = $false
    factor_runs = 0
    credential_value_read = $false
    credential_value_captured = $false
} | ConvertTo-Json -Depth 8
