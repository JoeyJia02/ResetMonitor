# Default offline runner; finite Live candidate remains explicitly opt-in.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$StatePath,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [string]$FixturePath = '',
    [ValidateSet('Offline', 'Live')][string]$Mode = 'Offline',
    [string]$AuthorizationPath = '',
    [string]$LiveConfigPath = '',
    [switch]$EnableLiveValidation,
    [switch]$PreflightOnly
)
$ErrorActionPreference = 'Stop'
function Get-SafeAbsolutePath([string]$Value) {
    if ($Value -notmatch '^[A-Za-z]:[\\/]' -or $Value.StartsWith('\\') -or $Value.Contains('"') -or $Value -match '[\x00-\x1f\x7f]') {
        throw 'An absolute local path without quotes/control characters is required.'
    }
    return [IO.Path]::GetFullPath($Value)
}
function Quote-Argument([string]$Value) {
    return '"' + [regex]::Replace($Value, '(\\+)$', '$1$1') + '"'
}

function Invoke-LivePython([string]$Python, [string]$Project, [string[]]$CliArguments) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $info.Arguments = (($CliArguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
    $info.WorkingDirectory = $Project
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -match '^(RESET_MONITOR_|TWITTER_|APIFY_|DEEPSEEK_|SMTP_|EMAIL_|OPENAI_|ANTHROPIC_|AWS_|AZURE_)|(_API_KEY|_TOKEN|_PASSWORD|_SECRET)$' -or
            $name -in @('TARGET_USERNAME','TARGET_USER_ID','INCLUDE_REPLIES','EXCLUDE_RETWEETS','FETCH_LIMIT','CHECK_INTERVAL_HOURS','SCHEDULE_TIMEZONE','SCHEDULE_TIMES','STATE_FILE')) {
            $info.EnvironmentVariables.Remove($name)
        }
    }
    $info.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $info.EnvironmentVariables['SCHEDULE_TIMEZONE'] = 'Asia/Shanghai'
    $info.EnvironmentVariables['SCHEDULE_TIMES'] = '08:00,13:00,20:00'
    $info.EnvironmentVariables['FETCH_LIMIT'] = '3'
    $child = New-Object Diagnostics.Process
    $child.StartInfo = $info
    try {
        [void]$child.Start()
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        $child.WaitForExit()
        # Never return child stderr or raw provider/credential exception text.
        return [pscustomobject]@{ code=$child.ExitCode; output=$stdout.Result }
    } finally { $child.Dispose() }
}

function Get-SafeLiveOutput([string]$Text) {
    if ($Text.Length -gt 16384) { throw 'Invalid public result.' }
    $raw = $Text | ConvertFrom-Json
    if ($raw.mode -ne 'LIVE_VALIDATION' -or $raw.status -notin @('PUBLIC_PREFLIGHT_OK','OK','DRY_RUN','LOCKED','DEGRADED','RECOVERY_REQUIRED','REFUSED')) {
        throw 'Invalid public result.'
    }
    # A fixed status and numeric counters are sufficient for the task log.
    # Do not reflect arbitrary JSON strings or error bodies into logs.
    $safe = [ordered]@{ mode='LIVE_VALIDATION'; status=$raw.status }
    if ($raw.status -eq 'REFUSED') { $safe.issue_codes=@('LIVE_RUNNER_PUBLIC_PREFLIGHT_REFUSED') }
    foreach ($name in @('ingested','classified','sent')) {
        $value = $raw.$name
        if ($null -ne $value) {
            if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 0 -or $value -gt 1000000) { throw 'Invalid public result.' }
            $safe[$name]=$value
        }
    }
    return $safe
}

try {
    if ($Mode -eq 'Live') {
        if (-not $EnableLiveValidation -or $FixturePath) { throw 'Explicit finite Live opt-in and no fixture are required.' }
        $liveProject = Get-SafeAbsolutePath $ProjectPath
        $livePython = Get-SafeAbsolutePath $PythonPath
        $liveState = Get-SafeAbsolutePath $StatePath
        $liveLog = Get-SafeAbsolutePath $LogPath
        $authorization = Get-SafeAbsolutePath $AuthorizationPath
        $publicConfig = Get-SafeAbsolutePath $LiveConfigPath
        $liveMain = Join-Path $liveProject 'main.py'
        $paths = @($liveState,$liveLog,$authorization,$publicConfig)
        if (@($paths | Select-Object -Unique).Count -ne $paths.Count) { throw 'Paths must be distinct.' }
        foreach ($path in @($livePython,$liveMain,$authorization,$publicConfig)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Required public file is missing.' }
        }
        if ((Get-Item -LiteralPath $authorization).Length -gt 65536 -or (Get-Item -LiteralPath $publicConfig).Length -gt 16384) { throw 'Public file is too large.' }
        $publicAuthorization = [IO.File]::ReadAllText($authorization) | ConvertFrom-Json
        if ((Get-SafeAbsolutePath $publicAuthorization.state_path) -ne $liveState) { throw 'Authorized state path mismatch.' }
        $baseArgs = @('-B',$liveMain,'--live','--scheduled-validation','--authorization',$authorization,'--live-config',$publicConfig)
        $preflight = Invoke-LivePython $livePython $liveProject ($baseArgs + @('--dry-run'))
        $safePreflight = Get-SafeLiveOutput $preflight.output
        if ($preflight.code -ne 0 -or $safePreflight.status -ne 'PUBLIC_PREFLIGHT_OK') {
            $safePreflight | ConvertTo-Json -Compress -Depth 3
            exit 2
        }
        if ($PreflightOnly) {
            $safePreflight | ConvertTo-Json -Compress -Depth 3
            exit 0
        }
        # The same CLI repeats all public gates and validates the finite ledger
        # before reading the current Windows user's project-only saved record.
        $liveChild = Invoke-LivePython $livePython $liveProject ($baseArgs + @('--credential-source','stored'))
        $safeLive = Get-SafeLiveOutput $liveChild.output
        $publicOutput = $safeLive | ConvertTo-Json -Compress -Depth 3
        [void][IO.Directory]::CreateDirectory((Split-Path -Parent $liveLog))
        [IO.File]::AppendAllText($liveLog, ([DateTime]::UtcNow.ToString('o') + ' exit=' + $liveChild.code + [Environment]::NewLine + $publicOutput + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        [Console]::Out.WriteLine($publicOutput)
        exit $liveChild.code
    }
    if ($EnableLiveValidation -or $PreflightOnly -or $AuthorizationPath -or $LiveConfigPath) { throw 'Live-only arguments require Live mode.' }
    $project = Get-SafeAbsolutePath $ProjectPath
    $python = Get-SafeAbsolutePath $PythonPath
    $state = Get-SafeAbsolutePath $StatePath
    $log = Get-SafeAbsolutePath $LogPath
    $fixture = Get-SafeAbsolutePath $FixturePath
    $main = Join-Path $project 'main.py'
    if ($state -eq $log -or $state -eq $fixture -or $log -eq $fixture) { throw 'Output paths must be distinct.' }
    foreach ($filePath in @($python, $fixture, $main)) {
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) { throw 'Required offline file is missing.' }
    }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = @('-B', (Quote-Argument $main), '--fixture', (Quote-Argument $fixture), '--state-file', (Quote-Argument $state)) -join ' '
    $info.WorkingDirectory = $project
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    # Remove credentials by name without inspecting or printing their values.
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -match '^(RESET_MONITOR_|TWITTER_|APIFY_|DEEPSEEK_|SMTP_|EMAIL_|OPENAI_|ANTHROPIC_|AWS_|AZURE_)|(_API_KEY|_TOKEN|_PASSWORD|_SECRET)$' -or
            $name -in @('TARGET_USERNAME', 'TARGET_USER_ID', 'INCLUDE_REPLIES', 'EXCLUDE_RETWEETS', 'FETCH_LIMIT', 'CHECK_INTERVAL_HOURS', 'SCHEDULE_TIMEZONE', 'SCHEDULE_TIMES', 'STATE_FILE')) {
            $info.EnvironmentVariables.Remove($name)
        }
    }
    $info.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $info.EnvironmentVariables['SCHEDULE_TIMEZONE'] = 'Asia/Shanghai'
    $info.EnvironmentVariables['SCHEDULE_TIMES'] = '08:00,13:00,20:00'
    $info.EnvironmentVariables['FETCH_LIMIT'] = '3'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    # CLI output contains fixed metadata/errors and no provider exception text.
    $output = $stdoutTask.Result
    $errorOutput = $stderrTask.Result
    $logDirectory = Split-Path -Parent $log
    [void][IO.Directory]::CreateDirectory($logDirectory)
    [IO.File]::AppendAllText($log, (([DateTime]::UtcNow.ToString('o')) + ' exit=' + $exitCode + [Environment]::NewLine + $output + $errorOutput), (New-Object Text.UTF8Encoding($false)))
    [Console]::Out.Write($output)
    $process.Dispose()
    exit $exitCode
} catch {
    # No raw exception, command line, environment values, or account data.
    [Console]::Error.WriteLine($(if ($Mode -eq 'Live') { 'LIVE_RUNNER_ERROR' } else { 'OFFLINE_RUNNER_ERROR' }))
    exit 2
}
