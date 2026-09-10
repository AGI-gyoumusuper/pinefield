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
$referenceXml = [xml](Export-ScheduledTask -TaskPath $taskPath -TaskName $reference.TaskName)
$referenceUserId = [string]$referenceXml.Task.Principals.Principal.UserId
if ($referenceUserId -notmatch '^S-1-5-') {
    throw 'The established task does not expose a Windows user SID.'
}
$existing = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Execute -ne $Pythonw -or $existing.Actions.Arguments -notlike '*Run-LocalScrapeRecoveryHidden.pyw*')) {
    throw 'An unrelated task already has the intended name; it was not changed.'
}
# Start tomorrow to avoid an unreviewed missed-trigger run during installation.
# A deliberate Start-ScheduledTask below the caller's verification tests today's path.
$firstRun = (Get-Date).Date.AddDays(1).AddHours(7)
# Preserve the verified SID in XML. The CIM constructor resolves it back to a
# short account name, which Task Scheduler cannot reliably resolve on this PC.
$commandXml = [System.Security.SecurityElement]::Escape($Pythonw)
$argumentsXml = [System.Security.SecurityElement]::Escape(('"{0}" --execute --repo "{1}" --max-runtime-seconds 10800' -f $wrapper, $RepoDir))
$directoryXml = [System.Security.SecurityElement]::Escape($RepoDir)
$startXml = $firstRun.ToString('yyyy-MM-ddTHH:mm:ss')
$definition = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Audit 20 daily GitHub sources and recover only missing outputs after Cloud completes.</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><Repetition><Interval>PT30M</Interval><Duration>PT12H</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$startXml</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$referenceUserId</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT4H</ExecutionTimeLimit><Priority>7</Priority></Settings>
  <Actions Context="Author"><Exec><Command>$commandXml</Command><Arguments>$argumentsXml</Arguments><WorkingDirectory>$directoryXml</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Xml $definition -Force -ErrorAction Stop | Out-Null
$registered = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
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
