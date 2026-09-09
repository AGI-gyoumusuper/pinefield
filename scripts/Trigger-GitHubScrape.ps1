param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 20)]
    [int]$Account,
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$TargetDate,
    [switch]$PrepareNextDay,
    [switch]$DryRun,
    [string]$RepoDir,
    [ValidateRange(0, 120)]
    [int]$MutexWaitSeconds = 60,
    [ValidateRange(0, 30)]
    [int]$RetryDelaySeconds = 10
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($RepoDir)) { $RepoDir = Split-Path -Parent $PSScriptRoot }
$RepoDir = [System.IO.Path]::GetFullPath($RepoDir)
$LogDir = Join-Path $RepoDir 'work\scheduler-logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("trigger-account{0}-{1}-{2}.log" -f $Account, (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID)

function Write-Log {
    param([string]$Message)
    $safeMessage = $Message -replace '(?i)(https?://)[^/\s@]+@', '$1[redacted]@'
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'), $safeMessage
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-Git {
    param([string]$Directory, [string[]]$Arguments, [switch]$Capture)
    Write-Log ("git " + ($Arguments -join ' '))
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git -C $Directory @Arguments 2>&1)
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    foreach ($line in $output) { Write-Log ([string]$line) }
    if ($result -ne 0) { throw "git failed (exit $result): $($Arguments -join ' ')" }
    if ($Capture) { return $output }
}

function Remove-TriggerWorktree {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetFileName($resolved) -notmatch '^pinefield-trigger-[a-f0-9]{32}$') {
        throw "Refusing to remove unexpected worktree path: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Invoke-Git -Directory $RepoDir -Arguments @('worktree', 'remove', '--force', $resolved)
    }
}

$Now = Get-Date
if ($PrepareNextDay -and -not [string]::IsNullOrWhiteSpace($TargetDate)) {
    throw 'PrepareNextDay and TargetDate cannot be used together.'
}
if ([string]::IsNullOrWhiteSpace($TargetDate)) {
    $TargetDate = if ($PrepareNextDay) { $Now.Date.AddDays(1).ToString('yyyy-MM-dd') } else { $Now.ToString('yyyy-MM-dd') }
} else {
    $parsedDate = [datetime]::MinValue
    if (-not [datetime]::TryParseExact($TargetDate, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None, [ref]$parsedDate)) { throw "Invalid TargetDate: $TargetDate" }
}
$AccountName = "account$Account"
$relativeTrigger = ".github/triggers/$AccountName.json"
$Payload = [ordered]@{
    account = $AccountName
    target_date = $TargetDate
    requested_at_jst = $Now.ToString('yyyy-MM-ddTHH:mm:sszzz')
    source = 'windows-task-scheduler'
}
$CommitMessage = "Trigger GitHub scrape [scrape:$AccountName] [target:$TargetDate] $($Now.ToString('yyyy-MM-dd HH:mm:ss K'))"
Write-Log "Starting trigger for $AccountName"
Write-Log "Repo: $RepoDir"
Write-Log "Target date: $TargetDate"
if ($DryRun) {
    Write-Log 'Plan: fetch origin/main; isolated worktree; commit only the account trigger; push HEAD:main.'
    Write-Log 'DryRun completed; no commit or push performed.'
    exit 0
}

$Mutex = [System.Threading.Mutex]::new($false, 'Local\CodexPinefieldGitTrigger')
$MutexAcquired = $false
$oldPrompt = $env:GIT_TERMINAL_PROMPT
try {
    Write-Log "Waiting up to $MutexWaitSeconds seconds for the shared Pinefield Git lock."
    try { $MutexAcquired = $Mutex.WaitOne([TimeSpan]::FromSeconds($MutexWaitSeconds)) }
    catch [System.Threading.AbandonedMutexException] { $MutexAcquired = $true }
    if (-not $MutexAcquired) { throw 'Pinefield Git lock timed out; no trigger was pushed.' }
    $env:GIT_TERMINAL_PROMPT = '0'
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $tempWorktree = Join-Path ([System.IO.Path]::GetTempPath()) ('pinefield-trigger-' + [guid]::NewGuid().ToString('N'))
        try {
            Write-Log "Isolated trigger attempt $attempt/5"
            Invoke-Git -Directory $RepoDir -Arguments @('fetch', '--quiet', 'origin', 'main')
            Invoke-Git -Directory $RepoDir -Arguments @('worktree', 'add', '--quiet', '--detach', $tempWorktree, 'origin/main')
            $triggerPath = Join-Path $tempWorktree $relativeTrigger
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $triggerPath) | Out-Null
            [System.IO.File]::WriteAllText($triggerPath, ($Payload | ConvertTo-Json -Depth 3) + "`n", [Text.UTF8Encoding]::new($false))
            Invoke-Git -Directory $tempWorktree -Arguments @('add', '--', $relativeTrigger)
            $stagedPaths = @(Invoke-Git -Directory $tempWorktree -Arguments @('diff', '--cached', '--name-only') -Capture)
            if ($stagedPaths.Count -eq 0) {
                Write-Log "Trigger already recorded for $AccountName; no commit or push performed."
                exit 0
            }
            if ($stagedPaths.Count -ne 1 -or [string]$stagedPaths[0] -ne $relativeTrigger) {
                throw 'Isolated index must contain exactly the requested account trigger.'
            }
            Invoke-Git -Directory $tempWorktree -Arguments @('commit', '--quiet', '-m', $CommitMessage)
            $committedPaths = @(Invoke-Git -Directory $tempWorktree -Arguments @('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD') -Capture)
            if ($committedPaths.Count -ne 1 -or [string]$committedPaths[0] -ne $relativeTrigger) {
                throw 'Commit contains a file other than the requested account trigger.'
            }
            Invoke-Git -Directory $tempWorktree -Arguments @('push', 'origin', 'HEAD:main')
            Write-Log "Trigger push completed for $AccountName"
            exit 0
        } catch {
            Write-Log "Attempt $attempt failed: $($_.Exception.Message)"
            if ($attempt -eq 5) { throw }
        } finally { Remove-TriggerWorktree -Path $tempWorktree }
        Start-Sleep -Seconds $RetryDelaySeconds
    }
} finally {
    $env:GIT_TERMINAL_PROMPT = $oldPrompt
    if ($MutexAcquired) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
