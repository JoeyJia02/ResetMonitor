# Manage the per-user, hidden logon task for the long-running monitor.
[CmdletBinding()]
param(
    [ValidateSet('Preview', 'Install', 'Status', 'Uninstall')][string]$Action = 'Preview',
    [string]$PythonPath = ''
)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexResetMonitorBackground'
$ownership = 'ResetMonitor owned background v1; user logon; Asia/Shanghai 08:00,13:00,20:00'
$project = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$source = Join-Path $project 'scripts\background_monitor.py'
if (-not $PythonPath) {
    $pythonCommand = Get-Command pythonw.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if (-not $pythonCommand) { throw 'Python is missing. Supply -PythonPath with the full path to pythonw.exe.' }
    $PythonPath = Join-Path (Split-Path -Parent $pythonCommand.Source) 'pythonw.exe'
}
$python = [IO.Path]::GetFullPath($PythonPath)
if ([IO.Path]::GetFileName($python) -ne 'pythonw.exe') { throw 'PythonPath must point to pythonw.exe.' }
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$accountSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = '-X utf8 -B "' + $source + '" --serve'
function Test-TaskPrincipal {
    param([string]$UserId, [string]$ExpectedSid)
    if (-not $UserId) { return $false }
    if ($UserId -match '^S-\d+(?:-\d+)+$') { return $UserId -eq $ExpectedSid }
    try {
        $identity = New-Object Security.Principal.NTAccount($UserId)
        return $identity.Translate([Security.Principal.SecurityIdentifier]).Value -eq $ExpectedSid
    } catch {
        return $false
    }
}
$plan = [ordered]@{
    action = $Action; task_name = $taskName; project = $project
    executable = $python; arguments = $arguments; account = $account
    trigger = 'AtLogOn'; timezone = 'Asia/Shanghai'; times = @('08:00','13:00','20:00')
    fetch_limit = 3; hidden = $true; credentials_in_task = $false
    registered = $false; running = $false; mutation = $false
}
if ($Action -eq 'Preview') {
    $plan.ready = (Test-Path -LiteralPath $python -PathType Leaf) -and
                  (Test-Path -LiteralPath $source -PathType Leaf)
    $plan | ConvertTo-Json -Depth 3
    return
}
Import-Module ScheduledTasks -ErrorAction Stop
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($Action -eq 'Status') {
    if ($existing) {
        $plan.registered = $true
        $plan.running = ($existing.State -eq 'Running')
        $plan.owned = ($existing.Description -eq $ownership -and
            $existing.Actions.Count -eq 1 -and
            $existing.Actions[0].Execute -eq $python -and
            $existing.Actions[0].Arguments -eq $arguments -and
            $existing.Actions[0].WorkingDirectory -eq $project -and
            (Test-TaskPrincipal -UserId $existing.Principal.UserId -ExpectedSid $accountSid))
    }
    $plan | ConvertTo-Json -Depth 3
    return
}
if ($existing) {
    if ($existing.Description -ne $ownership -or $existing.Actions.Count -ne 1 -or
        $existing.Actions[0].Execute -ne $python -or
        $existing.Actions[0].Arguments -ne $arguments -or
        $existing.Actions[0].WorkingDirectory -ne $project -or
        -not (Test-TaskPrincipal -UserId $existing.Principal.UserId -ExpectedSid $accountSid)) {
        throw 'Refusing to modify a task without this project ownership marker and action.'
    }
}
if ($Action -eq 'Uninstall') {
    if ($existing) {
        if ($existing.State -eq 'Running') {
            Stop-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
        }
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
        $plan.mutation = $true
    }
    $plan | ConvertTo-Json -Depth 3
    return
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw 'Pythonw or background monitor entry point is missing.'
}
if (-not $existing) {
    $taskAction = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $project
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
    $settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
        -StartWhenAvailable:$false -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $taskAction `
        -Trigger $trigger -Settings $settings -Principal $principal `
        -Description $ownership -ErrorAction Stop | Out-Null
    $plan.mutation = $true
}
$current = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
if ($current.State -ne 'Running') {
    Start-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
}
$current = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
$plan.registered = $true
$plan.running = ($current.State -eq 'Running')
$plan | ConvertTo-Json -Depth 3
