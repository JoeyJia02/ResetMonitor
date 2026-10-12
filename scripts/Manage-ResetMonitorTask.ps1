# Default is a read-only plan. No secrets are accepted by this script.
[CmdletBinding()]
param(
    [ValidateSet('Preview', 'Install', 'Uninstall')][string]$Action = 'Preview',
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = '',
    [string]$StatePath = '',
    [string]$LogPath = '',
    [string]$FixturePath = '',
    [ValidateSet('Offline', 'Live')][string]$Mode = 'Offline',
    [string]$AuthorizationPath = '',
    [string]$LiveConfigPath = '',
    [switch]$EnableLiveValidation
)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexResetMonitor'
$ownership = 'ResetMonitor managed task v2; offline only; fixed Shanghai schedule'
if ($Mode -eq 'Live') { $ownership = 'ResetMonitor managed task v3; finite Live candidate; fixed Shanghai schedule' }
$scheduleTimes = @('08:00', '13:00', '20:00')

function Get-ResetMonitorStartBoundaries([DateTimeOffset]$Now) {
    # An explicit offset makes Task Scheduler ignore the host time zone/DST.
    $chinaDate = $Now.ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
    foreach ($time in @('08:00', '13:00', '20:00')) {
        $chinaDate + 'T' + $time + ':00+08:00'
    }
}
$startBoundaries = @(Get-ResetMonitorStartBoundaries ([DateTimeOffset]::UtcNow))

function Get-SafeAbsolutePath([string]$Value) {
    if ($Value -notmatch '^[A-Za-z]:[\\/]' -or $Value.StartsWith('\\') -or
        $Value.Contains('"') -or $Value -match '[\x00-\x1f\x7f]') {
        throw 'An absolute local path without quotes/control characters is required.'
    }
    return [IO.Path]::GetFullPath($Value)
}
function Quote-Argument([string]$Value) {
    # Windows argv quoting: double trailing backslashes before the closing quote.
    return '"' + [regex]::Replace($Value, '(\\+)$', '$1$1') + '"'
}

$project = Get-SafeAbsolutePath $ProjectPath
$runner = Join-Path $project 'scripts\Run-ResetMonitor.ps1'
if (-not $StatePath) { $StatePath = Join-Path $project $(if ($Mode -eq 'Live') { 'runtime\scheduled-live-validation-state.json' } else { 'runtime\scheduled-offline-state.json' }) }
if (-not $LogPath) { $LogPath = Join-Path $project $(if ($Mode -eq 'Live') { 'runtime\scheduled-live-validation.log' } else { 'runtime\scheduled-offline.log' }) }
$state = Get-SafeAbsolutePath $StatePath
$log = Get-SafeAbsolutePath $LogPath
$python = if ($PythonPath) { Get-SafeAbsolutePath $PythonPath } else { $null }
$fixture = if ($FixturePath) { Get-SafeAbsolutePath $FixturePath } else { $null }
$authorization = if ($AuthorizationPath) { Get-SafeAbsolutePath $AuthorizationPath } else { $null }
$publicConfig = if ($LiveConfigPath) { Get-SafeAbsolutePath $LiveConfigPath } else { $null }
if ($state -eq $log -or ($fixture -and ($state -eq $fixture -or $log -eq $fixture))) {
    throw 'State, log, and fixture paths must be distinct.'
}
$blocked = @()
if ($Mode -eq 'Live') {
    # A finite validation approval is not a long-term installation approval.
    $blocked += 'Live task installation is disabled pending separate long-term approval and accepted production evidence.'
    if (-not $EnableLiveValidation) { $blocked += 'Live candidate execution requires explicit EnableLiveValidation.' }
    if ($fixture) { $blocked += 'Live mode cannot accept an offline fixture.' }
    if (-not $authorization -or -not (Test-Path -LiteralPath $authorization -PathType Leaf)) { $blocked += 'An existing public finite authorization is required.' }
    if (-not $publicConfig -or -not (Test-Path -LiteralPath $publicConfig -PathType Leaf)) { $blocked += 'An existing public Live configuration is required.' }
} elseif ($EnableLiveValidation -or $AuthorizationPath -or $LiveConfigPath) {
    $blocked += 'Live arguments require Live mode.'
}
if (-not $python -or -not (Test-Path -LiteralPath $python -PathType Leaf)) { $blocked += 'An explicit existing Python executable is required.' }
if ($Mode -eq 'Offline' -and (-not $fixture -or -not (Test-Path -LiteralPath $fixture -PathType Leaf))) { $blocked += 'An explicit synthetic offline fixture is required.' }
if (-not (Test-Path -LiteralPath $runner -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $project 'main.py') -PathType Leaf)) {
    $blocked += 'Project entry point and runner must exist.'
}
$powershell = Join-Path $PSHOME 'powershell.exe'
if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) { $powershell = Join-Path $PSHOME 'pwsh.exe' }
$arguments = @('-NoProfile', '-NonInteractive', '-File', (Quote-Argument $runner),
    '-ProjectPath', (Quote-Argument $project), '-StatePath', (Quote-Argument $state),
    '-LogPath', (Quote-Argument $log), '-Mode', $Mode)
if ($python) { $arguments += @('-PythonPath', (Quote-Argument $python)) }
if ($fixture) { $arguments += @('-FixturePath', (Quote-Argument $fixture)) }
if ($Mode -eq 'Live') {
    if ($EnableLiveValidation) { $arguments += '-EnableLiveValidation' }
    if ($authorization) { $arguments += @('-AuthorizationPath', (Quote-Argument $authorization)) }
    if ($publicConfig) { $arguments += @('-LiveConfigPath', (Quote-Argument $publicConfig)) }
}
$plan = [ordered]@{
    action = $Action; task_name = $taskName; mode = $Mode
    schedule_timezone = 'Asia/Shanghai'; schedule_times = $scheduleTimes; start_boundaries = $startBoundaries
    rounds_per_day = 3; fetch_limit = 3; max_items_per_day = 9; missed_runs = 'Skip'
    executable = $powershell; arguments = ($arguments -join ' '); working_directory = $project
    state_path = $state; log_path = $log; multiple_instances = 'IgnoreNew'; start_when_available = $false
    run_level = 'Limited'; logon_type = 'Interactive'
    can_install = ($blocked.Count -eq 0); blocked_reasons = @($blocked)
    credentials_in_task = $false; mutation = $false
    public_preflight_required = ($Mode -eq 'Live'); credential_source = $(if ($Mode -eq 'Live') { 'stored' } else { $null })
    finite_validation_only = ($Mode -eq 'Live'); automatic_ledger_initialization = $false
}
if ($Action -eq 'Preview') { $plan | ConvertTo-Json -Depth 4; return }
if ($Action -eq 'Install' -and $blocked.Count) { throw ('Install blocked: ' + ($blocked -join ' ')) }

# These branches require an explicit human invocation; automated QA uses Preview.
Import-Module ScheduledTasks -ErrorAction Stop
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($Action -eq 'Uninstall') {
    if ($existing) {
        if ($existing.Description -ne $ownership -or $existing.Actions.Count -ne 1 -or
            $existing.Actions[0].Execute -ne $powershell -or
            $existing.Actions[0].WorkingDirectory -ne $project -or
            $existing.Actions[0].Arguments -ne ($arguments -join ' ')) {
            throw 'Refusing to remove a task without this project ownership marker and runner.'
        }
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
    # Never delete state, logs, fixtures, or project files.
    $plan.mutation = [bool]$existing
    $plan | ConvertTo-Json -Depth 4
    return
}
if ($blocked.Count) { throw ('Install blocked: ' + ($blocked -join ' ')) }
if ($existing) { throw 'Task already exists; it will not be overwritten.' }
$taskAction = New-ScheduledTaskAction -Execute $powershell -Argument ($arguments -join ' ') -WorkingDirectory $project
$triggers = @(foreach ($boundary in $startBoundaries) {
    $wallTime = [DateTime]::ParseExact($boundary.Substring(0, 19), 'yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
    $trigger = New-ScheduledTaskTrigger -Daily -DaysInterval 1 -At $wallTime
    $trigger.StartBoundary = $boundary
    $trigger
})
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable:$false -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10))
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $taskAction -Trigger $triggers -Settings $settings -Principal $principal -Description $ownership -ErrorAction Stop | Out-Null
$plan.mutation = $true
$plan | ConvertTo-Json -Depth 4
