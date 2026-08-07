# Quantification Skill States

Updated: 2026-07-19

Only folders directly under `${QUANTIFICATION_ROOT}\.agents\skills` are active and
eligible for automatic discovery. Inventory labels alone do not enable or
disable a skill.

## Runtime states

| State | Physical location | Runtime behavior |
| --- | --- | --- |
| `Active` | `.agents\skills\<skill>` | Discoverable in a new Codex task |
| `NeedsUpgrade` | `.agents\skills-disabled\needs-upgrade\<skill>` | Disabled; preserve for redesign and testing |
| `PendingReplacement` | `.agents\skills-disabled\pending-replacement\<skill>` | Disabled; preserve until replacement coverage is verified |
| `Retired` | `.agents\skills-disabled\retired\<skill>` | Disabled; retained only for recovery or historical reference |

## Current decisions

| Skill | State | Reason |
| --- | --- | --- |
| `a-share-capital-flow-monitor` | `PendingReplacement` | The newer capital-flow attribution project is more rigorous, but does not yet fully cover sector/theme ranking, lightweight watchlists, and every V0 daily-monitoring behavior. |
| `a-share-risk-warning-event-study` | `NeedsUpgrade` | Current implementation is not approved for automatic use; source coverage, matching design, and statistical validation require an upgrade. |
| `stock-relative-dominance` | `NeedsUpgrade` | A narrow exploratory tool without validated predictive value; keep disabled until its purpose and methodology are upgraded. |

## Switch commands

List states:

```powershell
& C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ${QUANTIFICATION_ROOT}\.agents\tools\set-skill-state.ps1 -List
```

Preview a change:

```powershell
& C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ${QUANTIFICATION_ROOT}\.agents\tools\set-skill-state.ps1 -Name stock-relative-dominance -State Active
```

Apply a reviewed change:

```powershell
& C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ${QUANTIFICATION_ROOT}\.agents\tools\set-skill-state.ps1 -Name stock-relative-dominance -State Active -Apply
```

Before re-enabling a skill, validate its folder, run its declared offline
self-tests, restore or update its Harness target and cases, and synchronize the
control-plane inventory. The current Codex task may retain a cached skill list;
runtime discovery should be checked in a new task after a state change.
