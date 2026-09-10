param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [string]$Pythonw = "$env:LOCALAPPDATA\Programs\Python\Python310\pythonw.exe"
)
$ErrorActionPreference = 'Stop'
$RepoDir = [System.IO.Path]::GetFullPath($RepoDir)
$wrapper = Join-Path $RepoDir 'scripts\Run-LocalScrapeRecoveryHidden.pyw'
$controller = Join-Path $RepoDir 'scripts\recover_missing_daily_sources.py'
foreach ($requiredFile in @($Pythonw, $wrapper, $controller)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file is missing: $requiredFile"
    }
}
$taskPath = '\Codex\pinefield\'
$taskName = 'pinefield-local-recovery-all'
$reference = Get-ScheduledTask -TaskPath $taskPath -TaskName 'pinefield-github-scrape-account1'
$existing = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Execute -ne $Pythonw -or $existing.Actions.Arguments -notlike '*Run-LocalScrapeRecoveryHidden.pyw*')) {
    throw 'An unrelated task already has the intended name; it was not changed.'
}
# Use the same interactive account as the established nightly trigger tasks.
$principal = New-ScheduledTaskPrincipal -UserId $reference.Principal.UserId -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}" --execute --repo "{1}" --max-runtime-seconds 10800' -f $wrapper, $RepoDir) -WorkingDirectory $RepoDir
# Start tomorrow to avoid an unreviewed missed-trigger run during installation.
# A deliberate Start-ScheduledTask below the caller's verification tests today's path.
$firstRun = (Get-Date).Date.AddDays(1).AddHours(7)
$trigger = New-ScheduledTaskTrigger -Daily -At $firstRun
$repeating = New-ScheduledTaskTrigger -Once -At $firstRun -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 12)
$trigger.Repetition = $repeating.Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Audit all 20 current-day GitHub scrape outputs. Once Cloud is finished, recover missing sources locally once per account and date. Existing valid sources are preserved.'
Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -InputObject $task -Force | Out-Null
$registered = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName
$info = Get-ScheduledTaskInfo -InputObject $registered
[pscustomobject]@{
    TaskPath = $registered.TaskPath
    TaskName = $registered.TaskName
    State = [string]$registered.State
    Execute = $registered.Actions.Execute
    Arguments = $registered.Actions.Arguments
    NextRunTime = $info.NextRunTime.ToString('o')
    DailyStart = '07:00 Asia/Tokyo'
    Repeat = 'Every 30 minutes for 12 hours'
    StartWhenAvailable = $registered.Settings.StartWhenAvailable
    WakeToRun = $registered.Settings.WakeToRun
    MultipleInstances = [string]$registered.Settings.MultipleInstances
    ExecutionTimeLimit = $registered.Settings.ExecutionTimeLimit
} | ConvertTo-Json -Depth 3
